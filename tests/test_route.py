from __future__ import annotations

import os
import time

from deepcrew import member, providers, route


def _age(board, tid, seconds):
    old = time.time() - seconds
    os.utime(board.tasks_dir / f"{tid}.json", (old, old))


def test_plan_wakes_an_idle_member_that_has_work_or_mail_from_the_leader(board, add_member, post_task):
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    add_member(board, "busy", ["lib/"], "active")
    add_member(board, "broken", ["web/"], "failed")
    t = post_task(board, "t", ["src/a.py"])
    post_task(board, "lib task", ["lib/x.py"])
    post_task(board, "web task", ["web/x.py"])
    assert route.plan(board)["wake"] == ["alpha"]
    board.mark("alpha", [t["id"]], "pass", "no")
    assert route.plan(board)["wake"] == []
    t2 = post_task(board, "t2", ["src/b.py"])
    assert route.plan(board)["wake"] == ["alpha"]
    board.mark("alpha", [t2["id"]], "skip", "offered; neither taken nor declined")
    assert route.plan(board)["wake"] == []
    board.send("alpha", "beta", "message", "from a member")
    assert route.plan(board)["wake"] == []
    board.send("leader", "beta", "notice", "from the leader")
    assert route.plan(board)["wake"] == ["beta"]


def test_plan_blocks_what_only_the_leader_can_resolve(board, add_member, post_task, task_edit):
    add_member(board, "alpha", ["src/pkg/"])
    add_member(board, "broken", ["lib/"], "failed")
    cross = post_task(board, "crosses", ["src/pkg/b.py", "docs/guide.md"])
    declined, gave_back, ignored = (post_task(board, name, [f"src/pkg/{name}.py"]) for name in ("declined", "gave back", "ignored"))
    orphan, scopeless, gone = post_task(board, "orphan", ["lib/x.py"]), post_task(board, "unusable scope"), post_task(board, "cancelled")
    after, after2 = post_task(board, "after", ["web/"], [gone["id"]]), post_task(board, "after2", [], [gone["id"]])
    waiting = post_task(board, "waits for an open task", ["web/"], [cross["id"]])
    task_edit(board, scopeless["id"], scope="src/")
    board.mark("alpha", [declined["id"]], "pass", "needs the docs owner")
    board.claim("alpha", gave_back["id"])
    board.release("alpha", gave_back["id"], "too big")
    board.mark("alpha", [ignored["id"]], "skip", "offered; neither taken nor declined")
    board.cancel(gone["id"], "leader", "")
    blocked = {b["task"]: b["reason"] for b in route.plan(board)["blocked"]}
    assert set(blocked) == {cross["id"], declined["id"], gave_back["id"], ignored["id"], orphan["id"], scopeless["id"], after["id"], after2["id"]}
    assert waiting["id"] not in blocked and gone["id"] not in blocked
    assert blocked[cross["id"]] == "scope crosses a region boundary" and "failed" in blocked[orphan["id"]]
    assert blocked[declined["id"]] == "no member left to take it (alpha declined it: needs the docs owner)"
    assert blocked[gave_back["id"]] == "no member left to take it (alpha gave it back: too big)"
    assert blocked[ignored["id"]] == "no member left to take it (alpha left it undecided: offered; neither taken nor declined)"
    assert blocked[scopeless["id"]] == "its metadata.scope is not a list of project paths"
    assert blocked[after["id"]] == blocked[after2["id"]] == f"dependency {gone['id']} was cancelled"


def test_a_members_own_step_without_files_reaches_the_leader_once_nobody_is_left_to_take_it(board, add_member):
    add_member(board, "alpha", ["src/"], "failed")
    t = board.post("check my numbers", "", [], [], "alpha")
    assert route.plan(board)["blocked"] == []
    _age(board, t["id"], route.UNTAKEN_S + 1)
    assert route.plan(board)["blocked"] == [{"task": t["id"], "reason": f"no member left to take it; start one with {providers.NAME} spawn NAME --task {t['id']}"}]
    hard = board.post("review the design", "", [], [], "leader", strength="strongest")
    reasons = {b["task"]: b["reason"] for b in route.plan(board)["blocked"]}
    assert reasons[hard["id"]] == f"it needs a member on m: start one with {providers.NAME} spawn NAME --strength strongest --task {hard['id']}"


def test_a_scoped_task_its_region_owner_turned_down_is_still_blocked_for_the_leader(board, add_member, post_task):
    add_member(board, "alpha", ["src/"])
    t = post_task(board, "parser", ["src/p.py"])
    board.mark("alpha", [t["id"]], "pass", "not mine")
    (b,) = route.plan(board)["blocked"]
    assert b == {"task": t["id"], "reason": "no member left to take it (alpha declined it: not mine)"}


def test_moving_a_boundary_unblocks_a_task_that_crossed_it_and_the_notices_wake_the_idle_members(board, add_member, post_task, procs):
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    t = post_task(board, "shared", ["src/a.py", "docs/x.md"])
    p = route.plan(board)
    assert [b["reason"] for b in p["blocked"]] == ["scope crosses a region boundary"] and p["wake"] == []
    board.set_region("alpha", ["src/pkg/"])
    board.set_region("beta", ["docs/", "src/a.py"])
    p = route.plan(board)
    assert p["blocked"] == [] and p["wake"] == ["alpha", "beta"]
    assert [x["id"] for x in member.offer(board, board.member("beta"))] == [t["id"]] and member.offer(board, board.member("alpha")) == []
    assert route.run(board)["woken"] == ["alpha", "beta"]
    prompts = {c["member"]["name"]: c["prompt"] for c in procs.calls}
    assert "Your region was changed by the leader. It is now: src/pkg/." in prompts["alpha"] and "You no longer own: src/." in prompts["alpha"]
    assert "It is now: docs/, src/a.py." in prompts["beta"] and "You may now also change: src/a.py." in prompts["beta"] and "shared" in prompts["beta"]
    assert board.peek_mail("alpha") == [] and board.peek_mail("beta") == []


