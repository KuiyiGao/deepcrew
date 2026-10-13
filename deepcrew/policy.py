from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from pathlib import Path

from . import providers

from .paths import covers, fold, git_bash_native, ignores_case, inside, norm, norm_all, overlap, pathmod, relpath, rule_path, within
from .shell import SHELL_META, allow_refusal, allowlisted, joined_parts, parts, program, runs_code

READ_TOOLS = ("Read", "Glob", "Grep")
SECRET_PATHS = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.netrc", "~/.config/gh", "~/.kube", "~/.docker")
CLOSED_TO_LINKS = (*SECRET_PATHS, "~/.claude", "~/.config/claude", "~/Library/Keychains")
DOMAIN = re.compile(r"(?:\*\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)(?::[0-9]{1,5})?(?:[/?#][^\s\\]*)?", re.I)
TLD = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")
MCP_TOOL = re.compile(r"mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+")
OWN_TOOLS = "mcp__deepcrew__"
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
ALLOW_LOG = "allow.jsonl"


def in_sandbox(board, member: str | None = None) -> bool:
    started = (board.member(member) or {}).get("sandbox") if member else None
    return started if isinstance(started, bool) else bool(board.live_cfg().get("sandbox"))


NULL_DEVICES = ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr")


def outside(board, command: str, member: str | None = None) -> list[str]:
    found, root = {}, os.path.realpath(board.root)
    cwds, sandboxed, before = {root}, in_sandbox(board, member), ""

    def away(path: str) -> bool:
        return not (inside(path, root) or board.in_shared(path) or board.reachable(path))

    for words, after in joined_parts(command) or []:
        if words[0] == "cd":
            targets = [w for w in words[1:] if not w.startswith("-") or w == "-"]
            if len(targets) != 1 or targets[0] == "-":
                found[" ".join(targets) or "~"] = True
            else:
                joined = [os.path.join(c, os.path.expanduser(targets[0])) for c in cwds]
                if any(away(os.path.realpath(j)) or away(os.path.normpath(j)) for j in joined):
                    found[targets[0]] = True
                moved = {os.path.realpath(j) for j in joined if os.path.isdir(j)}
                lasting = moved and len(moved) == len(joined) and "|" not in (before, after) and after != "||"
                cwds = moved if lasting else cwds | moved
        else:
            for word in words[1:]:
                for arg in (word, word.partition("=")[2] if word.startswith("-") else ""):
                    if arg in NULL_DEVICES:
                        continue
                    where = arg
                    if providers.is_windows() and arg.startswith("/") and not arg.startswith("//"):
                        where = git_bash_native(arg)
                        if where is None:
                            found[arg] = True
                            continue
                    shaped = where.startswith("~") or pathmod().isabs(where) or ".." in where.replace("\\", "/").split("/")
                    for c in cwds if where else ():
                        full = os.path.join(c, os.path.expanduser(where))
                        there = os.path.exists(full)
                        if (there or shaped and not sandboxed) and away(os.path.realpath(full)):
                            found[arg] = True
        before = after
    return list(found)


def protected(board) -> set[str]:
    from .board import state_home
    return providers.spellings(state_home()) | providers.spellings(board.dir) | {
        place for p in providers.team_tables(board.cfg) for place in providers.secret_locations(p)}


def url_host(url) -> str | None:
    found = URL_HOST.fullmatch(url.strip()) if isinstance(url, str) else None
    host = found.group(1).lower().removesuffix(".") if found else ""
    return host if DOMAIN.fullmatch(host) else None


def site_covers(site: str, host: str) -> bool:
    site = site.lower()
    return host.endswith(site[1:]) if site.startswith("*.") else host == site


SANDBOX_NETWORK = "SandboxNetworkAccess"


def plain_host(host) -> str | None:
    name = host.strip().lower().removesuffix(".") if isinstance(host, str) else ""
    return name if DOMAIN.fullmatch(name) and not name.startswith("*") else None


