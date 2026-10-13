from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from deepcrew import cli, member, policy, providers, route
from deepcrew.board import Board
from deepcrew.standin import StandIn

CLAUDE = os.environ.get("DEEPCREW_CLAUDE") or shutil.which("claude")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(CLAUDE is None, reason="needs the claude CLI")]
KEY = "sk-e2e-" + "k" * 24


def _norm(o):
    if isinstance(o, dict):
        out = {k: _norm(v) for k, v in o.items() if k != "cache_control"}
        if "role" in out and isinstance(out.get("content"), str):
            out["content"] = [{"type": "text", "text": out["content"]}]
        return out
    return [_norm(v) for v in o] if isinstance(o, list) else o


def share(a: dict, b: dict) -> float:
    x, y = ("\x00".join(json.dumps(_norm(r.get(k)), ensure_ascii=False) for k in ("tools", "system", "messages")) for r in (a, b))
    n = 0
    while n < min(len(x), len(y)) and x[n] == y[n]:
        n += 1
    return n / len(x)


def commit_all(proj) -> None:
    for args in (("init", "-q"), ("add", "-A"), ("-c", "user.name=e2e", "-c", "user.email=e2e@localhost", "-c", "commit.gpgsign=false", "commit", "-qm", "base")):
        subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True)


def wait_until(pred, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def world(tmp_path):
    proj = tmp_path / "proj"
    project_settings = {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": f"echo ran >> {tmp_path / 'hook.log'}"}]}]},
                        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}, "permissions": {"allow": ["Bash(touch:*)"]}}
    for rel, text in {"CLAUDE.md": "# Notes\nShared project notes.\n", "src/a/a.py": "A = 1\n", "src/b/b.py": "B = 1\n",
                      ".claude/settings.json": json.dumps(project_settings)}.items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text(text)
    commit_all(proj)
    (tmp_path / "key").write_text(KEY)
    with StandIn() as si:
        p = {"name": "standin", "base_url": si.base_url, "models": ["standin-model"], "efforts": ["max"], "fast_model": "standin-fast",
             "auto_compact_window": 100000, "timeout_ms": 60000, "key_sources": [f"file:{tmp_path / 'key'}"], "key_pattern": ".+"}
        board = cli.create_team(proj, "e2e", p, {"model": "standin-model", "effort": "max"}, claude=CLAUDE, team_dir=tmp_path / "team",
                                hold_s=2, max_turns=60, approval_timeout_s=30, question_timeout_s=30, bash_allow=["echo"])
        try:
            yield board, si, proj.resolve(), tmp_path
        finally:
            b = Board(board.dir)
            for m in b.members():
                member.stop(b, m["name"])


