from __future__ import annotations

import http.client
import json
import os
import queue
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from deepcrew import board as board_module
from deepcrew import member, policy, providers, route, ui
from deepcrew import live

T0 = 1_800_000_000.0


@pytest.fixture
def make_board(make_board):
    return lambda *a, **kw: make_board(*a, **{"approval_timeout_s": 300, "question_timeout_s": 1800, **kw})


@pytest.fixture
def priced(make_board, add_member, provider_table):
    board = make_board(provider={**provider_table, "prices": {"m": {"cache_hit": 0.05, "cache_miss": 0.5, "output": 2.0}}})
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"])
    return board


def write_log(board, name, **usage):
    lines = [{"type": "assistant", "message": {"id": "a1"}}, {"type": "result", "usage": usage}]
    (board.dir / "logs" / f"{name}.ndjson").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def seen(board, prev=None):
    cur = live.observe(board)
    return cur, (live.events(prev, cur) if prev else [])


def kinds(evs):
    return [e["kind"] for e in evs]


def stuck(board, where=None):
    return [s for s in live.snapshot(board)["stuck"] if where is None or s["where"] == where]


def ask(board, kind, member, payload, age):
    r = board.request(kind, member, payload)
    board_module.write_json(board.dir / kind / f"{r['id']}.json", {**r, "ts": time.time() - age})
    return r


def test_claude_equivalent_is_the_list_price_of_the_same_tokens():
    tokens = {"input": 1_000_000, "cache_write": 500_000, "cache_read": 2_000_000, "output": 100_000}
    assert live.claude_equivalent(tokens, "Sonnet 5.5") == pytest.approx(3.75 + 0.2 + 1.0)
    assert live.claude_equivalent(tokens, "Haiku 5.5") == pytest.approx(0.1875 + 0.02 + 0.05)
    assert live.claude_equivalent(tokens, "Opus 5.5") == pytest.approx(7.5 + 0.4 + 2.0)
    assert live.claude_equivalent({}, "Opus 5.5") == 0
    assert set(live.CLAUDE_PRICES) == {"Haiku 5.5", "Sonnet 5.5", "Opus 5.5", "Fable 5.1"}


def test_a_team_without_prices_has_an_unknown_cost_not_a_free_one(crew, post_task):
    s = live.snapshot(crew)
    assert s["cost"]["actual_usd"] is None and s["cost"]["ratio"] is None and s["cost"]["saved_usd"] is None
    assert [m["cost_usd"] for m in s["members"]] == [None, None] and s["cost"]["claude"]["Sonnet 5.5"] == 0


