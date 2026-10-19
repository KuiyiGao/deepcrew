from __future__ import annotations

import json
import os
import time

import pytest

from deepcrew import member, policy, providers, route
from deepcrew.inbox import _item_line
from deepcrew.board import MEMBER_NAME


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
    assert route.plan(board)["wake"] == ["alpha"]
    board.mark("alpha", [t["id"]], "pass", "no")
    board.mark("beta", [t["id"]], "skip", "offered; neither taken nor declined")
    assert route.plan(board)["wake"] == []
    t2 = post_task(board, "t2", ["src/b.py"])
    assert route.plan(board)["wake"] == ["alpha"]
    board.mark("alpha", [t2["id"]], "skip", "offered; neither taken nor declined")
    board.mark("beta", [t2["id"]], "skip", "offered; neither taken nor declined")
    assert route.plan(board)["wake"] == []
    board.send("alpha", "beta", "message", "from a member")
    assert route.plan(board)["wake"] == []
    board.send("leader", "beta", "notice", "from the leader")
    assert route.plan(board)["wake"] == ["beta"]
    post_task(board, "web task", ["web/x.py"])
    assert route.plan(board)["wake"] == ["alpha", "beta"]


def _core(proposals):
    return [{k: g[k] for k in ("region", "tasks", "name")} for g in proposals]


def test_plan_proposes_overlapping_uncovered_scopes_as_one_member_and_disjoint_ones_as_two(board, add_member, post_task):
    add_member(board, "alpha", ["lib/"], "active")
    board.claim("alpha", post_task(board, "covered", ["lib/x.py"])["id"])
    ids = [post_task(board, name, scope)["id"] for name, scope in (("inner", ["src/pkg/"]), ("outer", ["src/"]), ("file", ["src/a.py"]), ("docs", ["docs/"]))]
    assert _core(route.plan(board)["proposals"]) == [{"region": ["src/"], "tasks": ids[:3], "name": "inner"}, {"region": ["docs/"], "tasks": ids[3:], "name": "docs"}]


def test_plan_proposes_a_reader_only_when_there_are_no_members_and_no_other_proposals(board, add_member, post_task):
    r1, r2 = post_task(board, "review"), post_task(board, "research")
    assert _core(route.plan(board)["proposals"]) == [{"region": [], "tasks": [r1["id"], r2["id"]], "name": "review"}]
    s = post_task(board, "write", ["src/"])
    assert [g["name"] for g in route.plan(board)["proposals"]] == ["write"]
    board.cancel(s["id"], "leader", "")
    add_member(board, "alpha", ["lib/"], "active")
    assert route.plan(board)["proposals"] == []


def test_a_members_own_step_without_files_is_never_proposed_as_a_new_member(board, post_task):
    board.post("check my numbers", "", [], [], "alpha")
    assert route.plan(board)["proposals"] == []
    post_task(board, "review")
    assert [g["name"] for g in route.plan(board)["proposals"]] == ["review"]


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
    p = route.plan(board)
    blocked = {b["task"]: b["reason"] for b in p["blocked"]}
    assert set(blocked) == {declined["id"], gave_back["id"], ignored["id"], scopeless["id"], after["id"], after2["id"]}
    assert p["wake"] == ["alpha"]
    board.mark("alpha", [cross["id"], orphan["id"]], "skip", "offered; neither taken nor declined")
    blocked = {b["task"]: b["reason"] for b in route.plan(board)["blocked"]}
    assert set(blocked) == {cross["id"], declined["id"], gave_back["id"], ignored["id"], orphan["id"], scopeless["id"], after["id"], after2["id"]}
    assert waiting["id"] not in blocked and gone["id"] not in blocked
    assert blocked[cross["id"]] == "scope crosses a region boundary" and "alpha left it undecided" in blocked[orphan["id"]]
    assert blocked[declined["id"]] == "no member left to take it (alpha declined it: needs the docs owner)"
    assert blocked[gave_back["id"]] == "no member left to take it (alpha gave it back: too big)"
    assert blocked[ignored["id"]] == "no member left to take it (alpha left it undecided: offered; neither taken nor declined)"
    assert blocked[scopeless["id"]] == "its metadata.scope is not a list of project paths"
    assert blocked[after["id"]] == blocked[after2["id"]] == f"dependency {gone['id']} was cancelled"


def test_a_read_only_task_every_member_turned_down_comes_back_as_a_proposal_saying_why(board, add_member, post_task):
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    t = post_task(board, "review")
    board.mark("alpha", [t["id"]], "pass", "no time")
    assert route.plan(board)["proposals"] == []
    board.claim("beta", t["id"])
    board.release("beta", t["id"], "needs the build")
    (g,) = route.plan(board)["proposals"]
    assert (g["region"], g["tasks"]) == ([], [t["id"]])
    assert g["declined"] == ["alpha declined it: no time", "beta gave it back: needs the build"] and route.plan(board)["blocked"] == []


