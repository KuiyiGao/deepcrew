from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import member as _member
from . import policy, providers, route, toolkit
from .board import LANGS, PAGE_SEEN, Board, read_json, reply_lang, team_path
from .member import CLAUDE_PRICES, DEFAULT_COMPARE, claude_equivalent, claude_tokens
from .ui import Extension, Refused

OBSERVE_S, PING_S = 0.5, 15.0
STREAM_POLL_S, JOIN_S = 1.0, 5
USAGE_TTL_S = 2.0
PLAN_S = 5.0
SHOT_S = 3.0
SEEN_EVERY_S = 5.0
SHOT_BYTES = 3 << 20
SHOT_KINDS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
PENDING_BLOCK_S = 30.0
STALL_S = 180.0
MAIL_WAIT_S = 60.0
BUSY_N = 30
FIELD_CHARS, SUMMARY_CHARS, DESCRIPTION_CHARS = 120, 600, 2000
QUESTION_CHARS = 4000
SUMMARIES, DECISIONS = 10, 12
TITLE_CHARS, REASON_CHARS, BRIEF_CHARS, ROLE_CHARS = 64, 90, 90, 60
COPY = "(copy) "
MAIL_CHARS, FEED_N, SAY_CHARS = 300, 60, 4000

LEVELS = {"error": 0, "block": 1, "wait": 2}
VOLATILE = {"now", "since_s", "age_s", "progress", "waited"}