def test_team_end_to_end(world):
    board, si, proj, tmp = world
    t1 = board.post("a1", "write x.py", ["src/a/"], [], by="leader")
    t2 = board.post("b1", "write z.py", ["src/b/"], [], by="leader")
    t3 = board.post("a2", "after a1", ["src/a/"], [t1["id"]], by="leader")
    t4 = board.post("review", "changes no files", [], [], by="leader")
    t5 = board.post("b-declined", "b passes on this one", ["src/b/"], [], by="leader")
    t6 = board.post("b-undecided", "b neither claims nor declines this one", ["src/b/"], [], by="leader")
    si.passes.add(t5["id"])
    si.ignores.add(t6["id"])
    si.review = "ask"
    si.actions.update({
        t1["id"]: [("Write", {"file_path": str(proj / "src/a/x.py"), "content": "X = 1\n"}),
                   ("Write", {"file_path": str(proj / "src/b/y.py"), "content": "Y = 1\n"}),
                   ("mcp__deepcrew__message", {"to": "leader", "text": "dispute: a1 needs src/b/y.py"})],
        t2["id"]: [("Write", {"file_path": str(proj / "src/b/z.py"), "content": "Z = 1\n"}),
                   ("Bash", {"command": "touch src/b/made.txt", "description": "make a file"}),
                   ("Bash", {"command": "echo hi", "description": "say hi"})],
        t3["id"]: [("Write", {"file_path": str(proj / "src/a/w.py"), "content": "W = 1\n"}),
                   ("Agent", {"description": "probe", "prompt": "SUBAGENT-PROBE: write outside the region"})],
        t4["id"]: [("Write", {"file_path": str(board.notes_dir / "{me}-api-contract.md"), "content": "parse() returns a list of tokens\n"}),
                   ("Grep", {"pattern": "tokens", "path": str(board.notes_dir)}),
                   ("mcp__deepcrew__message", {"to": "leader", "text": "Ship the parser now?", "wait": True}),
                   ("Read", {"file_path": str(tmp / "key")}),
                   ("Bash", {"command": f"cat {tmp / 'key'}", "description": "read the key"})],
    })
    si.prompt_actions["SUBAGENT-PROBE"] = [("Write", {"file_path": str(proj / "src/b/sub.py"), "content": "S = 1\n"})]
    allowed, answered, done = [], [], threading.Event()

    def leader() -> None:
        while not done.is_set():
            for r in board.pending("perms"):
                allowed.append(policy.answer_permission(board, r["id"], allow=True))
            for q in board.pending("questions"):
                answered.append(board.answer("questions", q["id"], "answered", answer="yes, ship it"))
            time.sleep(0.2)

    threading.Thread(target=leader, daemon=True).start()
    try:
        route.run(board)
        assert not Board(board.dir).members()
        member.start(board, "alpha", ["src/a/"], t1["id"], "owns src/a/")
        member.start(board, "beta", ["src/b/"], t2["id"], "owns src/b/")
        wait_until(lambda: all(Board(board.dir).task(t["id"])["state"] == "done" for t in (t1, t2, t3, t4)), 120, "the four tasks done")
        wait_until(lambda: all(m["state"] == "idle" for m in Board(board.dir).members()), 60, "members idle")
        b = Board(board.dir)
        assert b.task(t1["id"])["owner"] == b.task(t3["id"])["owner"] == "alpha" and b.task(t2["id"])["owner"] == "beta"
        assert b.task(t5["id"])["state"] == "open" and b.task(t5["id"])["passed_by"] == ["beta"]
        wait_until(lambda: Board(board.dir).task(t6["id"])["ignored_by"] == ["beta"], 30, "beta's undecided offer handed to the leader")
        blocked = {x["task"]: x["reason"] for x in route.plan(Board(board.dir))["blocked"]}
        assert "beta declined it: the stand-in declines it" in blocked[t5["id"]] and "beta left it undecided" in blocked[t6["id"]]

        a_bodies = json.dumps([r["body"] for r in si.member_requests("alpha")])
        assert "owns src/a" in a_bodies
        assert f"{t1['id']} done by the stand-in" in a_bodies
        assert "outside your region" in a_bodies and not (proj / "src/b/y.py").exists()
        assert (proj / "src/a/x.py").exists() and (proj / "src/a/w.py").exists() and (proj / "src/b/z.py").exists()
        assert not (proj / "src/b/sub.py").exists()

        assert [r["input"]["command"] for r in allowed] == ["touch src/b/made.txt"] and (proj / "src/b/made.txt").exists()
        assert [q["question"] for q in answered] == ["Ship the parser now?"] and "yes, ship it" in json.dumps(si.requests)
        assert [n.name for n in b.notes_dir.glob("*.md")] == [f"{b.task(t4['id'])['owner']}-api-contract.md"]
        assert "-api-contract.md" in json.dumps(si.requests) and not (proj / ".deepcrew").exists()
        assert [(m["from"], m["kind"], m["text"]) for m in b.take_mail("leader")] == [("alpha", "message", "dispute: a1 needs src/b/y.py")]
        assert (tmp / "hook.log").exists()

        files = {f["path"]: f for f in cli.audit(b)["files"]}
        assert files["src/a/x.py"]["edited_by"] == ["alpha"] and files["src/b/made.txt"]["flags"] == ["shell_edit"]
        assert files["src/b/made.txt"]["shell"] == {"member": "beta", "command": "touch src/b/made.txt"}

        before, n_before = si.member_requests("beta")[-1]["body"], len(si.member_requests("beta"))
        b.send("leader", "beta", "notice", "the parser API is frozen")
        assert route.run(b)["woken"] == ["beta"]
        wait_until(lambda: Board(board.dir).member("beta")["state"] == "idle" and len(si.member_requests("beta")) > n_before, 60, "beta woken and idle again")
        assert Board(board.dir).member("beta")["runs"] == 2 and "the parser API is frozen" in json.dumps(si.member_requests("beta")[n_before]["body"])
    finally:
        done.set()

    reqs = [r for r in si.requests if r["body"].get("tools")]
    assert reqs and all(r["authorization"] == f"Bearer {KEY}" for r in reqs)
    assert not any("x-anthropic-billing-header" in json.dumps(r["body"]) for r in reqs)
    assert not any(KEY in json.dumps(r["body"]) for r in si.requests)
    assert "names a location that holds team state or keys" in json.dumps([r["body"] for r in si.requests])

    a_reqs, b_reqs = [r["body"] for r in si.member_requests("alpha")], [r["body"] for r in si.member_requests("beta")]
    prefix_share = {"consecutive": min(share(x, y) for x, y in zip(a_reqs, a_reqs[1:], strict=False)), "members": share(a_reqs[0], b_reqs[0]),
                    "resume": share(before, b_reqs[n_before])}
    assert prefix_share["consecutive"] > 0.999, f"a member's next request starts with (99.9% of) its last one, for the provider's prompt cache: {prefix_share}"
    assert prefix_share["resume"] > 0.999, f"the driver feeds ordinary user messages, which a resumed session replays unchanged: {prefix_share}"
    assert prefix_share["members"] > 0.65, f"members share one prompt prefix: {prefix_share}"

    for f in [*board.dir.rglob("*"), *proj.rglob("*")]:
        assert not f.is_file() or KEY.encode() not in f.read_bytes(), f
    assert all(KEY not in json.dumps(member.env(board, m)) for m in Board(board.dir).members())

    member.stop(board, "beta")
    assert board.forget("beta")["region"] == ["src/b/"]
    assert [m["name"] for m in Board(board.dir).members()] == ["alpha"]


