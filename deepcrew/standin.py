from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TASK = re.compile(r'"id": "(\d+)"')
REVIEW_MARK = "# Command review"
MEMBER = re.compile(r'member "([a-z][a-z0-9-]*)"')
OFFER = "Tasks you may take now"


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
        elif isinstance(b, dict) and b.get("type") == "tool_result":
            out += _texts(b.get("content"))
    return out


def _use(name: str, args: dict, me: str = "member") -> tuple[dict, str]:
    def fill(v):
        return v.replace("{me}", me) if isinstance(v, str) else {k: fill(x) for k, x in v.items()} if isinstance(v, dict) else \
            [fill(x) for x in v] if isinstance(v, list) else v
    return {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}", "name": name, "input": fill(args)}, "tool_use"


def _offered(text: str) -> list[str]:
    rest = text.split(OFFER, 1)[1]
    try:
        return [t["id"] for t in json.loads(rest[rest.index("["):])]
    except (ValueError, KeyError, TypeError):
        return TASK.findall(rest)


def next_block(body: dict, actions: dict, prompt_actions: dict | None = None, passes: set[str] = frozenset(),
               ignores: set[str] = frozenset()) -> tuple[dict, str]:
    if not {"TaskUpdate", "mcp__deepcrew__message"} <= {t.get("name") for t in body.get("tools") or []}:
        return {"type": "text", "text": "ok"}, "end_turn"
    first = " ".join(_texts((body.get("messages") or [{}])[0].get("content")))
    me = (MEMBER.findall(first) or ["member"])[0]
    delivered: list[tuple[str, int]] = []
    offered: list[str] = []
    closed, decided, calls, uses = set(), set(), [], {}
    for i, m in enumerate(body.get("messages", [])):
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [b for b in content or [] if isinstance(b, dict)]
        if m.get("role") == "assistant":
            for b in blocks:
                if b.get("type") == "tool_use":
                    calls.append((i, b["name"]))
                    uses[b["id"]] = b
                    ti = b.get("input") or {}
                    if b["name"] == "TaskUpdate":
                        tid = str(ti.get("taskId"))
                        closed |= {tid} if ti.get("status") in ("completed", "pending") else set()
                        decided |= {tid} if ti.get("status") == "in_progress" or f"pass:{me}" in (ti.get("metadata") or {}) else set()
            continue
        for b in blocks:
            if b.get("type") == "text" and OFFER in b.get("text", ""):
                offered = _offered(b["text"])
                delivered = [(t, j) for t, j in delivered if t not in offered]
                decided -= set(offered)
            elif b.get("type") == "text":
                delivered += [(t, i) for t in TASK.findall(b["text"]) if t not in {d for d, _ in delivered}][:1]
            elif b.get("type") == "tool_result" and not b.get("is_error"):
                use = uses.get(b.get("tool_use_id")) or {}
                if use.get("name") == "TaskUpdate" and (use.get("input") or {}).get("status") == "in_progress":
                    delivered.append((str(use["input"]["taskId"]), i))
    current = next(((tid, i) for tid, i in delivered if tid not in closed), None)
    if current:
        tid, since = current
        script = [*actions.get(tid, []), ("TaskUpdate", {"taskId": tid, "status": "completed", "metadata": {"summary": f"{tid} done by the stand-in"}})]
        n = sum(1 for i, _ in calls if i > since)
        return _use(*script[n], me) if n < len(script) else ({"type": "text", "text": f"{tid} finished"}, "end_turn")
    choice = next((t for t in offered if t not in decided and t not in closed and t not in ignores), None)
    if choice:
        return _use("TaskUpdate", {"taskId": choice, "metadata": {f"pass:{me}": "the stand-in declines it"}}) if choice in passes else \
            _use("TaskUpdate", {"taskId": choice, "status": "in_progress"})
    script = next((s for marker, s in (prompt_actions or {}).items() if marker in first), None)
    if script is None or len(calls) >= len(script):
        return {"type": "text", "text": "no task"}, "end_turn"
    return _use(*script[len(calls)], me)


class StandIn:
    def __init__(self, actions: dict[str, list[tuple[str, dict]]] | None = None):
        self.actions = actions if actions is not None else {}
        self.prompt_actions: dict[str, list[tuple[str, dict]]] = {}
        self.passes: set[str] = set()
        self.ignores: set[str] = set()
        self.error: tuple[int, dict, dict] | None = None
        self.review = "allow"
        self.review_text: str | None = None
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status: int, obj: dict, headers: dict | None = None) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._json(404, {"error": "not here"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                h = {k.lower(): v for k, v in self.headers.items()}
                with outer._lock:
                    outer.requests.append({"path": self.path, "authorization": h.get("authorization"), "x-api-key": h.get("x-api-key"),
                                           "body": body})
                if "count_tokens" in self.path:
                    return self._json(200, {"input_tokens": 1})
                if not self.path.startswith("/v1/messages"):
                    return self._json(404, {"error": "not here"})
                if outer.error:
                    return self._json(*outer.error)
                system = body.get("system")
                system = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system or [] if isinstance(b, dict))
                if REVIEW_MARK in system:
                    said = outer.review_text if outer.review_text is not None else json.dumps({"verdict": outer.review, "reason": "stand-in review"})
                    return self._json(200, {"id": "msg_" + uuid.uuid4().hex[:12], "type": "message", "role": "assistant", "model": body.get("model", "x"),
                                            "content": [{"type": "text", "text": said}], "stop_reason": "end_turn", "stop_sequence": None,
                                            "usage": {"input_tokens": 1, "output_tokens": 1}})
                block, stop = next_block(body, outer.actions, outer.prompt_actions, outer.passes, outer.ignores)
                msg = {"id": "msg_" + uuid.uuid4().hex[:12], "type": "message", "role": "assistant", "model": body.get("model", "x"),
                       "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
                if not body.get("stream"):
                    return self._json(200, {**msg, "content": [block], "stop_reason": stop})
                start = {**block, "input": {}} if block["type"] == "tool_use" else {"type": "text", "text": ""}
                delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if block["type"] == "tool_use"
                         else {"type": "text_delta", "text": block["text"]})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for name, data in (("message_start", {"type": "message_start", "message": msg}),
                                   ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": start}),
                                   ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                                   ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                                   ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                                      "usage": {"output_tokens": 1}}),
                                   ("message_stop", {"type": "message_stop"})):
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> StandIn:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def member_requests(self, name: str) -> list[dict]:
        marker = f'member "{name}"'
        with self._lock:
            return [r for r in self.requests if any(marker in t for m in r["body"].get("messages", [])[:1] for t in _texts(m.get("content")))]