def test_a_members_own_step_without_files_reaches_the_leader_once_nobody_is_left_to_take_it(board, add_member):
    add_member(board, "alpha", ["src/"], "failed")
    t = board.post("check my numbers", "", [], [], "alpha")
    assert route.plan(board)["blocked"] == []
    _age(board, t["id"], route.UNTAKEN_S + 1)
    assert route.plan(board)["blocked"] == [{"task": t["id"], "reason": f"no member left to take it; start one with {providers.NAME} spawn NAME --task {t['id']}"}]
    assert route.plan(board)["proposals"] == []
    hard = board.post("review the design", "", [], [], "leader", strength="strongest")
    reasons = {b["task"]: b["reason"] for b in route.plan(board)["blocked"]}
    assert reasons[hard["id"]] == f"it needs a member on m: start one with {providers.NAME} spawn NAME --strength strongest --task {hard['id']}"


def test_a_task_without_files_that_rides_along_with_a_proposal_is_not_also_blocked(board, post_task):
    review, parser = post_task(board, "review"), post_task(board, "write the parser", ["src/"])
    for t in (review, parser):
        _age(board, t["id"], route.UNTAKEN_S + 1)
    p = route.plan(board)
    assert [g["tasks"] for g in p["proposals"]] == [[parser["id"]]] and p["blocked"] == []


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
    assert p["blocked"] == [] and p["wake"] == ["alpha", "beta"]
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


def test_member_names_are_readable_unique_and_never_reserved(board, add_member):
    cases = {("src/parser/",): "parser", ("README.md",): "readme", (): "reader", (".github/",): "member", ("123/",): "m-123", ("my_module/",): "my-module",
             ("all/",): "m-all", ("LEADER.md",): "m-leader", ("a" * 40 + "/",): "a" * 20, ("ui/",): "m-ui", ("a/",): "m-a"}
    for region, name in cases.items():
        assert route.member_name(board, list(region)) == name and MEMBER_NAME.fullmatch(name)
    add_member(board, "parser", ["x/"])
    add_member(board, "parser-2", ["y/"])
    assert route.member_name(board, ["lib/parser/"]) == "parser-3" and route.member_name(board, ["a/parser/"], taken={"parser-3"}) == "parser-4"


def test_run_wakes_idle_members_but_never_starts_new_ones(board, add_member, post_task, procs, monkeypatch):
    add_member(board, "alpha", ["src/"])
    post_task(board, "wake", ["src/a.py"])
    post_task(board, "new", ["lib/"])
    woken = []
    monkeypatch.setattr(member, "wake", lambda b, name: woken.append(name) or b.member(name))
    p = route.run(board)
    assert p["woken"] == woken == ["alpha"] and p["errors"] == [] and p["proposals"] == []
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


def test_start_proposals_starts_every_proposed_member_with_its_role(make_board, provider_table, post_task, procs, monkeypatch):
    board = make_board(provider={**provider_table, "prefix_cache": True})
    t1, t2 = post_task(board, "parse input", ["a/"]), post_task(board, "render output", ["b/"])
    waited = []
    monkeypatch.setattr(member, "first_reply", lambda b, name, offset, timeout: waited.append(name) or True)
    started = route.start_proposals(board, roles={"parse-input": "owns a"})
    assert [(m["name"], m["task"]) for m in started] == [("parse-input", t1["id"]), ("render-output", t2["id"])]
    assert "owns a" in procs.calls[0]["prompt"] and "Your role, from the leader: render output" in procs.calls[1]["prompt"]
    assert waited == ["parse-input"]


def test_a_proposed_name_comes_from_the_first_tasks_title_without_filler_words(board, add_member):
    cases = {"Write the physics engine": "write-physics", "Review of the API": "review-api", "Parser": "parser", "2D physics": "m-2d-physics",
             "An implementation of everything for the whole project": "implementation-every", "x": "m-x", "": "src"}
    for title, name in cases.items():
        assert route.member_name(board, ["src/"], title=title) == name and MEMBER_NAME.fullmatch(name), title
    assert route.member_name(board, [], title="the and of") == "reader"
    assert route.member_name(board, ["lib/"], title="Ωμέγα λόγος") == "lib"
    add_member(board, "write-physics", ["x/"])
    assert route.member_name(board, ["src/"], taken={"write-physics-2"}, title="write physics") == "write-physics-3"