def decide(board, member: str, tool: str, tool_input: dict) -> dict | None:
    blob, places = json.dumps(tool_input, ensure_ascii=False), protected(board)
    if ignores_case():
        blob, places = blob.casefold(), {p.casefold() for p in places}
    if any(place and place in blob for place in places):
        return {"behavior": "deny", "message": "this call names a location that holds team state or keys; members may not use it"}
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if tool in READ_TOOLS + EDIT_TOOLS and isinstance(path, str) and path:
        rel, absolute = relpath(board.root, path), path if os.path.isabs(path) else str(board.root / path)
        is_root = fold(os.path.realpath(absolute)) == fold(os.path.realpath(board.root))
        if rel is None and not board.in_shared(absolute) and not (is_root and tool in READ_TOOLS):
            if is_root:
                return {"behavior": "deny", "message": f"{path} is the project folder itself"}
            if board.reachable(absolute, write=tool in EDIT_TOOLS):
                return {"behavior": "allow", "updatedInput": tool_input}
            if board.reachable(absolute):
                return {"behavior": "deny", "message": f"{path} is in a folder linked for reading only"}
            return None
        if tool in EDIT_TOOLS:
            area = board.write_area(member)
            if not ((rel is not None and covers(area, rel)) or board.own_file(member, absolute)):
                return {"behavior": "deny", "message": f"{path} is outside your region {json.dumps(area)}"}
        return {"behavior": "allow", "updatedInput": tool_input}
    cfg = board.live_cfg()
    command = tool_input.get("command", "")
    if (tool == "Bash" and allowlisted(command, cfg["bash_allow"], in_sandbox(board, member), set(cfg.get("exact_allow", [])) - set(cfg.get("allow_off", [])))
            and not outside(board, command, member)):
        return {"behavior": "allow", "updatedInput": tool_input}
    if tool == "WebFetch" and (host := url_host(tool_input.get("url"))) and any(site_covers(s, host) for s in board.sites()):
        return {"behavior": "allow", "updatedInput": tool_input}
    if tool == SANDBOX_NETWORK and (host := plain_host(tool_input.get("host"))) and any(site_covers(s, host) for s in board.sites()):
        return {"behavior": "allow", "updatedInput": tool_input}
    if isinstance(tool, str) and tool.startswith("mcp__") and not tool.startswith(OWN_TOOLS) and tool in listed_tools(cfg):
        return {"behavior": "allow", "updatedInput": tool_input}
    return None


def listed_tools(cfg: dict) -> list[str]:
    found = cfg.get("tool_allow")
    return [t for t in found if isinstance(t, str)] if isinstance(found, list) else []


def verdict(r: dict, tool_input: dict, timeout: float) -> dict:
    if r["state"] == "allowed":
        return {"behavior": "allow", "updatedInput": tool_input}
    if r["state"] == "expired":
        return {"behavior": "deny", "message": f"no answer from the leader within {timeout} s; denied"}
    return {"behavior": "deny", "message": f"the {r.get('by') or 'leader'} denied this call"}


_ASKS: dict[tuple[str, str], list] = {}
_ASKS_GUARD = threading.Lock()


@contextlib.contextmanager
def one_ask(board, tool_use_id: str | None):
    if not tool_use_id:
        yield
        return
    key = (str(board.dir), tool_use_id)
    with _ASKS_GUARD:
        entry = _ASKS.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _ASKS_GUARD:
            entry[1] -= 1
            if not entry[1]:
                del _ASKS[key]


def approve(board, member: str, tool: str, tool_input: dict, tool_use_id: str | None = None) -> dict:
    from . import review
    from .board import read_json
    now = decide(board, member, tool, tool_input)
    if now is not None:
        return now
    timeout = board.live_cfg()["approval_timeout_s"]
    with one_ask(board, tool_use_id):
        if tool_use_id:
            prior = next((r for r in (read_json(p) for p in sorted((board.dir / "perms").glob("P*.json")))
                          if r.get("tool_use_id") == tool_use_id and r["member"] == member and r["state"] != "pending"
                          and r.get("tool") == tool and r.get("input") == tool_input), None)
            if prior is not None:
                return verdict(prior, tool_input, timeout)
        asked = {"tool": tool, "input": tool_input, "tool_use_id": tool_use_id, "always": False, **folder_wanted(board, tool, tool_input, member)}
        seen = review.review(board, member, tool, tool_input)
        if seen and seen["verdict"] == "allow":
            return verdict(board.request("perms", member, {**asked, "state": "allowed", "by": "reviewer", "answered": time.time(),
                                                           "review": seen["reason"]}), tool_input, timeout)
        r = board.request("perms", member, {**asked, **({"review": seen["reason"]} if seen else {})})
        return verdict(board.wait("perms", r["id"], timeout), tool_input, timeout)


def folder_wanted(board, tool: str, tool_input: dict, member: str | None = None) -> dict:
    if tool in READ_TOOLS + EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
        found = [path] if isinstance(path, str) and path and relpath(board.root, path) is None and not board.in_shared(path) else []
    elif tool == "Bash" and isinstance(tool_input.get("command"), str):
        found = outside(board, tool_input["command"], member)
    else:
        found = []
    for raw in found:
        real = os.path.realpath(os.path.join(board.root, os.path.expanduser(raw)))
        folder = real if os.path.isdir(real) else os.path.dirname(real)
        try:
            return {"folder": check_folder(board, folder), "write": tool in EDIT_TOOLS}
        except ValueError:
            continue
    return {}