def test_the_cost_is_the_teams_spend_as_the_settings_tab_counts_it(priced, add_member):
    write_log(priced, "alpha", input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    priced.update_cfg(lambda cfg: cfg.__setitem__("spent_removed", 0.25))
    s = live.snapshot(priced)
    assert s["cost"]["actual_usd"] == pytest.approx(0.5 + 0.25) == member.spent(priced)
    assert s["cost"]["saved_usd"] == pytest.approx(s["cost"]["claude"]["Sonnet 5.5"] - 0.75)
    priced.update_member("beta", model="unpriced")
    s = live.snapshot(priced)
    assert s["cost"]["actual_usd"] is None is member.spent(priced) and s["cost"]["saved_usd"] is None
    assert [m["cost_usd"] for m in s["members"]] == [pytest.approx(0.5), None]


def test_the_snapshot_carries_the_leaders_summaries_newest_first(crew):
    assert live.snapshot(crew)["summaries"] == []
    for i in range(12):
        crew.add_summary(f"step {i}")
    got = live.snapshot(crew)["summaries"]
    assert len(got) == live.SUMMARIES == 10 and [x["text"] for x in got[:2]] == ["step 11", "step 10"] and set(got[0]) == {"ts", "text", "done"}
    assert got == crew.summaries(10)


def test_the_snapshot_carries_the_decisions_with_who_made_them(crew):
    assert live.snapshot(crew)["decisions"] == []
    allowed = crew.request("perms", "alpha", {"tool": "Bash", "input": {"command": "ls docs"}, "tool_use_id": None, "always": False})["id"]
    policy.answer_permission(crew, allowed, True, by="user")
    denied = crew.request("perms", "beta", {"tool": "Bash", "input": {"command": "rm -rf x"}, "tool_use_id": None, "always": False})["id"]
    policy.answer_permission(crew, denied, False, by="leader")
    q = crew.request("questions", "beta", {"question": "which tone?", "task": None, "options": ["formal", "casual"]})["id"]
    crew.answer("questions", q, "answered", by="user", answer="casual")
    assert policy.set_allow(crew, "du -sh", "leader", on=False) is None
    rows = live.snapshot(crew)["decisions"]
    assert rows == crew.decisions(live.DECISIONS) and [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True)
    by_id = {r["id"]: r for r in rows if r["id"]}
    assert (by_id[allowed]["state"], by_id[allowed]["by"], by_id[allowed]["always"], by_id[allowed]["what"]) == ("allowed", "user", True, "Bash ls docs")
    assert (by_id[denied]["state"], by_id[denied]["by"], by_id[denied]["always"]) == ("denied", "leader", False)
    assert (by_id[q]["kind"], by_id[q]["state"], by_id[q]["by"], by_id[q]["what"]) == ("question", "answered", "user", "casual")
    allow = [r for r in rows if r["kind"] == "allow"]
    assert sorted((r["what"], r["state"], r["by"]) for r in allow) == [("du -sh", "removed", "leader"), ("ls", "added", "user")]
    assert set(rows[0]) == {"kind", "id", "member", "state", "by", "ts", "always", "widened", "what"}
    json.dumps(rows)


def test_the_decisions_are_read_again_only_when_a_request_or_the_allowlist_or_a_summary_changed(crew, monkeypatch):
    calls = []
    real = type(crew).decisions
    monkeypatch.setattr(type(crew), "decisions", lambda self, limit=20: calls.append(limit) or real(self, limit))
    notes = live._Notes(crew)
    obs = live.observe(crew)
    notes(obs), notes(live.observe(crew)), notes(live.observe(crew))
    assert calls == [live.DECISIONS]
    rid = crew.request("perms", "alpha", {"tool": "Bash", "input": {"command": "ls"}, "tool_use_id": None, "always": False})["id"]
    notes(live.observe(crew))
    assert len(calls) == 1
    policy.answer_permission(crew, rid, False, by="leader")
    summaries, decisions = notes(live.observe(crew))
    assert len(calls) == 2 and [d["state"] for d in decisions] == ["denied"]
    policy.set_allow(crew, "ls", "user")
    notes(live.observe(crew))
    assert len(calls) == 3
    crew.add_summary("half way")
    summaries, _ = notes(live.observe(crew))
    assert len(calls) == 4 and [x["text"] for x in summaries] == ["half way"]


def test_the_snapshot_carries_the_routers_capacity(crew, post_task):
    assert live.snapshot(crew)["capacity"] is None
    post_task(crew, "Parse", ["src/"])
    crew.claim("alpha", "1")
    snap = live.snapshot(crew)
    free = crew.cfg["max_active"] - 2
    assert snap["capacity"] == route.plan(crew)["capacity"] == {"idle": ["beta"], "free": free, "ready": 0, "room": 1 + free, "done": 0,
                                                                  "running": [{"task": "1", "title": "Parse", "owner": "alpha", "helpers": []}]}
    json.dumps(snap)


def test_members_carry_whom_they_help_and_the_model_they_run_on(crew, post_task, finish, add_member):
    post_task(crew, "Parse", ["src/"])
    crew.claim("alpha", "1")
    help_ = {"task": "1", "owner": "alpha", "paths": ["src/"], "since": 1.0}
    crew.update_member("beta", helps=help_, model="strong-model")
    snap = live.snapshot(crew)
    alpha, beta = snap["members"]
    assert (alpha["helps"], beta["helps"]) == (None, help_) and (alpha["model"], beta["model"]) == ("m", "strong-model") and snap["model"] == "m"
    assert snap["capacity"] is None
    add_member(crew, "gamma", ["lib/"], "active")
    cap = live.snapshot(crew)["capacity"]
    assert cap["running"] == [{"task": "1", "title": "Parse", "owner": "alpha", "helpers": ["beta"]}] and cap["idle"] == ["gamma"]
    finish(crew, "alpha", "1")
    assert live.snapshot(crew)["members"][1]["helps"] is None


def test_a_member_that_helps_is_not_running_without_a_task(crew, post_task, finish):
    post_task(crew, "Parse", ["src/"])
    crew.claim("alpha", "1")
    crew.update_member("beta", helps={"task": "1", "owner": "alpha", "paths": ["src/"], "since": 1.0})
    write_events(crew, "beta", requests(2 * live.BUSY_N))
    snap = live.snapshot(crew)
    assert snap["members"][1]["requests_without_task"] == 0 and not [s for s in snap["stuck"] if s["where"] == "m:beta"]
    finish(crew, "alpha", "1")
    snap = live.snapshot(crew)
    assert snap["members"][1]["requests_without_task"] == 2 * live.BUSY_N and [s for s in snap["stuck"] if s["where"] == "m:beta"]


def test_tasks_carry_who_posted_what_they_wait_for_and_how_they_ended(crew, post_task, finish):
    post_task(crew, "Parse", ["src/"])
    post_task(crew, "Docs", ["docs/"], deps=["1"])
    crew.mark("beta", ["1"], "pass", "not mine")
    crew.mark("alpha", ["1"], "skip", "")
    crew.claim("alpha", "1")
    finish(crew, "alpha", "1", "x" * (live.SUMMARY_CHARS + 40))
    t1, t2 = live.snapshot(crew)["tasks"]
    assert (t1["by"], t1["deps"], t1["blocks"], t1["state"]) == ("leader", [], ["2"], "done")
    assert t1["marks"] == {"pass": {"beta": "not mine"}, "release": {}, "skip": ["alpha"]}
    assert len(t1["summary"]) == live.SUMMARY_CHARS and t1["summary"].endswith("…")
    assert t1["updated"] == pytest.approx((crew.tasks_dir / "1.json").stat().st_mtime)
    assert (t2["deps"], t2["blocks"], t2["summary"], t2["owner"]) == (["1"], [], None, None)


@pytest.mark.sandbox
def test_config_is_the_teams_own_and_the_hint_names_what_the_next_team_would_use(crew, monkeypatch):
    s = live.snapshot(crew)
    assert s["config"] == {"provider": "fake", "model": "m", "effort": "default", "sandbox": True, "allow": 2, "pack": "basic", "max_active": crew.cfg["max_active"]}
    assert s["next"] is None
    monkeypatch.setenv("DEEPCREW_MODEL", "other")
    monkeypatch.setenv("DEEPCREW_EFFORT", "high")
    assert live.snapshot(crew)["next"] == {"model": "other", "effort": "high"}
    assert "base_url" not in json.dumps(s) and "key_sources" not in json.dumps(s)


def test_members_show_when_they_were_last_seen_what_they_wait_for_and_why_they_failed(crew):
    log = crew.dir / "logs" / "alpha.ndjson"
    log.write_text("{}\n")
    os.utime(log, (time.time() - 1000,) * 2)
    crew.log_edit("alpha", "Write", "src/a.py")
    perm = ask(crew, "perms", "alpha", {"tool": "Bash", "input": {"command": "make test"}, "tool_use_id": "x", "always": False}, 12)
    crew.update_member("beta", state="failed", failure="API error 402: no balance")
    m = {x["name"]: x for x in live.snapshot(crew)["members"]}
    assert m["alpha"]["last_seen"] == pytest.approx(time.time(), abs=5)
    assert m["alpha"]["waiting"]["kind"] == "perm" and m["alpha"]["waiting"]["id"] == perm["id"] and m["alpha"]["waiting"]["age_s"] == pytest.approx(12, abs=3)
    assert m["alpha"]["error"] is None and m["beta"]["waiting"] is None and m["beta"]["error"] == "API error 402: no balance"


def test_a_corrupt_file_that_moved_a_member_is_the_members_error(crew):
    (crew.dir / "members" / "gamma.json").write_text("{nope")
    crew.members()
    assert live.observe(crew)["errors"] == {"gamma": live.observe(crew)["errors"]["gamma"]}


def test_pending_requests_and_unread_leader_mail_list_their_fields_and_ages(crew):
    long_command = "echo " + "x" * 300
    perm = ask(crew, "perms", "alpha", {"tool": "Bash", "input": {"command": long_command}, "tool_use_id": "x", "always": False}, 40)
    question = ask(crew, "questions", "beta", {"question": "Which format?", "task": None, "answer": None}, 5)
    crew.send("alpha", "leader", "notice", "done with the parser")
    p = live.snapshot(crew)["pending"]
    assert [(x["id"], x["member"], x["tool"]) for x in p["perms"]] == [(perm["id"], "alpha", "Bash")]
    assert list(p["perms"][0]["fields"]) == ["command"] and len(p["perms"][0]["fields"]["command"]) == live.FIELD_CHARS
    assert p["perms"][0]["age_s"] == pytest.approx(40, abs=3)
    assert [(x["id"], x["member"], x["text"]) for x in p["questions"]] == [(question["id"], "beta", "Which format?")]
    assert [(x["from"], x["kind"], x["chars"]) for x in p["leader_mail"]] == [("alpha", "notice", len("done with the parser"))]


def test_a_request_its_member_stopped_waiting_for_is_not_pending(crew):
    crew.cfg["approval_timeout_s"], crew.cfg["question_timeout_s"] = 60, 600
    ask(crew, "perms", "alpha", {"tool": "Bash", "input": {"command": "make"}, "tool_use_id": "a", "always": False}, 90)
    ask(crew, "questions", "beta", {"question": "Which format?", "task": None, "answer": None}, 700)
    snap = live.snapshot(crew)
    assert snap["pending"]["perms"] == [] and snap["pending"]["questions"] == []
    assert not [s for s in snap["stuck"] if {"perm", "question"} & set(s["ref"])]


def test_paths_in_a_request_show_the_home_directory_as_a_tilde_also_where_the_text_is_cut(crew):
    command = f"node {crew.root}/sim/check.mjs > {Path.home()}/out.txt"
    ask(crew, "perms", "alpha", {"tool": "Bash", "input": {"command": command}, "tool_use_id": "a", "always": False}, 1)
    assert live.snapshot(crew)["pending"]["perms"][0]["fields"]["command"] == "node ~/proj/sim/check.mjs > ~/out.txt"
    pad = "x" * (live.FIELD_CHARS - 8)
    for r in crew.pending("perms"):
        crew.answer("perms", r["id"], "denied")
    ask(crew, "perms", "alpha", {"tool": "Bash", "input": {"command": f"{pad} {Path.home()}/deep/file"}, "tool_use_id": "b", "always": False}, 1)
    assert live.snapshot(crew)["pending"]["perms"][0]["fields"]["command"] == f"{pad} ~/deep…"


def test_nothing_the_monitor_publishes_names_the_home_directory(crew, post_task):
    home = str(Path.home())
    assert str(crew.root).startswith(home) and str(crew.shared_dir).startswith(home)
    hub = live.Hub(crew.dir)
    stream = hub.subscribe()
    hub.tick()
    secret = f"{home}/private/plan.txt"
    cut = "y" * 40 + " " + secret
    post_task(crew, f"Read {secret}", ["src/"], body=f"see {secret}")
    crew.mark("beta", ["1"], "pass", cut)
    crew.update_member("alpha", role=f"You own {home}/proj/src. Keep it small.")
    crew.update_member("beta", state="failed", failure=cut)
    crew.send("alpha", "leader", "message", cut)
    crew.log_edit("alpha", "Write", secret)
    ask(crew, "questions", "beta", {"question": cut, "task": None, "answer": None}, 1)
    rid = crew.request("perms", "alpha", {"tool": "Bash", "input": {"command": f"cat {secret}"}, "tool_use_id": None, "always": False})["id"]
    policy.answer_permission(crew, rid, False, by="leader")
    crew.add_summary(f"working in {home}/proj")
    hub.tick()
    published = [json.dumps(hub.current())]
    while not stream.empty():
        published.append(stream.get_nowait()[1])
    probes = (home, home[: len(home) // 2], json.dumps(home)[1:-1])
    assert len(published) == 4 and not [p for p in probes for text in published if p in text]
    last = json.loads(published[-1])
    assert last["snapshot"]["project"] == "~/proj" and last["snapshot"]["decisions"][0]["what"] == "Bash cat ~/private/plan.txt"
    assert {"~/private/plan.txt", "y" * 40 + " ~/private/plan.txt"} <= {e["fields"].get("path") for e in last["events"]} | {e.get("text") for e in last["events"]}


def test_regions_list_what_each_member_may_write_and_conflicts_where_that_is_contradicted(crew, post_task):
    s = live.snapshot(crew)
    assert {(r["path"], r["member"], r["kind"]) for r in s["regions"]} == {
        ("src/", "alpha", "region"), ("docs/", "beta", "region"), ("notes/alpha-*", "alpha", "note"), ("notes/beta-*", "beta", "note"),
        ("artifacts/alpha/", "alpha", "artifact"), ("artifacts/beta/", "beta", "artifact")}
    assert s["conflicts"] == []
    post_task(crew, "Misplaced", ["docs/"])
    raw = board_module.read_json(crew.tasks_dir / "1.json")
    board_module.write_json(crew.tasks_dir / "1.json", {**raw, "owner": "alpha", "status": "in_progress"})
    crew.put_member({**crew.member("beta"), "region": ["docs/", "src/pkg/"]})
    conflicts = live.snapshot(crew)["conflicts"]
    assert {"task": "1", "path": "docs/", "reason": "outside alpha's region"} in conflicts
    assert {"task": None, "path": "src/", "reason": "regions of alpha and beta overlap"} in conflicts


def test_a_request_waits_then_blocks_its_member(crew):
    fresh = ask(crew, "perms", "alpha", {"tool": "Bash", "input": {"command": "make"}, "tool_use_id": "a", "always": False}, 5)
    old = ask(crew, "questions", "beta", {"question": "Which format?", "task": None, "answer": None}, live.PENDING_BLOCK_S + 30)
    by = {s["where"]: s for s in stuck(crew)}
    assert (by["m:alpha"]["level"], by["m:alpha"]["ref"], by["m:alpha"]["what"]) == ("wait", {"perm": fresh["id"]}, "needs approval: Bash make")
    assert (by["m:beta"]["level"], by["m:beta"]["ref"]) == ("block", {"question": old["id"]}) and by["m:beta"]["what"].startswith("asks: Which format")
    assert by["m:beta"]["since_s"] == pytest.approx(live.PENDING_BLOCK_S + 30, abs=3)


def test_an_open_task_waits_for_its_dependencies(crew, post_task):
    post_task(crew, "Parse", ["src/"])
    post_task(crew, "Docs", ["docs/"], deps=["1"])
    waiting = stuck(crew, "t:2")
    assert [(s["level"], s["what"], s["ref"]) for s in waiting] == [("wait", "waits for #1", {"task": "2"})]
    assert stuck(crew, "t:1") == []
    snap = live.snapshot(crew)
    assert next(t for t in snap["tasks"] if t["id"] == "2")["waiting"]["what"] == "waits for #1"
    crew.claim("alpha", "1")
    assert stuck(crew, "t:2")


def test_the_routers_blocked_tasks_block_with_its_own_reason(crew, post_task):
    post_task(crew, "Parse", ["src/"])
    post_task(crew, "Docs", ["docs/"], deps=["1"])
    crew.cancel("1", "leader", "no longer needed")
    blocked = stuck(crew, "t:2")
    assert [(s["level"], s["what"]) for s in blocked] == [("block", "dependency 1 was cancelled")]
    assert live.snapshot(crew)["blocked"] == [{"task": "2", "reason": "dependency 1 was cancelled"}]


def test_a_proposal_waits_at_the_leader_until_it_is_started(crew, post_task):
    post_task(crew, "Library", ["lib/"])
    post_task(crew, "Tools", ["tools/"])
    assert stuck(crew, "leader") == [] and live.snapshot(crew)["proposals"] == []
    crew.claim("beta", post_task(crew, "Docs", ["docs/"])["id"])
    crew.mark("alpha", ["1", "2"], "skip", "offered; neither taken nor declined")
    items = stuck(crew, "leader")
    assert sorted((s["level"], s["what"], s["ref"]["task"]) for s in items) == [("wait", "nobody took #1 yet", "1"), ("wait", "nobody took #2 yet", "2")]
    assert len(live.snapshot(crew)["proposals"]) == 2
    old = time.time() - 61
    os.utime(crew.tasks_dir / "1.json", (old, old))
    assert ("block", "nobody took #1: proposed library") in {(s["level"], s["what"]) for s in stuck(crew, "leader")}


def test_a_task_every_member_turned_down_is_proposed_to_the_leader_at_once(crew, post_task):
    post_task(crew, "Library", ["lib/"])
    for name in ("alpha", "beta"):
        crew.mark(name, ["1"], "skip", "offered; neither taken nor declined")
    assert ("block", "nobody took #1: proposed library") in {(s["level"], s["what"]) for s in stuck(crew, "leader")}


def test_a_task_a_member_declined_blocks_at_the_leader_unless_the_router_already_says_so(crew, post_task):
    post_task(crew, "Anything", [])
    crew.mark("alpha", ["1"], "pass", "not my area")
    [item] = stuck(crew, "leader")
    assert (item["level"], item["what"], item["ref"]) == ("block", "declined by alpha: not my area", {"task": "1"})
    post_task(crew, "Parse", ["src/"])
    crew.mark("alpha", ["2"], "pass", "too big")
    crew.mark("beta", ["2"], "skip", "offered; neither taken nor declined")
    assert [s["what"] for s in stuck(crew, "t:2")][0].startswith("no member left to take it (alpha declined it: too big; beta left it undecided")
    assert [s["ref"] for s in stuck(crew, "leader")] == [{"task": "1"}]


def test_a_failed_or_stalled_member_is_flagged(crew, add_member):
    log = crew.dir / "logs" / "alpha.ndjson"
    log.write_text("{}\n")
    os.utime(log, (time.time() - live.STALL_S - 60,) * 2)
    add_member(crew, "gamma", ["README.md"], "failed", failure="API error 402: no balance")
    by = {s["where"]: s for s in stuck(crew)}
    assert (by["m:alpha"]["level"], by["m:alpha"]["what"], by["m:alpha"]["ref"]) == ("wait", "no activity", {"member": "alpha"})
    assert by["m:alpha"]["since_s"] == pytest.approx(live.STALL_S + 60, abs=5)
    assert (by["m:gamma"]["level"], by["m:gamma"]["what"]) == ("error", "API error 402: no balance")
    log.write_text("{}\n")
    assert "m:alpha" not in {s["where"] for s in stuck(crew)}
    crew.update_member("beta", state="idle")
    assert "m:beta" not in {s["where"] for s in stuck(crew)}


def test_leader_mail_waits_only_after_it_has_been_unread_for_a_while(crew):
    crew.send("alpha", "leader", "notice", "new")
    assert stuck(crew, "leader") == []
    box = crew.dir / "mail" / "leader"
    board_module.write_json(box / "0001.json", {"id": "m1", "ts": time.time() - live.MAIL_WAIT_S - 30, "from": "beta", "to": "leader", "kind": "message", "text": "ping", "task": None})
    [item] = stuck(crew, "leader")
    assert (item["level"], item["what"]) == ("wait", "mail unread from beta, alpha") and item["ref"] == {"member": "beta"}


def test_the_worst_items_come_first_and_a_quiet_team_has_none(crew, post_task):
    assert stuck(crew) == []
    post_task(crew, "Parse", ["src/"])
    post_task(crew, "Docs", ["docs/"], deps=["1"])
    ask(crew, "questions", "beta", {"question": "?", "task": None, "answer": None}, live.PENDING_BLOCK_S + 10)
    crew.update_member("alpha", state="failed", failure="boom")
    crew.mark("beta", ["1"], "skip", "offered; neither taken nor declined")
    levels = [s["level"] for s in stuck(crew)]
    assert levels == ["error", "block", "block", "wait"]


def test_a_task_is_posted_claimed_and_done(crew, post_task, finish):
    before, _ = seen(crew)
    post_task(crew, "Parse", ["src/"], deps=[])
    after, evs = seen(crew, before)
    assert kinds(evs) == ["post"] and (evs[0]["from"], evs[0]["to"], evs[0]["task"]) == ("leader", "board", "1") and "Parse" in evs[0]["label"]
    assert evs[0]["fields"] == {"subject": "Parse", "scope": ["src/"], "blockedBy": []}
    crew.claim("alpha", "1")
    claimed, evs = seen(crew, after)
    assert kinds(evs) == ["claim"] and (evs[0]["from"], evs[0]["to"], evs[0]["member"]) == ("board", "m:alpha", "alpha")
    assert evs[0]["fields"] == {"status": "in_progress", "owner": "alpha"}
    finish(crew, "alpha", "1", "parsed")
    _, evs = seen(crew, claimed)
    assert kinds(evs) == ["done"] and (evs[0]["from"], evs[0]["to"]) == ("m:alpha", "board")
    assert evs[0]["fields"] == {"status": "completed", "summary": "6 chars"}
    assert all(isinstance(e["ts"], float) for e in evs)


def test_several_steps_between_two_looks_are_all_reported_in_order(crew, post_task, finish):
    before, _ = seen(crew)
    post_task(crew, "Parse", ["src/"])
    crew.claim("alpha", "1")
    finish(crew, "alpha", "1")
    _, evs = seen(crew, before)
    assert kinds(evs) == ["post", "claim", "done"]


def test_pass_release_skip_and_cancel(crew, post_task):
    post_task(crew, "Parse", ["src/"])
    post_task(crew, "Other", ["src/pkg/"])
    before, _ = seen(crew)
    crew.mark("beta", ["1"], "pass", "not my area")
    crew.mark("alpha", ["2"], "skip", "")
    cur, evs = seen(crew, before)
    assert kinds(evs) == ["pass", "skip"]
    assert (evs[0]["from"], evs[0]["to"], evs[0]["member"], evs[0]["task"]) == ("m:beta", "board", "beta", "1") and "not my area" in evs[0]["label"]
    assert evs[0]["fields"] == {"metadata.pass:beta": "not my area"} and evs[1]["from"] == "m:alpha" and list(evs[1]["fields"]) == ["metadata.skip:alpha"]
    crew.claim("alpha", "1")
    claimed, _ = seen(crew, cur)
    crew.release("alpha", "1", "too big")
    cur, evs = seen(crew, claimed)
    assert kinds(evs) == ["release"] and evs[0]["from"] == "m:alpha" and "too big" in evs[0]["label"]
    assert evs[0]["fields"] == {"metadata.release:alpha": "too big", "status": "pending"}
    crew.cancel("2", "leader", "no longer needed")
    _, evs = seen(crew, cur)
    assert kinds(evs) == ["cancel"] and (evs[0]["from"], evs[0]["to"]) == ("leader", "board")
    assert evs[0]["fields"] == {"status": "completed", "metadata.outcome": "cancelled"}


def test_mail_is_an_event_even_if_it_was_read_before_anyone_looked(crew):
    before, _ = seen(crew)
    crew.send("leader", "alpha", "message", "please start")
    crew.send("alpha", "beta", "message", "api is ready")
    crew.send("alpha", "leader", "notice", "done")
    crew.take_mail("beta")
    cur, evs = seen(crew, before)
    assert sorted((e["from"], e["to"]) for e in evs) == [("leader", "m:alpha"), ("m:alpha", "leader"), ("m:alpha", "m:beta")]
    assert set(kinds(evs)) == {"mail"} and any("api is ready" in e["label"] for e in evs)
    assert next(e for e in evs if "api is ready" in e["label"])["fields"] == {"kind": "message", "chars": len("api is ready")}
    crew.take_mail("alpha")
    assert seen(crew, cur)[1] == []


def test_file_edits_go_to_the_project_or_the_team_folder(crew):
    before, _ = seen(crew)
    crew.log_edit("alpha", "Write", "src/a.py")
    crew.log_edit("alpha", "Edit", str(crew.artifacts_dir / "alpha" / "out.txt"))
    cur, evs = seen(crew, before)
    assert kinds(evs) == ["edit", "edit"]
    assert (evs[0]["from"], evs[0]["to"], evs[0]["member"], evs[0]["path"]) == ("m:alpha", "project", "alpha", "src/a.py")
    assert evs[0]["fields"] == {"tool": "Write", "path": "src/a.py"}
    assert (evs[1]["from"], evs[1]["to"]) == ("m:alpha", "folder") and evs[1]["fields"] == {"tool": "Edit", "path": "artifacts/alpha/out.txt"}
    assert seen(crew, cur)[1] == []


def test_an_edit_belongs_to_the_project_whatever_folder_the_page_runs_in(crew, monkeypatch):
    monkeypatch.chdir(crew.shared_dir)
    before, _ = seen(crew)
    crew.log_edit("alpha", "Write", "src/a.py")
    crew.log_edit("alpha", "Edit", str(crew.root / "src" / "b.py"))
    crew.log_edit("alpha", "Write", str(Path.home() / "elsewhere.txt"))
    _, evs = seen(crew, before)
    assert [(e["to"], e["fields"]["path"]) for e in evs] == [("project", "src/a.py"), ("project", "src/b.py"), ("project", "~/elsewhere.txt")]
    assert evs[2]["label"] == "alpha writes ~/elsewhere.txt"


def test_edits_are_read_on_from_the_last_byte_read(crew, monkeypatch):
    path = crew.dir / "edits.jsonl"

    def line(n):
        return json.dumps({"ts": T0 + n, "member": "alpha", "task": None, "tool": "Write", "path": f"src/{n}.py"}) + "\n"

    path.write_text(line(1) + line(2))
    observer, real, early = live.Observer(crew), Path.stat, len(line(1).encode())
    monkeypatch.setattr(Path, "stat", lambda self, **kw: SimpleNamespace(st_size=early) if self == path else real(self, **kw))
    assert [e["path"] for e in observer._new_edits()] == ["src/1.py", "src/2.py"]
    monkeypatch.setattr(Path, "stat", real)
    assert [e["path"] for e in observer._new_edits()] == ["src/1.py", "src/2.py"]
    with open(path, "a") as f:
        f.write(line(3)[:20])
    assert [e["path"] for e in observer._new_edits()] == ["src/1.py", "src/2.py"]
    with open(path, "a") as f:
        f.write(line(3)[20:])
    assert [e["path"] for e in observer._new_edits()] == ["src/1.py", "src/2.py", "src/3.py"]


def test_notes_are_events_when_written_and_when_changed(crew):
    before, _ = seen(crew)
    crew.note_write("leader", "plan", "first")
    (crew.notes_dir / "alpha-parser.md").write_text("notes")
    cur, evs = seen(crew, before)
    assert sorted((e["by"], e["to"]) for e in evs) == [("alpha", "folder"), ("leader", "folder")] and set(kinds(evs)) == {"note"}
    assert sorted(e["fields"]["file"] for e in evs) == ["alpha-parser.md", "leader-plan.md"]
    note = crew.notes_dir / "alpha-parser.md"
    os.utime(note, (T0, T0))
    cur, _ = seen(crew, cur)
    note.write_text("more")
    os.utime(note, (T0 + 5, T0 + 5))
    _, evs = seen(crew, cur)
    assert kinds(evs) == ["note"] and evs[0]["by"] == "alpha" and "updated" in evs[0]["label"]


def test_permission_requests_and_questions_are_asked_and_answered(crew):
    before, _ = seen(crew)
    perm = crew.request("perms", "alpha", {"tool": "Bash", "input": {"command": "make test"}, "tool_use_id": "x", "always": False})
    question = crew.request("questions", "beta", {"question": "Which format?", "task": None, "answer": None})
    cur, evs = seen(crew, before)
    assert [(e["kind"], e["from"], e["to"], e["state"]) for e in evs] == [("perm", "m:alpha", "leader", "pending"), ("question", "m:beta", "leader", "pending")]
    assert "make test" in evs[0]["label"] and "Which format?" in evs[1]["label"]
    assert evs[0]["fields"] == {"tool": "Bash", "command": "make test"} and evs[1]["fields"] == {"chars": len("Which format?")}
    crew.answer("perms", perm["id"], "allowed")
    crew.answer("questions", question["id"], "answered", answer="json")
    cur, evs = seen(crew, cur)
    assert [(e["kind"], e["from"], e["to"], e["state"]) for e in evs] == [("perm", "leader", "m:alpha", "allowed"), ("question", "leader", "m:beta", "answered")]
    assert evs[0]["fields"] == {"decision": "allow"} and evs[1]["fields"] == {"decision": "answered", "chars": 4}
    assert seen(crew, cur)[1] == []


def test_member_states_and_new_members_are_events(crew, add_member):
    before, _ = seen(crew)
    crew.update_member("beta", state="idle")
    add_member(crew, "gamma", ["README.md"], "starting")
    cur, evs = seen(crew, before)
    assert [(e["kind"], e["name"], e["state"], e["from"], e["to"]) for e in evs] == [("member_state", "beta", "idle", "m:beta", "m:beta"),
                                                                                    ("member_state", "gamma", "starting", "m:gamma", "m:gamma")]
    assert evs[0]["fields"] == {"state": "idle"}
    crew.forget("gamma")
    assert [(e["name"], e["state"]) for e in seen(crew, cur)[1]] == [("gamma", "removed")]


def test_tasks_carry_their_full_title_and_description(crew, post_task):
    title = "Parse the configuration files of every service and report each missing key with the file it came from"
    body = "First line of the brief.\n\nSecond paragraph, kept apart.   \n" + "x" * (live.DESCRIPTION_CHARS + 50)
    post_task(crew, title, ["src/"], body=body)
    post_task(crew, "No brief", ["docs/"])
    t1, t2 = live.snapshot(crew)["tasks"]
    assert t1["title"] == title and len(title) > live.TITLE_CHARS
    assert t1["description"].startswith("First line of the brief.\n\nSecond paragraph, kept apart.\n") and "\n\n" in t1["description"]
    assert len(t1["description"]) == live.DESCRIPTION_CHARS and t1["description"].endswith("…")
    assert t2["description"] == ""
    assert live.events({"tasks": {}, "members": {}, "mail": {}, "edits": [], "notes": {}, "requests": {}}, live.observe(crew))[0]["label"] == f"#1 {title[: live.TITLE_CHARS - 1]}…"


@pytest.mark.parametrize("role, clause", [
    ("You own the physics engine (sim/). Favour a stable, simple method over a clever one.", "You own the physics engine (sim/)"),
    ("Write the tests; keep them small", "Write the tests"),
    ("Review everything - then report", "Review everything"),
    ("Own the page", "Own the page"),
    ("a" * 40 + " " + "b" * 40, "a" * 40 + "…"),
    ("c" * 100, "c" * (live.ROLE_CHARS - 1) + "…"),
    ("", None),
    (None, None),
])
def test_a_role_shows_its_first_clause(role, clause):
    assert live._clause(role) == clause
    assert clause is None or len(clause) <= live.ROLE_CHARS


def test_a_member_shows_its_role_clause_and_the_title_of_the_task_it_holds(crew, post_task):
    post_task(crew, "Parse the input files", ["src/"])
    crew.update_member("alpha", role="You own the parser. Keep it small.")
    crew.claim("alpha", "1")
    alpha, beta = live.snapshot(crew)["members"]
    assert (alpha["role_brief"], alpha["task"], alpha["task_title"]) == ("You own the parser", "1", "Parse the input files")
    assert (beta["role_brief"], beta["task"], beta["task_title"]) == (None, None, None)


def update(call, mid=None, ts="2026-10-05T20:00:00.000Z", error=False, **inp):
    return [{"type": "assistant", "message": {"id": mid or "u" + call, "content": [{"type": "tool_use", "id": call, "name": "TaskUpdate", "input": {"taskId": "1", **inp}}]}},
            {"type": "user", "timestamp": ts, "message": {"content": [{"type": "tool_result", "tool_use_id": call, "is_error": error}]}}]


def requests(n, start=0):
    return [{"type": "assistant", "message": {"id": f"r{i}", "content": [{"type": kind}]}} for i in range(start, start + n) for kind in ("thinking", "text")]


def scan(*events):
    s = live.TaskScan()
    for e in events:
        s.feed(json.dumps(e))
    return s


def test_requests_are_counted_once_per_message_since_the_start():
    s = scan({"type": "user", "timestamp": "2026-10-05T20:00:01.000Z", "message": {"content": "hello"}}, *requests(12))
    assert s.count == 12 and s.stamp == live._epoch("2026-10-05T20:00:01.000Z") and s.stamp > 1e9


@pytest.mark.parametrize("inp", [{"status": "in_progress"}, {"status": "in_progress", "owner": "alpha"}, {"owner": "alpha"}, {"status": "completed", "metadata": {"summary": "s"}},
                                 {"status": "pending", "metadata": {"release:alpha": "not mine"}}])
def test_a_claim_a_completion_and_a_release_start_the_count_again(inp):
    s = scan(*requests(40), *update("c1", mid="r40", ts="2026-10-05T20:10:00.000Z", **inp), *requests(5, 41))
    assert s.count == 5 and s.stamp == live._epoch("2026-10-05T20:10:00.000Z")


def test_a_refused_update_and_other_calls_do_not_start_the_count_again():
    refused = update("c1", mid="r40", error=True, status="in_progress")
    passed = update("c2", mid="r41", metadata={"pass:alpha": "not mine"})
    other = [{"type": "assistant", "message": {"id": "r42", "content": [{"type": "tool_use", "id": "c3", "name": "Write", "input": {"status": "completed"}}]}},
             {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "c3"}]}}]
    assert scan(*requests(40), *refused, *passed, *other).count == 43
    assert scan(*requests(40), *refused, *update("c4", mid="r43", status="completed"), *requests(2, 44)).count == 2