def test_plan_names_each_proposal_after_its_first_task_and_keeps_names_the_team_used(board, add_member, post_task):
    first = post_task(board, "Write the parser", ["lib/parser/"])
    post_task(board, "Test the parser", ["lib/parser/tests/"])
    post_task(board, "Write the parser docs", ["web/"])
    assert [(g["name"], g["tasks"][0]) for g in route.plan(board)["proposals"]] == [("write-parser", first["id"]), ("write-parser-2", "3")]
    with board.locked():
        board.use_name("write-parser")
    assert [g["name"] for g in route.plan(board)["proposals"]] == ["write-parser-2", "write-parser-3"]


def test_start_proposals_starts_only_as_many_members_as_the_team_may_have(make_board, add_member, post_task, procs):
    board = make_board(max_active=3)
    add_member(board, "alpha", ["src/"])
    board.claim("alpha", post_task(board, "busy", ["src/x.py"])["id"])
    first, second = post_task(board, "one thing", ["a/"]), post_task(board, "other thing", ["b/"])
    post_task(board, "third thing", ["c/"])
    assert [(m["name"], m["task"]) for m in route.start_proposals(board)] == [("one-thing", first["id"]), ("other-thing", second["id"])]
    assert [g["name"] for g in route.plan(board)["proposals"]] == ["third-thing"]
    with pytest.raises(ValueError, match="already has 3 members, its limit"):
        route.start_proposals(board)


def _ask(board, me, tid):
    out = member.task_rules(board, me, "TaskUpdate", {"taskId": tid, "status": "in_progress", "owner": me})
    return out and out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_member_alone_on_an_offer_gets_a_read_only_task_at_once(board, add_member, post_task):
    t = post_task(board, "review the parser")
    add_member(board, "alpha", ["src/"], "active", offered=[t["id"]])
    add_member(board, "beta", ["docs/"], "active", task=None)
    assert _ask(board, "alpha", t["id"]) is None
    assert (board.task(t["id"])["owner"], board.member("alpha")["task"]) == ("alpha", t["id"])


def test_several_askers_the_leader_picks_with_the_closest_role_suggested(board, add_member, post_task):
    t = post_task(board, "Review the parser tests", body="check parser edge cases")
    add_member(board, "alpha", ["src/"], "active", offered=[t["id"]], role="writes the parser and its tests")
    add_member(board, "beta", ["docs/"], "active", offered=[t["id"]], role="writes the user guide")
    add_member(board, "gamma", ["web/"], "active", offered=[t["id"]], role="builds the page")
    assert _ask(board, "beta", t["id"]).startswith("Noted: you asked for task")
    assert board.eligible(board.member("beta")) == []
    assert _ask(board, "alpha", t["id"]) == (f"Noted: you asked for task {t['id']}. Other members asked for it too, so the leader picks one of those "
                                             "who asked. If it is you, it comes as your next message. End your turn now.")
    board.mark("gamma", [t["id"]], "pass", "busy with the page")
    p = route.run(board)
    assert p["given"] == {} and p["choices"] == [{"task": t["id"], "title": t["title"], "volunteers": ["beta", "alpha"], "suggested": "alpha", "pitches": {}}]
    assert route.assign(board, "alpha", t["id"]) is None
    assert board.task(t["id"])["owner"] == "alpha" and member.offer(board, board.member("alpha")) == [board.task(t["id"])]
    assert route.plan(board)["choices"] == []


def test_one_asker_gets_the_task_when_the_others_turn_it_down(board, add_member, post_task):
    t = post_task(board, "Review the user guide")
    add_member(board, "alpha", ["src/"], "active", offered=[t["id"]])
    add_member(board, "beta", ["docs/"], "active", offered=[t["id"]])
    assert _ask(board, "alpha", t["id"]).startswith("Noted")
    board.mark("beta", [t["id"]], "pass", "not now")
    assert route.run(board)["given"] == {t["id"]: "alpha"} and board.task(t["id"])["owner"] == "alpha"


def test_a_member_that_asks_again_with_nobody_else_in_the_round_gets_the_task_at_once(board, add_member, post_task):
    t = post_task(board, "Review the guide")
    add_member(board, "alpha", ["src/"], "active", offered=[t["id"]])
    add_member(board, "beta", ["docs/"], "active", offered=[t["id"]])
    assert _ask(board, "alpha", t["id"]) == (f"Noted: you asked for task {t['id']}. Other members are deciding on it too: if one of them asks for it "
                                             "as well, the leader picks one of those who asked; if none does, it is yours. If it is you, it comes as "
                                             "your next message. End your turn now.")
    board.mark("beta", [t["id"]], "pass", "not now")
    assert _ask(board, "alpha", t["id"]) is None and board.task(t["id"])["owner"] == "alpha"


