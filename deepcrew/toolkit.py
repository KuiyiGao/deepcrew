from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import urllib.parse
from pathlib import Path

from . import extensions, policy, providers
from .board import lock_file, unlock_file

FILE = "toolkit.json"
SNAPSHOTS = "skills"
VALID_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?")
OWN = "deepcrew"
STDIO_KEYS = frozenset({"type", "command", "args", "env"})
REMOTE_KEYS = frozenset({"type", "url", "headers"})
MAX_PIN_BYTES = 32 * 1024 * 1024
MAX_SKILL_FILES, MAX_SKILL_BYTES = 500, 5 * 1024 * 1024
IGNORED = frozenset({"__pycache__", ".DS_Store"})
DROPPED_KEYS = ("allowed-tools", "hooks")
SECRET_WORD = re.compile(r"(?i)(?:^|[^a-z0-9])(?:key|apikey|token|secret|password|passwd|pwd|credential|auth|bearer)(?:[^a-z0-9]|$)")
SKILL_DIR_VARIABLE = "${CLAUDE_SKILL_DIR}"
REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
TOP_KEY = re.compile(r"([A-Za-z_][\w-]*)[ \t]*:(.*)$")
FENCE = "---"
_said: set[tuple[str, str, str]] = set()


def _name(name, what: str, reserved: tuple[str, ...] = (OWN,)) -> str:
    if not isinstance(name, str) or not VALID_NAME.fullmatch(name) or "__" in name:
        raise ValueError(f"the name of {what} is 1-64 characters of letters, digits, '-' and '_', with a letter or digit at each end and no '__': {name!r}")
    if name.lower() in reserved:
        raise ValueError(f"{name!r} is the name of {providers.NAME} itself: choose another name")
    return name


def _section(project, section: str) -> dict[str, dict]:
    mine = extensions._read(FILE).get(extensions.project_key(project))
    got = mine.get(section) if isinstance(mine, dict) else None
    return {n: e for n, e in sorted(got.items()) if VALID_NAME.fullmatch(n) and isinstance(e, dict)} if isinstance(got, dict) else {}


def _put(project, section: str, entries: dict[str, dict]) -> None:
    data, key = extensions._read(FILE), extensions.project_key(project)
    mine = {k: v for k, v in (data.get(key) if isinstance(data.get(key), dict) else {}).items() if k != section}
    if entries:
        mine[section] = dict(sorted(entries.items()))
    if mine:
        data[key] = mine
    else:
        data.pop(key, None)
    extensions._store(FILE, data)


def _sha(path) -> str:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            size += len(chunk)
            if size > MAX_PIN_BYTES:
                raise ValueError(f"{providers.shown(path)} is larger than {MAX_PIN_BYTES >> 20} MB")
            digest.update(chunk)
    return digest.hexdigest()


def _say(project, what: str, line: str) -> None:
    key = (extensions.project_key(project), what, line)
    if key not in _said:
        _said.add(key)
        print(line, file=sys.stderr)


def _left_out(project, what: str, name: str, why: str) -> None:
    _say(project, f"{what} {name}", f"{providers.NAME}: {what} {name} is left out for the members: {why}. Add it again with "
                                   f"`{providers.NAME} {'mcp' if what == 'MCP server' else 'skill'} {name}`.")


def _string_map(value) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())