def test_the_scan_skips_what_is_not_a_request_line():
    s = live.TaskScan()
    for line in ("", "not json", "[1]", '{"type": "assistant"}', '{"type": "assistant", "message": "x"}', json.dumps({"type": "system", "message": {"content": []}})):
        s.feed(line)
    assert s.count == 0 and s.stamp is None


def write_events(board, name, events):
    (board.dir / "logs" / f"{name}.ndjson").write_text("".join(json.dumps(e) + "\n" for e in events))


def test_a_snapshot_counts_requests_without_a_task_from_the_log(crew, post_task):
    write_events(crew, "alpha", [*requests(12), *update("c1", mid="r12", ts="2026-10-05T20:10:00.000Z", status="in_progress"), *requests(4, 13)])
    write_events(crew, "beta", requests(6))
    alpha, beta = live.snapshot(crew)["members"]
    assert (alpha["requests_without_task"], beta["requests_without_task"]) == (4, 6)
    assert alpha["no_task_since"] == live._epoch("2026-10-05T20:10:00.000Z") and beta["no_task_since"] is None
    post_task(crew, "Parse", ["src/"])
    crew.claim("alpha", "1")
    assert live.snapshot(crew)["members"][0]["requests_without_task"] == 0


def test_a_member_without_a_task_that_keeps_asking_the_model_waits_then_blocks(crew, post_task):
    assert live.BUSY_N == 30

    def busy(n, name="alpha"):
        write_events(crew, name, requests(n))
        return [s for s in live.snapshot(crew)["stuck"] if s["where"] == f"m:{name}"]

    assert busy(live.BUSY_N - 1) == []
    (s,) = busy(live.BUSY_N)
    assert (s["what"], s["level"], s["ref"]) == (f"{live.BUSY_N} requests without a task", "wait", {"member": "alpha"})
    assert busy(2 * live.BUSY_N - 1)[0]["level"] == "wait"
    (s,) = busy(2 * live.BUSY_N)
    assert (s["what"], s["level"]) == (f"{2 * live.BUSY_N} requests without a task", "block")
    crew.update_member("beta", state="idle")
    assert busy(3 * live.BUSY_N, "beta") == []
    post_task(crew, "Parse", ["src/"])
    crew.claim("alpha", "1")
    assert [s for s in live.snapshot(crew)["stuck"] if s["where"] == "m:alpha"] == []