def test_the_ask_of_a_member_that_failed_no_longer_counts(board, add_member, post_task):
    t = post_task(board, "Review the guide")
    for name, region in (("alpha", "src/"), ("beta", "docs/"), ("gamma", "lib/")):
        add_member(board, name, [region], "active", offered=[t["id"]])
    _ask(board, "alpha", t["id"])
    _ask(board, "beta", t["id"])
    board.mark("gamma", [t["id"]], "pass", "busy")
    assert route.plan(board)["choices"][0]["volunteers"] == ["alpha", "beta"]
    board.update_member("beta", state="failed", pid=None)
    p = route.plan(board)
    assert (p["choices"], p["awards"]) == ([], [t["id"]])
    board.update_member("alpha", state="failed", pid=None)
    p = route.plan(board)
    assert (p["choices"], p["awards"], [g["tasks"] for g in p["proposals"]]) == ([], [], [[t["id"]]])


def test_ties_in_the_suggestion_go_to_whoever_asked_first(board, add_member, post_task):
    t = post_task(board, "Look around")
    add_member(board, "alpha", ["src/"], "active", offered=[t["id"]])
    add_member(board, "beta", ["docs/"], "active", offered=[t["id"]])
    _ask(board, "beta", t["id"])
    _ask(board, "alpha", t["id"])
    assert route.plan(board)["choices"][0]["suggested"] == "beta"


def test_assign_refuses_what_the_member_may_not_take(board, add_member, post_task):
    t = post_task(board, "parser", ["src/p.py"])
    add_member(board, "beta", ["docs/"], "active")
    add_member(board, "gamma", ["src/"], "failed")
    assert route.assign(board, "gamma", t["id"]) == "'gamma' is not a working member of this team (it failed; resume the crew once the cause is fixed)"
    assert board.task(t["id"])["state"] == "open" and board.member("gamma")["task"] is None
    add_member(board, "delta", ["lib/"], "active")
    board.claim("delta", post_task(board, "lib", ["lib/"])["id"])
    assert route.assign(board, "beta", post_task(board, "overlaps delta's lease", ["lib/x.py"])["id"]) == "task 3 waits: delta holds lib/ for task 2"


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


def test_a_ready_task_nobody_took_is_proposed_once_it_waited_propose_after_s(make_board, add_member, post_task):
    board = make_board(propose_after_s=60)
    t = post_task(board, "write the parser", ["src/"], body="Parse the input file.\nDetails follow.")
    (g,) = route.plan(board)["proposals"]
    assert (g["due"], g["role"]) == (False, "write the parser. Parse the input file")
    _age(board, t["id"], 61)
    assert route.plan(board)["proposals"][0]["due"] is True


@pytest.fixture
def helped(make_board, add_member, post_task, procs):
    board = make_board(max_active=6)
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    add_member(board, "gamma", ["lib/"], "active")
    t = post_task(board, "big job", ["src/"])
    board.claim("alpha", t["id"])
    return board, t["id"]


def test_add_helper_refuses_what_cannot_be_helped_and_says_why(helped, add_member, post_task, procs):
    board, tid = helped
    open_task, other = post_task(board, "not started", ["docs/x.md"]), post_task(board, "other", ["lib/a.py"])
    add_member(board, "broken", ["web/"], "failed")
    board.claim("gamma", other["id"])
    assert route.add_helper(board, "99", "beta") == "there is no task 99"
    assert route.add_helper(board, open_task["id"], "beta") == f"task {open_task['id']} is not being worked on"
    assert route.add_helper(board, tid, "ghost") == "'ghost' is not a working member of this team"
    assert route.add_helper(board, tid, "broken") == "'broken' is not a working member of this team"
    assert route.add_helper(board, tid, "alpha") == "alpha is busy with that task"
    assert route.add_helper(board, tid, "gamma") == f"gamma is busy with task {other['id']}"
    assert "is not inside task" in route.add_helper(board, tid, "beta", ["docs/"]) and "is not inside task" in route.add_helper(board, other["id"], "beta", ["src/"])
    assert "project-relative" in route.add_helper(board, tid, "beta", ["/etc"]) and "must stay inside" in route.add_helper(board, tid, "beta", ["../x"])
    assert board.helping(board.member("beta")) is None and board.peek_mail("beta") == [] and board.peek_mail("alpha") == [] and procs.calls == []
    assert route.add_helper(board, tid, "beta", ["src/pkg/"]) is None
    assert route.add_helper(board, tid, "beta") == f"beta is busy with task {tid}"
    board.release("gamma", other["id"], "x", by_choice=False)
    assert route.add_helper(board, other["id"], "beta") == f"task {other['id']} is not being worked on"