def _check_server(name: str, server) -> dict:
    if not isinstance(server, dict):
        raise ValueError(f"the definition of {name!r} is not a JSON object")
    kind = server.get("type", "stdio")
    if kind == "stdio":
        keys, shape = STDIO_KEYS, "a command takes only command, args and env"
        if "url" in server:
            raise ValueError(f"{name!r} has a url, which only a remote server takes: it says \"type\": \"http\" or \"sse\"")
    elif kind in ("http", "sse"):
        keys, shape = REMOTE_KEYS, "a remote server takes only type, url and headers"
    else:
        raise ValueError(f"{name!r} has the type {kind!r}: only stdio, http and sse servers are supported")
    if extra := sorted(set(server) - keys):
        raise ValueError(f"{name!r} has {', '.join(map(repr, extra))}, which members do not get ({shape})")
    if kind == "stdio":
        if not isinstance(server.get("command"), str) or not server["command"].strip():
            raise ValueError(f"{name!r} has no command to start")
        if not isinstance(server.get("args", []), list) or not all(isinstance(a, str) for a in server.get("args", [])):
            raise ValueError(f"the args of {name!r} are a list of strings")
        if "env" in server and not _string_map(server["env"]):
            raise ValueError(f"the env of {name!r} maps names to strings")
    else:
        url = urllib.parse.urlsplit(server["url"]) if isinstance(server.get("url"), str) else None
        if url is None or url.scheme not in ("http", "https") or not url.netloc:
            raise ValueError(f"the url of {name!r} is an http or https address")
        if "headers" in server and not _string_map(server["headers"]):
            raise ValueError(f"the headers of {name!r} map names to strings")
    return json.loads(json.dumps(server))


def _json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _table(data, *via: str) -> dict:
    for key in via:
        data = data.get(key) if isinstance(data, dict) else None
    table = data.get("mcpServers") if isinstance(data, dict) else None
    return table if isinstance(table, dict) else {}


def _places(project) -> list[tuple[dict, str]]:
    root = Path(extensions.project_key(project))
    mine, theirs = _json_file(root / ".mcp.json"), _json_file(Path.home() / ".claude.json")
    keys = dict.fromkeys(k for p in (root, Path(os.path.abspath(project or os.getcwd()))) for k in (str(p), str(p).replace("\\", "/")))
    return [(_table(mine), "the project's .mcp.json"), (_table(theirs), "your Claude Code settings (~/.claude.json)"),
            *((_table(theirs, "projects", k), "your Claude Code settings for this project (~/.claude.json)") for k in keys)]


def _found(project, name: str, source: str | None) -> tuple[dict, str]:
    if source:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError(f"{providers.shown(path)} is not a file")
        data = _json_file(path)
        if data is None:
            raise ValueError(f"{providers.shown(path)} is not JSON")
        table = _table(data)
        if name not in table:
            raise ValueError(f"{providers.shown(path)} has no MCP server named {name!r} in \"mcpServers\""
                             + (f" (it has {', '.join(sorted(table))})" if table else ""))
        return table[name], providers.shown(path)
    places = _places(project)
    for table, where in places:
        if name in table:
            return table[name], where
    known = sorted({n for table, _ in places for n in table})
    raise ValueError(f"no MCP server named {name!r} in the project's .mcp.json or in the mcpServers of ~/.claude.json"
                     + (f" (they have {', '.join(known)})" if known else "") + f". From another file: {providers.NAME} mcp {name} --from FILE")


def _inside(place: str, root: Path) -> bool:
    try:
        return extensions._in_project(place, root)
    except (OSError, ValueError):
        return False


def _explicit(token: str) -> bool:
    return token.startswith(("./", "../", ".\\", "..\\")) or os.path.isabs(token)


def _relative(root: Path, place: str) -> str | None:
    written = Path(place)
    for path in (written, written.resolve()):
        if root in path.parents:
            return path.relative_to(root).as_posix()
    return None


def _references(server: dict) -> list[str]:
    from .member import PASS_ENV
    parts = [server.get("command", ""), server.get("url", ""), *server.get("args", []), *server.get("env", {}).values(), *server.get("headers", {}).values()]
    return sorted({n for part in parts for n in REFERENCE.findall(part) if n not in PASS_ENV})


