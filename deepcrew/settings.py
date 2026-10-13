from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import member, policy, presets, prompts, providers
from .board import Board, default_shared_dir, read_json, read_jsonl, state_home, team_path, write_json

DEFAULT_TOOLS = ("Agent", "Bash", "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit", "WebFetch", "WebSearch", "TaskOutput", "TaskStop",
                 "TaskCreate", "TaskGet", "TaskList", "TaskUpdate")
OPTIONAL_TOOLS = ("Agent", "NotebookEdit", "WebFetch", "WebSearch", "TaskOutput", "TaskStop")
DEFAULTS = {"max_active": 8, "hold_s": 600, "max_turns": 200, "approval_timeout_s": 300, "question_timeout_s": 1800, "offer_size": 8,
            "note_max_chars": 20000}
MAX_MEMBERS = 32
PKG_PARENT = str(Path(__file__).resolve().parent.parent)
SANDBOX_DOMAINS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
DENY_READ = (*policy.SECRET_PATHS, "~/.claude", *(("/Volumes",) if sys.platform == "darwin" else ()))


def sandbox_on() -> bool:
    return (providers.setting("DEEPCREW_SANDBOX") or "off").lower() in ("on", "1", "true", "yes")


def sandbox_settings(provider: dict, project: Path | None = None, backups=()) -> dict | None:
    if not sandbox_on():
        return None
    root = os.path.realpath(project) if project else None
    closed = {p for p in map(_expand, listed("DEEPCREW_SANDBOX_DENY_READ", DENY_READ))
              if Path(p).exists() and not (root and policy.inside(root, os.path.realpath(p)))}
    fs = {"denyRead": sorted(set().union(*map(protected_paths, [provider, *backups])) | closed)}
    if write := sorted(map(_expand, listed("DEEPCREW_SANDBOX_ALLOW_WRITE"))):
        fs["allowWrite"] = write
    net = {"allowedDomains": listed("DEEPCREW_SANDBOX_DOMAINS", SANDBOX_DOMAINS)}
    if providers.setting("DEEPCREW_SANDBOX_LOCAL_PORTS") == "on":
        net["allowLocalBinding"] = True
    return {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": False,
            "filesystem": fs, "network": net}


def closing(box: dict, tables) -> dict:
    box = json.loads(json.dumps(box))
    fs = box.setdefault("filesystem", {})
    fs["denyRead"] = sorted({*fs.get("denyRead", []), *(place for p in tables for place in protected_paths(p))})
    return box


def protected_paths(provider: dict) -> set[str]:
    return {str(state_home()), str(providers.CONFIG_DIR)} | {str(Path(s.partition(":")[2]).expanduser())
                                                            for s in provider["key_sources"] if s.startswith("file:")}


def _expand(path: str) -> str:
    return str(Path(path).expanduser())


def listed(key: str, preset=()) -> list[str]:
    raw = (providers.setting(key) or "").strip()
    return [] if raw.lower() == "none" else providers.split_list(raw) if raw else list(preset)


def tools_setting(pack_tools=None) -> list[str]:
    off = set(listed("DEEPCREW_TOOLS_OFF")) & set(OPTIONAL_TOOLS)
    return [t for t in (pack_tools or DEFAULT_TOOLS) if t not in off]


def own_allow(cfg: dict) -> list[str]:
    return cfg.get("allow_scope", [c for c in cfg.get("bash_allow", []) if c not in cfg.get("pack_allow", [])])


def team_allow(cfg: dict) -> list[str]:
    off, sandboxed = set(cfg.get("allow_off", [])), bool(cfg.get("sandbox"))
    return [c for c in dict.fromkeys([*own_allow(cfg), *cfg.get("pack_allow", []), *cfg.get("approved_allow", [])])
            if c not in off and policy.allow_refusal(c, sandboxed) is None]


def left_out(cfg: dict) -> list[str]:
    off = set(cfg.get("allow_off", []))
    return [c for c in dict.fromkeys([*own_allow(cfg), *cfg.get("pack_allow", [])]) if c not in off and c not in cfg.get("bash_allow", [])]