def test_a_task_that_changes_no_files_may_be_helped_with_no_files(make_board, add_member, post_task, procs):
    board = make_board()
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    t = post_task(board, "survey")
    board.claim("alpha", t["id"])
    assert "is not inside task" in route.add_helper(board, t["id"], "beta", ["docs/"])
    assert route.add_helper(board, t["id"], "beta") is None and board.helping(board.member("beta"))["paths"] == []
    assert "changes no files" in board.peek_mail("beta")[0][1]["text"] and board.write_area("beta") == ["docs/"]


def test_both_sides_hear_of_the_help_from_the_leader_and_the_record_names_the_paths(helped):
    board, tid = helped
    before = time.time()
    assert route.add_helper(board, tid, "beta", ["src//pkg/", "src/a.py"]) is None
    assert board.helping(board.member("beta")) == {"task": tid, "owner": "alpha", "paths": ["src/a.py", "src/pkg/"], "since": board.member("beta")["helps"]["since"]}
    assert board.member("beta")["helps"]["since"] >= before and board.member("gamma").get("helps") is None
    (_, to_helper), = board.peek_mail("beta")
    (_, to_owner), = board.peek_mail("alpha")
    assert (to_helper["from"], to_helper["kind"], to_helper["task"], to_owner["from"], to_owner["kind"], to_owner["task"]) == ("leader", "notice", tid, "leader", "notice", tid)
    assert "Help alpha with task" in to_helper["text"] and "src/a.py, src/pkg/" in to_helper["text"] and "until the task is done" in to_helper["text"]
    assert "beta now helps you" in to_owner["text"] and "src/a.py, src/pkg/" in to_owner["text"] and "you still complete the task" in to_owner["text"]
    assert board.peek_mail("gamma") == []


def test_a_helper_may_change_the_given_paths_while_the_owner_holds_the_task_and_not_after(helped, project, finish):
    board, tid = helped
    inside, outside, elsewhere = (str(project / p) for p in ("src/pkg/b.py", "src/a.py", "web/x.py"))
    edit = lambda path: {"tool_name": "Edit", "tool_input": {"file_path": path}}
    assert member.hook_pre(board, "beta", edit(inside))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert route.add_helper(board, tid, "beta", ["src/pkg/"]) is None
    assert member.hook_pre(board, "beta", edit(inside)) is None and policy.decide(board, "beta", "Write", {"file_path": inside})["behavior"] == "allow"
    for path, why in ((outside, f"alpha holds it for task {tid}: ask alpha by message"), (elsewhere, "no member's region holds it: ask the leader")):
        assert why in member.hook_pre(board, "beta", edit(path))["hookSpecificOutput"]["permissionDecisionReason"]
        assert policy.decide(board, "beta", "Edit", {"file_path": path})["behavior"] == "deny"
    assert member.hook_pre(board, "beta", edit(str(project / "docs/guide.md"))) is None
    assert member.hook_pre(board, "gamma", edit(inside))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert f"beta holds it for task {tid}" in member.hook_pre(board, "alpha", edit(inside))["hookSpecificOutput"]["permissionDecisionReason"]
    finish(board, "alpha", tid)
    assert board.write_area("beta") == ["docs/"] and member.hook_pre(board, "beta", edit(inside))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert policy.decide(board, "beta", "Edit", {"file_path": inside})["behavior"] == "deny"


def test_help_without_paths_reads_and_advises_and_the_holder_keeps_writing(helped, project):
    board, tid = helped
    write = lambda who: member.hook_pre(board, who, {"tool_name": "Write", "tool_input": {"file_path": str(project / "src/new/x.py")}})
    assert route.add_helper(board, tid, "beta") is None and board.helping(board.member("beta"))["paths"] == []
    assert write("beta")["hookSpecificOutput"]["permissionDecision"] == "deny" and write("alpha") is None
    assert "reads, checks and advises" in board.peek_mail("beta")[0][1]["text"]


def test_a_member_helping_takes_no_offer_until_the_task_closes(helped, post_task, finish):
    board, tid = helped
    mine = post_task(board, "docs task", ["docs/x.md"])
    assert [t["id"] for t in member.offer(board, board.member("beta"))] == [mine["id"]]
    route.add_helper(board, tid, "beta")
    assert member.offer(board, board.member("beta")) == [] and board.eligible(board.member("beta"))[0]["id"] == mine["id"]
    assert route.plan(board)["wake"] == [] and route.plan(board)["blocked"] == [] and route.plan(board)["proposals"] == []
    finish(board, "alpha", tid)
    assert [t["id"] for t in member.offer(board, board.member("beta"))] == [mine["id"]]