def _short(text, n: int) -> str:
    s = " ".join(str(providers.shown(text) or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


GIST_CHARS, GIST_MIN = 110, 24
MARKUP = re.compile(r"\*\*|__|^\s*(?:[-*+]|\d+[.)]|#+)\s+", re.M)
CODE = re.compile(r"`([^`]*)`")
BRACKETS = re.compile(r"\s*[(（][^()（）]*[)）]")
PATH = re.compile(r"(?<![\w/])(?:~|\.{1,2})?/?(?:[\w.@-]+/)+([\w.@-]+)")
ENDS = re.compile(r"(?<=[.!?])\s+(?=\S)|(?<=[。！？；;])\s*")


def gist(text, question: bool = False) -> str:
    t = MARKUP.sub("", str(providers.shown(text) or ""))
    t = " ".join(t.split())
    t = re.sub(r"^(?:Leader|[a-z][\w-]{0,23})(?:\s*[:：]|\s+[—–]\s)\s*", "", t)
    t = CODE.sub(lambda m: c if len(c := PATH.sub(r"\1", m.group(1))) <= 32 else "", t)
    t = PATH.sub(r"\1", BRACKETS.sub("", t))
    parts = [x.strip() for x in ENDS.split(t) if x.strip()]
    if not parts:
        return ""
    if question and (asks := [x for x in parts if x.endswith(("?", "？"))]):
        out = asks[-1]
    else:
        out = parts[0]
        if " — " in out and len(out.split(" — ", 1)[0]) >= 10:
            out = out.split(" — ", 1)[0].rstrip(",;:") + "."
        for nxt in parts[1:]:
            if len(out) >= GIST_MIN:
                break
            out = f"{out} {nxt}"
    out = re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", " ".join(out.split()))
    out = out[:1].upper() + out[1:]
    if len(out) > GIST_CHARS:
        cut = out[: GIST_CHARS - 1]
        out = (cut.rsplit(" ", 1)[0] if " " in cut[GIST_CHARS // 2:] else cut).rstrip(",;:—- ") + "…"
    return out


def _text(text, n: int) -> str:
    s = "\n".join(line.rstrip() for line in str(providers.shown(text) or "").strip().splitlines())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _clause(text, n: int = ROLE_CHARS) -> str | None:
    s = " ".join(str(providers.shown(text) or "").split())
    m = re.search(r"[.;:!?](?:\s|$)| [-\u2013\u2014] ", s)
    s = s[: m.start()] if m else s
    if len(s) > n:
        s = (s[: n - 1].rsplit(" ", 1)[0] or s[: n - 1]).rstrip(",;:- ") + "…"
    return s or None


def _mark_label(kind: str, who: str, label: str, reason) -> str:
    verb = {"pass": "declined", "release": "released", "skip": "left"}[kind]
    return f"{who} {verb} {label}" + (f": {_short(reason, REASON_CHARS)}" if reason else "")


def _node(name) -> str:
    return name if name in ("leader", "user") else "board" if name in (None, "all", "board") else f"m:{name}"


def _mtime_ns(path: Path) -> int | None:
    with contextlib.suppress(OSError):
        return path.stat().st_mtime_ns
    return None


def _stamp(path: Path) -> tuple[int, int] | None:
    with contextlib.suppress(OSError):
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    return None


def _id_key(tid: str) -> tuple[int, str]:
    return (len(tid), tid)


def _request_fields(r: dict) -> dict:
    ti = r.get("input") if isinstance(r.get("input"), dict) else {}
    return {k: _short(ti[k], FIELD_CHARS) for k in ("command", "file_path", "path", "url", "host") if ti.get(k)}


def _limits(cfg: dict) -> dict:
    return {"perm": float(cfg.get("approval_timeout_s") or 300), "question": float(cfg.get("question_timeout_s") or 1800)}


def _mark_fields(kind: str, who: str, reason) -> dict:
    return {f"metadata.{kind}:{who}": _short(reason, FIELD_CHARS)} | ({"status": "pending"} if kind == "release" else {})


def _task_fields(kind: str, t: dict) -> dict:
    if kind == "post":
        return {"subject": _short(t["title"], FIELD_CHARS), "scope": t["scope"] or [], "blockedBy": t["deps"]}
    if kind == "claim":
        return {"status": "in_progress", "owner": t["owner"]}
    if kind == "done":
        return {"status": "completed", "summary": f"{len(t.get('summary') or '')} chars"}
    return {"status": "completed", "metadata.outcome": "cancelled"}


def _decision(state: str) -> str:
    return {"allowed": "allow", "denied": "deny"}.get(state, state)


def _request_event_fields(r: dict, answered: bool) -> dict:
    if r["kind"] == "perm":
        return {"decision": _decision(r["state"])} if answered else {"tool": r["tool"], **r["fields"]}
    return {"decision": r["state"], "chars": r["answer_chars"]} if answered else {"chars": r["chars"]}


class _UsageCache:
    def __init__(self, board: Board, ttl: float = USAGE_TTL_S):
        self.board, self.ttl, self.seen = board, ttl, {}

    def _get(self, key, read, fallback):
        now = time.monotonic()
        hit = self.seen.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        try:
            value = read()
        except OSError:
            value = hit[1] if hit else fallback
        self.seen[key] = (now, value)
        return value

    def __call__(self, name: str) -> dict:
        return self._get(("usage", name), lambda: _member.usage(self.board, name), {"requests": 0})

    def spent(self) -> float | None:
        return self._get(("spent",), lambda: _member.spent(self.board), None)


def _epoch(stamp) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError:
        return None


class TaskScan:
    def __init__(self):
        self.ids, self.pending, self.skip, self.since, self.first = set(), {}, None, None, None

    def feed(self, line) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            return
        msg = ev.get("message") if isinstance(ev, dict) else None
        if not isinstance(msg, dict):
            return
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        if ev.get("type") == "assistant":
            if msg.get("id") and msg["id"] != self.skip:
                self.ids.add(msg["id"])
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "TaskUpdate":
                    ti = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if ti.get("status") in ("in_progress", "completed", "pending") or ti.get("owner"):
                        self.pending[b.get("id")] = msg.get("id")
        elif ev.get("type") == "user":
            ts = _epoch(ev.get("timestamp"))
            self.first = self.first or ts
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in self.pending:
                    mid = self.pending.pop(b["tool_use_id"])
                    if not b.get("is_error"):
                        self.ids.clear()
                        self.skip, self.since = mid, ts

    @property
    def count(self) -> int:
        return len(self.ids)

    @property
    def stamp(self) -> float | None:
        return self.since or self.first


class _TaskClock:
    def __init__(self, board: Board):
        self.board, self.seen, self.works = board, {}, {}

    def __call__(self, name: str, works=None) -> TaskScan:
        path = self.board.dir / "logs" / f"{name}.ndjson"
        if name in self.works and self.works[name] != works:
            fresh = TaskScan()
            fresh.since = time.time()
            self.seen[name] = ((_stamp(path) or (0, 0))[1], b"", fresh)
        self.works[name] = works
        if works:
            return TaskScan()
        pos, tail, scan = self.seen.get(name) or (0, b"", TaskScan())
        try:
            size = path.stat().st_size
            if size < pos:
                pos, tail, scan = 0, b"", TaskScan()
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            return scan
        *lines, tail = (tail + chunk).split(b"\n")
        for line in lines:
            scan.feed(line)
        self.seen[name] = (pos + len(chunk), tail, scan)
        return scan


class ToolUse:
    def __init__(self, board: Board):
        self.board, self.seen, self.ids = board, {}, set()
        self.use: dict[str, dict] = {"mcp": {}, "skills": {}}

    def _count(self, kind: str, name: str, member: str, ts: float, tool: str | None = None) -> None:
        u = self.use[kind].setdefault(name, {"calls": 0, "by": {}, "last": 0.0, **({"tools": {}} if kind == "mcp" else {})})
        u["calls"] += 1
        u["by"][member] = u["by"].get(member, 0) + 1
        u["last"] = max(u["last"], ts)
        if tool:
            u["tools"][tool] = u["tools"].get(tool, 0) + 1

    def _feed(self, member: str, line: bytes, ts: float) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            return
        msg = ev.get("message") if isinstance(ev, dict) and ev.get("type") == "assistant" else None
        for b in msg.get("content") if isinstance(msg, dict) and isinstance(msg.get("content"), list) else []:
            if not (isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("name"), str)) or b.get("id") in self.ids:
                continue
            if b.get("id"):
                self.ids.add(b["id"])
            name, ti = b["name"], b.get("input") if isinstance(b.get("input"), dict) else {}
            when = _epoch(ev.get("timestamp")) or ts
            if name.startswith("mcp__") and not name.startswith(policy.OWN_TOOLS):
                server, _, tool = name[5:].partition("__")
                self._count("mcp", server, member, when, tool or None)
            elif name == "Skill" and isinstance(skill := ti.get("skill") or ti.get("command"), str) and skill.strip():
                self._count("skills", skill.strip().lstrip("/").split()[0], member, when)

    def __call__(self, names) -> dict:
        for name in names:
            path = self.board.dir / "logs" / f"{name}.ndjson"
            pos, tail = self.seen.get(name, (0, b""))
            try:
                st = path.stat()
                if st.st_size < pos:
                    pos, tail = 0, b""
                if st.st_size == pos:
                    continue
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
            except OSError:
                continue
            *lines, tail = (tail + chunk).split(b"\n")
            for line in lines:
                self._feed(name, line, st.st_mtime)
            self.seen[name] = (pos + len(chunk), tail)
        return json.loads(json.dumps(self.use))


class _Notes:
    def __init__(self, board: Board):
        self.board, self.key, self.value = board, None, ([], [])

    def __call__(self, obs: dict) -> tuple[list[dict], list[dict]]:
        run = self.board.dir / "run"
        asked = tuple(sorted((rid, r["state"], r["mtime"]) for rid, r in obs["requests"].items() if r["state"] != "pending"))
        key = (asked, _stamp(run / policy.ALLOW_LOG), _stamp(run / "summary.jsonl"))
        if key != self.key:
            self.value, self.key = (self.board.summaries(SUMMARIES), self.board.decisions(DECISIONS)), key
        return self.value


class _Plan:
    def __init__(self, board: Board):
        self.board, self.key, self.at, self.value = board, None, 0.0, None

    def __call__(self) -> dict:
        try:
            key = (self.board.version(), _stamp(self.board.dir / "team.json"))
        except OSError:
            key = None
        now = time.monotonic()
        if self.value is None or key is None or key != self.key or now - self.at >= PLAN_S:
            self.value, self.key, self.at = route.plan(self.board), key, now
        return self.value


def _cost(members: list[dict], spent: float | None) -> dict:
    tokens = claude_tokens([m["usage"] for m in members])
    claude = {name: round(claude_equivalent(tokens, name), 6) for name in CLAUDE_PRICES}
    return {"actual_usd": spent, "tokens": tokens, "claude": claude, "compare": DEFAULT_COMPARE,
            "saved_usd": None if spent is None else round(claude[DEFAULT_COMPARE] - spent, 6),
            "ratio": round(claude[DEFAULT_COMPARE] / spent, 1) if spent else None}


def _names(directory: Path) -> list[str]:
    with contextlib.suppress(OSError), os.scandir(directory) as it:
        return sorted(e.name for e in it if e.name.endswith(".json") and not e.name.startswith("."))
    return []


def _brief(kind: str, r: dict) -> str:
    if kind == "question":
        return _short(r.get("question"), BRIEF_CHARS)
    return _short(f"{r.get('tool', '')} {next(iter(_request_fields(r).values()), '')}", BRIEF_CHARS)


def _note_author(names: list[str], fname: str) -> str:
    return next((n for n in names if fname.startswith(n + "-")), fname.split("-", 1)[0])


class Observer:
    def __init__(self, board: Board):
        self.board, self._json, self._edits, self._pos, self._tail, self._errors = board, {}, [], 0, b"", (None, {})
        self._mail: dict[str, dict] = {}
        self._shots, self._shots_at = {}, 0.0
        self._tools = ToolUse(board)

    def _read(self, path: Path):
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
            hit = self._json.get(path)
            if hit and hit[0] == key:
                return hit[1], st.st_mtime
            value = read_json(path)
        except (OSError, ValueError):
            return None, 0.0
        self._json[path] = (key, value)
        return value, st.st_mtime

    def _new_edits(self) -> list[dict]:
        path = self.board.dir / "edits.jsonl"
        try:
            if path.stat().st_size < self._pos:
                self._edits, self._pos, self._tail = [], 0, b""
            with open(path, "rb") as f:
                f.seek(self._pos)
                chunk = f.read()
        except OSError:
            return self._edits
        self._pos += len(chunk)
        *lines, self._tail = (self._tail + chunk).split(b"\n")
        for line in lines:
            with contextlib.suppress(ValueError):
                e = json.loads(line)
                if isinstance(e, dict) and e.get("member") and e.get("path"):
                    self._edits.append(e)
        return self._edits

    def _member_errors(self) -> dict[str, str]:
        path = self.board.dir / "errors.jsonl"
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
            if self._errors[0] == key:
                return self._errors[1]
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return {}
        found: dict[str, str] = {}
        for line in lines:
            with contextlib.suppress(ValueError):
                e = json.loads(line)
                who = e.get("member") or (Path(e["file"]).stem if Path(str(e.get("file"))).parent.name == "members" else None)
                if who:
                    found[who] = _short(e.get("error") or e.get("where"), 100)
        self._errors = (key, found)
        return found

    def observe(self) -> dict:
        b = self.board
        records = b.members()
        tasks = {t["id"]: {"title": t["title"], "body": t["body"], "state": t["state"], "owner": t["owner"], "marks": dict(t["reasons"]),
                           "deps": t["deps"], "scope": t["scope"], "by": t["posted_by"], "summary": t["summary"], "skipped": t["ignored_by"],
                           "volunteers": sorted(t["volunteers"]), "posted": t["posted"], "mtime": t["changed"],
                           "gist": _short(t.get("gist"), GIST_CHARS) if t.get("gist") else None} for t in b.tasks()}
        mail = self._mail_now()
        notes, authors = {}, sorted([*(m["name"] for m in records), "leader"], key=len, reverse=True)
        with contextlib.suppress(OSError), os.scandir(b.notes_dir) as it:
            for e in it:
                if e.name.endswith(".md") and not e.name.startswith("."):
                    notes[e.name] = {"mtime": e.stat().st_mtime, "by": _note_author(authors, e.name)}
        requests = {}
        for kind, folder in (("perm", "perms"), ("question", "questions")):
            for name in _names(b.dir / folder):
                r, mtime = self._read(b.dir / folder / name)
                if isinstance(r, dict) and "id" in r:
                    fields = _request_fields(r) if kind == "perm" else {}
                    requests[r["id"]] = {"kind": kind, "member": r.get("member"), "state": r.get("state"), "ts": r.get("ts", 0), "mtime": mtime,
                                         "brief": _brief(kind, r), "tool": r.get("tool"), "fields": fields,
                                         "text": _short(r.get("question"), FIELD_CHARS), "chars": len(str(r.get("question") or "")),
                                         "gist": _short(r.get("gist"), GIST_CHARS) if r.get("gist") else gist(r.get("question"), question=True),
                                         "answer_chars": len(str(r.get("answer") or "")), "by": r.get("by"),
                                         "answer": _short(r.get("answer"), FIELD_CHARS), "options": [_short(o, FIELD_CHARS) for o in r.get("options") or []],
                                         "user_until": r.get("user_until"), "task": r.get("task"),
                                         "full": _short(r.get("question"), QUESTION_CHARS) if kind == "question" else None}
        return {"ts": time.time(), "project": str(b.root), "shared_dir": str(b.shared_dir), "tasks": tasks, "records": records,
                "members": {m["name"]: m["state"] for m in records}, "mail": mail, "edits": list(self._new_edits()), "notes": notes,
                "requests": requests, "errors": self._member_errors(), "shots": self._newest_pictures(records),
                "tools": self._tools([m["name"] for m in records])}

    def _mail_now(self) -> dict:
        mail, known, seen = {}, self._mail, set()
        for box in sorted((self.board.dir / "mail").glob("*")):
            for read, folder in ((False, box), (True, box / "read")):
                for name in _names(folder):
                    path = folder / name
                    seen.add(str(path))
                    m = known.get(str(path))
                    if m is None:
                        raw, _ = self._read(path)
                        self._json.pop(path, None)
                        if not isinstance(raw, dict):
                            continue
                        m = known[str(path)] = {"from": raw.get("from"), "to": raw.get("to") or box.name, "kind": raw.get("kind"), "ts": raw.get("ts", 0),
                                                "gist": _short(raw.get("gist"), GIST_CHARS) if raw.get("gist") else gist(raw.get("text")),
                                                "report": raw.get("report"), "task": raw.get("task"), "text": _short(raw.get("text"), MAIL_CHARS),
                                                "chars": len(str(raw.get("text") or "")), "read": read}
                    mail[f"{box.name}/{name}"] = m
        for gone in known.keys() - seen:
            del known[gone]
        return mail

    def _newest_pictures(self, records: list[dict]) -> dict:
        now = time.monotonic()
        if now - self._shots_at < SHOT_S:
            return self._shots
        found = {}
        for m in records:
            if best := newest_picture(self.board.artifacts_dir / m["name"]):
                found[m["name"]] = {"name": best[1], "mtime": round(best[0], 3)}
        self._shots, self._shots_at = found, now
        return found


def observe(board: Board) -> dict:
    return Observer(board).observe()


def _config(cfg: dict) -> dict:
    return {"provider": cfg["provider"]["name"], "model": cfg["member"]["model"], "effort": cfg["member"].get("effort") or "default",
            "sandbox": bool(cfg.get("sandbox")), "allow": len(cfg.get("bash_allow") or []), "pack": cfg.get("pack") or "basic",
            "max_active": cfg.get("max_active")}


def _next_team(cfg: dict) -> dict | None:
    now, want = providers.choices(), {"provider": cfg["provider"]["name"], "model": cfg["member"]["model"], "effort": cfg["member"].get("effort") or "default"}
    diff = {k: now[k] for k in want if now[k] and now[k] != want[k]}
    return diff or None


def _marks(t: dict) -> dict:
    out = {"pass": {}, "release": {}, "skip": list(t["skipped"])}
    for key, reason in t["marks"].items():
        kind, who = key.split(":", 1)
        if kind in out and kind != "skip":
            out[kind][who] = reason
    return out


def _task_list(obs_tasks: dict) -> list[dict]:
    blocks: dict[str, list[str]] = {}
    for tid, t in obs_tasks.items():
        for d in t["deps"]:
            blocks.setdefault(d, []).append(tid)
    return [{"id": tid, "title": t["title"], "description": _text(t["body"], DESCRIPTION_CHARS), "state": t["state"], "owner": t["owner"], "by": t["by"],
             "deps": t["deps"], "blocks": sorted(blocks.get(tid, []), key=_id_key), "scope": t["scope"], "marks": _marks(t),
             "summary": _short(t["summary"], SUMMARY_CHARS) if t["summary"] else None, "waiting": None, "updated": t["mtime"],
             "volunteers": t.get("volunteers", [])} for tid, t in obs_tasks.items()]


def _pending(obs: dict, now: float, limits: dict) -> dict:
    perms, questions = [], []
    for rid, r in obs["requests"].items():
        age = round(max(0.0, now - r["ts"]), 1)
        if r["state"] != "pending" or age > limits[r["kind"]]:
            continue
        if r["kind"] == "perm":
            perms.append({"id": rid, "member": r["member"], "tool": r["tool"], "fields": r["fields"], "age_s": age})
        else:
            questions.append({"id": rid, "member": r["member"], "text": r.get("full") or r["text"], "age_s": age, "options": r.get("options") or [],
                              "gist": r.get("gist"),
                              "task": r.get("task"), "user_until": r["user_until"] if (r.get("user_until") or 0) > now else None})
    mail = [{"id": key, "from": m["from"], "kind": m["kind"], "chars": m["chars"], "age_s": round(max(0.0, now - m["ts"]), 1)}
            for key, m in obs["mail"].items() if not m["read"] and key.split("/", 1)[0] == "leader"]
    return {k: sorted(v, key=lambda x: -x["age_s"]) for k, v in (("perms", perms), ("questions", questions), ("leader_mail", mail))}


def _regions(members: list[dict], tasks: list[dict]) -> tuple[list[dict], list[dict]]:
    regions = [{"path": p, "member": m["name"], "kind": "region"} for m in members for p in m["region"]]
    regions += [{"path": f"notes/{m['name']}-*", "member": m["name"], "kind": "note"} for m in members]
    regions += [{"path": f"artifacts/{m['name']}/", "member": m["name"], "kind": "artifact"} for m in members]
    by_name, conflicts = {m["name"]: m for m in members}, []
    for t in tasks:
        owner = by_name.get(t["owner"])
        if owner and t["state"] in ("claimed", "done"):
            outside = [s for s in t["scope"] or [] if not policy.covers(owner["region"], s)]
            conflicts += [{"task": t["id"], "path": s, "reason": f"outside {owner['name']}'s region"} for s in outside]
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            if policy.overlap(a["region"], b["region"]):
                path = next(x for x in a["region"] if policy.overlap([x], b["region"]))
                conflicts.append({"task": None, "path": path, "reason": f"regions of {a['name']} and {b['name']} overlap"})
    return regions, conflicts


def derive_stuck(snap: dict) -> list[dict]:
    out, now = [], snap["now"]
    tasks = {t["id"]: t for t in snap["tasks"]}

    def add(where, what, since, level, **ref):
        out.append({"where": where, "what": _short(what, 100), "since_s": round(max(0.0, since), 1), "level": level, "ref": ref})

    def level(age):
        return "block" if age > PENDING_BLOCK_S else "wait"

    for p in snap["pending"]["perms"]:
        add(f"m:{p['member']}", f"needs approval: {p['tool']} {next(iter(p['fields'].values()), '')}", p["age_s"], level(p["age_s"]), perm=p["id"])
    for q in snap["pending"]["questions"]:
        add(f"m:{q['member']}", f"asks: {q['text']}", q["age_s"], level(q["age_s"]), question=q["id"])
    done = {tid for tid, t in tasks.items() if t["state"] == "done"}
    dead = {tid for tid, t in tasks.items() if t["state"] == "cancelled"}
    reported = {b["task"] for b in snap["blocked"]}
    for t in (t for t in tasks.values() if t["state"] == "open"):
        unmet = sorted(set(t["deps"]) - done, key=_id_key)
        if unmet and not set(unmet) & dead:
            add(f"t:{t['id']}", "waits for " + ", ".join(f"#{d}" for d in unmet), now - t["updated"], "wait", task=t["id"])
        marks = [f"{'declined' if k == 'pass' else 'released'} by {w}: {r}" for k in ("pass", "release") for w, r in t["marks"][k].items()]
        if marks and t["id"] not in reported:
            add("leader", "; ".join(marks), now - t["updated"], "block", task=t["id"])
    for b in snap["blocked"]:
        add(f"t:{b['task']}", b["reason"], now - tasks.get(b["task"], {}).get("updated", now), "block", task=b["task"])
    for t in snap["tasks"]:
        if t["state"] == "open" and t.get("volunteers"):
            add(f"t:{t['id']}", "asked for by " + ", ".join(t["volunteers"]) + ("; the leader picks" if len(t["volunteers"]) > 1 else ""),
                now - t["updated"], "wait", task=t["id"])
    for g in snap["proposals"]:
        ids = ", ".join(f"#{i}" for i in g["tasks"])
        add("leader", f"nobody took {ids}: proposed {g['name']}" if g.get("due", True) else f"nobody took {ids} yet",
            now - min(tasks.get(i, {}).get("updated", now) for i in g["tasks"]), "block" if g.get("due", True) else "wait", task=g["tasks"][0])
    for m in snap["members"]:
        seen = m["last_seen"]
        if m["error"] or m["state"] == "failed":
            add(f"m:{m['name']}", m["error"] or "failed", now - seen if seen else 0.0, "error", member=m["name"])
        elif m["state"] == "active" and seen and now - seen > STALL_S:
            add(f"m:{m['name']}", "no activity", now - seen, "wait", member=m["name"])
        elif m["state"] == "active" and not m["task"] and m["requests_without_task"] >= BUSY_N:
            n = m["requests_without_task"]
            add(f"m:{m['name']}", f"{n} requests without a task", now - m["no_task_since"] if m["no_task_since"] else 0.0,
                "block" if n >= 2 * BUSY_N else "wait", member=m["name"])
    old = snap["pending"]["leader_mail"]
    if old and old[0]["age_s"] > MAIL_WAIT_S:
        add("leader", f"mail unread from {', '.join(dict.fromkeys(m['from'] for m in old))}", old[0]["age_s"], "wait", member=old[0]["from"])
    return sorted(out, key=lambda s: (LEVELS[s["level"]], -s["since_s"]))


def assemble(base: dict, tasks: list[dict], members: list[dict], pending: dict, now: float, proposals: list, blocked: list, mail_total: int,
             spent: float | None) -> dict:
    waiting, titles = {}, {t["id"]: t["title"] for t in tasks}
    for kind, key in (("perm", "perms"), ("question", "questions")):
        for p in pending[key]:
            waiting.setdefault(p["member"], {"kind": kind, "id": p["id"], "age_s": p["age_s"]})
    for m in members:
        m["waiting"], m["task_title"] = waiting.get(m["name"]), titles.get(m["task"])
    counts = {s: sum(1 for t in tasks if t["state"] == s) for s in ("open", "claimed", "done", "cancelled")}
    counts.update(mail_unread_leader=len(pending["leader_mail"]), mail_total=mail_total, perms_pending=len(pending["perms"]),
                  questions_pending=len(pending["questions"]))
    regions, conflicts = _regions(members, tasks)
    snap = {**base, "members": members, "tasks": tasks, "counts": counts, "pending": pending, "proposals": proposals, "blocked": blocked,
            "regions": regions, "conflicts": conflicts, "cost": _cost(members, spent), "now": now}
    snap["stuck"] = derive_stuck(snap)
    for s in snap["stuck"]:
        t = next((t for t in tasks if t["id"] == s["ref"].get("task")), None)
        if t is not None and t["waiting"] is None:
            t["waiting"] = {"level": s["level"], "what": s["what"], "since_s": s["since_s"]}
    return snap


def _base(board: Board) -> dict:
    return {"team": board.cfg["team"], "project": str(board.root), "shared_dir": str(board.shared_dir), "provider": board.cfg["provider"]["name"],
            "model": board.cfg["member"]["model"], "effort": board.cfg["member"].get("effort"), "config": _config(board.cfg), "next": _next_team(board.cfg),
            "lang": reply_lang(board.cfg), "langs": list(LANGS), "busy_n": BUSY_N, "block_s": PENDING_BLOCK_S}


STEP = {"kickoff": 30, "progress": 60}


def _steps(obs: dict) -> dict:
    seen: dict[tuple, list[str]] = {}
    for m in obs["mail"].values():
        if m.get("report") in STEP and m.get("task") is not None and m["to"] in ("leader", "user"):
            seen.setdefault((m["from"], str(m["task"])), []).append(m["report"])
    return {k: min(80, max(STEP[r] for r in v) + 20 * max(0, v.count("progress") - 1)) for k, v in seen.items()}


def snapshot(board: Board, usage_of=None, obs: dict | None = None, clock=None, notes=None, plan=None, spent=None, feed=None) -> dict:
    usage_of = usage_of or (lambda name: _member.usage(board, name))
    clock = clock or _TaskClock(board)
    obs = obs or observe(board)
    routed = plan() if plan else route.plan(board)
    now, last_edit, steps = obs["ts"], {}, _steps(obs)
    for e in obs["edits"]:
        last_edit[e["member"]] = max(last_edit.get(e["member"], 0.0), e.get("ts") or 0.0)
    members = []
    for m in obs["records"]:
        u = usage_of(m["name"])
        log = _mtime_ns(board.dir / "logs" / f"{m['name']}.ndjson")
        seen = max([x for x in ((log or 0) / 1e9, last_edit.get(m["name"], 0.0)) if x] or [m.get("updated") or 0.0])
        helps = routed["helpers"].get(m["name"])
        works = (m.get("task"), helps["task"] if helps else None)
        scan = clock(m["name"], works if any(works) else None)
        task = m.get("task")
        members.append({"name": m["name"], "state": m["state"], "region": m["region"], "role": m.get("role"), "role_brief": _clause(m.get("role")),
                        "task": task, "task_title": None, "helps": helps, "model": _member.member_model(board, m)[0], "runs": m.get("runs", 0),
                        "requests_without_task": scan.count, "no_task_since": scan.stamp if scan.count else None, "usage": u,
                        "cost_usd": u.get("cost_usd"), "last_seen": seen, "waiting": None,
                        "pct": steps.get((m["name"], str(task)), 10) if task is not None else None, "shot": obs.get("shots", {}).get(m["name"]),
                        "error": _short(obs["errors"].get(m["name"]) or m.get("failure"), 100) or None})
    pending = _pending(obs, now, _limits(board.cfg))
    summaries, decisions = (notes or _Notes(board))(obs)
    paid = spent() if spent else _member.spent(board)
    snap = assemble(_base(board), _task_list(obs["tasks"]), members, pending, now, routed["proposals"], routed["blocked"], len(obs["mail"]), paid)
    cap = board.budget()
    tools = obs.get("tools") or {"mcp": {}, "skills": {}}
    return {**snap, "summaries": summaries, "decisions": decisions, "capacity": routed["capacity"], "feed": (feed or _feed)(obs),
            "budget": {"usd": cap["usd"] if cap else None, "spent": paid}, "load": routed.get("load"),
            "tools": {**tools, "given": {"mcp": toolkit.mcp_names(board.root), "skills": toolkit.skill_names(board.root)}}}


def _edit_event(e: dict, shared: str, root: str) -> tuple[str, str, str]:
    path = str(e.get("path") or "")
    full = os.path.join(root, path) if root else path
    folder = policy.relpath(Path(shared), full) if shared else None
    shown = folder or (policy.relpath(Path(root), full) if root else None) or providers.shown(path)
    tail = "/".join(shown.replace("\\", "/").split("/")[-2:])
    return ("folder" if folder else "project"), f"{e.get('member')} {str(e.get('tool') or 'edit').lower()}s {tail}", shown


def events(prev: dict, cur: dict) -> list[dict]:
    ts, out = cur.get("ts") or time.time(), []

    def add(kind, frm, to, label, fields, **extra):
        out.append({"kind": kind, "ts": ts, "from": frm, "to": to, "label": label, "fields": fields, **extra})

    for tid, t in cur["tasks"].items():
        p = prev["tasks"].get(tid)
        label = f"#{tid} {_short(t['title'], TITLE_CHARS)}"
        if p is None:
            add("post", "leader", "board", label, _task_fields("post", t), task=tid, title=_short(t["title"], TITLE_CHARS))
            p = {"state": "open", "owner": None, "marks": {}}
        gave_back = None
        if p["state"] == "claimed" and (t["state"] == "open" or (t["state"] == "claimed" and t["owner"] != p["owner"])):
            gave_back = p["owner"]
            why = t["marks"].get(f"release:{gave_back}")
            add("release", _node(gave_back), "board", _mark_label("release", gave_back, label, why), _mark_fields("release", gave_back, why),
                task=tid, member=gave_back)
        for key, reason in t["marks"].items():
            kind, who = key.split(":", 1)
            if key not in p["marks"] and not (kind == "release" and who == gave_back):
                add(kind, _node(who), "board", _mark_label(kind, who, label, reason), _mark_fields(kind, who, reason), task=tid, member=who)
        if t["owner"] and t["state"] in ("claimed", "done") and (p["state"] == "open" or (p["state"] == "claimed" and p["owner"] != t["owner"])):
            add("claim", "board", _node(t["owner"]), f"{t['owner']} took {label}", _task_fields("claim", t), task=tid, member=t["owner"])
        if t["state"] == "done" and p["state"] != "done":
            who = t["owner"] or "leader"
            add("done", _node(who), "board", f"{who} finished {label}", _task_fields("done", t), task=tid, member=t["owner"],
                title=_short(t["title"], TITLE_CHARS), gist=t.get("gist"))
        if t["state"] == "cancelled" and p["state"] != "cancelled":
            add("cancel", "leader", "board", f"cancelled {label}", _task_fields("cancel", t), task=tid)

    for name, st in cur["members"].items():
        if prev["members"].get(name) != st:
            add("member_state", _node(name), _node(name), f"{name} {st}", {"state": st}, name=name, state=st)
    for name in prev["members"].keys() - cur["members"].keys():
        add("member_state", _node(name), _node(name), f"{name} removed", {"state": "removed"}, name=name, state="removed")

    for key, m in cur["mail"].items():
        if key not in prev["mail"] and not _copy(m):
            add("mail", _node(m["from"]), _node(m["to"]), f"{m['from']} → {m['to']}: {m['text']}", {"kind": m["kind"], "chars": m["chars"]},
                mail_kind=m["kind"], text=m["text"], gist=m["gist"], report=m.get("report"), on=m.get("task"))

    for e in cur["edits"][len(prev["edits"]):]:
        target, label, shown = _edit_event(e, cur.get("shared_dir", ""), cur.get("project", ""))
        add("edit", _node(e["member"]), target, label, {"tool": e.get("tool"), "path": shown}, member=e["member"], path=e.get("path"),
            tool=e.get("tool"))

    for fname, n in cur["notes"].items():
        before = prev["notes"].get(fname)
        if before is None or before["mtime"] != n["mtime"]:
            add("note", _node(n["by"]), "folder", f"{n['by']} {'wrote' if before is None else 'updated'} {fname}", {"file": fname}, by=n["by"], name=fname)

    for kind, what in (("mcp", "MCP server"), ("skills", "skill")):
        for name, u in (cur.get("tools") or {}).get(kind, {}).items():
            before = ((prev.get("tools") or {}).get(kind) or {}).get(name) or {"by": {}, "tools": {}}
            for who, n in u["by"].items():
                if n > before["by"].get(who, 0):
                    tool = next((t for t, k in u.get("tools", {}).items() if k > before.get("tools", {}).get(t, 0)), None)
                    label = f"{who} used {what} {name}" + (f": {tool}" if tool else "")
                    add("tool", _node(who), "tools", label, {"kind": kind, "name": name, "calls": u["calls"]},
                        member=who, tool_kind=kind, name=name, tool=tool, calls=n - before["by"].get(who, 0))

    for rid, r in cur["requests"].items():
        p = prev["requests"].get(rid)
        if p is None:
            add(r["kind"], _node(r["member"]), "leader", f"{r['member']}: {r['brief']}", _request_event_fields(r, False),
                member=r["member"], state="pending", id=rid, text=r["text"], brief=r["brief"], gist=r["gist"], user=bool(r.get("user_until")),
                options=r.get("options") or [], task=r.get("task"))
        if (p is None or p["state"] == "pending") and r["state"] != "pending":
            add(r["kind"], _answerer(r), _node(r["member"]), f"{r['state']}: {r['brief']}", _request_event_fields(r, True),
                member=r["member"], state=r["state"], id=rid, brief=r["brief"], by=r.get("by"), answer=r.get("answer"))
    return out


def _answerer(r: dict) -> str:
    return "user" if r.get("by") == "user" else "leader"


def _copy(m: dict) -> bool:
    return m["to"] == "leader" and str(m.get("text") or "").startswith(COPY)


def _reported(obs: dict) -> set[tuple[str, str]]:
    return {(m["from"], str(m["task"])) for m in obs["mail"].values() if m.get("report") == "done" and m.get("task") is not None}


def _feed(obs: dict) -> list[dict]:
    out, reported = [], _reported(obs)
    for m in obs["mail"].values():
        if not _copy(m):
            out.append({"ts": m["ts"], "kind": "mail", "from": m["from"], "to": m["to"], "text": m["text"], "gist": m["gist"], "mail_kind": m["kind"],
                        "report": m.get("report"), "task": m.get("task")})
    for rid, r in obs["requests"].items():
        if r["kind"] == "perm" and r["state"] != "pending" and r.get("by") != "user":
            continue
        text = r["text"] if r["kind"] == "question" else r["brief"]
        to = "user" if r.get("user_until") else "leader"
        out.append({"ts": r["ts"], "kind": r["kind"], "from": r["member"], "to": to, "text": text, "gist": r["gist"] if r["kind"] == "question" else text,
                    "id": rid})
        if r["state"] != "pending":
            text = r.get("answer") or r["brief"]
            out.append({"ts": max(r["mtime"], r["ts"] + 0.05), "kind": "answer", "from": _answerer(r), "to": r["member"], "text": text, "gist": text,
                        "state": r["state"], "id": rid})
    for tid, t in obs["tasks"].items():
        title = f"#{tid} {_short(t['title'], TITLE_CHARS)}"
        if t.get("posted"):
            out.append({"ts": t["posted"], "kind": "post", "from": "leader", "to": "all", "text": title, "gist": title, "task": tid})
        if t["state"] == "done" and (t["owner"], str(tid)) not in reported:
            out.append({"ts": t["mtime"], "kind": "done", "from": t["owner"] or "leader", "to": "leader", "text": title, "gist": t.get("gist") or title,
                        "task": tid})
    return sorted(out, key=lambda x: x["ts"])[-FEED_N:]


def _stable(obj):
    if isinstance(obj, dict):
        return {k: _stable(v) for k, v in obj.items() if k not in VOLATILE}
    return [_stable(v) for v in obj] if isinstance(obj, list) else obj


def _key(snap: dict | None) -> str:
    return json.dumps(_stable(snap or {}), sort_keys=True)


class Hub:
    def __init__(self, team_dir: Path):
        self.dir, self.board, self.observer, self.usage, self.clock, self.notes, self.plan = team_dir, None, None, None, None, None, None
        self.subs, self.lock, self.work, self.stop = set(), threading.Lock(), threading.Lock(), threading.Event()
        self.prev, self.latest, self.latest_key = None, {"team": None}, ""
        self.cfg_mtime, self.env_mtime, self.said = None, _mtime_ns(providers.ENV_FILE), set()
        self.seen_at = 0.0

    def make_observer(self, board: Board) -> Observer:
        return Observer(board)

    def find_events(self, prev: dict, cur: dict) -> list[dict]:
        return events(prev, cur)

    def feed(self, obs: dict) -> list[dict]:
        return _feed(obs)

    def current(self) -> dict:
        return self.latest

    def _attach(self) -> bool:
        try:
            self.board = Board(self.dir)
        except (OSError, ValueError, KeyError):
            return False
        self.observer, self.usage, self.clock = self.make_observer(self.board), _UsageCache(self.board), _TaskClock(self.board)
        self.notes, self.plan, self.prev = _Notes(self.board), _Plan(self.board), None
        self.cfg_mtime = _mtime_ns(self.dir / "team.json")
        return True

    def _seen(self) -> None:
        now = time.time()
        with self.lock:
            watched = bool(self.subs)
        if watched and now - self.seen_at >= SEEN_EVERY_S:
            self.seen_at = now
            with contextlib.suppress(OSError):
                path = self.dir / "run" / PAGE_SEEN
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

    def _refresh_config(self) -> None:
        mtime = _mtime_ns(self.dir / "team.json")
        if mtime != self.cfg_mtime:
            with contextlib.suppress(OSError, ValueError):
                self.board.cfg = read_json(self.dir / "team.json")
                self.cfg_mtime = mtime

    def _live(self) -> tuple[list[dict], dict]:
        obs = self.observer.observe()
        snap = snapshot(self.board, self.usage, obs, self.clock, self.notes, self.plan, self.usage.spent, self.feed)
        evs = self.find_events(self.prev, obs) if self.prev is not None else []
        self.prev = obs
        return evs, snap

    def tick(self) -> None:
        with self.work:
            env = _mtime_ns(providers.ENV_FILE)
            if env != self.env_mtime:
                self.env_mtime = env
                with self.lock:
                    self._put("env", "{}")
            if self.board is None and not self._attach():
                return
            self._seen()
            self._refresh_config()
            evs, snap = self._live()
            if evs or _key(snap) != self.latest_key:
                self._publish(evs, snap)

    def look(self) -> None:
        try:
            self.tick()
        except Exception as e:
            said = f"{type(e).__name__}: {e}"
            if said not in self.said:
                self.said.add(said)
                print(f"{providers.NAME} page: a look at the team failed ({said})", file=sys.stderr)

    def _put(self, kind: str, data: str) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    while True:
                        q.get_nowait()
                q.put_nowait(("snapshot", json.dumps(self.latest)))

    def _publish(self, evs: list[dict], snap: dict, reset: bool = False) -> None:
        with self.lock:
            self.latest, self.latest_key = providers.shown(snap), _key(snap)
            self._put("update", json.dumps({"events": providers.shown(evs), "snapshot": self.latest, "reset": reset}))

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self.lock:
            q.put(("snapshot", json.dumps(self.latest)))
            self.subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.subs.discard(q)

    def run(self) -> None:
        while not self.stop.wait(OBSERVE_S):
            self.look()


def newest_picture(folder: Path) -> tuple[float, str] | None:
    with contextlib.suppress(OSError), os.scandir(folder) as it:
        return max(((e.stat().st_mtime, e.name) for e in it if e.is_file(follow_symlinks=False) and Path(e.name).suffix.lower() in SHOT_KINDS),
                   default=None)
    return None


class View(Extension):
    name = "live"
    hub_class = Hub

    def __init__(self, team_dir: Path):
        super().__init__()
        self.hub, self.thread = self.hub_class(Path(team_dir)), None
        self.html = Path(__file__).with_name("live.html").read_bytes()
        self.routes = {("GET", "/api/live/snapshot"): lambda body, query: self.hub.current(), ("POST", "/api/live/say"): self._say,
                       ("POST", "/api/live/lang"): self._lang, ("GET", "/api/live/shot"): self._shot}
        self.streams = {"/api/live/events": self._stream}

    def start(self) -> None:
        self.hub.look()
        self.thread = threading.Thread(target=self.hub.run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.hub.stop.set()
        if self.thread:
            self.thread.join(JOIN_S)

    def _team(self) -> Board:
        if self.hub.board is None:
            raise Refused("no team yet")
        return self.hub.board

    def _say(self, body: dict, query: dict) -> dict:
        board, to, text = self._team(), body.get("to"), body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > SAY_CHARS:
            raise Refused(f"the message must be 1 to {SAY_CHARS} characters")
        names = [m["name"] for m in board.members()]
        if to not in ("leader", "all", *names):
            raise Refused(f"to is leader, all or a member: {', '.join(names) or 'no members yet'}")
        text = text.strip()
        for who in names if to == "all" else [to]:
            board.send("user", who, "message", text)
        if to != "leader":
            board.send("user", "leader", "notice", f"{COPY}the user wrote to {to}: {text}")
            route.run(board)
        return {"sent": to}

    def _lang(self, body: dict, query: dict) -> dict:
        board, lang = self._team(), body.get("lang")
        if lang not in LANGS:
            raise Refused(f"lang is one of {', '.join(LANGS)}")
        if reply_lang(board.live_cfg()) != lang:
            board.update_cfg(lambda cfg: cfg.update(reply_lang=lang))
            board.send("user", "leader", "notice", f"The user chose to read replies in {lang}. From now on write everything for the user in {lang}: "
                       "your replies, what you put on the page and your round reports. Work with the crew stays in English; members learn it with "
                       "their next message.")
        return {"lang": lang}

    def _shot(self, body: dict, query: dict) -> dict:
        board, name = self._team(), (query.get("m") or [""])[0]
        if name not in {m["name"] for m in board.members()}:
            raise Refused("no such member", 404)
        folder = board.artifacts_dir / name
        best = newest_picture(folder)
        if best is None:
            raise Refused("nothing shared", 404)
        path = folder / best[1]
        if os.path.realpath(path.parent) != os.path.realpath(folder) or path.stat().st_size > SHOT_BYTES:
            raise Refused("not shared", 404)
        data = base64.b64encode(path.read_bytes()).decode()
        return {"name": best[1], "mtime": round(best[0], 3), "data": f"data:{SHOT_KINDS[path.suffix.lower()]};base64,{data}"}

    def _stream(self, handler) -> None:
        hub, q = self.hub, self.hub.subscribe()
        handler.sse()
        last = time.monotonic()
        try:
            while not (handler.server.closing.is_set() or hub.stop.is_set()):
                try:
                    kind, data = q.get(timeout=min(STREAM_POLL_S, PING_S))
                except queue.Empty:
                    if time.monotonic() - last < PING_S:
                        continue
                    handler.wfile.write(b": ping\n\n")
                else:
                    handler.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
                handler.wfile.flush()
                last = time.monotonic()
        except (OSError, ValueError):
            pass
        finally:
            hub.unsubscribe(q)


def page(project: Path, team: str | None) -> View:
    return View(team_path(Path(project).resolve(), team or "main"))
