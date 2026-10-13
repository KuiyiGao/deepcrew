from __future__ import annotations

import os
import time

from deepcrew import member, providers, route


def _age(board, tid, seconds):
    old = time.time() - seconds
    os.utime(board.tasks_dir / f"{tid}.json", (old, old))


def test_a_read_only_task_goes_to_the_first_member_that_asks(board, add_member, post_task):
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    t = post_task(board, "Review everything")
    for name in ("alpha", "beta"):
        board.update_member(name, state="active", pid=4242, offered=[t["id"]])
    assert member.task_rules(board, "alpha", "TaskUpdate", {"taskId": t["id"], "status": "in_progress"}) is None
    assert board.task(t["id"])["owner"] == "alpha"
    denied = member.task_rules(board, "beta", "TaskUpdate", {"taskId": t["id"], "status": "in_progress"})
    assert denied and "claimed by alpha" in str(denied)


def test_a_task_no_region_covers_reaches_the_leader_once_it_waited(board, add_member, post_task):
    add_member(board, "alpha", ["src/"])
    t = post_task(board, "parser", ["lib/"])
    assert route.plan(board)["blocked"] == []
    _age(board, t["id"], route.UNTAKEN_S + 1)
    (b,) = route.plan(board)["blocked"]
    assert b == {"task": t["id"], "reason": f"no member's region covers its scope; start one with {providers.NAME} spawn NAME --region PATH --task {t['id']}"}


def test_a_read_only_task_waits_for_a_busy_member_and_reaches_the_leader_with_none_left(board, add_member, post_task):
    t = post_task(board, "Review")
    _age(board, t["id"], route.UNTAKEN_S + 1)
    assert [b["reason"] for b in route.plan(board)["blocked"]] == [f"no member left to take it; start one with {providers.NAME} spawn NAME --task {t['id']}"]
    other = post_task(board, "busy work", ["src/a.py"])
    add_member(board, "alpha", ["src/"], task=other["id"])
    assert route.plan(board)["blocked"] == []


def test_assign_gives_an_open_task_to_a_member(lead, deepcrew, post_task):
    t = post_task(lead, "Review everything")
    code, out, _ = deepcrew("assign", t["id"], "beta")
    assert (code, out) == (0, f"task {t['id']} assigned to beta; it comes as its next message\n") and lead.task(t["id"])["owner"] == "beta"
    code, _, err = deepcrew("assign", t["id"], "alpha")
    assert code == 1 and "claimed by beta" in err
