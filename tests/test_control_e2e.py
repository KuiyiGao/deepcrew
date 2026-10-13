from __future__ import annotations

import http.server
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
KEY = "sk-e2e-" + "c" * 24
ASKS = policy.SANDBOX_NETWORK

ASKING = '''
import json, sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


TOOLS = [{"name": "ask", "description": "Ask the user which colour they want.", "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    m = json.loads(line)
    i, method = m.get("id"), m.get("method")
    if i is None or method is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "asking", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": "elicit-1", "method": "elicitation/create",
              "params": {"message": "Which colour?", "requestedSchema": {"type": "object", "properties": {"colour": {"type": "string"}}}}})
        for reply in sys.stdin:
            r = json.loads(reply)
            if r.get("id") == "elicit-1":
                break
        result = {"content": [{"type": "text", "text": "the client answered: " + json.dumps(r.get("result", r.get("error")), sort_keys=True)}], "isError": False}
    else:
        result = {}
    send({"jsonrpc": "2.0", "id": i, "result": result})
'''


def commit_all(proj) -> None:
    for args in (("init", "-q"), ("add", "-A"), ("-c", "user.name=e2e", "-c", "user.email=e2e@localhost", "-c", "commit.gpgsign=false", "commit", "-qm", "base")):
        subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True)