def test_busy_since_follows_the_log_clock(crew):
    write_events(crew, "alpha", [{"type": "user", "timestamp": "2026-10-05T20:00:00.000Z", "message": {"content": "go"}}, *requests(live.BUSY_N)])
    snap = live.snapshot(crew)
    (s,) = [s for s in snap["stuck"] if s["where"] == "m:alpha"]
    assert s["since_s"] == pytest.approx(snap["now"] - live._epoch("2026-10-05T20:00:00.000Z"), abs=1.0)
    assert snap["busy_n"] == live.BUSY_N


def test_the_clock_reads_a_growing_log_on_and_a_shorter_one_again(crew):
    clock = live._TaskClock(crew)
    assert clock("alpha").count == 0
    write_events(crew, "alpha", requests(5))
    assert clock("alpha").count == 5
    with open(crew.dir / "logs" / "alpha.ndjson", "a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in requests(3, 5)))
        f.write(json.dumps(requests(1, 8)[0])[:20])
    assert clock("alpha").count == 8
    with open(crew.dir / "logs" / "alpha.ndjson", "a") as f:
        f.write(json.dumps(requests(1, 8)[0])[20:] + "\n")
    assert clock("alpha").count == 9
    write_events(crew, "alpha", requests(2, 100))
    assert clock("alpha").count == 2


def append_events(board, name, events):
    with open(board.dir / "logs" / f"{name}.ndjson", "a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in events))


def test_the_count_starts_again_when_the_leader_gives_or_takes_a_task(crew, post_task):
    clock = live._TaskClock(crew)

    def count():
        return live.snapshot(crew, clock=clock)["members"][0]["requests_without_task"]

    write_events(crew, "alpha", requests(40))
    assert count() == 40
    post_task(crew, "Parse", ["src/"])
    crew.claim("alpha", "1")
    append_events(crew, "alpha", requests(10, 40))
    assert count() == 0
    crew.cancel("1", "leader", "not needed after all")
    assert crew.member("alpha")["task"] is None and count() == 0
    append_events(crew, "alpha", requests(3, 50))
    snap = live.snapshot(crew, clock=clock)
    assert snap["members"][0]["requests_without_task"] == 3 and snap["members"][0]["no_task_since"] == pytest.approx(time.time(), abs=5)


def start_page(board, monkeypatch=None, team_dir=None):
    ext = live.View(team_dir or board.dir)
    ready, result = queue.Queue(), {}
    thread = threading.Thread(target=lambda: result.update(ui.serve(on_ready=ready.put, timeout_s=30, extensions=[ext])), daemon=True)
    thread.start()
    parts = urlsplit(ready.get(timeout=5))
    token = parse_qs(parts.query)["t"][0]

    def call(method, path, body=None, *, auth=token, host=f"127.0.0.1:{parts.port}", raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=5)
        head = {"Host": host, "Content-Type": "application/json", **({"X-Deepcrew-Token": auth} if auth is not None else {})}
        conn.request(method, path, body=raw if raw is not None else json.dumps(body) if body is not None else None, headers=head)
        reply = conn.getresponse()
        data = reply.read()
        conn.close()
        return reply.status, reply, (json.loads(data) if reply.getheader("Content-Type", "").startswith("application/json") else data)

    def stream(auth=token, timeout=3):
        conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=timeout)
        conn.request("GET", "/api/live/events", headers={"Host": f"127.0.0.1:{parts.port}", "X-Deepcrew-Token": auth})
        return conn, conn.getresponse()

    return SimpleNamespace(token=token, port=parts.port, call=call, stream=stream, thread=thread, result=result, board=board, ext=ext)


@pytest.fixture
def page(crew, monkeypatch):
    monkeypatch.setattr(live, "OBSERVE_S", 0.05)
    p = start_page(crew)
    yield p
    if p.thread.is_alive():
        p.call("POST", "/api/done", {})
    p.thread.join(5)


def next_block(resp):
    lines = []
    while True:
        line = resp.readline().decode()
        if line == "":
            pytest.fail(f"the stream ended{' inside a block: ' + repr(lines) if lines else ''}")
        if line == "\n":
            if lines:
                break
            continue
        lines.append(line.rstrip("\n"))
    name = next((ln[7:] for ln in lines if ln.startswith("event: ")), None)
    data = next((json.loads(ln[6:]) for ln in lines if ln.startswith("data: ")), None)
    return name, data, lines


def test_the_stream_starts_with_the_snapshot_and_delivers_a_change(page, post_task):
    conn, resp = page.stream()
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
    name, snap, _ = next_block(resp)
    assert name == "snapshot" and snap["team"] == "t" and snap["tasks"] == []
    post_task(page.board, "Parse", ["src/"])
    page.board.claim("alpha", "1")
    page.board.log_edit("alpha", "Write", "src/a.py")
    got = []
    for _ in range(20):
        name, data, _ = next_block(resp)
        assert name == "update"
        got += [e["kind"] for e in data["events"]]
        last = data["snapshot"]
        if len(got) >= 3:
            break
    conn.close()
    assert got == ["post", "claim", "edit"]
    assert last["counts"]["claimed"] == 1 and last["tasks"][0]["owner"] == "alpha"


def test_a_snapshot_that_differs_only_in_how_long_a_proposal_waited_is_not_new(crew, post_task):
    post_task(crew, "Library", ["lib/"])
    for name in ("alpha", "beta"):
        crew.mark(name, ["1"], "skip", "offered; neither taken nor declined")
    first = live.snapshot(crew)
    time.sleep(0.01)
    second = live.snapshot(crew)
    assert first["proposals"][0]["waited"] != second["proposals"][0]["waited"] and live._key(first) == live._key(second)


def test_the_hub_works_the_routing_plan_out_again_only_when_the_board_changed(crew, post_task, monkeypatch):
    calls, real = [], route.plan
    monkeypatch.setattr(route, "plan", lambda board: calls.append(1) or real(board))
    hub = live.Hub(crew.dir)
    for _ in range(3):
        hub.tick()
    assert len(calls) == 1
    post_task(crew, "Parse", ["src/"])
    hub.tick()
    assert len(calls) == 2 and hub.current()["tasks"][0]["title"] == "Parse"
    crew.update_cfg(lambda cfg: cfg.__setitem__("max_active", 3))
    hub.tick()
    assert len(calls) == 3
    monkeypatch.setattr(live, "PLAN_S", 0.0)
    hub.tick()
    assert len(calls) == 4
    monkeypatch.setattr(live, "PLAN_S", 3600.0)
    hub.usage.ttl = 3600.0
    reads, members = [], type(crew).members
    monkeypatch.setattr(type(crew), "members", lambda self: reads.append(1) or members(self))
    for name in ("alpha-parser.md", "beta-docs.md", "leader-plan.md"):
        (crew.notes_dir / name).write_text("notes")
    hub.tick()
    assert len(reads) == 1 and {n["by"] for n in hub.prev["notes"].values()} == {"alpha", "beta", "leader"}


def test_the_stream_pings_while_nothing_happens(page, monkeypatch):
    monkeypatch.setattr(live, "PING_S", 0.1)
    conn, resp = page.stream()
    assert next_block(resp)[0] == "snapshot"
    assert next_block(resp)[2] == [": ping"]
    conn.close()


def test_the_stream_says_when_the_env_file_changes(page):
    conn, resp = page.stream()
    assert next_block(resp)[0] == "snapshot"
    providers.env_file_set({"DEEPCREW_MAX_ACTIVE": "3"})
    names = [next_block(resp)[0]]
    while "env" not in names and len(names) < 20:
        names.append(next_block(resp)[0])
    conn.close()
    assert "env" in names


def test_done_ends_the_page_and_the_monitor_with_it(page):
    conn, resp = page.stream()
    assert next_block(resp)[0] == "snapshot"
    assert page.call("POST", "/api/done", {})[0] == 200
    page.thread.join(5)
    assert not page.thread.is_alive() and page.result["done"] is True
    assert page.ext.hub.stop.is_set()
    assert resp.read() == b""
    conn.close()


@pytest.fixture
def history(priced, post_task, task_edit):
    priced.cfg["created"] = T0
    post_task(priced, "Parse", ["src/"])
    post_task(priced, "Docs", ["docs/"], deps=["1"])
    for tid, when in (("1", T0 + 1), ("2", T0 + 2)):
        path = priced.tasks_dir / f"{tid}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["metadata"]["posted"] = when
        path.write_text(json.dumps(raw))
    priced.claim("alpha", "1")
    task_edit(priced, "1", status="completed", summary="parsed")
    priced.sync_member("alpha")
    priced.update_member("alpha", state="idle", created=T0 + 10, runs=1)
    priced.update_member("beta", created=T0 + 20)
    for tid, when in (("1", T0 + 100), ("2", T0 + 5)):
        os.utime(priced.tasks_dir / f"{tid}.json", (when, when))
    (priced.dir / "edits.jsonl").write_text("".join(json.dumps({"ts": T0 + t, "member": "alpha", "task": "1", "tool": "Write", "path": p}) + "\n"
                                                   for t, p in ((20, "src/a.py"), (30, "src/b.py"))))
    box = priced.dir / "mail" / "leader"
    (box / "read").mkdir(parents=True)
    board_module.write_json(box / "read" / "0001.json", {"id": "m1", "ts": T0 + 90, "from": "alpha", "to": "leader", "kind": "message", "text": "task 1 done", "task": "1"})
    board_module.write_json(box / "0002.json", {"id": "m2", "ts": T0 + 50, "from": "alpha", "to": "leader", "kind": "notice", "text": "halfway", "task": None})
    perm = priced.request("perms", "alpha", {"tool": "Bash", "input": {"command": "make"}, "tool_use_id": "x", "always": False})
    path = priced.dir / "perms" / f"{perm['id']}.json"
    board_module.write_json(path, {**perm, "ts": T0 + 40, "state": "allowed", "by": "leader"})
    os.utime(path, (T0 + 45, T0 + 45))
    write_log(priced, "alpha", input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=2_000_000, cache_creation_input_tokens=0)
    return priced


PAGES = [Path(live.__file__).with_name(n) for n in ("live.html", "live.py")] + [Path(ui.__file__).with_name(n) for n in ("ui.html", "ui.py")]


def test_no_page_text_uses_a_middle_dot():
    for page in PAGES:
        assert "\u00b7" not in page.read_text(encoding="utf-8"), page.name


def test_the_monitor_lays_out_nothing_from_a_snapshot_without_a_team():
    assert "!!(s.snap && s.snap.members) && (s.snap.members.length > 6" in monitor_page()


def monitor_page():
    return Path(live.__file__).with_name("live.html").read_text(encoding="utf-8")


def core_page():
    return Path(ui.__file__).with_name("ui.html").read_text(encoding="utf-8")


def iso(t):
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def said_by(kind, content, t, uid, **extra):
    return {"type": kind, "uuid": uid, "timestamp": iso(t), "message": {"role": kind, "content": content}, **extra}


def shell(command):
    return {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}


@pytest.fixture
def leader_log(crew, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (board_module.state_home() / "leaders").mkdir(parents=True, exist_ok=True)
    board_module.write_json(board_module.state_home() / "leaders" / "sess-1.json", {"project": str(crew.root), "team": "t", "team_dir": str(crew.dir)})
    path = Path.home() / ".claude" / "projects" / "-proj" / "sess-1.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()

    def write(*records, into=path):
        with open(into, "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in records))
    write.path = path
    return write


def test_an_answer_says_who_gave_it_and_the_leaders_routine_approvals_stay_out_of_the_call(crew):
    prev = live.observe(crew)
    mine = ask(crew, "perms", "alpha", {"tool": "Bash", "input": {"command": "npm install"}}, 1)
    routine = ask(crew, "perms", "beta", {"tool": "Bash", "input": {"command": "ls docs"}}, 1)
    policy.answer_permission(crew, mine["id"], allow=True, always=False, by="user")
    policy.answer_permission(crew, routine["id"], allow=True, always=False, by="leader")
    _, evs = seen(crew, prev)
    answered = {e["id"]: e["from"] for e in evs if e["kind"] == "perm" and e["state"] != "pending"}
    assert answered == {mine["id"]: "user", routine["id"]: "leader"}
    feed = live.snapshot(crew)["feed"]
    assert {f["id"] for f in feed if f["kind"] in ("perm", "answer")} == {mine["id"]}
    assert [f for f in feed if f["kind"] == "answer"][0]["from"] == "user"


def test_a_questions_gist_is_its_question_and_a_members_own_gist_wins(crew):
    assert live.gist("I read chart.ts. There are two ways. Should the weekly chart use bars or a line?", question=True) == "Should the weekly chart use bars or a line?"
    prev = live.observe(crew)
    crew.send("alpha", "leader", "message", "tokenizer.py and its 14 tests are in; parse() is next", gist="The tokenizer is done.")
    crew.send("beta", "leader", "message", "beta: docs/intro.md is written. Next is the API page.")
    _, evs = seen(crew, prev)
    assert {e["from"]: e["gist"] for e in evs if e["kind"] == "mail"} == {"m:alpha": "The tokenizer is done.", "m:beta": "Intro.md is written. Next is the API page."}
    feed = {f["from"]: f for f in live.snapshot(crew)["feed"] if f["kind"] == "mail"}
    assert feed["alpha"]["gist"] == "The tokenizer is done." and feed["alpha"]["text"].startswith("tokenizer.py")


def test_snapshot_fields_and_savings(priced, post_task):
    write_log(priced, "alpha", input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=2_000_000, cache_creation_input_tokens=0)
    post_task(priced, "Parse", ["src/"])
    post_task(priced, "Docs", ["docs/"])
    priced.claim("alpha", "1")
    priced.send("alpha", "leader", "message", "hello")
    s = live.snapshot(priced)
    assert (s["team"], s["project"], s["provider"], s["model"]) == ("t", str(priced.root), "fake", "m")
    assert s["shared_dir"] == str(priced.shared_dir) and "view_only" not in s and s["lang"] == "English" and "Simplified Chinese" in s["langs"]
    alpha = next(m for m in s["members"] if m["name"] == "alpha")
    assert alpha["state"] == "active" and alpha["region"] == ["src/"] and alpha["task"] == "1" and alpha["pct"] == 10 and alpha["shot"] is None
    assert alpha["usage"]["output_tokens"] == 100_000 and alpha["usage"]["requests"] == 1
    assert alpha["cost_usd"] == pytest.approx(1_000_000 * 0.5e-6 + 2_000_000 * 0.05e-6 + 100_000 * 2.0e-6)
    assert [(t["id"], t["state"], t["owner"], t["scope"]) for t in s["tasks"]] == [("1", "claimed", "alpha", ["src/"]), ("2", "open", None, ["docs/"])]
    assert s["counts"] == {"open": 1, "claimed": 1, "done": 0, "cancelled": 0, "mail_unread_leader": 1, "mail_total": 1, "perms_pending": 0, "questions_pending": 0}
    assert s["proposals"] == [] and s["blocked"] == [] and s["budget"] == {"usd": None, "spent": pytest.approx(0.8)}
    cost = s["cost"]
    assert cost["actual_usd"] == pytest.approx(0.8)
    assert cost["tokens"] == {"input": 1_000_000, "cache_write": 0, "cache_read": 2_000_000, "output": 100_000,
                              "long": {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}}
    assert cost["claude"]["Sonnet 5.5"] == pytest.approx(2.5 + 0.2 + 1.0)
    assert set(cost["claude"]) == set(live.CLAUDE_PRICES) and cost["compare"] == "Sonnet 5.5"
    assert cost["saved_usd"] == pytest.approx(cost["claude"]["Sonnet 5.5"] - cost["actual_usd"]) == pytest.approx(2.9)
    json.dumps(s)
    assert "key" not in json.dumps(s).lower().replace("key_", "")


def test_how_far_a_member_is_counts_the_reports_it_sent_on_the_task_it_holds(crew, post_task):
    post_task(crew, "Parse", ["src/"])
    crew.claim("alpha", "1")
    pct = lambda: next(m for m in live.snapshot(crew)["members"] if m["name"] == "alpha")["pct"]
    assert pct() == 10
    crew.send("alpha", "leader", "message", "plan: tokenizer, then the parser", "1", report="kickoff")
    assert pct() == 30
    crew.send("alpha", "leader", "message", "the tokenizer works", "1", report="progress")
    crew.send("alpha", "beta", "message", "a progress note to a member is no report to the leader", "1", report="progress")
    assert pct() == 60
    crew.send("alpha", "leader", "message", "the parser works too", "1", report="progress")
    crew.send("alpha", "leader", "message", "and the errors", "1", report="progress")
    assert pct() == 80
    assert next(m for m in live.snapshot(crew)["members"] if m["name"] == "beta")["pct"] is None


def test_a_done_report_and_the_task_done_are_one_line_and_the_leaders_copies_are_left_out(crew, post_task, finish):
    post_task(crew, "Parse", ["src/"])
    post_task(crew, "Docs", ["docs/"])
    crew.claim("alpha", "1")
    crew.claim("beta", "2")
    crew.send("alpha", "leader", "message", "Parser done, 12 tests pass", "1", gist="The parser works now.", report="done")
    finish(crew, "alpha", "1", "parser")
    finish(crew, "beta", "2", "docs")
    crew.send("beta", "user", "message", "The docs read well now.")
    crew.send("beta", "leader", "notice", live.COPY + "beta wrote to the user: The docs read well now.")
    prev = live.observe(crew)
    feed = live.snapshot(crew)["feed"]
    assert [(f["kind"], f["from"], f.get("task")) for f in feed if f["kind"] in ("done", "mail")] == [
        ("mail", "alpha", "1"), ("done", "beta", "2"), ("mail", "beta", None)]
    crew.send("alpha", "user", "message", "Shall I go on?")
    crew.send("alpha", "leader", "notice", live.COPY + "alpha wrote to the user: Shall I go on?")
    evs = live.events(prev, live.observe(crew))
    assert [(e["from"], e["to"]) for e in evs if e["kind"] == "mail"] == [("m:alpha", "user")]


def test_the_observer_reads_a_message_once_and_follows_it_to_read(crew):
    obs = live.Observer(crew)
    crew.send("alpha", "leader", "message", "first")
    assert [m["text"] for m in obs.observe()["mail"].values()] == ["first"]
    opened = []
    real = live.Observer._read
    live.Observer._read = lambda self, path: opened.append(path.name) or real(self, path)
    try:
        crew.send("beta", "leader", "message", "second")
        assert sorted(m["text"] for m in obs.observe()["mail"].values()) == ["first", "second"] and len(opened) == 1
        crew.take_mail("leader")
        after = obs.observe()["mail"]
        assert all(m["read"] for m in after.values()) and len(after) == 2 and len(opened) == 3
        assert len(obs._mail) == 2
    finally:
        live.Observer._read = real


def test_a_members_newest_picture_is_its_shared_screen(page, monkeypatch):
    monkeypatch.setattr(live, "SHOT_S", 0.0)
    folder = page.board.artifacts_dir / "alpha"
    folder.mkdir(parents=True)
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    (folder / "old.png").write_bytes(png)
    (folder / "notes.txt").write_text("not a picture")
    os.utime(folder / "old.png", (time.time() - 60, time.time() - 60))
    (folder / "new.png").write_bytes(png + b"y")
    snap = live.snapshot(page.board, obs=live.Observer(page.board).observe())
    shot = next(m for m in snap["members"] if m["name"] == "alpha")["shot"]
    assert shot["name"] == "new.png" and next(m for m in snap["members"] if m["name"] == "beta")["shot"] is None
    status, _, body = page.call("GET", "/api/live/shot?m=alpha")
    assert status == 200 and body["name"] == "new.png" and body["data"].startswith("data:image/png;base64,")
    assert page.call("GET", "/api/live/shot?m=beta")[0] == 404 and page.call("GET", "/api/live/shot?m=..%2Falpha")[0] == 404
    assert page.call("GET", "/api/live/shot?m=alpha", auth="wrong")[0] == 403
    (folder / "new.png").write_bytes(b"x" * (live.SHOT_BYTES + 1))
    assert page.call("GET", "/api/live/shot?m=alpha")[0] == 404


def test_the_user_chooses_the_language_replies_come_in(page):
    assert page.call("POST", "/api/live/lang", {"lang": "Klingon"})[0] == 400
    assert page.call("POST", "/api/live/lang", {"lang": "Simplified Chinese"}, auth="wrong")[0] == 403
    status, _, body = page.call("POST", "/api/live/lang", {"lang": "Simplified Chinese"})
    assert status == 200 and body == {"lang": "Simplified Chinese"} and page.board.live_cfg()["reply_lang"] == "Simplified Chinese"
    told = [board_module.read_json(p)["text"] for p in page.board.mail_pending("leader")]
    assert len(told) == 1 and "Simplified Chinese" in told[0] and "stays in English" in told[0]
    page.call("POST", "/api/live/lang", {"lang": "Simplified Chinese"})
    assert len(page.board.mail_pending("leader")) == 1
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and page.call("GET", "/api/live/snapshot")[2].get("lang") != "Simplified Chinese":
        time.sleep(0.05)
    assert page.call("GET", "/api/live/snapshot")[2]["lang"] == "Simplified Chinese"


def test_the_page_stream_carries_the_new_fields(page):
    page.board.add_summary("started")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snap = page.call("GET", "/api/live/snapshot")[2]
        if snap["summaries"]:
            break
        time.sleep(0.05)
    assert [x["text"] for x in snap["summaries"]] == ["started"] and snap["decisions"] == [] and snap["capacity"] is None
    assert all(set(m) >= {"helps", "model", "pct", "shot"} for m in snap["members"]) and set(snap) >= {"lang", "langs", "budget"}


def test_the_page_stream_carries_the_count(page):
    write_events(page.board, "alpha", requests(live.BUSY_N))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snap = page.call("GET", "/api/live/snapshot")[2]
        if snap["members"][0]["requests_without_task"]:
            break
        time.sleep(0.05)
    assert snap["members"][0]["requests_without_task"] == live.BUSY_N and any(s["where"] == "m:alpha" for s in snap["stuck"])


def test_the_live_view_is_part_of_the_setup_page(page):
    status, reply, body = page.call("GET", f"/?t={page.token}")
    assert status == 200 and b"<title>deepcrew" in body and body.count(b"</body>") == 1
    assert body.index(b'id="live"') < body.index(b"</body>") and b"'/api/live/'" in body
    assert reply.getheader("Content-Security-Policy").endswith("img-src 'self' data:")


def test_the_live_api_needs_the_token_and_the_host(page):
    assert page.call("GET", "/api/live/snapshot", auth=None)[0] == 403
    assert page.call("GET", "/api/live/snapshot", auth="wrong")[0] == 403
    assert page.call("GET", "/api/live/snapshot", host="evil.example")[0] == 403
    assert page.call("POST", "/api/live/say", {"to": "leader", "text": "x"}, auth="wrong")[0] == 403
    assert page.stream(auth="wrong")[1].status == 403
    assert page.call("GET", "/api/live/nothing")[0] == 404
    assert page.call("POST", "/api/live/say", raw=b"x" * (ui.MAX_BODY + 1))[0] == 413
    status, _, snap = page.call("GET", "/api/live/snapshot")
    assert status == 200 and snap["team"] == "t" and [m["name"] for m in snap["members"]] == ["alpha", "beta"]
    assert page.thread.is_alive()


def test_the_stream_does_not_repeat_itself_while_only_ages_change(page):
    ask(page.board, "perms", "alpha", {"tool": "Bash", "input": {"command": "make"}, "tool_use_id": "x", "always": False}, 1)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not page.call("GET", "/api/live/snapshot")[2]["pending"]["perms"]:
        time.sleep(0.05)
    conn, resp = page.stream(timeout=0.6)
    assert next_block(resp)[0] == "snapshot"
    with pytest.raises(TimeoutError):
        next_block(resp)
    conn.close()


def test_a_look_that_fails_is_said_once_on_stderr_and_the_looks_go_on(crew, monkeypatch, capsys):
    monkeypatch.setattr(live, "OBSERVE_S", 0.01)
    calls = []

    def broken(self):
        calls.append(1)
        raise OSError("a file was replaced mid-read")

    monkeypatch.setattr(live.Observer, "observe", broken)
    ext = live.View(crew.dir)
    ext.start()
    deadline = time.monotonic() + 5
    while len(calls) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    ext.stop()
    err = capsys.readouterr().err
    assert len(calls) >= 5 and err.count("page: a look at the team failed (OSError: a file was replaced mid-read)") == 1


def test_the_page_and_its_approvals_stay_up_when_the_first_look_fails(crew, monkeypatch):
    def broken(self):
        raise ValueError("unreadable")

    monkeypatch.setattr(live.Observer, "observe", broken)
    p = start_page(crew)
    try:
        assert p.call("GET", "/api/live/snapshot")[0] == 200 and p.call("GET", "/api/ping")[0] == 200
    finally:
        p.call("POST", "/api/done", {})
        p.thread.join(5)


def test_the_page_follows_a_team_that_does_not_exist_yet(make_board, tmp_path, monkeypatch):
    monkeypatch.setattr(live, "OBSERVE_S", 0.05)
    team_dir = tmp_path / "later"
    p = start_page(None, team_dir=team_dir)
    try:
        status, _, snap = p.call("GET", "/api/live/snapshot")
        assert status == 200 and snap == {"team": None}
        assert p.call("POST", "/api/live/say", {"to": "leader", "text": "hi"})[0] == 400
        conn, resp = p.stream()
        assert next_block(resp)[1] == {"team": None}
        make_board("later").dir.rename(team_dir)
        name, data, _ = next_block(resp)
        conn.close()
        assert name == "update" and data["snapshot"]["team"] == "later"
    finally:
        p.call("POST", "/api/done", {})
        p.thread.join(5)


def test_team_json_changes_reach_the_page_but_nothing_else_polls_the_config(page):
    before = page.call("GET", "/api/live/snapshot")[2]["config"]["allow"]
    cfg = board_module.read_json(page.board.dir / "team.json")
    board_module.write_json(page.board.dir / "team.json", {**cfg, "bash_allow": [*cfg["bash_allow"], "ls"]})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and page.call("GET", "/api/live/snapshot")[2]["config"]["allow"] == before:
        time.sleep(0.05)
    assert page.call("GET", "/api/live/snapshot")[2]["config"]["allow"] == before + 1


def test_the_page_hook_builds_the_view_of_the_named_team(lead):
    ext = live.page(lead.root, "main")
    assert ext.name == "live" and ext.hub.dir == lead.dir
    assert live.page(lead.root, None).hub.dir == lead.dir
    assert ext.routes and ext.streams and b"</script>" in ext.html


def test_the_user_writes_to_a_member_the_leader_or_everyone_from_the_call(page, monkeypatch):
    ran = []
    monkeypatch.setattr(live.route, "run", lambda board: ran.append(board.dir) or {})
    assert page.call("POST", "/api/live/say", {"to": "alpha", "text": "  try the fast path  "})[0] == 200
    got = [board_module.read_json(p) for p in page.board.mail_pending("alpha")]
    assert [(m["from"], m["kind"], m["text"]) for m in got] == [("user", "message", "try the fast path")]
    copy = [board_module.read_json(p) for p in page.board.mail_pending("leader")]
    assert copy[-1]["from"] == "user" and copy[-1]["text"] == live.COPY + "the user wrote to alpha: try the fast path" and len(ran) == 1
    assert page.call("POST", "/api/live/say", {"to": "leader", "text": "pause after this round"})[0] == 200
    assert board_module.read_json(page.board.mail_pending("leader")[-1])["text"] == "pause after this round" and len(ran) == 1
    assert page.call("POST", "/api/live/say", {"to": "all", "text": "stand-up in 5"})[0] == 200
    assert [board_module.read_json(p)["text"] for p in page.board.mail_pending("beta")] == ["stand-up in 5"]
    for bad in ({"to": "gamma", "text": "x"}, {"to": "alpha", "text": "   "}, {"to": "alpha", "text": "x" * (live.SAY_CHARS + 1)}, {"to": "alpha"}):
        assert page.call("POST", "/api/live/say", bad)[0] == 400, bad


@pytest.mark.parametrize(("text", "short"), [
    ("physics: task 64 (bake landed markers) is in — src/sim/bake.js now writes speeds[i] = -1 (see notes/x.md)", "Task 64 is in."),
    ("textures: tasks 3, 18, 19 all completed. Region is quiet now. Delivered: src/textures/noise.js", "Tasks 3, 18, 19 all completed."),
    ("Leader: time box. Please land task 68 within 20 minutes.", "Time box. Please land task 68 within 20 minutes."),
    ("**Done**: wrote `src/very/long/path/to/the/module_with_a_long_name.py` and `ok`", "Done: wrote module_with_a_long_name.py and ok"),
    ("x" * 300, "X" + "x" * (live.GIST_CHARS - 2) + "…"),
])
def test_a_gist_is_the_first_plain_sentence_without_names_brackets_markup_or_paths(text, short):
    assert live.gist(text) == short


def page_text():
    return Path(live.__file__).with_name("live.html").read_text(encoding="utf-8")


def test_the_page_puts_the_team_call_and_the_monitor_beside_settings_and_is_one_fragment():
    text = page_text()
    assert "addTab('settings', 'Settings', 'gear');\naddTab('call', 'Team Call', 'call');\naddTab('monitor', 'Monitor', 'graph');" in text
    assert "ROUTE_BASE = '/api/live/'" in text and "ROUTE_BASE + 'say'" in text and "ROUTE_BASE + 'lang'" in text and "ROUTE_BASE + 'shot?m='" in text
    assert not re.findall(r"(?:src|href)\s*=\s*[\"']", text) and "@import" not in text and "url(http" not in text
    assert set(re.findall(r"https?://[^\s'\"]+", text)) <= {"http://www.w3.org/2000/svg"}
    assert "prefers-reduced-motion" in text and "prefers-color-scheme: dark" in text and "<html" not in text


def test_the_page_opens_on_settings_until_there_is_a_team_unless_a_tab_was_picked():
    text = page_text()
    assert "show(chosen ? wanted() : 'settings', true);" in text
    assert "if (!snap.team && shown !== 'settings' && !location.hash.slice(1)) show('settings', true);" in text
    assert "else if (snap.team && !chosen) show('call', true);" in text


def test_the_header_shows_the_team_a_dot_a_member_raised_hands_the_tasks_as_a_bar_the_spend_as_a_tank_and_the_hosts_load():
    text = page_text()
    assert "chipsEl.append(teamChip, crewChip, needChip, doneChip, spendChip, hostChip);" in text and "icon('hand'), needN);" in text
    assert "hostChip.dataset.level = ld.level;" in text and ".lv-chip-s.host[data-level=full] .gg i { background: var(--bad); }" in text
    assert "class: asks.has(m.name) ? 'ask'" in text and "m.state === 'active' || m.state === 'starting' ? 'on' : ''" in text
    assert "tasksBar.set(total ? c.done / total : 0)" in text and "spendTank.set(b.usd ? (spent || 0) / b.usd" in text
    assert "frac >= 0.9 ? 'var(--bad)' : frac >= 0.7 ? 'var(--warn)'" in text
    assert "const usd = (v) => (v == null ? '–'" in text
    assert "setText(team, snap.team || 'no team');\n    if (!snap.team) return;" in text


def test_both_views_share_one_column_the_leaders_last_word_the_chat_and_what_needs_you():
    text = page_text()
    assert "asideCol.append(leadCard, chatCard, needCard);" in text and "h('h3', {}, 'Leader → You')" in text and "h('h3', {}, 'Needs you')" in text
    assert "post('/api/answer', { id: r.id, allow: true })" in text and "post(ROUTE_BASE + 'say', { to: s.to, text })" in text
    assert "const parts = () => [head, aside, ...Object.values(views)];" in text


def test_whoever_sends_or_receives_lights_up_and_a_member_that_asks_raises_a_hand_that_opens_its_request():
    text = page_text()
    assert "function light(node, color) {" in text and "light(a, color);" in text and "function bump(node, color) { if (!still()) { light(node, color);" in text
    assert "function lightWires(from, to, color) {" in text and "setTimeout(() => lightWires(ev.from, ev.to, color), delay);" in text
    assert text.count("aside.focusNeeds(name); };") == 2
    ui_page = core_page()
    assert "hand: raised(0), handWave: raised(1)," in ui_page and 'waiting: [...hold({eyes: "up", prop: "hand"}, 7), ...hold({eyes: "up", prop: "handWave"}, 7)],' in ui_page


def test_the_monitor_is_a_network_a_task_table_and_folded_panes_an_add_on_may_add_to():
    text = page_text()
    assert "h('h2', {}, 'Network ', h('span', { class: 'sl' }, '/'), ' Blackboard')" in text and "h('th', {}, 'Waiting')" in text
    assert "const act = pane('Activity'), dec = pane('Decisions'), cost = pane('Cost'), tools = pane('Tools');" in text and "\n    pane,\n" in text
    assert "if (id === 'tools') return tools.el.hidden ? null : tools.el.firstChild;" in text
    assert "s.snap.members.length > 6 || net.clientWidth < 900" in text


def test_the_settings_of_the_view_choose_the_reply_language_and_you_as_he_or_she():
    text = page_text()
    assert "post(ROUTE_BASE + 'lang', { lang: sel.value })" in text and "for (const k of ['he', 'she'])" in text
    assert "store.set('deepcrew-live-avatar', k)" in text and "emit('avatar', k)" in text


def test_members_are_the_settings_tabs_divers_in_cream_and_you_a_person_of_your_choice():
    text = page_text()
    assert "const PX = typeof PixelCrew !== 'undefined' ? PixelCrew : null;" in text and "new PX.Diver({ trim: color, body: 'var(--diver)', size" in text
    assert "kind === 'leader' ? 'var(--c-lead)' : color" in text
    assert 'constructor({suit = "fast", color = null, trim = null, body = CREAM, size = 2' in core_page()
    rows = re.findall(r"^  (he|she): \[(.*?)\],$", text, re.M | re.S)
    assert [k for k, _ in rows] == ["he", "she"]
    for _, body in rows:
        lines = re.findall(r"'([^']*)'", body)
        assert len(lines) == 24 and {len(x) for x in lines} == {24}


def test_every_colour_of_the_page_style_is_a_token_of_its_colour_map():
    text = page_text()
    style = text[: text.index("</style>")]
    start, end = style.index("#live {"), style.index("#live[hidden]")
    rules = style[:start] + style[end:]
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", rules)
    block = style[start:end]
    assert sorted(set(re.findall(r"--m(\d+):", block)), key=int) == [str(i) for i in range(10)]


def test_the_reply_language_is_one_on_offer_and_its_script_shows_in_a_gist():
    assert board_module.reply_lang({}) == "English" and board_module.reply_lang({"reply_lang": "Korean"}) == "Korean"
    assert board_module.reply_lang({"reply_lang": "Klingon"}) == "English" and board_module.LANGS[0] == "English"
    assert board_module.in_lang("anything at all", "Spanish") and board_module.in_lang("\uc548\ub155", "Korean") and not board_module.in_lang("hello", "Korean")
    assert board_module.in_lang("\u3053\u3093\u306b\u3061\u306f", "Japanese") and not board_module.in_lang("ok", "Russian")


def tool_line(*blocks, ts="2027-01-15T08:00:00Z"):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": "a1", "content": list(blocks)}}) + "\n"


def tool_use(uid, name, **given):
    return {"type": "tool_use", "id": uid, "name": name, "input": given}


def test_tool_use_counts_the_users_servers_and_skills_by_member_once_a_call_and_reads_on(crew):
    logs, use = crew.dir / "logs", live.ToolUse(crew)
    (logs / "alpha.ndjson").write_text(tool_line(tool_use("1", "mcp__files__read", path="x"), tool_use("2", "mcp__deepcrew__message", to="leader"))
                                       + tool_line(tool_use("1", "mcp__files__read")) + tool_line(tool_use("3", "Skill", skill="greet")))
    got = use(["alpha", "beta"])
    assert got["mcp"] == {"files": {"calls": 1, "by": {"alpha": 1}, "last": datetime(2027, 1, 15, 8, tzinfo=UTC).timestamp(), "tools": {"read": 1}}}
    assert list(got["skills"]) == ["greet"]
    got["mcp"]["files"]["calls"] = 99
    with open(logs / "alpha.ndjson", "a") as f:
        f.write(tool_line(tool_use("4", "mcp__files__write")) + '{"type": "assistant", "mess')
    (logs / "beta.ndjson").write_text(tool_line(tool_use("5", "Skill", command="/greet now")))
    got = use(["alpha", "beta"])
    assert got["mcp"]["files"]["calls"] == 2 and got["mcp"]["files"]["tools"] == {"read": 1, "write": 1}
    assert got["skills"]["greet"]["by"] == {"alpha": 1, "beta": 1}


def test_a_member_using_a_tool_is_an_event_from_it_to_the_tools_and_the_snapshot_names_what_is_given(crew, project):
    from deepcrew import toolkit
    obs = live.Observer(crew)
    prev = obs.observe()
    (crew.dir / "logs" / "alpha.ndjson").write_text(tool_line(tool_use("1", "mcp__files__read"), tool_use("2", "mcp__files__read"), tool_use("3", "Skill", skill="greet")))
    cur = obs.observe()
    evs = [e for e in live.events(prev, cur) if e["kind"] == "tool"]
    assert [(e["from"], e["to"], e["tool_kind"], e["name"], e["calls"], e["label"]) for e in evs] == [
        ("m:alpha", "tools", "mcp", "files", 2, "alpha used MCP server files: read"), ("m:alpha", "tools", "skills", "greet", 1, "alpha used skill greet")]
    assert [e for e in live.events(cur, obs.observe()) if e["kind"] == "tool"] == []
    (project / ".claude" / "skills" / "greet").mkdir(parents=True)
    (project / ".claude" / "skills" / "greet" / "SKILL.md").write_text("---\nname: greet\ndescription: Greets.\n---\nHello.\n")
    toolkit.add_skill(crew.root, "greet")
    snap = live.snapshot(crew, obs=obs.observe())
    assert snap["tools"]["given"] == {"mcp": [], "skills": ["greet"]} and snap["tools"]["mcp"]["files"]["calls"] == 2


def test_a_question_waiting_for_the_user_shows_its_deadline_and_the_leader_gets_it_after(crew, monkeypatch):
    from deepcrew import inbox
    from deepcrew.board import PAGE_SEEN
    hub = live.Hub(crew.dir)
    hub._attach()
    hub.subscribe()
    hub._seen()
    assert (crew.dir / "run" / PAGE_SEEN).exists() and crew.page_watched()
    obs = live.Observer(crew)
    prev = obs.observe()
    with crew.locked():
        q = crew.request("questions", "alpha", {"question": "Keep the old parser?", "answer": None, "options": ["Keep", "Drop"], "gist": "\u4fdd\u7559\u65e7\u89e3\u6790\u5668\u5417\uff1f",
                                                "user_until": crew.user_window()})
    pending = live.snapshot(crew, obs=obs.observe())["pending"]["questions"]
    assert [(x["id"], x["gist"], x["user_until"]) for x in pending] == [(q["id"], "\u4fdd\u7559\u65e7\u89e3\u6790\u5668\u5417\uff1f", q["user_until"])]
    ev = next(e for e in live.events(prev, obs.observe()) if e["kind"] == "question")
    assert (ev["user"], ev["options"], ev["to"]) == (True, ["Keep", "Drop"], "leader")
    assert f"question:{q['id']}" not in inbox.inbox_items(crew)
    monkeypatch.setattr(time, "time", lambda: q["user_until"] + 1)
    assert f"question:{q['id']}" in inbox.inbox_items(crew)