def test_the_reviewer_lets_a_routine_command_run_and_sends_another_to_the_leader_with_its_words(world):
    board, si, proj, tmp = world
    t = board.post("check", "run the check", ["src/a/"], [], by="leader")
    si.actions[t["id"]] = [("Bash", {"command": "python3 -c \"open('src/a/made.txt', 'w').write('x')\"", "description": "make a file"}),
                           ("Bash", {"command": "kill -0 1 || true", "description": "probe a process"})]
    asked, done = [], threading.Event()

    def leader() -> None:
        while not done.is_set():
            for r in board.pending("perms"):
                asked.append(r)
                policy.answer_permission(board, r["id"], allow=True)
            time.sleep(0.2)

    threading.Thread(target=leader, daemon=True).start()
    try:
        member.start(board, "worker", ["src/a/"], t["id"])
        wait_until(lambda: Board(board.dir).task(t["id"])["state"] == "done", 120, "the task done")
    finally:
        done.set()
    records = {r["input"]["command"].split()[0]: r for r in (json.loads(p.read_text()) for p in (board.dir / "perms").glob("P*.json"))}
    assert (proj / "src" / "a" / "made.txt").read_text() == "x"
    assert (records["python3"]["by"], records["python3"]["review"]) == ("reviewer", "stand-in review")
    assert [r["input"]["command"] for r in asked] == ["kill -0 1 || true"] and records["kill"]["review"] == "runs kill"
    reviews = [r for r in si.requests if str(r["body"].get("system", "")).startswith("# Command review")]
    assert len(reviews) == 1 and reviews[0]["body"]["model"] == "standin-fast" and reviews[0]["authorization"] == f"Bearer {KEY}"


def test_an_api_error_stops_the_member_and_tells_the_leader(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Broke(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            raw = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "Insufficient Balance"}}).encode()
            self.send_response(402)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Broke)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.py").write_text("A = 1\n")
    commit_all(proj)
    (tmp_path / "key").write_text(KEY)
    p = {"name": "broke", "base_url": f"http://127.0.0.1:{srv.server_address[1]}", "models": ["m"], "efforts": [], "fast_model": "m",
         "auto_compact_window": 100000, "timeout_ms": 20000, "key_sources": [f"file:{tmp_path / 'key'}"], "key_pattern": ".+"}
    board = cli.create_team(proj, "broke", p, {"model": "m", "effort": None}, claude=CLAUDE, team_dir=tmp_path / "team", hold_s=1)
    t = board.post("t", "x", ["src/"], [], by="leader")
    try:
        member.start(board, "worker", ["src/"], t["id"])
        wait_until(lambda: Board(board.dir).member("worker")["state"] == "failed", 120, "the member marked failed")
        b = Board(board.dir)
        assert "402" in b.member("worker")["failure"] and "402" in json.dumps(b.take_mail("leader"))
        assert b.task(t["id"])["state"] == "open"
        assert route.plan(b)["wake"] == []
    finally:
        member.stop(Board(board.dir), "worker")
        srv.shutdown()


BACKUP_KEY = "sk-e2e-backup-" + "b" * 20