def test_a_running_member_hears_of_its_new_region_after_its_next_tool_call(board, add_member):
    add_member(board, "alpha", ["src/"], "active")
    board.set_region("alpha", ["lib/"])
    out = member.hook_post(board, "alpha", {"tool_name": "Read", "tool_input": {"file_path": "lib/x.py"}})
    text = out["hookSpecificOutput"]["additionalContext"]
    assert ("Your region was changed by the leader. It is now: lib/. You may now also change: lib/. You no longer own: src/. "
            "Change files only inside your region.") in text
    assert member.hook_post(board, "alpha", {"tool_name": "Read", "tool_input": {"file_path": "lib/x.py"}}) is None


def test_a_dependency_that_is_done_blocks_nothing(board, add_member, post_task, finish):
    add_member(board, "alpha", ["src/"])
    a = post_task(board, "a", ["src/a.py"])
    post_task(board, "after", ["src/a.py"], [a["id"]])
    board.claim("alpha", a["id"])
    finish(board, "alpha", a["id"])
    assert route.plan(board)["blocked"] == []


def test_plan_limits_wakes_to_the_free_slots_and_lists_the_rest_as_waiting(make_board, add_member, post_task):
    board = make_board(max_active=2)
    add_member(board, "run", ["run/"], "active")
    for name in ("one", "two"):
        add_member(board, name, [f"{name}/"])
        post_task(board, name, [f"{name}/x.py"])
    p = route.plan(board)
    assert (p["wake"], p["waiting"]) == (["one"], ["two"])


def test_run_wakes_idle_members_but_never_starts_new_ones(board, add_member, post_task, procs, monkeypatch):
    add_member(board, "alpha", ["src/"])
    post_task(board, "wake", ["src/a.py"])
    post_task(board, "new", ["lib/"])
    woken = []
    monkeypatch.setattr(member, "wake", lambda b, name: woken.append(name) or b.member(name))
    p = route.run(board)
    assert p["woken"] == woken == ["alpha"] and p["errors"] == []
    assert [m["name"] for m in board.members()] == ["alpha"] and procs.calls == []

    def broken(b, name):
        raise RuntimeError("boom")

    monkeypatch.setattr(member, "wake", broken)
    assert route.run(board)["errors"] == ["wake alpha: boom"]


def test_run_reaps_members_whose_process_ended_before_it_plans(board, add_member, post_task, monkeypatch):
    add_member(board, "alpha", ["src/"], "active", runs=1)
    t = post_task(board, "held", ["src/a.py"])
    board.claim("alpha", t["id"])
    monkeypatch.setattr(member, "alive", lambda m: False)
    monkeypatch.setattr(member, "wake", lambda b, name: b.member(name))
    p = route.run(board)
    assert board.member("alpha")["state"] == "idle" and board.task(t["id"])["state"] == "open" and p["woken"] == ["alpha"]


def _ask(board, me, tid):
    out = member.task_rules(board, me, "TaskUpdate", {"taskId": tid, "status": "in_progress", "owner": me})
    return out and out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_member_alone_on_an_offer_gets_a_read_only_task_at_once(board, add_member, post_task):
    t = post_task(board, "review the parser")
    add_member(board, "alpha", ["src/"], "active", offered=[t["id"]])
    add_member(board, "beta", ["docs/"], "active", task=None)
    assert _ask(board, "alpha", t["id"]) is None
    assert (board.task(t["id"])["owner"], board.member("alpha")["task"]) == ("alpha", t["id"])


def test_assign_refuses_what_the_member_may_not_take(board, add_member, post_task):
    t = post_task(board, "parser", ["src/p.py"])
    add_member(board, "beta", ["docs/"], "active")
    add_member(board, "gamma", ["src/"], "failed")
    assert route.assign(board, "beta", t["id"]) == f"task {t['id']} changes ['src/p.py'], outside beta's region ['docs/']"
    assert route.assign(board, "gamma", t["id"]) == "'gamma' is not a working member of this team (it failed; resume the crew once the cause is fixed)"
    assert board.task(t["id"])["state"] == "open" and board.member("gamma")["task"] is None


def test_resume_makes_a_failed_member_idle_again_and_routes(board, add_member, post_task, monkeypatch):
    add_member(board, "alpha", ["src/"], "failed", failure="API error 402: top up the account")
    post_task(board, "t", ["src/a.py"])
    assert route.plan(board)["wake"] == []
    woken = []
    monkeypatch.setattr(member, "wake", lambda b, name: woken.append(name) or b.member(name))
    assert route.resume(board)["woken"] == woken == ["alpha"]
    assert (board.member("alpha")["state"], board.member("alpha")["failure"]) == ("idle", None)


def test_a_round_closes_when_a_member_ends_its_turn_without_answering(board, add_member, post_task, monkeypatch):
    t = post_task(board, "Review")
    add_member(board, "alpha", ["src/"], "active", offered=[t["id"]])
    add_member(board, "beta", ["docs/"], "active", offered=[t["id"]])
    _ask(board, "alpha", t["id"])
    monkeypatch.setattr(member, "wake", lambda b, n: None)
    assert member._end_of_turn(board, "beta", [t["id"]], None) == (None, None)
    assert board.task(t["id"])["owner"] == "alpha"