def check_folder(board, raw: str) -> str:
    real = os.path.realpath(os.path.expanduser(str(raw)))
    if not os.path.isdir(real):
        raise ValueError(f"{raw} is not a folder")
    if os.path.dirname(real) == real or fold(real) == fold(os.path.realpath(Path.home())):
        raise ValueError(f"{raw} is too wide to link: name the folder the work needs")
    if inside(real, os.path.realpath(board.root)):
        raise ValueError(f"{raw} is inside the project: members already reach it")
    from .board import state_home, tools_dir
    for name, place in (("the project", board.root), ("the team folder", board.shared_dir), ("the tools folder", tools_dir())):
        if inside(os.path.realpath(place), real):
            raise ValueError(f"{raw} holds {name}: name a folder beside it")
    for linked in board.folders():
        other = os.path.realpath(linked["path"])
        if other != real and (inside(real, other) or inside(other, real)):
            raise ValueError(f"{raw} holds or lies inside {linked['path']}, which is linked already: unlink that one first")
    keys = [os.path.dirname(os.path.expanduser(s.partition(":")[2])) for p in providers.team_tables(board.cfg) for s in p["key_sources"]
            if s.startswith("file:")]
    for place in (str(state_home()), str(board.dir), str(providers.CONFIG_DIR), *keys, *map(os.path.expanduser, CLOSED_TO_LINKS)):
        top = os.path.realpath(place)
        if inside(real, top) or inside(top, real):
            raise ValueError(f"{raw} holds or lies inside {place}, which members may not use")
    return real


def set_folder(board, raw: str, *, write: bool = False, by: str = "leader", on: bool = True) -> dict:
    real = check_folder(board, raw) if on else os.path.realpath(os.path.expanduser(str(raw)))
    entry = {"path": real, "write": bool(write), "by": by, "ts": time.time()}

    def change(cfg: dict) -> None:
        rest = [f for f in cfg.get("folders", []) if f.get("path") != real]
        if not on and len(rest) == len(cfg.get("folders", [])):
            raise ValueError(f"{raw} is not linked")
        cfg["folders"] = rest + ([entry] if on else [])
    board.update_cfg(change)
    return entry


def set_site(board, domain: str, *, by: str = "leader", on: bool = True) -> dict:
    name = str(domain).strip().lower()
    if not DOMAIN.fullmatch(name):
        raise ValueError(f"{domain!r} is not a host name such as api.example.com or *.example.com")
    entry = {"domain": name, "by": by, "ts": time.time()}

    def change(cfg: dict) -> None:
        rest = [x for x in cfg.get("sites", []) if x.get("domain") != name]
        if not on and len(rest) == len(cfg.get("sites", [])):
            raise ValueError(f"{name} is not on the team's sites")
        cfg["sites"] = rest + ([entry] if on else [])
    board.update_cfg(change)
    return entry