def provider_entry(name, si, key_file, models) -> str:
    return (f'[{name}]\nbase_url = "{si.base_url}"\nmodels = {json.dumps(models)}\nefforts = []\nfast_model = "{models[0]}"\n'
            f'auto_compact_window = 100000\ntimeout_ms = 20000\nkey_sources = ["file:{key_file}"]\nkey_pattern = ".+"\n')


def fallback_team(tmp_path, broke, backup, errors=None):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.py").write_text("A = 1\n")
    commit_all(proj)
    (tmp_path / "key").write_text(KEY)
    (tmp_path / "backup.key").write_text(BACKUP_KEY)
    providers.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (providers.CONFIG_DIR / "providers.toml").write_text(provider_entry("nokey", backup, tmp_path / "missing.key", ["nokey-model"])
                                                         + provider_entry("backup", backup, tmp_path / "backup.key", ["backup-small", "backup-big[1m]"]))
    providers.env_file_set({"DEEPCREW_FALLBACK": "nokey,backup"})
    p = {"name": "broke", "base_url": broke.base_url, "models": ["broke-small", "broke-big"], "efforts": [], "fast_model": "broke-small",
         "auto_compact_window": 100000, "timeout_ms": 20000, "key_sources": [f"file:{tmp_path / 'key'}"], "key_pattern": ".+", **({"errors": errors} if errors else {})}
    board = cli.create_team(proj, "fb", p, {"model": "broke-small", "effort": None}, claude=CLAUDE, team_dir=tmp_path / "team", hold_s=2)
    t = board.post("t", "write b.py", ["src/"], [], by="leader")
    backup.actions[t["id"]] = [("Write", {"file_path": str(proj / "src" / "b.py"), "content": "B = 1\n"})]
    return board, proj, t


def models_and_keys(si) -> set[tuple[str, str]]:
    return {(r["body"].get("model"), r["authorization"]) for r in si.requests if r["body"].get("tools")}


def test_a_provider_without_balance_moves_the_team_to_the_next_backup_that_has_a_key(tmp_path):
    from deepcrew import settings
    with StandIn() as broke, StandIn() as backup:
        broke.error = (402, {"type": "error", "error": {"type": "invalid_request_error", "message": "Insufficient Balance"}}, {})
        board, proj, t = fallback_team(tmp_path, broke, backup)
        try:
            member.start(board, "worker", ["src/"], t["id"])
            wait_until(lambda: Board(board.dir).task(t["id"])["state"] == "done", 120, "the task done on the backup")
            wait_until(lambda: Board(board.dir).member("worker")["state"] == "idle", 60, "the member idle")
            b = Board(board.dir)
            moved = settings.fallback_now(b.cfg)
            assert b.cfg["provider"]["name"] == "backup" and b.cfg["member"]["model"] == "backup-small" and (proj / "src" / "b.py").read_text() == "B = 1\n"
            assert (moved["from"], moved["to"], moved["kind"], moved["status"], moved["member"]) == ("broke", "backup", "balance", 402, "worker")
            assert moved["skipped"][0].startswith("nokey has no key") and b.member("worker")["failure"] is None
            assert models_and_keys(broke) == {("broke-small", f"Bearer {KEY}")} and models_and_keys(backup) == {("backup-small", f"Bearer {BACKUP_KEY}")}
            assert not any("stopped" in m["text"] for m in b.take_mail("leader"))
            (line,) = [cli._item_line(kind, x) for kind, x in cli.inbox_items(b).values() if kind == "provider"]
            assert line.startswith("provider: broke had no balance (API error 402 for worker: API Error: 402 Insufficient Balance), so the team "
                                   "switched to backup;") and f"{providers.NAME} config set provider broke moves the team back" in line
            assert any(x.startswith("provider  backup since ") and "broke had no balance" in x for x in cli.summary(b, None))
            assert len(b.member("worker")["session"]) and member.transcript(b, b.member("worker")) is not None
        finally:
            member.stop(Board(board.dir), "worker")