def wait_until(pred, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    if sys.platform not in cli.SANDBOX_TOOLS:
        pytest.skip("needs Claude Code's sandbox, which this host does not have")
    if shutil.which("curl") is None:
        pytest.skip("needs curl")
    monkeypatch.setenv("DEEPCREW_SANDBOX", "on")
    proj = tmp_path / "proj"
    for rel, text in {"src/a/a.py": "A = 1\n", "tools/asking_mcp.py": ASKING,
                      ".mcp.json": json.dumps({"mcpServers": {"asking": {"command": sys.executable, "args": ["tools/asking_mcp.py"]}}})}.items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text(text)
    commit_all(proj)
    (tmp_path / "key").write_text(KEY)
    with StandIn() as si:
        p = {"name": "standin", "base_url": si.base_url, "models": ["standin-model"], "efforts": ["max"], "fast_model": "standin-fast",
             "auto_compact_window": 100000, "timeout_ms": 60000, "key_sources": [f"file:{tmp_path / 'key'}"], "key_pattern": ".+"}
        board = cli.create_team(proj, "ctl", p, {"model": "standin-model", "effort": "max"}, claude=CLAUDE, team_dir=tmp_path / "team",
                                hold_s=2, max_turns=60, approval_timeout_s=60, question_timeout_s=30, bash_allow=["curl"])
        try:
            yield board, si, proj.resolve()
        finally:
            b = Board(board.dir)
            for m in b.members():
                member.stop(b, m["name"])


@pytest.fixture
def listener():
    paths: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            paths.append(self.path)
            body = b"hello from the test"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield type("Listener", (), {"port": server.server_address[1], "paths": paths})
    finally:
        server.shutdown()
        server.server_close()


def fetch(listener, host: str, path: str) -> tuple[str, dict]:
    return "Bash", {"command": f"curl -sS --max-time 25 --noproxy '' -w ' status=%{{http_code}}' http://{host}:{listener.port}/{path}", "description": f"fetch {path}"}


def results(si, name: str, tool: str) -> list[str]:
    messages = si.member_requests(name)[-1]["body"]["messages"]
    uses = {b["id"]: b["name"] for m in messages if m["role"] == "assistant" and isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_use"}
    out = []
    for m in messages:
        for b in (m["content"] if m["role"] == "user" and isinstance(m["content"], list) else []):
            if b.get("type") == "tool_result" and uses.get(b["tool_use_id"]) == tool:
                out.append(b["content"] if isinstance(b["content"], str) else " ".join(x.get("text", "") for x in b["content"]))
    return out


class Leader:
    def __init__(self, board, answer):
        self.board, self.answer, self.asked, self.stop = board, answer, [], threading.Event()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self) -> None:
        while not self.stop.is_set():
            for r in self.board.pending("perms"):
                self.asked.append((r["member"], r["tool"], r["input"], policy.widening(self.board, r)))
                how = self.answer(r)
                policy.answer_permission(self.board, r["id"], allow=how != "deny", always=how == "always")
            time.sleep(0.1)


def status(text: str) -> str:
    return text.rsplit("status=", 1)[1].split()[0]


def test_a_connection_the_sandbox_asks_about_reaches_the_leader_and_the_command_goes_on_when_it_is_answered(world, listener):
    board, si, proj = world
    t1 = board.post("a1", "fetches from a local server twice", ["src/a/"], [], by="leader")
    si.actions[t1["id"]] = [fetch(listener, "localhost", "first"), fetch(listener, "b.localhost", "second")]
    answers = iter(["allow", "deny"])
    leader = Leader(board, lambda r: next(answers))
    started = time.monotonic()
    try:
        member.start(board, "alpha", ["src/a/"], t1["id"], "owns src/a/")
        wait_until(lambda: Board(board.dir).task(t1["id"])["state"] == "done", 120, "alpha's task done")
    finally:
        leader.stop.set()
    first, second = results(si, "alpha", "Bash")
    assert first == "hello from the test status=200" and listener.paths == ["/first"]
    assert "blocked by network allowlist" in second and status(second) == "403"
    assert time.monotonic() - started < 25
    assert [x[:3] for x in leader.asked] == [("alpha", ASKS, {"host": "localhost"}), ("alpha", ASKS, {"host": "b.localhost"})]
    records = sorted((json.loads(p.read_text()) for p in (board.dir / "perms").glob("P*.json")), key=lambda r: r["ts"])
    assert [(r["tool"], r["state"], r["by"], r["always"], r["widened"]) for r in records] == [(ASKS, "allowed", "leader", False, None), (ASKS, "denied", "leader", False, None)]


def test_a_site_of_the_team_lets_a_running_members_command_connect_without_asking(world, listener):
    board, si, proj = world
    policy.set_site(board, "a.localhost")
    t1 = board.post("a1", "fetches from five hosts", ["src/a/"], [], by="leader")
    si.actions[t1["id"]] = [fetch(listener, "a.localhost", "a"), fetch(listener, "b.localhost", "b"), fetch(listener, "b.localhost", "b-again"),
                            fetch(listener, "c.localhost", "c"), fetch(listener, "d.localhost", "d")]

    def answer(r) -> str:
        if r["input"]["host"] == "c.localhost":
            policy.set_site(board, "d.localhost")
            return "deny"
        return "always"

    leader = Leader(board, answer)
    try:
        member.start(board, "alpha", ["src/a/"], t1["id"], "owns src/a/")
        wait_until(lambda: Board(board.dir).task(t1["id"])["state"] == "done", 120, "alpha's task done")
    finally:
        leader.stop.set()
    a, b, b_again, c, d = results(si, "alpha", "Bash")
    assert all(status(x) in ("200", "502") for x in (a, b, b_again, d)), (a, b, b_again, d)
    assert "blocked by network allowlist" in c and status(c) == "403"
    assert [(who, tool, i["host"]) for who, tool, i, _ in leader.asked] == [("alpha", ASKS, "b.localhost"), ("alpha", ASKS, "c.localhost")]
    assert [w and w["kind"] for *_, w in leader.asked] == ["site", "site"]
    assert sorted(board.sites()) == ["a.localhost", "b.localhost", "d.localhost"]


def test_an_mcp_server_that_asks_for_input_is_declined_at_once(world):
    board, si, proj = world
    toolkit.add_mcp(proj, "asking")
    assert policy.set_tool(board, "mcp__asking__ask", "leader") is None
    t1 = board.post("a1", "uses the asking server", ["src/a/"], [], by="leader")
    si.actions[t1["id"]] = [("mcp__asking__ask", {})]
    member.start(board, "alpha", ["src/a/"], t1["id"], "owns src/a/")
    wait_until(lambda: Board(board.dir).task(t1["id"])["state"] == "done", 60, "alpha's task done")
    assert results(si, "alpha", "mcp__asking__ask") == ['the client answered: {"action": "decline"}']