def set_allow(board, command: str, by: str, on: bool = True, exact: bool = False) -> str | None:
    from .board import write_json
    command = command.strip() if isinstance(command, str) else ""
    with board.locked():
        cfg = board.live_cfg()
        lists = {k: cfg.setdefault(k, []) for k in ("bash_allow", "approved_allow", "exact_allow")}
        if on:
            if why := allow_refusal(command, bool(cfg.get("sandbox")), exact):
                return why
            for key in ("exact_allow",) if exact else ("approved_allow", "bash_allow"):
                if command not in lists[key]:
                    lists[key].append(command)
            cfg["allow_off"] = [c for c in cfg.get("allow_off", []) if c != command]
        else:
            if command not in lists["bash_allow"] + lists["exact_allow"]:
                return f"{command!r} is not on the allowlist"
            for key in lists:
                cfg[key] = [c for c in lists[key] if c != command]
            if command in [*cfg.get("allow_scope", []), *cfg.get("pack_allow", [])] and command not in cfg.setdefault("allow_off", []):
                cfg["allow_off"].append(command)
        write_json(board.dir / "team.json", cfg)
        board.cfg = cfg
        with open(board.dir / "run" / ALLOW_LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": time.time(), "command": command, "by": by, "on": on, "exact": exact}, ensure_ascii=False) + "\n")
        board.bump()
    return None


def tool_refusal(tool) -> str | None:
    if not isinstance(tool, str) or not MCP_TOOL.fullmatch(tool):
        return f"{tool!r} is not the name of an MCP tool, such as mcp__github__create_issue"
    if tool.startswith(OWN_TOOLS):
        return f"{tool!r} is one of deepcrew's own tools, which members use without asking already"
    return None


def set_tool(board, tool: str, by: str, on: bool = True) -> str | None:
    from .board import write_json
    tool = tool.strip() if isinstance(tool, str) else ""
    if on and (why := tool_refusal(tool)):
        return why
    with board.locked():
        cfg = board.live_cfg()
        listed = listed_tools(cfg)
        if on:
            listed = listed if tool in listed else [*listed, tool]
        elif tool in listed:
            listed = [t for t in listed if t != tool]
        else:
            return f"{tool!r} is not on the list of tools members use without asking"
        cfg["tool_allow"] = listed
        write_json(board.dir / "team.json", cfg)
        board.cfg = cfg
        with open(board.dir / "run" / ALLOW_LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": time.time(), "kind": "tool", "tool": tool, "by": by, "on": on}, ensure_ascii=False) + "\n")
        board.bump()
    return None


def _widen(board, r: dict) -> tuple[dict | None, str]:
    tool, ti = r.get("tool"), r["input"] if isinstance(r.get("input"), dict) else {}
    if tool == "Bash":
        command = ti.get("command")
        command = command.strip() if isinstance(command, str) else ""
        if why := allow_refusal(command, bool(board.live_cfg().get("sandbox")), exact=True):
            return None, why
        if away := outside(board, command, r.get("member")):
            return None, (f"{command!r} names a place outside the project ({', '.join(away)}), so it is approved call by call"
                          + (", unless the folder is linked (--link)" if r.get("folder") else ""))
        return {"kind": "command", "value": command, "text": f"`{command}` runs without asking from now on"}, ""
    if tool in ("WebFetch", SANDBOX_NETWORK):
        host = url_host(ti.get("url")) if tool == "WebFetch" else plain_host(ti.get("host"))
        if host is None:
            return None, ("the address is not a plain http or https URL with a host name" if tool == "WebFetch"
                          else f"{ti.get('host')!r} is not a host name such as api.example.com, so it is approved call by call")
        if not TLD.fullmatch(host.rsplit(".", 1)[-1]):
            return None, f"{host} is an address, not a domain name, so it is approved call by call"
        if any(site_covers(s, host) for s in board.sites()):
            return None, f"{host} is among the team's sites already"
        return {"kind": "site", "value": host,
                "text": f"members reach {host} without asking"}, ""
    if isinstance(tool, str) and tool.startswith("mcp__"):
        if why := tool_refusal(tool):
            return None, why
        if tool in listed_tools(board.live_cfg()):
            return None, f"{tool} is on the list of tools members use without asking already"
        return {"kind": "tool", "value": tool, "text": f"members use {tool} without asking"}, ""
    return None, f"{tool} calls are answered one by one" + (" (--link opens its folder for every member)" if r.get("folder") else "")


def widening(board, r: dict) -> dict | None:
    return _widen(board, r)[0]


def widening_refusal(board, r: dict) -> str:
    return _widen(board, r)[1]


def widen(board, w: dict, by: str) -> bool:
    try:
        if w["kind"] == "command":
            return set_allow(board, w["value"], by, exact=True) is None
        if w["kind"] == "site":
            set_site(board, w["value"], by=by)
            return True
        return set_tool(board, w["value"], by) is None
    except ValueError:
        return False


def answer_permission(board, rid: str, allow: bool, always: bool = False, by: str = "leader", link: bool = False) -> dict:
    from .board import REQUEST_ID, read_json, write_json
    path = board.dir / "perms" / f"{rid}.json" if isinstance(rid, str) and REQUEST_ID.fullmatch(rid) else None
    if path is None or not path.exists():
        raise ValueError(f"no request {rid!r}")
    asked = read_json(path)
    if link:
        if not (allow and asked.get("state") == "pending" and asked.get("folder")):
            raise ValueError("only an allowed request that reaches into a folder outside the project can link it")
        check_folder(board, asked["folder"])
    w = widening(board, asked) if allow and (always or by == "user") else None
    r = board.answer("perms", rid, "allowed" if allow else "denied", by=by, always=False, widened=None)
    if link:
        set_folder(board, r["folder"], write=bool(r.get("write")), by=by)
    if w and widen(board, w, by):
        with board.locked():
            r = {**read_json(path), "always": True, "widened": w}
            write_json(path, r)
    return r