def test_a_balance_error_a_provider_answers_with_429_is_read_by_its_own_rules(tmp_path):
    from deepcrew import settings
    body = {"error": {"code": "1113", "message": "Insufficient balance or no resource package. Please recharge."}}
    with StandIn() as broke, StandIn() as backup:
        broke.error = (429, body, {"x-should-retry": "false"})
        board, proj, t = fallback_team(tmp_path, broke, backup, errors={"balance": ["429:insufficient balance"]})
        try:
            member.start(board, "worker", ["src/"], t["id"])
            wait_until(lambda: Board(board.dir).task(t["id"])["state"] == "done", 120, "the task done on the backup")
            b = Board(board.dir)
            moved = settings.fallback_now(b.cfg)
            assert (moved["to"], moved["kind"], moved["status"]) == ("backup", "balance", 429) and "Insufficient balance" in moved["said"]
            (line,) = [cli._item_line(kind, x) for kind, x in cli.inbox_items(b).values() if kind == "provider"]
            assert line.startswith("provider: broke had no balance (API error 429 for worker: API Error: Request rejected (429)")
        finally:
            member.stop(Board(board.dir), "worker")


def test_the_projects_imports_reach_the_model_and_nothing_outside_the_project_does(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / "outside.md").write_text("OUTSIDE-TEXT\n")
    (tmp_path / "key").write_text(KEY)
    proj = tmp_path / "proj"
    for rel, text in {"CLAUDE.md": "# Notes\nROOT-TEXT.\nRules: @docs/rules.md\nElsewhere: @~/outside.md and @../proj-outside.md\n", "src/a.py": "A = 1\n",
                      "docs/rules.md": "RULES-TEXT. More: @./more.md\n", "docs/more.md": "MORE-TEXT.\n"}.items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text(text)
    (tmp_path / "proj-outside.md").write_text("SIBLING-TEXT\n")
    commit_all(proj)
    with StandIn() as si:
        p = {"name": "standin", "base_url": si.base_url, "models": ["standin-model"], "efforts": ["max"], "fast_model": "standin-fast",
             "auto_compact_window": 100000, "timeout_ms": 60000, "key_sources": [f"file:{tmp_path / 'key'}"], "key_pattern": ".+"}
        board = cli.create_team(proj, "imp", p, {"model": "standin-model", "effort": "max"}, claude=CLAUDE, team_dir=tmp_path / "team", hold_s=0,
                                max_turns=20, approval_timeout_s=30)
        t = board.post("note", "does nothing", ["src/"], [], by="leader")
        try:
            member.start(board, "alpha", ["src/"], t["id"], "reads the notes")
            wait_until(lambda: Board(board.dir).task(t["id"])["state"] == "done", 90, "the task done")
        finally:
            member.stop(Board(board.dir), "alpha")
        sent = json.dumps([r["body"] for r in si.member_requests("alpha")])
    assert "ROOT-TEXT" in sent and "RULES-TEXT" in sent and "MORE-TEXT" in sent
    assert "OUTSIDE-TEXT" not in sent and "SIBLING-TEXT" not in sent
    assert "`@docs/rules.md`" in sent and "`@./more.md`" in sent and "`@~/outside.md`" in sent and "`@../proj-outside.md`" in sent
    assert "# The project's docs/more.md (imported by docs/rules.md)" in sent


def test_check_deep_proves_the_projects_imports_and_stops_the_outside_ones():
    r = cli.conformance(CLAUDE)
    assert r["level"] == "ok", r
    assert "the project's CLAUDE.md with its import" in r["detail"]


def test_check_deep_runs_its_member_on_the_prompt_pack_new_teams_get():
    pack = providers.CONFIG_DIR / "packs" / "trial"
    pack.mkdir(parents=True)
    (pack / "member_system.md").write_text("# Trial protocol\n\nTeam $team: notes in $notes, other files in $artifacts, tools in $tools.\n", encoding="utf-8")
    providers.env_file_set({"DEEPCREW_PROMPT_PACK": "trial"})
    r = cli.conformance(CLAUDE)
    assert r["level"] == "ok" and "the team protocol of prompt pack trial" in r["detail"], r