def allow_fields(cfg: dict, keep_off: bool = False) -> dict:
    scope = allow_setting()
    pack = [c for c in prompts.pack_settings(cfg.get("pack")).get("allow", []) if c not in scope]
    off = listed("DEEPCREW_ALLOW_OFF")
    fields = {"allow_scope": scope, "pack_allow": pack, "approved_allow": cfg.get("approved_allow", []),
              "allow_off": list(dict.fromkeys([*cfg.get("allow_off", []), *off])) if keep_off else off}
    return {**fields, "bash_allow": team_allow({**cfg, **fields})}


def positive(key: str, raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        raise ValueError(f"{key} must be a positive whole number, got {raw!r}")
    if key.lower().endswith("max_active") and value > MAX_MEMBERS:
        raise ValueError(f"{key} is at most {MAX_MEMBERS}, got {raw!r}")
    return value


def user_settings() -> dict:
    return {k: positive(k, providers.setting(f"DEEPCREW_{k.upper()}")) if providers.setting(f"DEEPCREW_{k.upper()}") else v for k, v in DEFAULTS.items()}


def allow_setting() -> list[str]:
    raw = os.environ["DEEPCREW_ALLOW"] if "DEEPCREW_ALLOW" in os.environ else providers.env_file_get("DEEPCREW_ALLOW") or ""
    if "DEEPCREW_ALLOW" not in os.environ and not providers.env_file_has("DEEPCREW_ALLOW"):
        raw = presets.SCOPES[presets.DEFAULTS["scope"]]["values"]["DEEPCREW_ALLOW"]
    return providers.split_list(raw)


def default_provider() -> str:
    return "deepseek" if "deepseek" in providers.known() else providers.known()[0]


def claude_bin() -> str | None:
    return member.claude_command()


def key_advice(name: str) -> str:
    return member.key_advice(name)


def git_state(project: Path) -> dict:
    def git(*args: str) -> str | None:
        r = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, check=False, **providers.SUBPROCESS_TEXT)
        return r.stdout.strip() if r.returncode == 0 else None

    top = git("rev-parse", "--show-toplevel")
    return {"repo": top, "base": git("rev-parse", "--verify", "HEAD") if top else None,
            "dirty": bool(git("status", "--porcelain", "--", ".")) if top else False}


UNTRACKED_AT_START = "untracked-at-start.json"
UNTRACKED_MAX = 100000


def untracked_files(project: Path) -> dict:
    r = subprocess.run(["git", "-C", str(project), "ls-files", "--others", "--exclude-standard", "-z"], capture_output=True, text=True,
                       check=False, **providers.SUBPROCESS_TEXT)
    paths = [p for p in r.stdout.split("\0") if p] if r.returncode == 0 else []
    if len(paths) > UNTRACKED_MAX:
        return {"": "too many"}
    out = {}
    for rel in paths:
        with contextlib.suppress(OSError):
            st = (project / rel).stat()
            out[rel] = [st.st_size, st.st_mtime_ns]
    return out


def shared_folder(project: Path, team: str, shared=None) -> Path:
    project = project.resolve()
    folder = Path(shared).expanduser().resolve() if shared else default_shared_dir(project, team)
    if policy.inside(str(folder), str(project)):
        raise ValueError(f"the team folder {providers.shown(folder)} is inside the project; give --shared a folder outside it")
    return folder


def create_team(project: Path, name: str, provider: dict, member_cfg: dict, *, claude: str, bash_allow=(), team_dir: Path | None = None,
                tools=DEFAULT_TOOLS, pack: str | None = None, shared_dir: Path | None = None, allow_off=None, fallback=None, **options) -> Board:
    prompts.check_pack(pack)
    project = project.resolve()
    shared = shared_folder(project, name, shared_dir)
    team_dir = team_dir or team_path(project, name)
    team_dir.mkdir(parents=True, exist_ok=True)
    p, extra = with_key_copy(team_dir, provider), prompts.pack_settings(pack)
    backups = fallback_tables(fallback_setting() if fallback is None else fallback)
    sandbox = sandbox_settings(p, project, backups)
    allow = {"allow_scope": list(bash_allow), "pack_allow": [c for c in extra.get("allow", []) if c not in bash_allow],
             "approved_allow": [], "allow_off": listed("DEEPCREW_ALLOW_OFF") if allow_off is None else list(allow_off)}
    cfg = {"team": name, "project": str(project), "created": time.time(), "claude": claude, "python": sys.executable,
           "pkg_parent": PKG_PARENT, "provider": p, "member": member_cfg,
           "fallback": backups,
           "tools": tools_setting(extra.get("tools")) if tools is DEFAULT_TOOLS else list(tools),
           **allow, "bash_allow": team_allow({**allow, "sandbox": sandbox}), "git": git_state(project), "pack": pack, "sandbox": sandbox,
           "shared_dir": str(shared), "project_memory": providers.setting("DEEPCREW_PROJECT_MEMORY") != "off",
           "review": providers.setting("DEEPCREW_REVIEW") != "off",
           **{**user_settings(), **options}}
    board = Board.create(team_dir, cfg)
    write_json(team_dir / UNTRACKED_AT_START, untracked_files(project) if cfg["git"]["base"] else {})
    refresh_key_copy(board)
    return board