def _pins(project, server: dict) -> tuple[dict[str, str], list[str]]:
    if server.get("type") in ("http", "sse"):
        return {}, []
    root = Path(extensions.project_key(project))
    args = server.get("args", [])
    pins, notes = {}, []
    for token in [server["command"], *args, *(a.partition("=")[2] for a in args if a.startswith("-") and "=" in a)]:
        place = os.path.abspath(os.path.join(root, token)) if token and "\0" not in token else ""
        if not place or not _inside(place, root):
            continue
        if os.path.isfile(place):
            try:
                pins[_relative(root, place) or place] = _sha(place)
            except OSError as e:
                raise ValueError(f"cannot read {providers.shown(place)} to pin it ({e.strerror or e})") from None
        elif _explicit(token) and not os.path.exists(place):
            if token == server["command"]:
                raise ValueError(f"{token} is not there: a command inside the project has to exist when the server is added, so that it can be pinned")
            notes.append(f"{token} is not there yet, so it is not pinned: add the server again once it exists")
    return dict(sorted(pins.items())), notes


def _secret_name(name: str) -> bool:
    return bool(SECRET_WORD.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)))


def _shown_args(args: list[str]) -> list[str]:
    out, hide = [], False
    for arg in args:
        flag, eq, _ = arg.partition("=")
        if hide:
            out.append("<hidden>")
            hide = False
        elif _secret_name(flag) and (arg.startswith("-") or eq):
            out.append(f"{flag}=<hidden>" if eq else arg)
            hide = not eq
        else:
            out.append(re.sub(r"(?<=://)[^/@\s]*@", "<hidden>@", arg))
    return out


def _word(text: str) -> str:
    return text if text and not re.search(r"[\s'\"]", text) else shlex.quote(text)


def _command(server) -> str:
    try:
        server = _check_server("server", server)
    except ValueError:
        return "(not a valid definition)"
    if server.get("type") in ("http", "sse"):
        url = urllib.parse.urlsplit(server["url"])
        return f"{server['type']} {url.scheme}://{url.netloc.rpartition('@')[2]}{url.path}"
    return " ".join(map(_word, [server["command"], *_shown_args(server.get("args", []))]))


def _problem(project, name: str, entry: dict) -> str | None:
    if name.lower() == OWN:
        return f"{providers.NAME}'s own server has that name"
    try:
        _check_server(name, entry.get("server"))
    except ValueError as e:
        return f"its definition is not valid ({e})"
    pins = entry.get("pins")
    if not isinstance(pins, dict):
        return "its pins are not valid"
    root = Path(extensions.project_key(project))
    for rel, want in sorted(pins.items()):
        try:
            got = _sha(root / rel)
        except FileNotFoundError:
            return f"{rel} is gone"
        except (OSError, ValueError):
            return f"{rel} cannot be read"
        if got != want:
            return f"{rel} changed since the server was added"
    return None


def _row(project, name: str, entry: dict) -> dict:
    why = _problem(project, name, entry)
    pins = entry.get("pins")
    return {"name": name, "command": _command(entry.get("server")), "pinned": sorted(pins) if isinstance(pins, dict) else [], "ok": why is None, "why": why}


def add_mcp(project, name: str, source: str | None = None) -> dict:
    _name(name, "an MCP server")
    server, where = _found(project, name, source)
    server = _check_server(name, server)
    pins, notes = _pins(project, server)
    notes += [f"${{{n}}} is not in the members' environment, so the server gets that text as it stands: write the value in the definition"
              for n in _references(server)]
    entries = _section(project, "mcp")
    entries[name] = {"server": server, "pins": pins, "source": where}
    _put(project, "mcp", entries)
    return {**_row(project, name, entries[name]), "source": where, "notes": notes}


def remove_mcp(project, name: str) -> None:
    entries = _section(project, "mcp")
    if name not in entries:
        raise ValueError(f"no MCP server named {name!r} is given to this project's teams" + (f" (they have {', '.join(entries)})" if entries else ""))
    del entries[name]
    _put(project, "mcp", entries)


def mcp_list(project) -> list[dict]:
    return [_row(project, n, e) for n, e in _section(project, "mcp").items()]


