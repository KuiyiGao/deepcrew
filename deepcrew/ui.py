from __future__ import annotations

import contextlib
import html
import http.client
import json
import math
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import extensions, policy, presets, providers
from .board import REQUEST_ID, Board, file_lock, read_json, team_path, tools_dir

MAX_BODY = 64 * 1024
CSP = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
DEFAULT_PROVIDER = "deepseek"
NUMBERS = ("DEEPCREW_MAX_ACTIVE", "DEEPCREW_HOLD_S", "DEEPCREW_MAX_TURNS", "DEEPCREW_APPROVAL_TIMEOUT_S", "DEEPCREW_QUESTION_TIMEOUT_S", "DEEPCREW_OFFER_SIZE",
           "DEEPCREW_NOTE_MAX_CHARS")
ON_OFF = ("DEEPCREW_THINKING", "DEEPCREW_PROJECT_MEMORY", "DEEPCREW_REVIEW")
KEYS = ("DEEPCREW_PROVIDER", "DEEPCREW_MODEL", "DEEPCREW_EFFORT", *ON_OFF, *NUMBERS, "DEEPCREW_SANDBOX", "DEEPCREW_SANDBOX_DOMAINS", "DEEPCREW_ALLOW",
        "DEEPCREW_ALLOW_OFF",
        "DEEPCREW_SANDBOX_DENY_READ", "DEEPCREW_SANDBOX_ALLOW_WRITE", "DEEPCREW_TOOLS_OFF")
THEME_KEY, THEMES = "DEEPCREW_THEME", ("auto", "light", "dark")
MAX_BUDGET_USD = 100000.0
MAX_MEMBERS = 32
UI_FILE, EVENTS_FILE, LOG_FILE = "ui.json", "ui-events.jsonl", "ui.log"
ADDR_FILE = "ui-address.json"
START_WAIT_S, START_POLL_S = 10.0, 0.1
PAGE_TIMEOUT_S, BACKGROUND_TIMEOUT_S = 1800.0, 28800.0
REQUEST_TIMEOUT_S, PING_TIMEOUT_S = 15, 1
_WRITE = threading.Lock()