PARTS = ("allow_scope", "pack_allow", "approved_allow", "allow_off")


def sync_team(board: Board, env_keys) -> dict:
    keys = set(env_keys)
    with board.locked():
        cfg = read_json(board.dir / "team.json")
        p, new = cfg["provider"], {}
        new.update({k: v for k, v in user_settings().items() if f"DEEPCREW_{k.upper()}" in keys})
        if keys & {"DEEPCREW_SANDBOX", *(f"DEEPCREW_SANDBOX_{k}" for k in ("DOMAINS", "DENY_READ", "ALLOW_WRITE", "LOCAL_PORTS"))}:
            new["sandbox"] = sandbox_settings(p, board.root, cfg.get("fallback") or [])
        if keys & {"DEEPCREW_ALLOW", "DEEPCREW_ALLOW_OFF", "DEEPCREW_SANDBOX"}:
            new.update(allow_fields({**cfg, **new}, keep_off="DEEPCREW_ALLOW_OFF" not in keys))
        if "DEEPCREW_TOOLS_OFF" in keys:
            new["tools"] = tools_setting(prompts.pack_settings(cfg.get("pack")).get("tools"))
        if "DEEPCREW_PROJECT_MEMORY" in keys:
            new["project_memory"] = providers.setting("DEEPCREW_PROJECT_MEMORY") != "off"
        if "DEEPCREW_REVIEW" in keys:
            new["review"] = providers.setting("DEEPCREW_REVIEW") != "off"
        if "DEEPCREW_FALLBACK" in keys:
            new["fallback"] = fallback_tables(fallback_setting())
            if box := new.get("sandbox", cfg.get("sandbox")):
                new["sandbox"] = closing(box, new["fallback"])
        chosen = providers.choices()["provider"]
        if "DEEPCREW_PROVIDER" in keys and chosen and chosen != p["name"]:
            moved = switch_to(board, json.loads(json.dumps({**cfg, **new})), providers.load(chosen), {"ts": time.time(), "by": "settings"})
            new.update({k: moved[k] for k in ("provider", "member", "sandbox", "provider_switches") if moved.get(k) != cfg.get(k)})
            p = moved["provider"]
        elif "DEEPCREW_PROVIDER" in keys and chosen == p["name"] and fallback_now(cfg):
            new["provider_switches"] = [*cfg["provider_switches"], {"from": chosen, "to": chosen, "ts": time.time(), "by": "settings"}][-SWITCHES_KEPT:]
        if keys & {"DEEPCREW_PROVIDER", "DEEPCREW_MODEL", "DEEPCREW_EFFORT", "DEEPCREW_THINKING"}:
            ch, member_cfg = providers.choices(), dict(new.get("member") or cfg["member"])
            if ch["provider"] in (None, p["name"]):
                if ch["model"] in p["models"]:
                    member_cfg["model"] = ch["model"]
                if ch["effort"]:
                    member_cfg["effort"] = None if ch["effort"] == "default" else ch["effort"]
            member_cfg["thinking"] = ch["thinking"] != "off"
            new["member"] = member_cfg
        names = {"provider": lambda v: (v or {}).get("name"), "fallback": lambda v: [t["name"] for t in v or []]}
        changed = {k: [names.get(k, _same)(cfg.get(k)), names.get(k, _same)(v)] for k, v in new.items()
                   if cfg.get(k) != v and k not in (*PARTS, "provider_switches")}
        changed = {k: v for k, v in changed.items() if v[0] != v[1]}
        if any(cfg.get(k) != v for k, v in new.items()):
            board.cfg = {**cfg, **new}
            write_json(board.dir / "team.json", board.cfg)
            if (board.dir / "run" / "settings.json").exists():
                write_json(board.dir / "run" / "settings.json", member.settings(board))
            board.bump()
            if {"provider", "fallback"} & set(new):
                refresh_key_copy(board)
    return {"team": cfg["team"], "changed": changed}