def mcp_names(project) -> list[str]:
    return list(_section(project, "mcp"))


def mcp_candidates(project) -> list[dict]:
    places = _places(project)
    given, found = set(mcp_names(project)), {}
    for table, where in places:
        for name, server in table.items():
            if name not in found and name != OWN and isinstance(name, str) and VALID_NAME.fullmatch(name) and isinstance(server, dict):
                found[name] = {"name": name, "where": where, "command": _command(server), "given": name in given}
    return [found[n] for n in sorted(found)]


def mcp_servers(project) -> dict[str, dict]:
    out = {}
    for name, entry in _section(project, "mcp").items():
        if why := _problem(project, name, entry):
            _left_out(project, "MCP server", name, why)
        else:
            out[name] = json.loads(json.dumps(entry["server"]))
    return out


def _split(text: str) -> tuple[list[str], str] | None:
    lines = text.lstrip("\ufeff").splitlines(keepends=True)
    if lines and lines[0].rstrip() == FENCE:
        for i in range(1, len(lines)):
            if lines[i].rstrip() == FENCE:
                return lines[1:i], "".join(lines[i + 1:])
    return None


def _unquoted(value: str) -> str:
    if len(value) > 1 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    return value[1:-1].replace("''", "'") if len(value) > 1 and value[0] == value[-1] == "'" else value


def _values(front: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    key, first, rest = None, "", []

    def close() -> None:
        if key is not None:
            lines = [x.strip() for x in rest if x.strip()]
            if first[:1] in ("|", ">"):
                out[key] = ("\n" if first[0] == "|" else " ").join(lines)
            else:
                out[key] = _unquoted(" ".join([first, *lines]).strip())
    for raw in front:
        line = raw.rstrip("\r\n")
        m = TOP_KEY.match(line)
        if m:
            close()
            key, first, rest = m.group(1), m.group(2).strip(), []
        elif not line.lstrip().startswith("#"):
            rest.append(line)
    close()
    return out


def _described(text: str) -> str:
    split = _split(text)
    first = next((x.strip().lstrip("#").strip() for x in (split[1] if split else text).splitlines() if x.strip()), "")
    return (_values(split[0]) if split else {}).get("description") or first


def _members_front(front: list[str]) -> tuple[list[str], list[str]]:
    kept, dropped, dropping = [], [], False
    for raw in front:
        m = TOP_KEY.match(raw.rstrip("\r\n"))
        if m:
            dropping = m.group(1).lower() in DROPPED_KEYS
            if dropping and m.group(1).lower() not in dropped:
                dropped.append(m.group(1).lower())
        if not dropping:
            kept.append(raw)
    return kept, dropped


def _for_members(text: str, folder: Path) -> tuple[str, list[str]]:
    line = f"This skill's files are in {folder}: read and run them from there.\n\n"
    split = _split(text)
    if split is None:
        return (line + text).replace(SKILL_DIR_VARIABLE, str(folder)), []
    kept, dropped = _members_front(split[0])
    return f"{FENCE}\n{''.join(kept)}{FENCE}\n{line}{split[1]}".replace(SKILL_DIR_VARIABLE, str(folder)), dropped


def _unreadable(error: OSError) -> None:
    raise error


def _scan(folder: Path) -> tuple[list[tuple[str, str]], int]:
    real = os.path.realpath(folder)
    found: list[tuple[str, str]] = []
    total = 0
    try:
        for top, dirs, names in os.walk(folder, onerror=_unreadable):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED)
            for entry in sorted([*dirs, *(n for n in names if n not in IGNORED)]):
                path = os.path.join(top, entry)
                rel = Path(os.path.relpath(path, folder)).as_posix()
                if os.path.islink(path):
                    if not policy.inside(os.path.realpath(path), real):
                        raise ValueError(f"{rel} is a link that leads outside the skill: put the file itself in the skill folder")
                    found.append((rel, "link"))
                elif os.path.isdir(path):
                    continue
                else:
                    info = os.lstat(path)
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError(f"{rel} is not a plain file")
                    found.append((rel, "file"))
                    total += info.st_size
                if len(found) > MAX_SKILL_FILES or total > MAX_SKILL_BYTES:
                    raise ValueError(f"a skill is at most {MAX_SKILL_FILES} files and {MAX_SKILL_BYTES >> 20} MB: {providers.shown(folder)} has more")
    except OSError as e:
        raise ValueError(f"cannot read {providers.shown(e.filename or folder)} ({e.strerror or e})") from None
    return found, total