MARKDOWN_CASES = [
    "{T}", "see {T} now", "- {T}", "1. {T}", "> {T}", "# Title {T}", "Title\n=====\n{T}", "---\n{T}", "text\n\n{T}", "x\t{T}", "x\u00a0{T}", "x\r\n{T}",
    "```\n{T}\n```\n{T}", "~~~\n{T}\n~~~", "````\n```\n{T}\n```\n````\n{T}", "   ```\n   {T}\n   ```", "    ```\n{T}\n    ```", "```\n{T}",
    "text\n```\n{T}\n```\n{T}", "`{T} x`", "` {T}`", "``{T} x``", "use `x` and `{T} x`", "`x` {T}", "`x`{T}", "text `a\n{T} b` c", "`a\n\n{T}\n\nb`",
    "- `a\n- {T}\n- b`", "a ` b {T}", "`{T}", "\\`a {T} b`", "<!-- {T} -->", "<!--\n{T}\n-->\n{T}", "text <!-- {T} --> more", "<!-- c --> {T}",
    "<!-- c -->{T}", "<!-- open\n{T}", "text <!-- open\n{T}", "`<!--` {T} `-->`", "    {T}", "para\n\n    {T}\n{T}", "para\n\n\t{T}", "  {T}",
    "- run `npm i {T} x`", "1. `{T} x`", "- a\n  - `{T} x`", "> - `{T} x`", "> `{T} x`", "run `npm i {T} x` now", "# `{T} x`",
    "- a\n  ```\n  {T}\n  ```", "> ```\n> {T}\n> ```", "1. a\n    ```\n    {T}\n    ```", "- a\n```\n{T}\n```\n- b {T}",
    "<div>\n{T}\n</div>", "<div>\n\n{T}\n\n</div>", "<span>{T}</span>", "<details>\n<summary>S</summary>\n\n{T}\n\n</details>",
    "<script>\n{T}\n</script>\n{T}", "<pre>\n{T}\n\n{T}\n</pre>", "<b>{T}</b>", "text<b>{T}", "<http://a.b>{T}", "[{T}](x)", "[x]({T})", "[x]( {T} )",
    "![{T}](x)", "[x](y) {T}", "[a](b`c) {T} `d`", "[ref]: u\n[ref] {T}", "[a `b]: u\nsee [a `b] and `c {T} d`",
    "**{T}**", "*{T}*", "_{T}_", "__{T}__", "***{T}***", "text **{T}**", "a**{T}**", "a_{T}_", "**x {T}**", "**{T}*", "*{T}**", "**bold** {T}", "**`x`{T}**",
    "[**{T}**](u)", "**[{T}](u)**", "_x `` {T} y_ z``", "**x `` {T} y** z``", "_x ` {T} y_ z`", "_x ``` {T} y_ z```", "_x `` {T} `` y_", "`` _x {T} `` y_",
    "- _x `` {T} y_ z``", "> _x `` {T} y_ z``", "_x <!-- a > b_ {T} --> y_", "_x <a title='a>b_ {T}'> y_", "_x [a](b_ {T}) y_", "*x ``a`` {T} y*",
    "\\{T}", "\\*{T}", "({T})", "\"{T}", "x:{T}", "a{T}", "$ {T}", "a  \n{T}", "a\\\n{T}", "| {T} |", "- - -\n{T}", "* * *\n{T}", "- a\n\n  {T}\n\n- b",
    ">[x]: u\nlazy {T}", "> a\n> b `c\n> d` {T}", "> a\nlazy `x {T} y` z", "- a\n> q {T}\n- b", "text\n- a {T}\n- b\ntext {T}", "- a\n        deep {T}\n- b",
    "Title\n---\n{T}", "# {T} #", "#{T}", "####### {T}", "1.\n   {T}", "-\n  {T}", "10. {T}\n    more {T}", "-   a {T}\n    b {T}",
]


def fill(template: str, i: int) -> str:
    parts = template.split("{T}")
    return "".join(part + (f"@~/c-{i}-{k}.md" if k < len(parts) - 1 else "") for k, part in enumerate(parts))


def claude_reads(tmp_path, docs: list[str], name: str) -> set[tuple[int, int]]:
    cfg, home, proj = tmp_path / name / "cfg", tmp_path / name / "home", tmp_path / name / "proj"
    for folder in (cfg / "rules", home, proj):
        folder.mkdir(parents=True)
    for i, doc in enumerate(docs):
        (cfg / "rules" / f"case-{i}.md").write_text(doc)
    canaries = {(i, k) for i, doc in enumerate(MARKDOWN_CASES) for k in range(doc.count("{T}"))}
    for i, k in canaries:
        (home / f"c-{i}-{k}.md").write_text(f"CANARY-{i}-{k}\n")
    with StandIn() as si:
        env = {"PATH": os.environ["PATH"], "HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg), "ANTHROPIC_BASE_URL": si.base_url, "ANTHROPIC_API_KEY": KEY,
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
        subprocess.run([CLAUDE, "-p", "--setting-sources", "user", "hello"], cwd=proj, env=env, capture_output=True, check=False, stdin=subprocess.DEVNULL,
                       timeout=180)
        sent = json.dumps([r["body"] for r in si.requests])
    return {(i, k) for i, k in canaries if f"CANARY-{i}-{k}" in sent}