def _same(value):
    return value


def key_copy(team_dir: Path, p: dict) -> Path:
    return team_dir / "secret" / f"{p['name']}.key"


def with_key_copy(team_dir: Path, p: dict) -> dict:
    copy = f"file:{key_copy(team_dir, p)}"
    return {**p, "key_sources": [copy, *[s for s in p["key_sources"] if s != copy]]}


def refresh_key_copy(board: Board) -> bool:
    kept = set()
    for p in providers.team_tables(board.cfg):
        path = key_copy(board.dir, p)
        found = providers.resolve_key({**p, "key_sources": [s for s in p["key_sources"] if s != f"file:{path}"]})
        if found and found[0].startswith("env:"):
            path.parent.mkdir(mode=0o700, exist_ok=True)
            providers.write_private(path, found[1])
            kept.add(path)
    for path in (board.dir / "secret").glob("*.key"):
        if path not in kept:
            path.unlink(missing_ok=True)
    return key_copy(board.dir, board.cfg["provider"]) in kept


LEVELS = ("low", "medium", "high", "xhigh", "max")
SWITCHES_KEPT = 20


def fallback_setting() -> list[str]:
    return listed("DEEPCREW_FALLBACK")


def fallback_tables(names) -> list[dict]:
    out = []
    for name in dict.fromkeys(names):
        try:
            out.append(providers.load(name))
        except providers.ProviderError as e:
            raise providers.ProviderError(f"backup provider {name}: {e}; or take it off: {providers.NAME} config set fallback ...") from None
    return out


def on_provider(model: str, effort: str | None, old: dict, new: dict) -> tuple[str, str | None]:
    models = list(old.get("models") or [])
    to = new["models"][-1] if len(models) > 1 and model == models[-1] else new["models"][0]
    efforts = list(new.get("efforts") or [])
    if not effort or not efforts or effort in efforts:
        return to, effort if efforts else None
    rank = LEVELS.index(effort) if effort in LEVELS else len(LEVELS)
    above = [e for e in efforts if e in LEVELS and LEVELS.index(e) > rank]
    return to, min(above, key=LEVELS.index) if above else efforts[-1]


def switch_to(board: Board, cfg: dict, table: dict, record: dict) -> dict:
    old, p = cfg["provider"], with_key_copy(board.dir, table)
    model, effort = on_provider(cfg["member"]["model"], cfg["member"].get("effort"), old, p)
    cfg["member"] = {**cfg["member"], "model": model, "effort": effort}
    for m in board.members():
        if m.get("model"):
            m["model"], m["effort"] = on_provider(m["model"], m.get("effort"), old, p)
            board.put_member(m)
    if cfg.get("sandbox"):
        cfg["sandbox"] = closing(cfg["sandbox"], [p])
    cfg["provider"] = p
    cfg["provider_switches"] = [*(cfg.get("provider_switches") or []), {"from": old["name"], "to": p["name"], **record}][-SWITCHES_KEPT:]
    return cfg


def fallback_now(cfg: dict) -> dict | None:
    last = (cfg.get("provider_switches") or [None])[-1]
    return last if isinstance(last, dict) and last.get("by") == "fallback" and last.get("to") == cfg["provider"]["name"] else None


def provider_view(cfg: dict) -> dict:
    p = cfg["provider"]
    return {"name": p["name"], "priced": bool((p.get("prices") or {}).get(cfg["member"]["model"].split("[")[0])),
            "backups": [t["name"] for t in cfg.get("fallback") or [] if t["name"] != p["name"]], "fallback": fallback_now(cfg)}


