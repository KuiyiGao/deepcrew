from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from deepcrew import cli, live, member, route
from deepcrew.board import Board, read_jsonl
from deepcrew.doctor import MEMBER_MB, host_memory_mb
from deepcrew.settings import MAX_MEMBERS
from deepcrew.standin import StandIn

CLAUDE = os.environ.get("DEEPCREW_CLAUDE") or shutil.which("claude")
KEY = "sk-scale-" + "k" * 24
N = min(int(os.environ.get("DEEPCREW_SCALE_N") or 0), MAX_MEMBERS)
RAM_MB = host_memory_mb() or 0
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(CLAUDE is None, reason="needs the claude CLI"),
              pytest.mark.skipif(N < 1, reason="starts DEEPCREW_SCALE_N real claude processes at once: set it to run, such as DEEPCREW_SCALE_N=4"),
              pytest.mark.skipif(N * MEMBER_MB > RAM_MB / 2, reason=f"{N} members take about {N * MEMBER_MB} MB, more than half of this "
                                                                    f"host's {RAM_MB} MB")]
LOOK_S, SNAPSHOT_BYTES, PLAN_S = 2.0, 600_000, 1.0


def commit_all(proj) -> None:
    for args in (("init", "-q"), ("add", "-A"), ("-c", "user.name=scale", "-c", "user.email=scale@localhost", "-c", "commit.gpgsign=false", "commit", "-qm", "base")):
        subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True)


def wait_until(pred, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def tool_errors(board, name: str) -> list[str]:
    out = []
    for line in (board.dir / "logs" / f"{name}.ndjson").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        content = (ev.get("message") or {}).get("content") if isinstance(ev, dict) else None
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                out.append(str(c.get("content"))[:200])
    return out


def test_a_full_crew_works_at_once_and_the_page_keeps_up(tmp_path):
    proj = tmp_path / "proj"
    for rel, text in {"CLAUDE.md": "# Notes\n", "src/README.md": "parts\n"}.items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text(text)
    commit_all(proj)
    (tmp_path / "key").write_text(KEY)
    with StandIn() as si:
        p = {"name": "standin", "base_url": si.base_url, "models": ["standin-model"], "efforts": ["max"], "fast_model": "standin-fast",
             "auto_compact_window": 100000, "timeout_ms": 60000, "key_sources": [f"file:{tmp_path / 'key'}"], "key_pattern": ".+"}
        board = cli.create_team(proj, "scale", p, {"model": "standin-model", "effort": "max"}, claude=CLAUDE, team_dir=tmp_path / "team",
                                hold_s=2, max_turns=30, max_active=N, approval_timeout_s=30, question_timeout_s=30)
        names = [f"m{i:02d}" for i in range(N)]
        try:
            tasks = [board.post(f"part {i}", f"write part {i}", [f"src/p{i}/"], [], by="leader") for i in range(N)]
            si.actions.update({t["id"]: [("Write", {"file_path": str(proj / f"src/p{i}/part.py"), "content": f"PART = {i}\n"}),
                                         ("mcp__deepcrew__message", {"to": "leader", "kind": "done", "gist": "My part works now.",
                                                                     "text": f"src/p{i}/part.py is in"})] for i, t in enumerate(tasks)})
            hub, looks, sizes = live.Hub(board.dir), [], []
            hub.tick()
            started = time.monotonic()
            for name, t, i in zip(names, tasks, range(N), strict=True):
                member.start(board, name, [f"src/p{i}/"], t["id"], f"owns part {i}")

            def done() -> bool:
                at = time.perf_counter()
                hub.tick()
                looks.append(time.perf_counter() - at)
                sizes.append(len(json.dumps(hub.current())))
                return all(Board(board.dir).task(t["id"])["state"] == "done" for t in tasks)
            wait_until(done, 600, f"the {N} tasks done")
            took = time.monotonic() - started
            wait_until(lambda: all(m["state"] == "idle" for m in Board(board.dir).members()), 120, "every member idle")
            at = time.perf_counter()
            route.plan(Board(board.dir))
            plan_s = time.perf_counter() - at
            b = Board(board.dir)
            assert all((proj / f"src/p{i}/part.py").read_text() == f"PART = {i}\n" for i in range(N))
            edits = [e for e in read_jsonl(b.dir / "edits.jsonl") if e.get("path")]
            assert sorted(e["member"] for e in edits) == sorted(names)
            assert not [f for f in cli.audit(b)["files"] if f["flags"]]
            hub.tick()
            reports = {m["from"] for m in hub.observer.observe()["mail"].values() if m.get("report") == "done"}
            snap = hub.current()
            assert len(snap["members"]) == N and snap["counts"]["done"] == N
            assert reports == set(names), {n: tool_errors(b, n) for n in sorted(set(names) - reports)}
            assert max(looks) < LOOK_S and max(sizes) < SNAPSHOT_BYTES and plan_s < PLAN_S, (max(looks), max(sizes), plan_s)
            print(f"\n{N} members: all done in {took:.1f} s; the page's look {sum(looks) / len(looks) * 1000:.0f} ms on average, "
                  f"{max(looks) * 1000:.0f} ms at most; snapshot {max(sizes) // 1024} KiB at most; plan {plan_s * 1000:.0f} ms")
        finally:
            b = Board(board.dir)
            for m in b.members():
                member.stop(b, m["name"])