def _copy(src: Path, entries: list[tuple[str, str]], dst: Path) -> None:
    real = os.path.realpath(src)
    for rel, kind in entries:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "link":
            inner = os.path.relpath(os.path.realpath(src / rel), real)
            os.symlink(os.path.relpath(dst / inner, target.parent), target)
        else:
            shutil.copy2(src / rel, target)


def _replace_dir(stage: Path, dest: Path) -> None:
    aside = dest.with_name(f".{dest.name}.{os.getpid()}.old")
    shutil.rmtree(aside, ignore_errors=True)
    if dest.exists():
        os.replace(dest, aside)
    os.replace(stage, dest)
    shutil.rmtree(aside, ignore_errors=True)


def _digest(folder: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        for rel, kind in _scan(folder)[0]:
            if kind == "link":
                digest.update(f"link {rel} {os.readlink(folder / rel)}\n".encode())
            else:
                digest.update(f"file {rel} {int(bool(os.stat(folder / rel).st_mode & 0o111))} {_sha(folder / rel)}\n".encode())
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


def _snapshot(project, name: str) -> Path:
    return providers.CONFIG_DIR / SNAPSHOTS / hashlib.sha256(extensions.project_key(project).encode()).hexdigest()[:12] / name


def _skill_folder(project, name: str, source: str | None) -> tuple[Path, str]:
    if source:
        base = Path(source).expanduser()
        for folder in (base, base / name):
            if (folder / "SKILL.md").is_file():
                return folder, providers.shown(folder)
        raise ValueError(f"no SKILL.md in {providers.shown(base)} or in {providers.shown(base / name)}")
    root = Path(extensions.project_key(project))
    for folder, where in ((root / ".claude" / "skills", "the project's .claude/skills"), (Path.home() / ".claude" / "skills", "~/.claude/skills")):
        if (folder / name / "SKILL.md").is_file():
            return folder / name, f"{where}/{name}"
    raise ValueError(f"no skill named {name!r} with a SKILL.md in the project's .claude/skills or in ~/.claude/skills. From another folder: "
                     f"{providers.NAME} skill {name} --from DIR")


def _skill_text(folder: Path) -> str:
    try:
        return (folder / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"cannot read {providers.shown(folder / 'SKILL.md')} as text ({e})") from None


def _skill_row(project, name: str, entry: dict) -> dict:
    files = entry.get("files") if isinstance(entry.get("files"), int) else 0
    try:
        description = _described((_snapshot(project, name) / "SKILL.md").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {"name": name, "description": "", "files": files, "ok": False, "why": "its copy in the settings folder is gone or unreadable"}
    return {"name": name, "description": description, "files": files, "ok": True, "why": None}


def add_skill(project, name: str, source: str | None = None) -> dict:
    _name(name, "a skill", (OWN, providers.NAME.lower()))
    folder, where = _skill_folder(project, name, source)
    text = _skill_text(folder)
    entries, size = _scan(folder)
    snap = _snapshot(project, name)
    stage = snap.with_name(f".{name}.{os.getpid()}.tmp")
    try:
        providers.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(providers.CONFIG_DIR, 0o700)
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        _copy(folder, entries, stage)
        _replace_dir(stage, snap)
    except OSError as e:
        shutil.rmtree(stage, ignore_errors=True)
        raise ValueError(f"cannot copy {providers.shown(folder)} ({e.strerror or e})") from None
    kept = _section(project, "skills")
    kept[name] = {"files": len(entries), "bytes": size, "source": where}
    _put(project, "skills", kept)
    split = _split(text)
    return {**_skill_row(project, name, kept[name]), "source": where, "bytes": size, "dropped": _members_front(split[0])[1] if split else []}


def remove_skill(project, name: str) -> None:
    kept = _section(project, "skills")
    if name not in kept:
        raise ValueError(f"no skill named {name!r} is given to this project's teams" + (f" (they have {', '.join(kept)})" if kept else ""))
    del kept[name]
    _put(project, "skills", kept)
    snap = _snapshot(project, name)
    shutil.rmtree(snap, ignore_errors=True)
    if snap.parent.is_dir() and not any(snap.parent.iterdir()):
        snap.parent.rmdir()


def skill_list(project) -> list[dict]:
    return [_skill_row(project, n, e) for n, e in _section(project, "skills").items()]


def skill_names(project) -> list[str]:
    return list(_section(project, "skills"))


def skill_candidates(project) -> list[dict]:
    root = Path(extensions.project_key(project))
    given, found = set(skill_names(project)), {}
    for base, where in ((root / ".claude" / "skills", "the project's .claude/skills"), (Path.home() / ".claude" / "skills", "~/.claude/skills")):
        try:
            names = sorted(e.name for e in os.scandir(base) if e.is_dir())
        except OSError:
            continue
        for name in names:
            if name in found or name in (OWN, providers.NAME.lower()) or not VALID_NAME.fullmatch(name) or not (base / name / "SKILL.md").is_file():
                continue
            try:
                description = _described((base / name / "SKILL.md").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                description = ""
            found[name] = {"name": name, "where": f"{where}/{name}", "description": description, "given": name in given}
    return [found[n] for n in sorted(found)]


def _install(board, name: str, shared: Path, state: Path) -> None:
    snap = _snapshot(board.root, name)
    if not (snap / "SKILL.md").is_file():
        raise ValueError("its copy in the settings folder is gone")
    text = _skill_text(snap)
    here = shared / name
    if not here.is_dir() or _digest(here) != _digest(snap):
        stage = shared / f".{name}.{os.getpid()}.tmp"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        _copy(snap, _scan(snap)[0], stage)
        _replace_dir(stage, here)
    wanted = _for_members(text, here)[0]
    target = state / name / "SKILL.md"
    if not target.is_file() or target.read_text(encoding="utf-8") != wanted:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".SKILL.md.{os.getpid()}.tmp")
        tmp.write_text(wanted, encoding="utf-8")
        providers.replace_file(tmp, target)


def _swept(places: tuple[Path, ...], names: list[str]) -> None:
    for place in places:
        if place.is_dir():
            for child in place.iterdir():
                if child.is_dir() and VALID_NAME.fullmatch(child.name) and child.name not in names:
                    shutil.rmtree(child, ignore_errors=True)
            if not any(place.iterdir()):
                place.rmdir()


def install_skills(board) -> None:
    names = skill_names(board.root)
    shared, state = board.shared_dir / "skills", board.dir / "claude" / "skills"
    if not names and not (shared.is_dir() or state.is_dir()):
        return
    try:
        with open(board.dir / "run" / "skills.lock", "ab") as lock:
            lock_file(lock)
            try:
                for name in names:
                    try:
                        _install(board, name, shared, state)
                    except ValueError as e:
                        _left_out(board.root, "skill", name, providers.shown(str(e)))
                    except OSError as e:
                        _left_out(board.root, "skill", name, f"its copy cannot be put in the team's folders ({providers.shown(str(e))})")
                _swept((shared, state), names)
            finally:
                unlock_file(lock)
    except OSError as e:
        _say(board.root, "skills", f"{providers.NAME}: the skills cannot be put in the team's folders ({providers.shown(str(e))}). Members start without them.")