def next_backup(board: Board, cfg: dict, failed: str) -> tuple[dict | None, list[str]]:
    gone = {failed}
    for s in reversed(cfg.get("provider_switches") or []):
        if s.get("by") != "fallback":
            break
        gone.add(s.get("from"))
    b = cfg.get("budget")
    budget = isinstance(b, dict) and isinstance(b.get("usd"), int | float) and b["usd"] > 0
    why = []
    for p in cfg.get("fallback") or []:
        if p["name"] in gone:
            why.append(f"{p['name']} failed")
        elif providers.resolve_key(with_key_copy(board.dir, p)) is None:
            why.append(f"{p['name']} has no key (`{providers.NAME} auth set {p['name']}` in your own terminal stores one)")
        elif budget and not (p.get("prices") or {}).get(on_provider(cfg["member"]["model"], None, cfg["provider"], p)[0].split("[")[0]):
            why.append(f"{p['name']} has no prices, so the budget could not be counted there")
        else:
            return p, why
    return None, why


def fall_back(board: Board, me: str, ran_on: str, status, kind: str, said: str) -> tuple[str | None, str]:
    with board.locked():
        cfg = read_json(board.dir / "team.json")
        if cfg["provider"]["name"] != ran_on:
            board.cfg = cfg
            return cfg["provider"]["name"], ""
        table, why = next_backup(board, cfg, ran_on)
        if table is None:
            return None, ("No backup provider could take over: " + "; ".join(why) + ".") if why else ""
        switch_to(board, cfg, table, {"ts": time.time(), "by": "fallback", "kind": kind, "status": status, "said": said[:300], "member": me,
                                      **({"skipped": why} if why else {})})
        write_json(board.dir / "team.json", cfg)
        board.cfg = cfg
        if (board.dir / "run" / "settings.json").exists():
            write_json(board.dir / "run" / "settings.json", member.settings(board))
        board.bump()
    return cfg["provider"]["name"], ""


def refresh_install(board: Board) -> None:
    now = {"claude": claude_bin(), "python": sys.executable, "pkg_parent": PKG_PARENT}
    if any(board.cfg.get(k) != v for k, v in now.items() if v):
        board.update_cfg(lambda cfg: cfg.update({k: v for k, v in now.items() if v}))


def member_config(a) -> tuple[dict, dict, bool]:
    ch = providers.choices()
    chosen = bool(ch["provider"] and ch["model"])
    if a.provider:
        providers.add_template(a.provider)
    p = providers.load(a.provider or ch["provider"] or default_provider())
    efforts = [*p["efforts"], "default"]
    for flag, value, allowed in (("--model", a.model, p["models"]), ("--effort", a.effort, efforts)):
        if value is not None and value not in allowed:
            raise ValueError(f"{flag} must be one of: {', '.join(allowed)}")
    default = presets.strength_values(presets.DEFAULTS["strength"], p)
    model = a.model or (ch["model"] if ch["model"] in p["models"] else default["DEEPCREW_MODEL"])
    effort = a.effort or (ch["effort"] if ch["effort"] in efforts else default["DEEPCREW_EFFORT"])
    return {"model": model, "effort": None if effort == "default" else effort, "thinking": ch["thinking"] != "off"}, p, chosen


SETUP_FILE = "setup.json"


def setup_state(board: Board) -> dict | None:
    path = board.dir / "run" / SETUP_FILE
    if not path.exists():
        return None
    s = read_json(path)
    done = any(e.get("kind") == "done" and e.get("ts", 0) >= s["since"] for e in read_jsonl(board.dir / "run" / "ui-events.jsonl"))
    missing = [k for k, still in (("key", providers.resolve_key(board.cfg["provider"]) is None), ("confirm", not done)) if s.get(k) and still]
    return {**s, "missing": missing}


def key_place(p: dict) -> str:
    found = providers.resolve_key(p)
    if not found:
        return ""
    kind, _, where = found[0].partition(":")
    return {"keychain": f"the system keychain of this computer (item {where})", "dotenv": f"{providers.shown(providers.ENV_FILE)} on this computer (mode 0600)",
            "file": "this team's private copy on this computer (mode 0600)", "env": f"the environment variable {where}"}.get(kind, found[0])


PAGE_KEYS = {**{k: f"DEEPCREW_{k.upper()}" for k in (*DEFAULTS, "provider", "model", "effort", "thinking", "project_memory", "review", "sandbox")},
             "deny_read": "DEEPCREW_SANDBOX_DENY_READ"}


def page_checked(values: dict) -> dict:
    from . import ui
    try:
        return ui.checked(values)
    except ui.Refused as e:
        raise ValueError(str(e)) from None


def setting_text(value) -> str:
    return "(not chosen)" if value is None else providers.shown(", ".join(value) or "none" if isinstance(value, list) else str(value))