def test_a_member_with_a_task_of_its_own_helps_nobody_and_a_helper_is_not_idle(helped, post_task):
    board, tid = helped
    route.add_helper(board, tid, "beta")
    p = route.plan(board)
    assert p["helpers"] == {"beta": board.member("beta")["helps"]} and "beta" not in (p["capacity"] or {"idle": []})["idle"]
    t2 = post_task(board, "later", ["lib/x.py"])
    assert route.assign(board, "beta", t2["id"]) is not None
    assert [h for h in p["capacity"]["running"] if h["task"] == tid] == [{"task": tid, "title": "big job", "owner": "alpha", "helpers": ["beta"]}]


def test_help_comes_to_an_end_on_a_release_and_is_not_given_to_another_owner(helped, task_edit):
    board, tid = helped
    route.add_helper(board, tid, "beta")
    board.release("alpha", tid, "too big")
    assert board.helping(board.member("beta")) is None and route.plan(board)["helpers"] == {}
    assert route.add_helper(board, tid, "gamma") == f"task {tid} is not being worked on"


def test_an_idle_helper_is_resumed_by_the_leaders_notice_at_once(make_board, add_member, post_task, procs):
    board = make_board()
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "idle")
    t = post_task(board, "big job", ["src/"])
    board.claim("alpha", t["id"])
    assert route.add_helper(board, t["id"], "beta") is None
    (launch,) = procs.calls
    assert launch["member"]["name"] == "beta" and "Help alpha with task" in launch["prompt"] and board.peek_mail("beta") == []


def test_plan_wakes_an_exited_member_on_mail_from_its_task_partner_but_not_on_other_members_mail(helped, add_member):
    board, tid = helped
    route.add_helper(board, tid, "beta")
    board.take_mail("beta")
    board.take_mail("alpha")
    board.update_member("beta", state="idle", pid=None)
    board.update_member("alpha", state="idle", pid=None)
    assert route.plan(board)["wake"] == []
    board.send("gamma", "beta", "message", "unrelated member")
    board.send("gamma", "alpha", "message", "unrelated member")
    assert route.plan(board)["wake"] == []
    board.send("alpha", "beta", "message", "your part is next")
    assert route.plan(board)["wake"] == ["beta"]
    board.send("beta", "alpha", "message", "mine is done")
    assert route.plan(board)["wake"] == ["alpha", "beta"]
    board.take_mail("beta")
    board.take_mail("alpha")
    board.send("leader", "alpha", "notice", "from the leader")
    assert route.plan(board)["wake"] == ["alpha"]


def test_a_partners_mail_resumes_an_exited_member_when_it_is_woken(helped, procs):
    board, tid = helped
    route.add_helper(board, tid, "beta")
    board.take_mail("beta")
    board.update_member("beta", state="idle", pid=None, runs=1)
    assert member.wake(board, "beta") is None and procs.calls == []
    board.send("gamma", "beta", "message", "unrelated")
    assert member.wake(board, "beta") is None and procs.calls == []
    board.send("alpha", "beta", "message", "your part is next")
    assert member.wake(board, "beta")["state"] == "active"
    (launch,) = procs.calls
    assert "your part is next" in launch["prompt"] and "unrelated" in launch["prompt"]
    assert board.peek_mail("beta") == []


def test_plan_reports_the_capacity_while_tasks_run_and_members_are_idle(make_board, add_member, post_task, finish):
    board = make_board(max_active=4)
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    add_member(board, "gamma", ["lib/"], "active")
    big = post_task(board, "big job", ["src/"])
    assert route.plan(board)["capacity"] is None
    board.claim("alpha", big["id"])
    running = [{"task": big["id"], "title": "big job", "owner": "alpha", "helpers": []}]
    assert route.plan(board)["capacity"] == {"idle": ["beta", "gamma"], "free": 1, "ready": 0, "room": 3, "running": running, "done": 0}
    post_task(board, "docs task", ["docs/x.md"])
    post_task(board, "blocked by the big job", ["docs/y.md"], [big["id"]])
    assert route.plan(board)["capacity"]["ready"] == 1 and route.plan(board)["capacity"]["room"] == 2
    post_task(board, "lib task", ["lib/a.py"])
    assert route.plan(board)["capacity"]["room"] == 1
    post_task(board, "another", ["web/"])
    assert route.plan(board)["capacity"] is None
    finish(board, "alpha", big["id"])
    assert route.plan(board)["capacity"] is None


def test_the_capacity_counts_finished_tasks_and_leaves_out_members_that_hold_a_task_or_failed(make_board, add_member, post_task, finish):
    board = make_board(max_active=3)
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    add_member(board, "broken", ["web/"], "failed")
    first, second = post_task(board, "one", ["src/"]), post_task(board, "two", ["docs/"])
    board.claim("alpha", first["id"])
    board.claim("beta", second["id"])
    assert route.plan(board)["capacity"] is None
    finish(board, "alpha", first["id"])
    cap = route.plan(board)["capacity"]
    assert cap["idle"] == ["alpha"] and (cap["done"], cap["free"], cap["room"]) == (1, 0, 1) and cap["running"][0]["owner"] == "beta"