class Refused(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def use_keychain(p: dict | None = None) -> bool:
    return Path(providers.SECURITY).exists() and (p is None or any(s.startswith("keychain:") for s in p["key_sources"]))


def _pick(name=None) -> dict:
    if name is None:
        saved = providers.setting("DEEPCREW_PROVIDER")
        name = saved if saved in providers.known() else DEFAULT_PROVIDER
    if name not in providers.known():
        raise Refused(f"unknown provider {name!r}; known: {', '.join(providers.known())}")
    return providers.load(name)


class TeamLink:
    def __init__(self, team: str, team_dir: Path):
        self.team, self.dir = team, Path(team_dir)
        self.events = self.dir / "run" / EVENTS_FILE

    def sync(self, keys) -> dict:
        from . import settings
        if not (self.dir / "team.json").exists():
            return {"team": self.team, "changed": {}}
        try:
            return settings.sync_team(Board(self.dir), keys)
        except (OSError, ValueError, KeyError, providers.ProviderError) as e:
            return {"team": self.team, "changed": {}, "error": providers.shown(f"{type(e).__name__}: {e}")}

    def board(self) -> Board:
        return Board(self.dir)

    def record(self, event: dict) -> None:
        self.events.parent.mkdir(parents=True, exist_ok=True)
        with open(self.events, "ab") as f:
            f.write((json.dumps(event, ensure_ascii=False) + "\n").encode())


def state(provider=None, link: TeamLink | None = None) -> dict:
    from . import settings
    p = _pick(provider)
    source = providers.key_source(p)
    return {"provider": p["name"], "providers": providers.known(), "models": list(p["models"]), "efforts": [*p["efforts"], "default"],
            "values": {k: v for k in KEYS if (v := providers.setting(k))}, "defaults": defaults(),
            "key": {"found": source is not None, "source": providers.shown(source), "where": settings.key_place(p), "endpoint": p["base_url"]},
            **presets.tables(p), "keychain": use_keychain(p), "env_file": providers.shown(providers.ENV_FILE),
            "team": link.team if link else None, "theme": theme()}


def defaults() -> dict[str, str]:
    from . import settings
    out = {f"DEEPCREW_{k.upper()}": str(v) for k, v in settings.DEFAULTS.items()}
    if not providers.setting("DEEPCREW_ALLOW") and (allowed := settings.allow_setting()):
        out["DEEPCREW_ALLOW"] = ",".join(allowed)
    if not providers.setting("DEEPCREW_SANDBOX"):
        out["DEEPCREW_SANDBOX"] = "off"
    return out


def theme() -> str:
    value = (providers.setting(THEME_KEY) or "auto").strip().lower()
    return value if value in THEMES else "auto"


def set_theme(body: dict) -> dict:
    value = body.get("theme")
    if value not in THEMES:
        raise Refused(f"theme must be one of: {', '.join(THEMES)}")
    providers.env_file_set({THEME_KEY: value})
    return {"theme": value}


def _domains(raw: str) -> str:
    if raw.lower() in ("", "none"):
        return raw.lower()
    names = [d.lower() for d in providers.split_list(raw)]
    if not names or "none" in names or not all(re.fullmatch(r"[a-z0-9.-]+", d) for d in names):
        raise Refused("DEEPCREW_SANDBOX_DOMAINS: use host names separated by commas, leave it empty for the package registries, or write none for no network")
    return ",".join(names)


def _allow(raw: str, key: str = "DEEPCREW_ALLOW") -> str:
    entries = [e for e in (" ".join(e.split()) for e in raw.split(",")) if e]
    for entry in entries:
        if policy.SHELL_META & set(entry):
            raise Refused(f"{key}: {entry!r} contains shell syntax; list plain commands such as ls or npm test")
        try:
            shlex.split(entry)
        except ValueError:
            raise Refused(f"{key}: {entry!r} has unbalanced quotes") from None
    return ",".join(entries)


def _paths(raw: str, key: str, none_ok: bool = False) -> str:
    if none_ok and raw.lower() == "none":
        return "none"
    paths = providers.split_list(raw)
    for path in paths:
        if any(ord(c) < 32 for c in path) or not (path.startswith("~") or os.path.isabs(path)):
            raise Refused(f"{key}: {path!r} is not an absolute path (or one starting with ~); separate paths with commas"
                          + (", or write none for no extra paths" if none_ok else ""))
    return ",".join(dict.fromkeys(paths))


def _tools_off(raw: str) -> str:
    from . import settings
    names = providers.split_list(raw)
    bad = [n for n in names if n not in settings.OPTIONAL_TOOLS]
    if bad:
        raise Refused(f"DEEPCREW_TOOLS_OFF: {', '.join(bad)} cannot be switched off; the tools that can: {', '.join(settings.OPTIONAL_TOOLS)}")
    return ",".join(dict.fromkeys(names))


def checked(values) -> dict[str, str]:
    if not isinstance(values, dict):
        raise Refused("values must be an object of DEEPCREW_* settings")
    unknown = [str(k) for k in values if k not in KEYS]
    if unknown:
        raise Refused(f"unknown setting: {', '.join(unknown)}")
    out: dict[str, str] = {}
    for key, raw in values.items():
        raw = str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else raw
        if not isinstance(raw, str):
            raise Refused(f"{key} must be text")
        out[key] = raw.strip()
    if "DEEPCREW_PROVIDER" in out and out["DEEPCREW_PROVIDER"] not in providers.known():
        raise Refused(f"DEEPCREW_PROVIDER must be one of: {', '.join(providers.known())}")
    if out.keys() & {"DEEPCREW_PROVIDER", "DEEPCREW_MODEL", "DEEPCREW_EFFORT"}:
        p = _pick(out.get("DEEPCREW_PROVIDER"))
        for key, allowed in (("DEEPCREW_MODEL", p["models"]), ("DEEPCREW_EFFORT", [*p["efforts"], "default"])):
            if key in out and out[key] not in allowed:
                raise Refused(f"{key} must be one of: {', '.join(allowed)} (provider {p['name']})")
    for key in NUMBERS:
        if key in out:
            if not (re.fullmatch(r"[0-9]{1,9}", out[key]) and int(out[key]) > 0):
                raise Refused(f"{key} must be a positive whole number")
            if key == "DEEPCREW_MAX_ACTIVE" and int(out[key]) > MAX_MEMBERS:
                raise Refused(f"{key} is at most {MAX_MEMBERS}")
            out[key] = str(int(out[key]))
    for key in ON_OFF:
        if key in out:
            out[key] = out[key].lower()
            if out[key] not in ("on", "off"):
                raise Refused(f"{key} must be on or off")
    if "DEEPCREW_SANDBOX" in out:
        out["DEEPCREW_SANDBOX"] = out["DEEPCREW_SANDBOX"].lower()
        if out["DEEPCREW_SANDBOX"] not in ("on", "off"):
            raise Refused("DEEPCREW_SANDBOX must be on or off")
    if "DEEPCREW_SANDBOX_DOMAINS" in out:
        out["DEEPCREW_SANDBOX_DOMAINS"] = _domains(out["DEEPCREW_SANDBOX_DOMAINS"])
    for key in ("DEEPCREW_ALLOW", "DEEPCREW_ALLOW_OFF"):
        if key in out:
            out[key] = _allow(out[key], key)
    if "DEEPCREW_SANDBOX_DENY_READ" in out:
        out["DEEPCREW_SANDBOX_DENY_READ"] = _paths(out["DEEPCREW_SANDBOX_DENY_READ"], "DEEPCREW_SANDBOX_DENY_READ", none_ok=True)
    if "DEEPCREW_SANDBOX_ALLOW_WRITE" in out:
        out["DEEPCREW_SANDBOX_ALLOW_WRITE"] = _paths(out["DEEPCREW_SANDBOX_ALLOW_WRITE"], "DEEPCREW_SANDBOX_ALLOW_WRITE")
    if "DEEPCREW_TOOLS_OFF" in out:
        out["DEEPCREW_TOOLS_OFF"] = _tools_off(out["DEEPCREW_TOOLS_OFF"])
    if out.keys() & {"DEEPCREW_SANDBOX", "DEEPCREW_ALLOW"}:
        from . import settings
        sandboxed = out["DEEPCREW_SANDBOX"] == "on" if out.get("DEEPCREW_SANDBOX") else settings.sandbox_on()
        for entry in providers.split_list(out["DEEPCREW_ALLOW"]) if "DEEPCREW_ALLOW" in out else settings.allow_setting():
            if why := policy.allow_refusal(entry, sandboxed):
                fix = "list plain commands such as ls or npm test" if policy.allow_refusal(entry, True) else "turn the sandbox on or remove it from the list"
                raise Refused(f"DEEPCREW_ALLOW: {why}; {fix}")
    return out


def _followed(result: dict, link: TeamLink | None, keys) -> dict:
    if link is None:
        return result
    synced = link.sync(keys) if keys else {"changed": {}}
    return {**result, "changed": synced["changed"], **({"sync_error": synced["error"]} if synced.get("error") else {})}


def save(values, link: TeamLink | None = None) -> dict:
    clean = checked(values)
    if clean:
        providers.env_file_set(clean)
    return _followed(state(link=link), link, clean)


def confirm(link: TeamLink) -> dict:
    values = state()["values"]
    synced = link.sync([k for k in values if k != "DEEPCREW_PROVIDER"])
    link.record({"ts": time.time(), "kind": "done", "values": values, "changed": synced["changed"]})
    return {"ok": True, "changed": synced["changed"], **({"sync_error": synced["error"]} if synced.get("error") else {})}


def preset(names, link: TeamLink | None = None, setup=None) -> dict:
    if setup is not None:
        if setup not in presets.SETUPS:
            raise Refused(f"unknown setup {setup!r}; setups: {', '.join(presets.SETUPS)}")
        names = list(presets.SETUPS[setup]["presets"])
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise Refused("names is a list of preset names")
    try:
        chosen = presets.values(names, _pick())
    except ValueError as e:
        raise Refused(str(e)) from None
    return save(chosen, link)


def store_key(key, provider=None) -> dict:
    if not isinstance(key, str):
        raise Refused("key must be text")
    p = _pick(provider)
    if not providers.valid_key(p, key.strip()):
        raise Refused("that does not look like a key for this provider; nothing was stored")
    (providers.store_keychain if use_keychain(p) else providers.store_env_file)(p, key.strip())
    return state(provider)


def check_key(provider=None) -> dict:
    p = _pick(provider)
    found = providers.resolve_key(p)
    if not found:
        raise Refused("no key found for this provider; save one first")
    if not p.get("balance_url"):
        return {"checked": False, "detail": "the provider has no balance endpoint to ask"}
    try:
        return providers.balance(p, found[1])
    except Exception as e:
        raise Refused(f"the balance check failed ({type(e).__name__})") from None


def _team_board(link: TeamLink | None, missing_ok: bool = False) -> Board | None:
    if link is None:
        raise Refused("no team")
    if not (link.dir / "team.json").exists():
        if missing_ok:
            return None
        raise Refused(f"no team: {link.team} has not been created yet")
    try:
        return link.board()
    except (ValueError, KeyError) as e:
        raise Refused(f"the team cannot be read ({type(e).__name__}: {e})") from None


def requests_view(link: TeamLink | None) -> dict:
    board = _team_board(link, missing_ok=True)
    if board is None:
        return {"perms": [], "questions": []}
    perms = [{"id": r["id"], "member": r.get("member", ""), "ts": r.get("ts"), "tool": r.get("tool", ""), "input": providers.shown(r.get("input") or {}),
              "widening": providers.shown(policy.widening(board, r)), "review": r.get("review"),
              **({"folder": providers.shown(r["folder"]), "write": bool(r.get("write"))} if r.get("folder") else {})}
             for r in sorted(board.pending("perms"), key=lambda r: r.get("ts") or 0)]
    questions = [{"id": r["id"], "member": r.get("member", ""), "ts": r.get("ts"), "question": providers.shown(r.get("question", "")), "task": r.get("task"),
                  "options": providers.shown(r.get("options") or [])} for r in sorted(board.pending("questions"), key=lambda r: r.get("ts") or 0)]
    return {"perms": perms, "questions": questions}


def answer_request(body: dict, link: TeamLink | None) -> dict:
    board = _team_board(link)
    rid = body.get("id")
    if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid):
        raise Refused("id must name a request, such as P1a2b3c4d or Q1a2b3c4d")
    try:
        if rid[0] == "P":
            allow, always, linked = body.get("allow"), body.get("always", False), body.get("link", False)
            if not all(isinstance(x, bool) for x in (allow, always, linked)):
                raise Refused("allow, always and link must be true or false")
            record = policy.answer_permission(board, rid, allow=allow, always=always, by="user", link=linked)
        else:
            text = body.get("text") if "option" not in body else _option(board, rid, body["option"])
            if not isinstance(text, str) or not text.strip():
                raise Refused("the answer must not be empty")
            record = board.answer("questions", rid, "answered", by="user", answer=text if "option" in body else text.strip())
    except ValueError as e:
        raise Refused(str(e)) from None
    with contextlib.suppress(OSError):
        link.record({"ts": time.time(), "kind": "answer", "id": rid, "state": record["state"]})
    return {"answered": providers.shown(record), "requests": requests_view(link)}


