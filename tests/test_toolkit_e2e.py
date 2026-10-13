from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from deepcrew import cli, member, policy, toolkit
from deepcrew.board import Board
from deepcrew.standin import StandIn

CLAUDE = os.environ.get("DEEPCREW_CLAUDE") or shutil.which("claude")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(CLAUDE is None, reason="needs the claude CLI")]
KEY = "sk-e2e-" + "t" * 24

TINY = '''
import json, os, sys

with open(os.environ["TINY_OUT"], "w") as f:
    json.dump(dict(os.environ), f)
TOOLS = [{"name": "echo", "description": "Echo the text back.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
         {"name": "mark", "description": "The mark in this server's definition.", "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    m = json.loads(line)
    i, method = m.get("id"), m.get("method")
    if i is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "tiny", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = m["params"]
        said = "echo: " + p["arguments"].get("text", "") if p["name"] == "echo" else "mark: " + os.environ.get("TINY_MARK", "")
        result = {"content": [{"type": "text", "text": said}], "isError": False}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": i, "result": result}) + "\\n")
    sys.stdout.flush()
'''
GREET = ("---\nname: greet\ndescription: Greets people by name. Use it when asked to say hello.\nallowed-tools: mcp__tiny__echo\nhooks:\n  PreToolUse:\n"
         "    - matcher: \"mcp__tiny__echo\"\n      hooks:\n        - type: command\n          command: \"touch {hook_mark}\"\n---\n"
         "Say hello to $ARGUMENTS. Run ${{CLAUDE_SKILL_DIR}}/scripts/hi.sh first.\n")


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
    marks = {"hook": tmp_path / "skill-hook-ran", "env": tmp_path / "tiny-env.json"}
    files = {"src/a/a.py": "A = 1\n", "src/b/b.py": "B = 1\n", "tools/tiny_mcp.py": TINY,
             ".mcp.json": json.dumps({"mcpServers": {"tiny": {"command": sys.executable, "args": ["tools/tiny_mcp.py"],
                                                              "env": {"TINY_OUT": str(marks["env"]), "TINY_MARK": "from the definition"}}}}),
             ".claude/skills/greet/SKILL.md": GREET.format(hook_mark=marks["hook"]), ".claude/skills/greet/scripts/hi.sh": "echo hi\n"}
    for rel, text in files.items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text(text)
    commit_all(proj)
    (tmp_path / "key").write_text(KEY)
    with StandIn() as si:
        p = {"name": "standin", "base_url": si.base_url, "models": ["standin-model"], "efforts": ["max"], "fast_model": "standin-fast",
             "auto_compact_window": 100000, "timeout_ms": 60000, "key_sources": [f"file:{tmp_path / 'key'}"], "key_pattern": ".+"}
        board = cli.create_team(proj, "tk", p, {"model": "standin-model", "effort": "max"}, claude=CLAUDE, team_dir=tmp_path / "team",
                                hold_s=2, max_turns=60, approval_timeout_s=60, question_timeout_s=30)
        try:
            yield board, si, proj.resolve(), marks
        finally:
            b = Board(board.dir)
            for m in b.members():
                member.stop(b, m["name"])


def names(request: dict) -> list[str]:
    return [t["name"] for t in request["tools"]]