def test_capacity_lists_the_helpers_of_each_running_task(helped, add_member):
    board, tid = helped
    assert route.plan(board)["capacity"]["running"][0]["helpers"] == [] and route.plan(board)["capacity"]["idle"] == ["beta", "gamma"]
    route.add_helper(board, tid, "beta")
    route.add_helper(board, tid, "gamma", ["src/pkg/"])
    assert route.plan(board)["capacity"] is None and set(route.plan(board)["helpers"]) == {"beta", "gamma"}
    add_member(board, "delta", ["web/"], "active")
    cap = route.plan(board)["capacity"]
    assert cap["running"][0]["helpers"] == ["beta", "gamma"] and cap["idle"] == ["delta"] and (cap["free"], cap["room"]) == (2, 3)


def test_capacity_is_none_while_no_member_is_idle_even_with_room_to_add_one(make_board, add_member, post_task):
    board = make_board(max_active=6)
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    add_member(board, "broken", ["web/"], "failed")
    big = post_task(board, "big job", ["src/"])
    board.claim("alpha", big["id"])
    assert route.add_helper(board, big["id"], "beta") is None
    plan = route.plan(board)
    assert plan["capacity"] is None and set(plan["helpers"]) == {"beta"}
    assert board.cfg["max_active"] - len(board.members()) > 0
    add_member(board, "gamma", ["lib/"], "active")
    cap = route.plan(board)["capacity"]
    assert cap["idle"] == ["gamma"] and cap["room"] == 3 and cap["running"][0]["helpers"] == ["beta"]


def test_a_volunteer_that_helps_another_member_is_not_awarded_the_read_only_task(make_board, add_member, post_task, finish):
    board = make_board(max_active=4)
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    big, review = post_task(board, "big job", ["src/"]), post_task(board, "review the guide")
    board.claim("alpha", big["id"])
    with board.locked():
        board._mark("beta", [review["id"]], "volunteer", "1.0")
    assert route.add_helper(board, big["id"], "beta") is None
    assert route._award(board, review["id"]) is None and route.run(board)["given"] == {}
    assert board.task(review["id"])["state"] == "open" and board.member("beta").get("task") is None
    finish(board, "alpha", big["id"])
    assert route.run(board)["given"] == {review["id"]: "beta"} and board.task(review["id"])["owner"] == "beta"


def test_a_lease_lets_only_its_holder_write_and_names_whom_to_ask(board, add_member, post_task, finish):
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    t = post_task(board, "fix the docs build", ["docs/conf.py"])
    assert route.assign(board, "alpha", t["id"], "keep the theme") is None
    assert board.leases()[0] == {"member": "alpha", "task": t["id"], "paths": ["docs/conf.py"]}
    assert board.write_block("alpha", "docs/conf.py") is None and board.write_block("alpha", "docs/intro.md") == "it is in beta's region: ask beta by message"
    assert board.write_block("beta", "docs/conf.py") == f"alpha holds it for task {t['id']}: ask alpha by message"
    assert board.write_block("beta", "docs/intro.md") is None and board.owner_of("docs/conf.py") == "alpha" and board.owner_of("lib/x") is None
    other = post_task(board, "conf too", ["docs/"])
    with board.locked():
        assert board.why_not("beta", other["id"]) == f"task {other['id']} waits: alpha holds docs/conf.py for task {t['id']}"
    brief, = board.peek_mail("alpha")
    told, = board.peek_mail("beta")
    assert "The leader chose you for task" in brief[1]["text"] and "Your lease: you may change docs/conf.py" in brief[1]["text"]
    assert "From the leader: keep the theme" in brief[1]["text"] and "kickoff" in brief[1]["text"]
    assert "alpha holds docs/conf.py for task" in told[1]["text"] and "leave those files to alpha" in told[1]["text"]
    finish(board, "alpha", t["id"])
    assert board.leases() == [] and board.write_block("beta", "docs/conf.py") is None and board.write_block("alpha", "docs/conf.py") is not None