def _option(board: Board, rid: str, n) -> str:
    try:
        options = read_json(board.dir / "questions" / f"{rid}.json").get("options") or []
    except (OSError, ValueError, AttributeError):
        raise Refused(f"no request {rid!r}") from None
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n < len(options):
        raise Refused("option must be the number of one of the question's options")
    return str(options[n])


def team_view(link: TeamLink | None) -> dict:
    from . import member, review, settings
    board = _team_board(link, missing_ok=True)
    if board is None:
        return {"exists": False}
    keys = ("requests", "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    rows = []
    usages = []
    for m in board.members():
        u = member.usage(board, m["name"])
        usages.append(u)
        rows.append({"name": m["name"], "state": m["state"], "task": m.get("task"), "model": board.model_of(m), "region": m["region"],
                     "effort": member.member_model(board, m)[1], "thinking": board.cfg["member"].get("thinking") is not False,
                     "turn": m.get("turn") if m.get("state") in ("starting", "active") else None,
                     **{k: u.get(k, 0) for k in keys}, "cost_usd": u.get("cost_usd")})
    reviewed = review.tally(board)
    tasks, total = board.tasks(), {k: sum(r[k] for r in rows) + (reviewed["reviews"] - reviewed["no_answer"] if k == "requests" else reviewed.get(k, 0))
                                   for k in keys}
    tokens = member.claude_tokens([*usages, {k: reviewed.get(k, 0) for k in member.USAGE_KEYS}])
    saving = {"claude": {name: round(member.claude_equivalent(tokens, name), 6) for name in member.CLAUDE_PRICES}, "compare": member.DEFAULT_COMPARE}
    return {"exists": True, "members": rows, "total": {**total, "cost_usd": member.spent(board)}, "saving": saving,
            "paused": board.paused(), "folders": providers.shown(board.folders()), "sites": board.sites(), "tools": providers.shown(tools_dir()),
            "tasks": {s: sum(t["state"] == s for t in tasks) for s in ("open", "claimed", "done", "cancelled")},
            "setup": settings.setup_state(board), "model": board.cfg["member"]["model"], "budget": member.budget_state(board),
            "prices": prices_view(board.cfg["provider"]), "provider": settings.provider_view(board.cfg)}


def prices_view(p: dict, now: float | None = None) -> dict | None:
    from . import inbox
    now = time.time() if now is None else now
    if (t := providers.tariff_now(p, now)) is None:
        return None
    return {**t, "text": f"{inbox.share_words(t['share'])} until {inbox.utc_at(t['until'], now)}, in {inbox.span(t['until'] - now)}, "
                         f"then {inbox.share_words(t['next'])}"}


def set_budget(body: dict, link: TeamLink | None) -> dict:
    from . import member, route
    board = _team_board(link)
    usd = body.get("usd")
    if usd is not None:
        if isinstance(usd, bool) or not isinstance(usd, int | float) or not math.isfinite(usd) or not 0.01 <= usd <= MAX_BUDGET_USD:
            raise Refused(f"a budget is an amount in USD from 0.01 to {MAX_BUDGET_USD:,.0f}")
        usd = round(float(usd), 2)
    now, before = time.time(), member.spent(board)
    board.update_cfg(lambda cfg: cfg.__setitem__("budget", {"usd": usd, "by": "user", "ts": now, "spent": before}))
    rest = board.paused()
    if not member.check_budget(board) and rest and rest.get("by") == "budget":
        route.resume(board, by="user", wait=False, budget=usd)
    with contextlib.suppress(OSError):
        link.record({"ts": now, "kind": "budget", "usd": usd})
    return team_view(link)


def crew_action(body: dict, link: TeamLink | None) -> dict:
    from . import route
    board = _team_board(link)
    action = body.get("action")
    if action not in ("rest", "resume"):
        raise Refused('action must be "rest" or "resume"')
    if action == "rest":
        route.rest(board, by="user", wait=False)
    else:
        try:
            route.resume(board, by="user", wait=False)
        except ValueError as e:
            raise Refused(str(e)) from None
    with contextlib.suppress(OSError):
        link.record({"ts": time.time(), "kind": "crew", "action": action})
    return team_view(link)


class Extension:
    name = ""
    html = b""

    def __init__(self):
        self.routes: dict = {}
        self.streams: dict = {}

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def load_extensions(project: Path, team: str | None) -> list[Extension]:
    found = []
    for spec, obj in extensions.loaded(project):
        if not callable(page := getattr(obj, "page", None)):
            continue
        try:
            ext = page(project, team)
        except Exception as e:
            print(f"{providers.NAME} ui: extension {spec} skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        if ext is not None:
            found.append(ext)
    return found


def tab_names(project=None) -> list[str]:
    return sorted(extensions.label(spec) for spec, obj in extensions.loaded(project)
                  if callable(page := getattr(obj, "page", None)) and getattr(page, "tab", True))


def _mount(exts: list[Extension]) -> tuple[list[Extension], dict, dict]:
    kept, routes, streams = [], {}, {}
    for ext in exts:
        prefix = f"/api/{ext.name}/"
        own = [k[1] for k in ext.routes if isinstance(k, tuple) and len(k) == 2 and k[0] in ("GET", "POST") and isinstance(k[1], str)]
        taken = {path for _, path in ROUTES} | {path for _, path in routes} | set(streams)
        if not isinstance(ext.name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", ext.name):
            why = "a name is lowercase letters, digits and dashes, starting with a letter"
        elif ext.name in (e.name for e in kept):
            why = "another extension has this name"
        elif len(own) < len(ext.routes) or not all(isinstance(p, str) and p.startswith(prefix) for p in [*own, *ext.streams]) \
                or set(ext.streams) & set(own) or taken & {*own, *ext.streams}:
            why = f"routes must be GET or POST under {prefix}, and a stream's path is not also a route's"
        else:
            kept.append(ext)
            routes.update(ext.routes)
            streams.update(ext.streams)
            continue
        print(f"{providers.NAME} ui: extension {ext.name!r} skipped ({why})", file=sys.stderr)
    return kept, routes, streams


ROUTES = {
    ("GET", "/api/state"): lambda body, query, link: state(query.get("provider", [None])[0], link),
    ("POST", "/api/save"): lambda body, query, link: save(body.get("values"), link),
    ("POST", "/api/preset"): lambda body, query, link: preset(body.get("names"), link, body.get("setup")),
    ("POST", "/api/key"): lambda body, query, link: store_key(body.get("key"), body.get("provider")),
    ("POST", "/api/check"): lambda body, query, link: check_key(body.get("provider")),
    ("POST", "/api/done"): lambda body, query, link: confirm(link) if link else {"ok": True},
    ("GET", "/api/requests"): lambda body, query, link: requests_view(link),
    ("POST", "/api/answer"): lambda body, query, link: answer_request(body, link),
    ("GET", "/api/team"): lambda body, query, link: team_view(link),
    ("POST", "/api/crew"): lambda body, query, link: crew_action(body, link),
    ("POST", "/api/budget"): lambda body, query, link: set_budget(body, link),
    ("POST", "/api/theme"): lambda body, query, link: set_theme(body),
}


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, token: str, page: bytes, routes: dict, streams: dict, link: TeamLink | None = None):
        super().__init__(("127.0.0.1", port), _Handler)
        self.token, self.page, self.routes, self.streams, self.link = token, page, routes, streams, link
        self.done, self.closing = threading.Event(), threading.Event()
        self.port = self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    server_version = "deepcrew-ui"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_S

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def _serve(self, method: str) -> None:
        try:
            self._route(method)
        except Refused as e:
            self._reply(e.status, {"error": providers.shown(str(e))})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except (providers.ProviderError, OSError) as e:
            self._reply(400, {"error": providers.shown(str(e))})
        except Exception as e:
            self._reply(500, {"error": f"internal error ({type(e).__name__})"})

    def _route(self, method: str) -> None:
        if self.headers.get("Host", "") != f"127.0.0.1:{self.server.port}":
            raise Refused("wrong Host header", 403)
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if (method, url.path) == ("GET", "/"):
            if not self._token_ok(query.get("t", [""])[0]):
                raise Refused("open the page with the URL deepcrew printed", 403)
            page = self.server.page.replace(b'data-theme="auto"', f'data-theme="{theme()}"'.encode(), 1)
            return self._send(200, page, "text/html; charset=utf-8")
        if (method, url.path) == ("GET", "/favicon.ico"):
            return self._send(204, b"", "image/x-icon")
        if not url.path.startswith("/api/"):
            raise Refused("not found", 404)
        if not self._token_ok(self.headers.get("X-Deepcrew-Token", "")):
            raise Refused("missing or wrong token", 403)
        if (method, url.path) == ("POST", "/api/stop") and self.server.link is not None:
            self._reply(200, {"ok": True})
            self.server.done.set()
            return None
        if (method, url.path) == ("GET", "/api/ping"):
            return self._reply(200, {"ok": True, "pid": os.getpid(), "team": self.server.link.team if self.server.link else None})
        if method == "GET" and url.path in self.server.streams:
            return self.server.streams[url.path](self)
        route = ROUTES.get((method, url.path))
        if route is None:
            route = self.server.routes.get((method, url.path))
            if route is None:
                raise Refused("not found", 404)
            result = route(self._body() if method == "POST" else {}, query)
        else:
            body = self._body() if method == "POST" else {}
            with _WRITE:
                result = route(body, query, self.server.link)
        self._reply(200, result)
        if url.path == "/api/done" and not self.server.link:
            self.server.done.set()

    def _token_ok(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate.encode(), self.server.token.encode())

    def _body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise Refused("send the body as application/json", 415)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise Refused("Content-Length is required", 411) from None
        if length < 0:
            raise Refused("bad Content-Length")
        if length > MAX_BODY:
            left = min(length, 1 << 20)
            while left > 0 and (chunk := self.rfile.read(min(left, MAX_BODY))):
                left -= len(chunk)
            raise Refused(f"the body is larger than {MAX_BODY // 1024} KiB", 413)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw) if raw.strip() else {}
        except (ValueError, RecursionError):
            raise Refused("the body is not valid JSON") from None
        if not isinstance(data, dict):
            raise Refused("the body must be a JSON object")
        return data

    def _reply(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _head(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        headers = [("Content-Type", content_type), ("Cache-Control", "no-store"), ("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"),
                   ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer")]
        for name, value in headers + ([("Content-Length", str(length))] if length is not None else []):
            self.send_header(name, value)
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self._head(status, content_type, len(body))
        self.wfile.write(body)

    def sse(self) -> None:
        self._head(200, "text/event-stream")
        self.close_connection = True


def _open(port: int, project: Path, team: str | None, extensions: list[Extension] | None, link: TeamLink | None = None,
          token: str | None = None) -> tuple:
    exts = load_extensions(project, team) if extensions is None else list(extensions)
    exts, routes, streams = _mount(exts)
    title = "<title>" + html.escape(providers.NAME + (f" {team}" if team else "")) + "</title>"
    page = Path(__file__).with_name("ui.html").read_bytes().replace(b"<title>deepcrew</title>", title.encode(), 1)
    for ext in exts:
        page = page.replace(b"</body>", ext.html + b"</body>", 1)
    token = token or secrets.token_urlsafe(16)
    httpd = _Server(port, token, page, routes, streams, link)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return httpd, thread, exts, f"http://127.0.0.1:{httpd.port}/?t={token}"


def _close(httpd: _Server, thread: threading.Thread, started: list[Extension]) -> None:
    httpd.closing.set()
    for ext in started:
        ext.stop()
    httpd.shutdown()
    thread.join()
    httpd.server_close()


def serve(port: int = 0, open_browser: bool = False, timeout_s: float = PAGE_TIMEOUT_S, on_ready=None, project: Path | None = None, team: str | None = None,
          extensions: list[Extension] | None = None) -> dict:
    httpd, thread, exts, url = _open(port, Path(project or Path.cwd()).resolve(), team, extensions)
    started = []
    try:
        for ext in exts:
            ext.start()
            started.append(ext)
        if on_ready:
            on_ready(url)
        if open_browser:
            webbrowser.open(url)
        httpd.done.wait(timeout_s)
    finally:
        _close(httpd, thread, started)
    try:
        result = state()
    except (Refused, providers.ProviderError, OSError) as e:
        result = {"error": providers.shown(str(e))}
    return {**result, "done": httpd.done.is_set()}


def _answers(info) -> bool:
    try:
        token = parse_qs(urlsplit(info["url"]).query)["t"][0]
        conn = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=PING_TIMEOUT_S)
        try:
            conn.request("GET", "/api/ping", headers={"X-Deepcrew-Token": token})
            return conn.getresponse().status == 200
        finally:
            conn.close()
    except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException):
        return False


def running(team_dir: Path, locked: bool = False) -> dict | None:
    path = Path(team_dir) / "run" / UI_FILE
    try:
        info = read_json(path)
    except (OSError, ValueError):
        return None
    if isinstance(info, dict) and _answers(info):
        return info
    with contextlib.suppress(OSError, ValueError), contextlib.nullcontext() if locked else file_lock(path.parent / "ui"):
        if read_json(path) == info:
            path.unlink()
    return None


def stop_page(team_dir: Path) -> bool:
    info = running(team_dir)
    if not info:
        return False
    conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=PING_TIMEOUT_S)
    try:
        conn.request("POST", "/api/stop", body=b"{}", headers={"X-Deepcrew-Token": parse_qs(urlsplit(info["url"]).query)["t"][0],
                                                              "Content-Type": "application/json"})
        return conn.getresponse().status == 200
    except (OSError, KeyError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def start_background(project: Path, team: str, team_dir: Path, timeout_s: float = BACKGROUND_TIMEOUT_S) -> str:
    info = running(team_dir)
    if info:
        return info["url"]
    from . import member
    run = Path(team_dir) / "run"
    run.mkdir(parents=True, exist_ok=True)
    log = run / LOG_FILE
    with open(log, "ab") as out:
        proc = subprocess.Popen([sys.executable, "-P", "-m", "deepcrew", "ui", "--serve", "--project", str(project), "--team", team,
                                 "--timeout", str(timeout_s)], stdin=subprocess.DEVNULL, stdout=out, stderr=out, **member.detach_options())
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        info = running(team_dir)
        if info:
            return info["url"]
        if proc.poll() not in (None, 0):
            break
        time.sleep(START_POLL_S)
    raise ValueError(f"see {providers.shown(log)}")


def serve_team(project: Path, team: str, timeout_s: float) -> None:
    project = Path(project).resolve()
    team_dir = team_path(project, team)
    link = TeamLink(team, team_dir)
    (team_dir / "run").mkdir(parents=True, exist_ok=True)
    with file_lock(team_dir / "run" / "ui"):
        if running(team_dir, locked=True):
            return
        port, token = _last_address(team_dir)
        try:
            httpd, thread, exts, url = _open(port, project, team, None, link, token)
        except OSError:
            httpd, thread, exts, url = _open(0, project, team, None, link, token)
        info = {"url": url, "port": httpd.port, "pid": os.getpid(), "started": time.time()}
        path = team_dir / "run" / UI_FILE
        try:
            for name, body in ((ADDR_FILE, {"port": httpd.port, "token": httpd.token}), (UI_FILE, info)):
                tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
                providers.write_private(tmp, json.dumps(body))
                providers.replace_file(tmp, path.with_name(name))
        except BaseException:
            _close(httpd, thread, [])
            raise
    started = []
    old = None
    try:
        if threading.current_thread() is threading.main_thread():
            old = signal.signal(signal.SIGTERM, lambda *_: httpd.done.set())
        for ext in exts:
            ext.start()
            started.append(ext)
        while not httpd.done.wait(timeout_s) and _crew_works(link):
            pass
    finally:
        if old is not None:
            signal.signal(signal.SIGTERM, old)
        with contextlib.suppress(OSError, ValueError), file_lock(team_dir / "run" / "ui"):
            if read_json(path) == info:
                path.unlink()
        _close(httpd, thread, started)


def _last_address(team_dir: Path) -> tuple[int, str | None]:
    try:
        last = read_json(Path(team_dir) / "run" / ADDR_FILE)
        port, token = int(last["port"]), last["token"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0, None
    return (port, token) if 0 < port < 65536 and isinstance(token, str) and re.fullmatch(r"[\w-]{16,64}", token) else (0, None)


def _crew_works(link: TeamLink) -> bool:
    try:
        return any(m.get("state") in ("starting", "active") for m in link.board().members())
    except (OSError, ValueError, KeyError):
        return False


def wait_done(team_dir: Path, timeout_s: float) -> dict | None:
    path = Path(team_dir) / "run" / EVENTS_FILE
    deadline = time.monotonic() + timeout_s
    pos, tail = path.stat().st_size if path.exists() else 0, b""
    while True:
        with contextlib.suppress(OSError):
            size = path.stat().st_size
            if size < pos:
                pos, tail = 0, b""
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            pos += len(chunk)
            *lines, tail = (tail + chunk).split(b"\n")
            for line in lines:
                with contextlib.suppress(ValueError):
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("kind") == "done":
                        return event
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