def test_the_skill_and_the_server_reach_every_member_and_its_calls_ask_the_leader(world):
    board, si, proj, marks = world
    toolkit.add_mcp(proj, "tiny")
    assert toolkit.add_skill(proj, "greet")["dropped"] == ["allowed-tools", "hooks"]
    t1 = board.post("a1", "uses the server and the skill", ["src/a/"], [], by="leader")
    t2 = board.post("b1", "changes no tool", ["src/b/"], [], by="leader")
    si.actions[t1["id"]] = [("Skill", {"skill": "greet", "args": "world"}), ("mcp__tiny__echo", {"text": "hello tiny"}), ("mcp__tiny__mark", {})]
    asked, done = [], threading.Event()

    def leader() -> None:
        while not done.is_set():
            for r in board.pending("perms"):
                asked.append((r["member"], r["tool"], r["input"]))
                policy.answer_permission(board, r["id"], allow=True)
            time.sleep(0.2)

    threading.Thread(target=leader, daemon=True).start()
    try:
        member.start(board, "alpha", ["src/a/"], t1["id"], "owns src/a/")
        wait_until(lambda: Board(board.dir).task(t1["id"])["state"] == "done", 120, "alpha's task done")
        member.start(board, "beta", ["src/b/"], t2["id"], "owns src/b/")
        wait_until(lambda: Board(board.dir).task(t2["id"])["state"] == "done", 120, "beta's task done")
    finally:
        done.set()
    a, b = [r["body"] for r in si.member_requests("alpha")], [r["body"] for r in si.member_requests("beta")]

    assert {"Skill", "Bash", "TaskUpdate", "mcp__deepcrew__message", "mcp__tiny__echo", "mcp__tiny__mark"} <= set(names(a[0]))
    assert json.dumps(a[0]["tools"]) == json.dumps(b[0]["tools"])
    assert share(a[0], b[0]) > 0.65, "members still share one prompt prefix with the user's tools and skills in it"

    def listing(body: dict) -> str:
        texts = [c.get("text", "") for m in body["messages"] for c in (m["content"] if isinstance(m["content"], list) else [{"text": m["content"]}])
                 if isinstance(c, dict)]
        return next(t for t in texts if "Greets people by name" in t)
    assert "- greet: Greets people by name. Use it when asked to say hello." in listing(a[0]) and listing(a[0]) == listing(b[0])
    after = listing(a[0]).split("The following skills are available for use with the Skill tool:", 1)[1].lstrip("\n").splitlines()
    listed = [line.split(":", 1)[0][2:] for line in itertools.takewhile(lambda line: line.startswith("- "), after)]
    assert listed == ["greet"], f"members are listed the user's skill and none of Claude Code's own (2.1.295 adds some): {listed}"

    here = board.shared_dir / "skills" / "greet"
    assert (here / "scripts" / "hi.sh").read_text() == "echo hi\n"
    result = json.dumps(a)
    assert f"This skill's files are in {here}: read and run them from there." in result and f"Run {here}/scripts/hi.sh first." in result
    assert f"Base directory for this skill: {board.dir / 'claude' / 'skills' / 'greet'}" in result
    assert str(board.dir / "claude" / "skills" / "greet" / "scripts") not in result and not marks["hook"].exists()

    assert [(who, tool) for who, tool, _ in asked if tool.startswith("mcp__tiny__")] == [("alpha", "mcp__tiny__echo"), ("alpha", "mcp__tiny__mark")]
    assert "echo: hello tiny" in result and "mark: from the definition" in result
    env = json.loads(marks["env"].read_text())
    assert env["TINY_MARK"] == "from the definition" and not any(KEY in v for v in env.values()) and not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"} & set(env)

    (proj / "tools" / "tiny_mcp.py").write_text(TINY + "\n# changed by a member\n")
    t3 = board.post("c1", "starts after the change", ["src/c/"], [], by="leader")
    member.start(board, "gamma", ["src/c/"], t3["id"], "owns src/c/")
    wait_until(lambda: Board(board.dir).task(t3["id"])["state"] == "done", 120, "gamma's task done")
    first = si.member_requests("gamma")[0]["body"]
    assert "Skill" in names(first) and not [n for n in names(first) if n.startswith("mcp__tiny__")] and "mcp__deepcrew__message" in names(first)
    assert "MCP server tiny is left out for the members: tools/tiny_mcp.py changed since the server was added" in (board.dir / "logs" / "gamma.driver.log").read_text()
    assert [r["name"] for r in toolkit.mcp_list(proj) if not r["ok"]] == ["tiny"]