def test_another_regions_task_is_asked_for_with_a_pitch_and_the_leader_picks(board, add_member, post_task):
    add_member(board, "alpha", ["src/"], "active", role="writes the parser")
    add_member(board, "beta", ["docs/"], "active", role="writes the guide")
    t = post_task(board, "Parser benchmarks", ["bench/"], body="time the parser")
    assert [x.get("ask") for x in json.loads(member.offer_text(board, "alpha", member.offer(board, board.member("alpha"))).split("\n", 1)[1])] == [True]
    why = route.ask(board, "alpha", t["id"], "I wrote the parser; I need pytest-benchmark installed")
    assert why.startswith(f"Noted: you asked for task {t['id']}, outside your region. The leader picks")
    assert board.task(t["id"])["state"] == "open"
    board.mark("beta", [t["id"]], "skip", "offered; neither taken nor declined")
    (c,) = route.plan(board)["choices"]
    assert c["volunteers"] == ["alpha"] and c["pitches"] == {"alpha": "I wrote the parser; I need pytest-benchmark installed"}
    line = _item_line("choose", c)
    assert "alpha: I wrote the parser; I need pytest-benchmark installed" in line and '--note "your brief"' in line


def test_a_task_no_region_member_can_take_wakes_only_the_closest_idle_members(board, add_member, post_task):
    for name, role in (("alpha", "parser and lexer"), ("beta", "user guide"), ("gamma", "web page"), ("delta", "parser tests")):
        add_member(board, name, [f"{name}/"], role=role)
    post_task(board, "Speed up the parser", ["shared/parser.py"], body="the lexer is slow")
    assert sorted(route.plan(board)["wake"]) == ["alpha", "delta"]


def test_an_answer_to_a_member_reaches_the_user_with_a_copy_to_the_leader(board, add_member):
    add_member(board, "alpha", ["src/"], "active")
    assert member.call(board, "alpha", "message", {"to": "user", "text": "The bars are orange now (#E07A52).", "gist": "The bars are orange.",
                                                    "kind": "done"}) == {"sent": 1}
    (_, to_user), = board.peek_mail("user")
    (_, copy), = board.peek_mail("leader")
    assert (to_user["from"], to_user["report"], to_user["gist"]) == ("alpha", "done", "The bars are orange.") and copy["text"].startswith("(copy) alpha wrote to the user")
    with pytest.raises(ValueError, match="kind is one of"):
        member.call(board, "alpha", "message", {"to": "leader", "text": "x", "kind": "gossip"})


def test_an_offer_lists_first_what_more_work_waits_on_its_own_tasks_before_those_it_may_only_ask_for(board, add_member, post_task):
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    leaf, other = post_task(board, "leaf", ["src/leaf.py"]), post_task(board, "other", ["src/other.py"])
    root = post_task(board, "root", ["src/root.py"])
    mid = post_task(board, "mid", ["docs/mid.md"], [root["id"]])
    post_task(board, "end", ["docs/end.md"], [mid["id"]])
    fan = post_task(board, "fan", ["src/fan.py"])
    post_task(board, "after fan", ["docs/after.md"], [fan["id"]])
    lone = post_task(board, "lone", ["lib/x.py"])
    view = {t["id"]: t for t in json.loads(member.offer_text(board, "alpha", member.offer(board, board.member("alpha"))).split("\n", 1)[1])}
    assert list(view) == [root["id"], fan["id"], leaf["id"], other["id"], lone["id"]]
    assert (view[root["id"]]["unblocks"], view[fan["id"]]["unblocks"], "unblocks" in view[leaf["id"]]) == (2, 1, False)


def test_a_proposal_is_due_at_once_when_every_member_that_could_take_it_said_no(board, add_member, post_task):
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    t = post_task(board, "write the library", ["lib/"])
    assert route.plan(board)["proposals"] == []
    board.mark("alpha", [t["id"]], "pass", "not my kind of work")
    board.claim("beta", post_task(board, "docs", ["docs/a.md"])["id"])
    (g,) = route.plan(board)["proposals"]
    assert (g["tasks"], g["due"]) == ([t["id"]], False)
    board.mark("beta", [t["id"]], "skip", "offered; neither taken nor declined")
    (g,) = route.plan(board)["proposals"]
    assert g["due"] is True


def test_a_plan_reads_each_member_file_a_fixed_number_of_times_however_many_tasks_there_are(board, add_member, post_task, monkeypatch):
    from deepcrew import board as board_module
    reads, real = [], board_module.read_json
    monkeypatch.setattr(board_module, "read_json", lambda p: (reads.append(p), real(p))[1])
    for i in range(6):
        add_member(board, f"m{i}", [f"src/p{i}/"])

    def member_reads(n_tasks):
        for j in range(len(board.tasks()), n_tasks):
            post_task(board, f"t{j}", [f"lib/q{j}/"] if j % 2 else [f"src/p{j % 6}/f{j}.py"])
        reads.clear()
        route.plan(board)
        return sum(p.parent.name == "members" for p in reads)
    assert member_reads(4) <= 12 and member_reads(40) <= 12
