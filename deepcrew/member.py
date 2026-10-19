from __future__ import annotations

import base64
import contextlib
import json
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, extensions, policy, prompts, providers, toolkit
from .board import LANGS, REPORTS, Board, _strings, in_lang, lock_file, reply_lang, state_home, tools_dir, unlock_file, write_json
from .memory import (
    INSTRUCTIONS as INSTRUCTIONS,
    INSTRUCTIONS_MAX_CHARS as INSTRUCTIONS_MAX_CHARS,
    PROJECT_MEMORY_CHARS as PROJECT_MEMORY_CHARS,
    instructions as instructions,
    memory as memory,
    project_memory as project_memory,
    write_memory,
)

COMMON_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "HTTP_PROXY", "HTTPS_PROXY",
              "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS")
WINDOWS_ENV = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "USERNAME", "USERDOMAIN", "HOMEDRIVE", "HOMEPATH",
               "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "ALLUSERSPROFILE", "TEMP", "TMP",
               "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "CLAUDE_CODE_GIT_BASH_PATH")
FIXED_ENV = {
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_ENABLE_TASKS": "1",
    "CLAUDE_CODE_ENABLE_TODO_TOOLS": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
}
TASK_TOOLS = ("TaskCreate", "TaskUpdate")
BUNDLED_SKILLS = ("batch", "claude-api", "debug", "fewer-permission-prompts", "init", "keybindings-help", "loop", "review",
                  "security-review", "simplify", "update-config", "plugin-authoring")
POLL_S = 0.5
CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW = 0x00000200, 0x08000000
LOCK_PROBES, LOCK_PROBE_PAUSE_S = 3, 0.01
HANDSHAKE_S = 20.0
HANDSHAKE_POLL_S = 0.1
EXIT_SETTLE_S = 10.0
STOP_WAIT_S = 10.0
ENDING_POLL_S = 0.2
ALIVE_LOCK_WAIT_S, ALIVE_LOCK_POLL_S = 10.0, 0.05
REPLY_POLL_S = 0.25
HOOK_TIMEOUT_S = 30
DRIVER_EXIT_S = 60
PROGRESS_S = 15
PROCESS_MARK = "deepcrew-process"
SHELL_LOG_CHARS = 300
TIME_MARK = "deepcrew-time"
ERR_TAIL_LINES, ERR_TAIL_BYTES = 5, 4000
TURN_LIMIT = "error_max_turns"
ERROR_ADVICE = {"balance": "the provider account has no balance: top it up, then resume the crew",
                "rate_limit": "the provider's rate or concurrency limit: run fewer members at once, or resume the crew later",
                "overload": "the provider is overloaded: resume the crew later",
                400: "the provider refused the request (for example a document or an image it cannot read)",
                401: "the provider rejected the key: {key_advice}, then resume the crew",
                500: "the provider had an internal error: resume the crew later"}


class LaunchError(Exception):
    pass


def key_advice(provider: str) -> str:
    return f"save a valid key on the team page, or run `{providers.NAME} auth set {provider}` in your own terminal"


def pass_env_names() -> tuple[str, ...]:
    return COMMON_ENV + (WINDOWS_ENV if providers.is_windows() else ())


def host_env() -> dict[str, str]:
    return {"PYTHONUTF8": "1"} if providers.is_windows() else {}


def detach_options() -> dict:
    if providers.is_windows():
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    return {"start_new_session": True}


PASS_ENV = pass_env_names()
FIXED_ENV.update(host_env())


def task_view(board: Board, t: dict, region: list[str] | None = None) -> dict:
    out = {**{k: t[k] for k in ("id", "title", "body", "scope", "deps")}, "prerequisites": board.prerequisites(t)}
    if waiting := board.depth()[t["id"]][1]:
        out["unblocks"] = waiting
    if region is not None and t["scope"] and not policy.within(t["scope"], region):
        out["ask"] = True
    return out


def offer_text(board: Board, name: str, tasks: list[dict]) -> str:
    region = (board.member(name) or {}).get("region", [])
    with board.still():
        views = [task_view(board, t, region) for t in tasks]
    return prompts.render("deliver_offer", board.cfg, name=name, tasks=json.dumps(views, ensure_ascii=False, indent=1))


def mail_text(board: Board, msgs: list[dict]) -> str:
    lines = "\n".join(json.dumps({k: m[k] for k in ("from", "kind", "task", "text")}, ensure_ascii=False) for m in msgs)
    return prompts.render("deliver_mail", board.cfg, messages=lines)


def lang_line(board: Board) -> str | None:
    lang = reply_lang(board.live_cfg())
    return None if lang == LANGS[0] else (f"The user reads {lang}: write every gist, and every message to the user, in {lang}. "
                                          "Everything else stays in English.")


def with_lang(board: Board, text: str) -> str:
    line = lang_line(board)
    return f"{text}\n\n{line}" if line else text


def gist_problem(board: Board, gist: str) -> str | None:
    if "`" in gist or PATHISH.search(gist):
        return "the gist is for the user watching the team: say what changed for them or what you need, in plain words, without paths or code"
    lang = reply_lang(board.live_cfg())
    if not in_lang(gist, lang):
        return f"write the gist in {lang}: the user reads replies in {lang} (everything else stays in English)"
    return None


def offer(board: Board, m: dict) -> list[dict]:
    t = board.task(m.get("assigned"))
    if t and t["state"] == "claimed" and t["owner"] == m["name"]:
        return [t]
    if board.helping(m):
        return []
    return board.eligible(m)[: board.cfg["offer_size"]]


def user_message(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}, ensure_ascii=False) + "\n"


def _windows_quoted(path: str) -> str:
    path = path.replace("\\", "/")
    if any(ch in path for ch in '"$`%'):
        raise LaunchError(f"{path!r} holds one of \" $ ` %, which Git Bash or cmd.exe would expand inside quotes: move or reinstall it")
    return f'"{path}"'


def module_args(*args: str) -> list[str]:
    return ["-P", "-m", "deepcrew.member", *args]


def _py(board: Board, *args: str) -> str:
    c = board.cfg
    if providers.is_windows():
        return " ".join([_windows_quoted(c["python"]), _windows_quoted(ntpath.join(c["pkg_parent"], "deepcrew", "_run.py")), *args])
    return " ".join([f"PYTHONPATH={shlex.quote(c['pkg_parent'])}", shlex.quote(c["python"]), *module_args(*args)])


def project_hooks(root) -> dict[str, list]:
    merged: dict[str, list] = {}
    for name in ("settings.json", "settings.local.json"):
        try:
            hooks = json.loads((root / ".claude" / name).read_text(encoding="utf-8-sig")).get("hooks") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for event, entries in hooks.items() if isinstance(hooks, dict) else ():
            if isinstance(entries, list):
                merged.setdefault(event, []).extend(entries)
    return merged


def settings(board: Board) -> dict:
    c = board.cfg

    def hook(event: str) -> list[dict]:
        return [{"type": "command", "command": _py(board, "hook", event), "timeout": HOOK_TIMEOUT_S}]

    guarded = (*policy.EDIT_TOOLS, *TASK_TOOLS, *(("Read",) if c["provider"].get("documents") is False else ()))
    hooks = {"PreToolUse": [{"matcher": "|".join(guarded), "hooks": hook("pre")}],
             "PostToolUse": [{"matcher": "*", "hooks": hook("post")}]}
    for event, entries in project_hooks(board.root).items():
        hooks[event] = hooks.get(event, []) + entries
    allow = ["mcp__deepcrew", *(["WebSearch"] if "WebSearch" in c.get("tools", []) else [])]
    deny = [f"Read(/{policy.rule_path(d)}/**)" for d in (state_home(), providers.CONFIG_DIR)]
    out = {"permissions": {"defaultMode": "default", "allow": allow, "deny": deny}, "hooks": hooks, "apiKeyHelper": _py(board, "key")}
    if "anthropic.com" not in urlparse(c["provider"]["base_url"]).netloc:
        out["skipWebFetchPreflight"] = True
    if toolkit.skill_names(board.root):
        out["skillOverrides"] = {name: "off" for name in BUNDLED_SKILLS}
    if c.get("sandbox"):
        box = json.loads(json.dumps(c["sandbox"]))
        fs, net = box.setdefault("filesystem", {}), box.setdefault("network", {})
        fs["allowWrite"] = sorted({*fs.get("allowWrite", []), str(tools_dir()), *(f["path"] for f in board.folders() if f.get("write"))})
        if closed := closed_to_writes(board):
            fs["denyWrite"] = sorted({*fs.get("denyWrite", []), *closed})
        if sites := board.sites():
            net["allowedDomains"] = sorted({*net.get("allowedDomains", []), *sites})
        out["sandbox"] = box
    return out


def closed_to_writes(board: Board) -> list[str]:
    folders = board.folders()
    written = [os.path.realpath(p) for p in (board.root, board.shared_dir, tools_dir(), *(f["path"] for f in folders if f.get("write")))]

    def meets(path: str) -> bool:
        return any(policy.inside(path, w) or policy.inside(w, path) for w in written)
    return sorted({f["path"] for f in folders if not f.get("write") and not meets(os.path.realpath(f["path"]))})


def install_env() -> dict:
    return {"DEEPCREW_NAME": providers.NAME, "DEEPCREW_CONFIG_DIR": str(providers.CONFIG_DIR), "DEEPCREW_HOME": str(state_home()),
            **{k: os.environ[k] for k in (extensions.KEY, "DEEPCREW_TOOLS") if os.environ.get(k)}}


def mcp_config(board: Board, m: dict) -> dict:
    c = board.cfg
    own = {"command": c["python"], "args": module_args("mcp"),
           "env": {"PYTHONPATH": c["pkg_parent"], "DEEPCREW_TEAM_DIR": str(board.dir), **install_env(), "DEEPCREW_MEMBER": m["name"]}}
    return {"mcpServers": dict(sorted({**toolkit.mcp_servers(board.root), "deepcrew": own}.items()))}


def member_model(board: Board, m: dict | None) -> tuple[str, str | None]:
    return board.model_of(m), (m or {}).get("effort") or board.cfg["member"].get("effort")


def strength_model(board: Board, strength: str) -> tuple[str, str | None]:
    from .presets import STRENGTHS, strength_values
    if strength not in STRENGTHS:
        raise LaunchError(f"strength is one of {', '.join(STRENGTHS)}, not {strength!r}")
    chosen = strength_values(strength, board.cfg["provider"])
    return chosen["DEEPCREW_MODEL"], None if chosen["DEEPCREW_EFFORT"] == "default" else chosen["DEEPCREW_EFFORT"]


def env(board: Board, m: dict) -> dict:
    p, (model, effort) = board.cfg["provider"], member_model(board, m)
    e = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    e["PATH"] = os.pathsep.join(filter(None, [e.get("PATH"), str(tools_dir() / "bin")]))
    e.update(FIXED_ENV)
    e.update({"CLAUDE_CONFIG_DIR": str(board.dir / "claude"), "CLAUDE_CODE_TASK_LIST_ID": board.cfg["task_list"],
              "ANTHROPIC_BASE_URL": p["base_url"], "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
              "ANTHROPIC_DEFAULT_SONNET_MODEL": model, "ANTHROPIC_DEFAULT_FABLE_MODEL": model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": p["fast_model"],
              "CLAUDE_CODE_SUBAGENT_MODEL": p.get("subagent_model") or p["fast_model"], "API_TIMEOUT_MS": str(p["timeout_ms"]),
              "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(p["auto_compact_window"]),
              "DEEPCREW_TEAM_DIR": str(board.dir), **install_env(), "DEEPCREW_MEMBER": m["name"]})
    if p.get("max_output_tokens"):
        e["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(p["max_output_tokens"])
    if effort:
        e["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    if board.cfg["member"].get("thinking") is False:
        e["CLAUDE_CODE_DISABLE_THINKING"] = "1"
        if p.get("thinking_off_body"):
            e["CLAUDE_CODE_EXTRA_BODY"] = json.dumps({"thinking": {"type": "disabled"}}, separators=(",", ":"))
    return e


def system_prompt(board: Board) -> str:
    return prompts.render("member_system", board.cfg, team=board.cfg["team"], notes=str(board.notes_dir), artifacts=str(board.artifacts_dir),
                          tools=str(tools_dir()))


def reach(board: Board) -> list[str]:
    tools = tools_dir()
    (tools / "bin").mkdir(parents=True, exist_ok=True)
    return [str(board.shared_dir), str(tools), *sorted(f["path"] for f in board.folders())]


RUNTIMES = ("node", "bun", "deno")


def running_claude() -> str | None:
    path = os.environ.get("CLAUDE_CODE_EXECPATH", "")
    name = Path(path).name.lower().removesuffix(".exe")
    return path if path and name not in RUNTIMES and shutil.which(path) else None


def claude_command(recorded: str | None = None) -> str | None:
    return (providers.setting("DEEPCREW_CLAUDE") or running_claude() or (recorded if recorded and shutil.which(recorded) else None)
            or (providers.is_windows() and shutil.which("claude.exe")) or shutil.which("claude"))


def argv(board: Board, m: dict, *, resume: bool) -> list[str]:
    c, run = board.cfg, board.dir / "run"
    write_json(run / "settings.json", settings(board))
    write_json(run / f"{m['name']}.mcp.json", mcp_config(board, m))
    os.chmod(run / f"{m['name']}.mcp.json", 0o600)
    write_memory(board)
    toolkit.install_skills(board)
    tools = [*c["tools"], *(["Skill"] if toolkit.skill_names(board.root) and "Skill" not in c["tools"] else [])]
    return [claude_command(c["claude"]) or c["claude"], "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",
            "--settings", str(run / "settings.json"), "--setting-sources", "user",
            "--mcp-config", str(run / f"{m['name']}.mcp.json"), "--strict-mcp-config", "--tools", ",".join(tools),
            *(x for d in reach(board) for x in ("--add-dir", d)),
            "--append-system-prompt", system_prompt(board), "--max-turns", str(c["max_turns"]),
            "--permission-prompt-tool", "mcp__deepcrew__approve",
            *(["--resume", m["session"]] if resume else ["--session-id", m["session"]])]


def _launch(board: Board, m: dict, first: str, *, resume: bool) -> subprocess.Popen:
    c = board.cfg
    if providers.resolve_key(c["provider"]) is None:
        raise LaunchError(f"no key for provider {c['provider']['name']!r}: {key_advice(c['provider']['name'])}")
    denv = {**{k: os.environ[k] for k in PASS_ENV if k in os.environ}, "PYTHONPATH": c["pkg_parent"], "DEEPCREW_TEAM_DIR": str(board.dir),
            **install_env(), "DEEPCREW_MEMBER": m["name"], **host_env()}
    with open(board.dir / "logs" / f"{m['name']}.driver.log", "ab") as log:
        proc = subprocess.Popen([c["python"], *module_args("drive", m["session"], *(["--resume"] if resume else []))],
                                cwd=board.root, env=denv, stdin=subprocess.PIPE, stdout=log, stderr=log, **detach_options())
    proc.stdin.write(first.encode())
    proc.stdin.close()
    threading.Thread(target=proc.wait, daemon=True).start()
    if providers.is_windows():
        try:
            await_alive(proc, alive_path(board, m["session"]), m["name"])
        except LaunchError:
            if proc.poll() is None:
                _end({"pid": proc.pid, "session": m["session"]}, True)
            raise
    return proc


def _opening(board: Board, m: dict, task: dict | None) -> dict:
    role = m.get("role")
    return {"name": m["name"], "team": board.cfg["team"], "region": json.dumps(m["region"]),
            "role": f"Your role, from the leader: {role}" if role else "",
            "task": json.dumps(task_view(board, task), ensure_ascii=False, indent=1) if task else "none yet"}


def start(board: Board, name: str, region, task_id: str | None = None, role: str | None = None, strength: str | None = None) -> dict:
    if role is not None and (not isinstance(role, str) or len(role) > 4000):
        raise LaunchError("role must be a string of at most 4000 characters")
    if check_budget(board):
        st = budget_state(board)
        raise LaunchError(f"the team has spent ${st['spent']:.2f} of the ${st['usd']:.2f} budget the user set; only the user can raise it, "
                          "on the team's page")
    if paused := board.paused():
        raise LaunchError(f"the crew is resting (the {paused.get('by', 'leader')} rested it); `{providers.NAME} resume` first, when the user asks")
    own = strength_model(board, strength) if strength else None
    with board.locked():
        refuse_name(board, name)
        m = board.new_member(name, region, str(uuid.uuid4()), role)
        if own:
            m.update(model=own[0], effort=own[1])
            board.put_member(m)
        try:
            task = None
            if task_id:
                task, why = board._claim(name, task_id)
                if task is None:
                    raise LaunchError(f"cannot give {task_id} to {name}: {why}")
            proc = _launch(board, m, with_lang(board, prompts.render("member_start", board.cfg, **_opening(board, m, task))), resume=False)
        except Exception:
            if task_id and (t := board.task(task_id)) and t["owner"] == name:
                board._release(name, task_id, by_choice=False)
            (board.dir / "members" / f"{name}.json").unlink(missing_ok=True)
            board.bump()
            raise
        m = board.member(name)
        m.update(pid=proc.pid, state="active", runs=1, updated=time.time(), sandbox=launch_sandbox(board), **alive_fields(board, m))
        board.put_member(m)
        board.use_name(name)
        board.bump()
    return m


def launch_sandbox(board: Board) -> bool:
    return bool(board.live_cfg().get("sandbox"))


def refuse_name(board: Board, name: str) -> None:
    if board.member(name) is not None:
        raise ValueError(f"member {name!r} exists: it wakes by itself on new work in its region or on mail; "
                         f"to tell it something: {providers.NAME} send {name} \"...\"")
    if name in board.used_names():
        raise ValueError(f"the name {name!r} was used earlier in this team and is not used again: choose another")
    if len(board.members()) >= board.cfg["max_active"]:
        raise ValueError(f"team {board.cfg['team']!r} already has {board.cfg['max_active']} members, its limit: give the work to an idle "
                         f"one (it wakes by itself on new work or mail), or raise the limit with `{providers.NAME} config set max_active N`")


def wake(board: Board, name: str) -> dict | None:
    m = board.member(name)
    if m is None or board.paused() or check_budget(board):
        return None
    deadline = time.monotonic() + EXIT_SETTLE_S
    while alive(m) and m["state"] not in ("starting", "active") and time.monotonic() < deadline:
        time.sleep(ENDING_POLL_S)
    with board.locked():
        m = board.member(name)
        if alive(m):
            return None
        tasks = offer(board, m)
        mail = board.peek_mail(name)
        senders = {msg.get("from") for _, msg in mail}
        if not tasks and "leader" not in senders and not board.partner_wake(name, senders, spend=True):
            return None
        msgs = [msg for _, msg in mail]
        body = with_lang(board, "\n\n".join([*([mail_text(board, msgs)] if msgs else []), *([offer_text(board, name, tasks)] if tasks else [])]))
        resumable = transcript(board, m) is not None
        if not resumable:
            m["session"] = str(uuid.uuid4())
            board.put_member(m)
        values = {**_opening(board, m, None), "body": body}
        prompt = prompts.render("member_wake", board.cfg, **values) if resumable else \
            prompts.render("member_start", board.cfg, **values) + "\n\n" + body
        proc = _launch(board, m, prompt, resume=resumable)
        board.ack_mail(mail)
        m.update(pid=proc.pid, state="active", runs=m["runs"] + 1, offered=[t["id"] for t in tasks], assigned=None, updated=time.time(),
                 sandbox=launch_sandbox(board), **alive_fields(board, m))
        board.put_member(m)
        board.bump()
    return m


def transcript(board: Board, m: dict):
    return next((board.dir / "claude" / "projects").glob(f"*/{m['session']}.jsonl"), None)


def alive_path(board: Board, session: str) -> Path:
    return board.dir / "run" / f"{session}.alive"


def alive_fields(board: Board, m: dict) -> dict:
    return {"lock": str(alive_path(board, m["session"]))} if providers.is_windows() else {}


_HELD: list = []


def hold_alive(path: Path, timeout: float = ALIVE_LOCK_WAIT_S) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    f, deadline = open(path, "ab"), time.monotonic() + timeout
    while not lock_file(f, wait=False):
        if time.monotonic() > deadline:
            f.close()
            return False
        time.sleep(ALIVE_LOCK_POLL_S)
    _HELD.append(f)
    return True


def lock_held(path) -> bool:
    if not path or not Path(path).exists():
        return False
    for attempt in range(LOCK_PROBES):
        try:
            with open(path, "ab") as f:
                if lock_file(f, wait=False):
                    unlock_file(f)
                    return False
        except OSError:
            return True
        if attempt < LOCK_PROBES - 1:
            time.sleep(LOCK_PROBE_PAUSE_S)
    return True


def await_alive(proc: subprocess.Popen, path: Path, name: str, timeout: float = HANDSHAKE_S) -> None:
    deadline = time.monotonic() + timeout
    while not lock_held(path):
        if proc.poll() is not None:
            raise LaunchError(f"the driver of {name} ended with status {proc.returncode} before it started: see logs/{name}.driver.log")
        if time.monotonic() >= deadline:
            raise LaunchError(f"the driver of {name} did not start within {timeout:g} s: see logs/{name}.driver.log")
        time.sleep(HANDSHAKE_POLL_S)


def alive(m: dict | None) -> bool:
    if not m or not m.get("pid"):
        return False
    if providers.is_windows():
        return lock_held(m.get("lock"))
    batch = getattr(_LOOK, "pids", None)
    if batch and str(m["pid"]) in batch:
        if _LOOK.seen is None:
            r = subprocess.run(["ps", "-o", "pid=,command=", "-p", ",".join(sorted(batch))], capture_output=True, text=True,
                               **providers.SUBPROCESS_TEXT, check=False)
            _LOOK.seen = dict(line.strip().partition(" ")[::2] for line in r.stdout.splitlines() if line.strip()) \
                if r.returncode == 0 or r.stdout.strip() else False
        if _LOOK.seen is not False:
            return m["session"] in _LOOK.seen.get(str(m["pid"]), "")
    r = subprocess.run(["ps", "-o", "command=", "-p", str(m["pid"])], capture_output=True, text=True, **providers.SUBPROCESS_TEXT, check=False)
    return m["session"] in r.stdout


_LOOK = threading.local()


def living(ms: list[dict]) -> set[str]:
    _LOOK.pids, _LOOK.seen = {str(m["pid"]) for m in ms if m and m.get("pid")}, None
    try:
        return {m["name"] for m in ms if alive(m)}
    finally:
        _LOOK.pids = _LOOK.seen = None


def end_command(m: dict, force: bool) -> list[str]:
    if providers.is_windows():
        return ["taskkill", "/PID", str(m["pid"]), "/T", *(["/F"] if force else [])]
    return ["pkill", "-KILL" if force else "-TERM", "-f", m["session"]]


def _end(m: dict, force: bool) -> bool:
    if not providers.is_windows():
        subprocess.run(end_command(m, force), check=False)
        return False
    return subprocess.run(end_command(m, force), capture_output=True, check=False).returncode != 0


def stop(board: Board, name: str, timeout: float = STOP_WAIT_S) -> bool:
    m = board.member(name)
    running = alive(m)
    if running:
        refused = _end(m, False)
        deadline = time.monotonic() + (0 if refused else timeout)
        while alive(m) and time.monotonic() < deadline:
            time.sleep(ENDING_POLL_S)
        if alive(m):
            _end(m, True)
            for _ in range(10 if providers.is_windows() else 0):
                if not alive(m):
                    break
                time.sleep(ENDING_POLL_S)
    board.reap(living)
    return running


def first_reply(board: Board, name: str, offset: int, timeout: float) -> bool:
    log, deadline = board.dir / "logs" / f"{name}.ndjson", time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists():
            with open(log, "rb") as f:
                f.seek(offset)
                for line in f:
                    try:
                        if json.loads(line).get("type") == "assistant":
                            return True
                    except ValueError:
                        continue
        time.sleep(REPLY_POLL_S)
    return False


BUDGET_WARN = 0.8
USAGE_CACHE = 512
TAIL_BYTES = 64
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
MODEL_USAGE_KEYS = dict(zip(USAGE_KEYS, ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"), strict=True))
_TALLY: dict[str, dict] = {}
_TALLY_LOCK = threading.Lock()
LONG_PROMPT = 100_000
CLAUDE_PRICES = {
    "Haiku 5.5": {"input_written_to_cache_5m": 0.125, "cache_read": 0.01, "output": 0.50,
                  "long": {"input_written_to_cache_5m": 0.625, "cache_read": 0.05, "output": 2.50}},
    "Sonnet 5.5": {"input_written_to_cache_5m": 2.50, "cache_read": 0.10, "output": 10.0},
    "Opus 5.5": {"input_written_to_cache_5m": 5.0, "cache_read": 0.20, "output": 20.0},
    "Fable 5.1": {"input_written_to_cache_5m": 12.50, "cache_read": 0.25, "output": 50.0},
}
DEFAULT_COMPARE = "Sonnet 5.5"
TOKEN_KINDS = {"input": "input_tokens", "cache_write": "cache_creation_input_tokens", "cache_read": "cache_read_input_tokens",
               "output": "output_tokens"}


def claude_equivalent(tokens: dict, model: str = DEFAULT_COMPARE) -> float:
    p = CLAUDE_PRICES[model]
    long = (tokens.get("long") or {}) if "long" in p else {}

    def at(t: dict, pr: dict) -> float:
        return ((t.get("input", 0) + t.get("cache_write", 0)) * pr["input_written_to_cache_5m"] + t.get("cache_read", 0) * pr["cache_read"]
                + t.get("output", 0) * pr["output"]) / 1e6
    return at({k: tokens.get(k, 0) - long.get(k, 0) for k in TOKEN_KINDS}, p) + (at(long, p["long"]) if long else 0.0)


def claude_tokens(usages: list[dict]) -> dict:
    return {**{k: sum(u.get(key, 0) for u in usages) for k, key in TOKEN_KINDS.items()},
            "long": {k: sum((u.get("long") or {}).get(key, 0) for u in usages) for k, key in TOKEN_KINDS.items()}}


def _long(replies: list[dict], output: int) -> dict:
    long = [u for u in replies if sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")) > LONG_PROMPT]
    out = {k: sum(int(u.get(k) or 0) for u in long) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
    out["output_tokens"] = round(output * len(long) / len(replies)) if replies else 0
    return out


def _tally(log: Path) -> dict | None:
    try:
        st = log.stat()
    except OSError:
        return None
    t = _TALLY.get(str(log))
    if t is not None and st.st_size > t["offset"] and t["tail"]:
        with open(log, "rb") as f:
            f.seek(t["offset"] - len(t["tail"]))
            if f.read(len(t["tail"])) != t["tail"]:
                t = None
    if t is None or t["ino"] != st.st_ino or st.st_size < t["offset"]:
        if len(_TALLY) >= USAGE_CACHE:
            _TALLY.clear()
        t = _TALLY[str(log)] = {"ino": st.st_ino, "offset": 0, "tail": b"", "lines": 0, "done": dict.fromkeys(USAGE_KEYS, 0), "ids": set(),
                                "turn": {}, "models": {}, "at": None, "turns": [], "seg": None, "long": dict.fromkeys(USAGE_KEYS, 0)}
    if st.st_size > t["offset"]:
        with open(log, "rb") as f:
            f.seek(t["offset"])
            data = f.read(st.st_size - t["offset"])
        whole = data[:data.rfind(b"\n") + 1]
        for raw in whole.splitlines():
            n, t["lines"] = t["lines"], t["lines"] + 1
            try:
                ev = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "assistant" and isinstance(ev.get("message"), dict):
                msg = ev["message"]
                t["ids"].add(msg.get("id"))
                t["turn"][msg.get("id") or f"line{n}"] = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            elif ev.get("type") == PROCESS_MARK:
                if t["turn"]:
                    cut = {k: sum(int(u.get(k) or 0) for u in t["turn"].values()) for k in USAGE_KEYS}
                    long = _long(list(t["turn"].values()), cut["output_tokens"])
                    for k in USAGE_KEYS:
                        t["done"][k] += cut[k]
                        t["long"][k] += long[k]
                    t["turns"].append((t["at"], {None: cut}, t["seg"]))
                    t["turn"], t["at"] = {}, None
                t["models"], t["seg"] = {}, _segment(ev)
            elif ev.get("type") == TIME_MARK:
                t["at"] = ev.get("ts") if isinstance(ev.get("ts"), int | float) else None
            elif ev.get("type") == "result":
                replies, t["turn"] = list(t["turn"].values()), {}
                by_model = _by_model(t, ev, {k: int((ev.get("usage") or {}).get(k) or 0) for k in USAGE_KEYS})
                long = _long(replies, sum(u["output_tokens"] for u in by_model.values()))
                for k in USAGE_KEYS:
                    t["done"][k] += sum(u[k] for u in by_model.values())
                    t["long"][k] += long[k]
                t["turns"].append((t["at"], by_model, t["seg"]))
                t["at"] = None
        t["offset"] += len(whole)
        t["tail"] = (t["tail"] + whole)[-TAIL_BYTES:]
    return t


def process_mark(board: Board, m: dict) -> dict:
    p = board.cfg["provider"]
    return {"type": PROCESS_MARK, "ts": time.time(), "provider": p["name"], "model": member_model(board, m)[0], "prices": p.get("prices") or {},
            "tariff": p.get("tariff")}


def _segment(ev: dict) -> dict | None:
    if not isinstance(ev.get("prices"), dict) or not isinstance(ev.get("model"), str):
        return None
    return {"model": ev["model"], "prices": ev["prices"], "tariff": ev.get("tariff") if isinstance(ev.get("tariff"), dict) else None}


def _by_model(t: dict, ev: dict, main: dict) -> dict:
    now = {model: {k: int((u or {}).get(camel) or 0) for k, camel in MODEL_USAGE_KEYS.items()}
           for model, u in (ev.get("modelUsage") or {}).items() if isinstance(u, dict)}
    if not now:
        return {None: main}
    out = {}
    for model, u in now.items():
        before = t["models"].get(model, {})
        out[model] = {k: v - before.get(k, 0) if v >= before.get(k, 0) else v for k, v in u.items()}
    t["models"] = now
    rest = {k: max(0, main[k] - sum(u[k] for u in out.values())) for k in USAGE_KEYS}
    return {**out, None: rest} if any(rest.values()) else out


def usage(board: Board, name: str) -> dict:
    total = {"requests": 0, **dict.fromkeys(USAGE_KEYS, 0)}
    running = dict.fromkeys(USAGE_KEYS, 0)
    with _TALLY_LOCK:
        t = _tally(board.dir / "logs" / f"{name}.ndjson")
        t = t and {**t, "done": dict(t["done"]), "turn": dict(t["turn"]), "turns": list(t["turns"]),
                   "long": dict(t["long"])}
    total["long"] = dict.fromkeys(USAGE_KEYS, 0)
    if t:
        total.update(t["done"], requests=len(t["ids"]), long=dict(t["long"]))
        if t["turn"]:
            total["running"] = True
            for u in t["turn"].values():
                for k in USAGE_KEYS:
                    running[k] += int(u.get(k) or 0)
            long = _long(list(t["turn"].values()), running["output_tokens"])
            for k in USAGE_KEYS:
                total[k] += running[k]
                total["long"][k] += long[k]
    p = board.cfg["provider"]
    now = {"model": member_model(board, board.member(name))[0], "prices": p.get("prices") or {}, "tariff": p.get("tariff")}

    def price(seg: dict, model: str | None = None) -> dict | None:
        base = seg["prices"].get(seg["model"].split("[")[0])
        return (seg["prices"].get(model.split("[")[0]) or base) if model else base

    def cost(u: dict, pr: dict) -> float:
        return ((u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)) * pr["cache_miss"]
                + u.get("cache_read_input_tokens", 0) * pr["cache_hit"] + u.get("output_tokens", 0) * pr["output"]) / 1e6
    turns = [(at, [(u, price(seg or now, model)) for model, u in by_model.items()], seg or now) for at, by_model, seg in (t["turns"] if t else [])]
    ran = (t["seg"] if t else None) or now
    if price(now) and price(ran) and all(pr for _, parts, _ in turns for _, pr in parts):
        costs = [(at, sum(cost(u, pr) for u, pr in parts), seg) for at, parts, seg in turns]
        listed = sum(c for _, c, _ in costs) + cost(running, price(ran))
        paid = sum(c * (providers.price_share(seg, at) if at else 1.0) for at, c, seg in costs)
        paid += cost(running, price(ran)) * providers.price_share(ran, time.time())
        total["cost_usd"] = round(paid, 6)
        if p.get("tariff") or any(seg["tariff"] for _, _, seg in costs):
            total["list_usd"] = round(listed, 6)
    return total


def spent(board: Board) -> float | None:
    from . import review
    reviewed = review.tally(board)["cost_usd"]
    if reviewed is None:
        return None
    total = float(board.live_cfg().get("spent_removed") or 0) + reviewed
    for m in board.members():
        cost = usage(board, m["name"]).get("cost_usd")
        if cost is None:
            return None
        total += cost
    return round(total, 6)


def budget_state(board: Board) -> dict:
    b, s = board.budget(), spent(board)
    return {"usd": b["usd"] if b else None, "spent": s, "reached": bool(b and s is not None and s >= b["usd"])}


def check_budget(board: Board) -> bool:
    if (b := board.budget()) is None:
        return False
    st = budget_state(board)
    if not st["reached"]:
        if st["spent"] is not None and st["spent"] >= BUDGET_WARN * st["usd"] and not b.get("near"):
            def near(cfg: dict) -> None:
                if (cfg.get("budget") or {}).get("ts") == b["ts"]:
                    cfg["budget"]["near"] = {"ts": time.time(), "spent": st["spent"]}
            board.update_cfg(near)
        return False

    def rest(cfg: dict) -> None:
        if not cfg.get("paused"):
            cfg["paused"] = {"by": "budget", "ts": time.time(), "usd": st["usd"], "spent": st["spent"]}
    if not board.paused():
        board.update_cfg(rest)
    return True


def _path(data: dict) -> str | None:
    ti = data.get("tool_input") or {}
    return ti.get("file_path") or ti.get("notebook_path")


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


def task_rules(board: Board, me: str, tool: str, ti: dict) -> dict | None:
    md = ti.get("metadata") or {}
    if not isinstance(md, dict):
        return _deny("metadata must be an object")
    if tool == "TaskCreate":
        if set(md) - {"scope"}:
            return _deny(f"a new task's metadata holds only scope (the paths it changes), not {sorted(set(md) - {'scope'})}")
        try:
            scope = policy.norm_all(_strings("scope", md.get("scope") or []))
        except ValueError as e:
            return _deny(f"scope: {e}")
        stray = next((e for e in scope if e.split("/", 1)[0] in {board.cfg["team"], board.shared_dir.name, "notes", "artifacts"}
                      and not (board.root / e.split("/", 1)[0]).exists()), None)
        if stray:
            return _deny(f"scope {stray!r} is not in the project: notes and artifacts live in {board.shared_dir}, outside the project; "
                         f"write them there with Write (a task that changes no project file needs no scope)")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "updatedInput": {**ti, "metadata": {"scope": scope, "by": me}}}}
    tid = str(ti.get("taskId", ""))
    t = board.task(tid)
    if t is None:
        return None
    own = {"summary", "gist", f"pass:{me}", f"release:{me}"}
    own.add(f"pitch:{me}")
    if set(md) - own:
        return _deny(f"the metadata keys you may set are {sorted(own)}")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 20000 for v in md.values()):
        return _deny("metadata values are 1-20000 characters of text")
    if len(md.get("gist", "")) > GIST_CHARS:
        return _deny(f"metadata.gist is one plain sentence for the user, at most {GIST_CHARS} characters")
    if md.get("gist") and (why := gist_problem(board, md["gist"])):
        return _deny(why)
    if "gist" in md and not (t["state"] == "claimed" and t["owner"] == me):
        return _deny(f"task {tid} is not yours")
    if f"pitch:{me}" in md and t["state"] == "open" and t["scope"] and not policy.within(t["scope"], (board.member(me) or {}).get("region", [])):
        from . import route
        why = route.ask(board, me, tid, md[f"pitch:{me}"])
        return _deny(why) if why else None
    status, owner = ti.get("status"), ti.get("owner")
    mine = t["state"] == "claimed" and t["owner"] == me
    if status == "deleted":
        return _deny("only the leader removes tasks")
    if owner not in (None, me):
        return _deny("a member takes tasks only for itself")
    if {"subject", "description", "addBlocks", "addBlockedBy"} & set(ti) and not (mine or t["posted_by"] == me):
        return _deny("only the member that posted or holds a task changes it")
    if f"pass:{me}" in md and t["state"] != "open":
        return _deny(f"you can decline only an open task; task {tid} is {t['state']}")
    not_mine = (f"task {tid} is already completed{' by ' + t['owner'] if t['owner'] and t['owner'] != me else ''}: send what changed since "
                "to the leader with message" if t["state"] == "done" else f"task {tid} is not yours")
    if "summary" in md and not mine:
        return _deny(not_mine)
    if status == "completed":
        if not mine:
            return _deny(not_mine)
        if not (md.get("summary") or t["summary"]):
            return _deny("say what the task came to in metadata.summary")
    elif status == "pending":
        if not mine:
            return _deny(not_mine)
        if f"release:{me}" not in md:
            return _deny(f'say why another member should take it, in metadata "release:{me}"')
    elif (status == "in_progress" or owner == me) and not mine:
        if t["state"] == "open" and not t["scope"]:
            from . import route
            why = route.ask(board, me, tid)
            return _deny(why) if why else None
        if t["state"] == "open" and t["scope"] and not policy.within(t["scope"], (board.member(me) or {}).get("region", [])):
            from . import route
            why = route.ask(board, me, tid, md.get(f"pitch:{me}"))
            return _deny(why) if why else None
        got, why = board.claim(me, tid)
        if got is None:
            return _deny(why)
    return None


PDF_REFUSED = ("this provider cannot read PDF documents, and the request would fail: read its text with `pdftotext FILE.pdf -` "
               "(Bash) instead")


def hook_pre(board: Board, me: str, data: dict) -> dict | None:
    tool = data.get("tool_name")
    if tool in TASK_TOOLS:
        return task_rules(board, me, tool, data.get("tool_input") or {})
    if tool == "Read":
        pdf = str(_path(data) or "").lower().endswith(".pdf") and board.cfg["provider"].get("documents") is False
        return _deny(PDF_REFUSED) if pdf else None
    path = _path(data)
    if not path:
        return None
    rel = policy.relpath(board.root, path)
    why = board.write_block(me, rel) if rel is not None else None
    if rel is not None and why is None:
        return None
    absolute = path if os.path.isabs(path) else str(board.root / path)
    root = policy.fold(os.path.realpath(absolute)) == policy.fold(os.path.realpath(board.root))
    if rel is None and not root and not board.in_shared(absolute):
        return None
    if board.own_file(me, absolute):
        content = (data.get("tool_input") or {}).get("content")
        if board.is_note(absolute) and isinstance(content, str) and len(content) > board.note_max_chars():
            return _deny(f"a note holds at most {board.note_max_chars()} characters")
        return None
    if board.in_shared(absolute):
        return _deny(f"{path} is not yours to write: in the team folder you write your own notes, {board.notes_dir}/{me}-<topic>.md, and files "
                     f"under {board.artifacts_dir}/{me}/. To add to another member's note, write a note of your own or send that member a message")
    why = why or "it is the project folder itself: ask the leader"
    return _deny(prompts.render("deny_outside", board.cfg, path=path, region=json.dumps(board.write_area(me)), why=why))


def checked_pre(me: str, stdin) -> dict | None:
    try:
        return hook_pre(Board.from_env(), me, json.load(stdin))
    except Exception as e:
        return _deny(f"this call could not be checked ({type(e).__name__}: {str(e)[:200]}), so it is refused: try it again, and if it keeps "
                     "failing tell the leader with message")


def hook_post(board: Board, me: str, data: dict) -> dict | None:
    tool, ti = data.get("tool_name"), data.get("tool_input") or {}
    if tool in policy.EDIT_TOOLS and _path(data):
        board.log_edit(me, tool, policy.relpath(board.root, _path(data)) or _path(data))
    elif tool == "Bash" and isinstance(ti.get("command"), str):
        board.log_edit(me, "Bash", "", command=ti["command"][:SHELL_LOG_CHARS])
    if tool in TASK_TOOLS:
        tid, md = str(ti.get("taskId", "")), ti.get("metadata") or {}
        if tool == "TaskUpdate" and ti.get("status") == "pending" and isinstance(md, dict) and md.get(f"release:{me}"):
            board.release(me, tid, md[f"release:{me}"])
        board.sync_member(me)
        if tool == "TaskCreate" or ti.get("status") in ("completed", "pending") or isinstance(md, dict) and f"pass:{me}" in md:
            _route(board)
    msgs = board.take_mail(me, only_from="leader")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": with_lang(board, mail_text(board, msgs))}} if msgs else None


def _route(board: Board) -> None:
    from . import route
    try:
        route.run(board)
    except Exception as e:
        with open(board.dir / "errors.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": time.time(), "where": "route", "error": str(e)}) + "\n")


def _wait_for_work(board: Board, me: str) -> tuple[str | None, list[str]]:
    deadline, seen = time.monotonic() + board.cfg["hold_s"], None
    while True:
        version = board.version()
        if version != seen or board.mail_pending(me):
            seen = version
            m = board.member(me)
            if m is None or board.paused() or (m.get("provider") and moved_on(board, m["provider"])):
                return None, []
            msgs, tasks = board.take_mail(me), offer(board, m)
            if msgs or tasks:
                parts = [*([mail_text(board, msgs)] if msgs else []), *([offer_text(board, me, tasks)] if tasks else [])]
                return with_lang(board, "\n\n".join(parts)), [t["id"] for t in tasks]
        if time.monotonic() >= deadline:
            return None, []
        time.sleep(POLL_S)


def _end_of_turn(board: Board, me: str, offered: list[str], reminded: str | None) -> tuple[str | None, str | None]:
    m = board.member(me) or {}
    held = board.task(m.get("task"))
    if held and held["state"] == "claimed" and held["owner"] == me and m.get("assigned") != held["id"]:
        if reminded != held["id"]:
            return prompts.render("task_open", board.cfg, id=held["id"], name=me), held["id"]
        board.release(me, held["id"], "the member ended its turn twice without finishing the task")
        return None, None
    undecided = [tid for tid in offered if (t := board.task(tid)) and t["state"] == "open"
                 and me not in [*t["passed_by"], *t["released_by"], *t["volunteers"]]]
    if undecided:
        board.mark(me, undecided, "skip", "offered; neither taken nor declined")
    if offered:
        _route(board)
    return None, None


def _note(board: Board, me: str, **fields) -> None:
    with board.locked():
        m = board.member(me)
        if m is not None and any(m.get(k) != v for k, v in fields.items()):
            m.update(fields)
            board.put_member(m)


def _note_turn(board: Board, me: str, turn: dict | None) -> None:
    _note(board, me, turn=turn)


def _turn_started(board: Board, me: str) -> dict:
    turn = {"since": time.time(), "at": time.time(), "doing": "waiting", "chars": 0, "noted": 0.0}
    _note_turn(board, me, {k: v for k, v in turn.items() if k != "noted"})
    return turn


STREAM_DOING = {"thinking": "thinking", "text": "writing", "tool_use": "calling a tool", "server_tool_use": "searching"}


def _progress(board: Board, me: str, turn: dict, line: str) -> None:
    try:
        ev = json.loads(line).get("event") or {}
    except (ValueError, AttributeError):
        return
    if ev.get("type") == "content_block_start":
        turn["doing"] = STREAM_DOING.get((ev.get("content_block") or {}).get("type"), turn["doing"])
    elif ev.get("type") == "content_block_delta":
        d = ev.get("delta") or {}
        turn["chars"] += len(d.get("thinking") or d.get("text") or d.get("partial_json") or "")
    now = time.time()
    turn["at"] = now
    if now - turn["noted"] >= PROGRESS_S:
        turn["noted"] = now
        _note_turn(board, me, {k: v for k, v in turn.items() if k != "noted"})


def _err_tail(board: Board, me: str, since: int) -> str:
    try:
        with open(board.dir / "logs" / f"{me}.err", "rb") as f:
            f.seek(max(since, f.seek(0, os.SEEK_END) - ERR_TAIL_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return " | ".join([line.strip() for line in text.splitlines() if line.strip()][-ERR_TAIL_LINES:])


def _ended_badly(board: Board, me: str, cut_short: bool, status: int | None, last: dict | None, since: int) -> str | None:
    last = last or {}
    if cut_short:
        what = f"Claude Code ended before its turn's result ({f'exit status {status}' if status is not None else 'no exit status'})"
    elif status not in (0, None) and last.get("subtype") != TURN_LIMIT:
        what = f"Claude Code exited with status {status}" + (f" after a turn that failed: {str(last.get('result', ''))[:300]}" if last.get("is_error") else "")
    else:
        return None
    tail, where = _err_tail(board, me, since), board.dir / "logs" / f"{me}.err"
    return providers.shown(what + (f"; the end of {where}: {tail}" if tail else f"; nothing in {where}") + "; after fixing the cause, resume the crew")


class Input:
    def __init__(self, stream):
        self.stream, self.lock, self.closed = stream, threading.Lock(), False

    def send(self, line: str) -> bool:
        with self.lock:
            if self.closed:
                return False
            try:
                self.stream.write(line)
                self.stream.flush()
            except (OSError, ValueError):
                return False
            return True

    def close(self) -> None:
        with self.lock:
            self.closed = True
            with contextlib.suppress(OSError, ValueError):
                self.stream.close()


def control_reply(board: Board, me: str, request: dict) -> dict:
    rid, body = request.get("request_id"), request.get("request")
    body = body if isinstance(body, dict) else {}
    kind = body.get("subtype")
    if kind == "elicitation":
        return {"subtype": "success", "request_id": rid, "response": {"action": "decline"}}
    if kind == "can_use_tool":
        tool, given, call = body.get("tool_name"), body.get("input"), body.get("tool_use_id")
        if not isinstance(tool, str) or not isinstance(given, dict):
            return {"subtype": "error", "request_id": rid, "error": "a can_use_tool request names a tool and its input"}
        return {"subtype": "success", "request_id": rid, "response": policy.approve(board, me, tool, given, call if isinstance(call, str) else None)}
    return {"subtype": "error", "request_id": rid, "error": f"{providers.NAME} does not answer {kind} requests"}


class Controls:
    def __init__(self, board: Board, me: str, stdin: Input):
        self.board, self.me, self.stdin = board, me, stdin

    def start(self, request: dict) -> threading.Thread | None:
        rid = request.get("request_id")
        if not isinstance(rid, str):
            return None
        thread = threading.Thread(target=self._answer, args=(request,), name=f"control-{rid}", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self._send({"subtype": "error", "request_id": rid, "error": f"{providers.NAME} cannot answer just now"})
            return None
        return thread

    def _answer(self, request: dict) -> None:
        try:
            reply = control_reply(self.board, self.me, request)
        except Exception as e:
            reply = {"subtype": "error", "request_id": request["request_id"], "error": f"{providers.NAME} could not answer: {type(e).__name__}: {e}"}
        self._send(reply)

    def _send(self, reply: dict) -> None:
        self.stdin.send(json.dumps({"type": "control_response", "response": reply}, ensure_ascii=False) + "\n")

    def expire(self) -> None:
        ended = time.time()
        for kind in ("perms", "questions"):
            for r in self.board.pending(kind):
                if r.get("member") == self.me and r.get("ts", ended) <= ended:
                    with contextlib.suppress(ValueError):
                        self.board.answer(kind, r["id"], "expired", by=None)


def drive(board: Board, me: str, resume: bool, first: str) -> str:
    m = board.member(me)
    args = argv(board, m, resume=resume)
    ran = board.cfg["provider"]
    _note(board, me, sandbox=bool(board.cfg.get("sandbox")), provider=ran["name"])
    with open(board.dir / "logs" / f"{me}.err", "ab") as err:
        since = err.tell()
        proc = subprocess.Popen(args, cwd=board.root, env=env(board, m), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err, text=True, bufsize=1, **providers.SUBPROCESS_TEXT)
    stdin = Input(proc.stdin)
    controls = Controls(board, me, stdin)
    stdin.send(user_message(first))
    offered, reminded, state, last, cut_short, restart = list(m.get("offered") or []), None, "idle", None, False, False
    turn = _turn_started(board, me)
    with open(board.dir / "logs" / f"{me}.ndjson", "a", buffering=1, encoding="utf-8", newline="\n") as log:
        log.write(json.dumps(process_mark(board, m)) + "\n")
        for line in proc.stdout:
            if line.startswith('{"type":"stream_event"'):
                _progress(board, me, turn, line)
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                ev = None
            if isinstance(ev, dict) and ev.get("type") == "result":
                log.write(json.dumps({"type": TIME_MARK, "ts": time.time()}) + "\n")
            log.write(line)
            if isinstance(ev, dict) and ev.get("type") == "control_request":
                controls.start(ev)
            if not isinstance(ev, dict) or ev.get("type") != "result":
                continue
            last = ev
            _note_turn(board, me, None)
            if ev.get("api_error_status"):
                status, said = ev["api_error_status"], str(ev.get("result", ""))
                kind = providers.error_kind(ran, status, said)
                if moved_on(board, ran["name"]):
                    restart = True
                    break
                note = ""
                if kind and board.live_cfg().get("fallback"):
                    from . import settings
                    try:
                        moved, note = settings.fall_back(board, me, ran["name"], status, kind, said)
                    except Exception as e:
                        moved, note = None, f"The move to a backup provider failed ({type(e).__name__}: {e})."
                    if moved:
                        restart = True
                        break
                advice = ERROR_ADVICE.get(kind or status, "see the provider's message").format(key_advice=key_advice(ran["name"]))
                state, why = "failed", f"API error {status}: {advice}. " + (f"{said[:300].rstrip('. ')}. {note}" if note else said[:300])
                board.update_member(me, failure=why)
                board.send(me, "leader", "notice", f"{me} stopped: {why}")
                break
            if board.member(me) is None:
                break
            if check_budget(board) or board.paused():
                break
            if moved_on(board, ran["name"]):
                restart = True
                break
            text, reminded = _end_of_turn(board, me, offered, reminded)
            offered = []
            if text is None:
                text, offered = _wait_for_work(board, me)
                if text is None:
                    restart = moved_on(board, ran["name"])
                    break
                board.update_member(me, state="active", offered=offered, assigned=None)
            stdin.send(user_message(text))
            turn = _turn_started(board, me)
        else:
            cut_short = True
    try:
        stdin.close()
        status = proc.wait(timeout=DRIVER_EXIT_S)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        status = None
    controls.expire()
    if state != "failed" and not restart and board.member(me) is not None and (why := _ended_badly(board, me, cut_short, status, last, since)):
        state = "failed"
        board.update_member(me, failure=why)
        board.send(me, "leader", "notice", f"{me} stopped: {why}")
    with board.locked():
        m = board.member(me)
        if m is not None:
            if m.get("task"):
                board._release(me, m["task"], by_choice=False)
                m = board.member(me)
            m.update(state=state, pid=None, offered=[], turn=None, updated=time.time())
            board.put_member(m)
            board.bump()
    if restart:
        _restart(board)
    return state


def moved_on(board: Board, ran_on: str) -> bool:
    return board.live_cfg()["provider"]["name"] != ran_on


def _restart(board: Board) -> None:
    while _HELD:
        _HELD.pop().close()
    board.cfg = board.live_cfg()
    _route(board)


STR = {"type": "string"}
MAX_OPTIONS = 6
GIST_CHARS = 120
PATHISH = re.compile(r"(?<![\w/])(?:~|\.{1,2})?/?(?:[\w.@-]+/)+[\w.@-]+")
TOOLS = [
    {"name": "message", "description": "Send a message to the leader, to a member by name, or to 'all'. Progress reports, questions and "
     "disputes go to the leader. With wait=true (to the leader only) you wait for the leader's answer; the leader may ask the user. options: the answers you "
     "expect, shown to the user as buttons. to='user' answers the user, who wrote to you from the team's page (the leader gets a copy). gist: what the "
     f"message says in one plain sentence for the user watching the team (no file paths, no code, at most {GIST_CHARS} characters); the recipient gets text "
     "in full. kind: kickoff (you start a task: your plan), progress, done (the result and how you checked it), blocker (what stops you or needs a "
     "decision), fyi (the default).",
     "inputSchema": {"type": "object", "properties": {"to": STR, "text": STR, "gist": {"type": "string", "maxLength": GIST_CHARS},
                                                      "kind": {"type": "string", "enum": list(REPORTS)}, "wait": {"type": "boolean"},
                                                      "options": {"type": "array", "items": STR, "maxItems": MAX_OPTIONS}},
                     "required": ["to", "text"]}},
    {"name": "approve", "description": "Permission prompts for this member (called by Claude Code, not by the model).",
     "inputSchema": {"type": "object", "properties": {"tool_name": STR, "input": {"type": "object"}, "tool_use_id": STR},
                     "required": ["tool_name", "input"]}},
]
INT = {"type": "integer"}
SCREENSHOT = {"name": "screenshot", "description": "See a page of the project as a browser draws it: the project folder is served at "
              "http://127.0.0.1 for this call and the page opens in a headless browser (WebGL works; the page reaches no other address). "
              "Returns the picture, the page's console lines, its uncaught errors and the files it asked for that were missing. path: the "
              "page, relative to the project (default index.html); query: what follows it, such as ?t=2.5; width and height in pixels "
              "(default 960x540); wait_ms: page time before the picture (default 3000); save: a .png path in your region or your artifacts "
              "folder to keep the picture.",
              "inputSchema": {"type": "object", "properties": {"path": STR, "query": STR, "width": INT, "height": INT, "wait_ms": INT, "save": STR}}}


def tools_for(board: Board) -> list[dict]:
    from . import browser
    return TOOLS + ([SCREENSHOT] if browser.find() else []) + [{k: v for k, v in t.items() if k != "call"} for t in extension_tools(board)]


_SKIPPED: set[str] = set()


def _left_out(why: str) -> None:
    if why not in _SKIPPED:
        _SKIPPED.add(why)
        print(f"{providers.NAME}: {why}", file=sys.stderr)


def _tool_problem(t, taken: set[str]) -> str | None:
    if not isinstance(t, dict) or not isinstance(t.get("name"), str):
        return "is not a dict with a name"
    if t["name"] in taken:
        return "has the name of another tool"
    if not callable(t.get("call")):
        return "has no call"
    if not isinstance(t.get("inputSchema"), dict) or t["inputSchema"].get("type") != "object":
        return "has no inputSchema of type object"
    try:
        json.dumps({k: v for k, v in t.items() if k != "call"})
    except (TypeError, ValueError):
        return "cannot be sent as JSON"
    return None


def extension_tools(board: Board) -> list[dict]:
    out, taken = [], {"message", "approve", "screenshot"}
    for got in extensions.call("tools", board, project=board.root):
        if got is None:
            continue
        if not isinstance(got, list):
            _left_out(f"an extension's tools hook returned {type(got).__name__}, not a list of tools: left out")
            continue
        for t in got:
            if why := _tool_problem(t, taken):
                label = repr(t["name"]) if isinstance(t, dict) and isinstance(t.get("name"), str) else repr(t)[:40]
                _left_out(f"an extension's tool {label} {why}: left out")
            else:
                out.append(t)
                taken.add(t["name"])
    return out


def call(board: Board, me: str, name: str, a: dict):
    if name == "message":
        to, text, wait, options, short, report = a["to"], a["text"], a.get("wait", False), a.get("options") or [], a.get("gist"), a.get("kind") or "fyi"
        if not isinstance(to, str) or not isinstance(text, str) or not isinstance(wait, bool):
            raise ValueError("to and text are strings; wait is true or false")
        if short is not None and (not isinstance(short, str) or len(short) > GIST_CHARS):
            raise ValueError(f"gist is one sentence of at most {GIST_CHARS} characters")
        short = " ".join(short.split()) if short else None
        if short and (why := gist_problem(board, short)):
            raise ValueError(why)
        if report not in REPORTS:
            raise ValueError(f"kind is one of {', '.join(REPORTS)}")
        if not isinstance(options, list) or len(options) > MAX_OPTIONS or not all(isinstance(o, str) and 0 < len(o.strip()) <= 200 for o in options):
            raise ValueError(f"options is a list of at most {MAX_OPTIONS} short answers")
        task = (board.member(me) or {}).get("task")
        if wait:
            if to != "leader":
                raise ValueError("only the leader answers questions: send it to 'leader'")
            if not text.strip() or len(text) > 20000:
                raise ValueError("text must be 1-20000 characters")
            r = board.request("questions", me, {"question": text, "task": task, "answer": None, "options": [o.strip() for o in options],
                                                **({"gist": short} if short else {})})
            timeout = board.live_cfg()["question_timeout_s"]
            r = board.wait("questions", r["id"], timeout)
            return {"answer": r["answer"]} if r["state"] == "answered" else {"answer": None, "note": f"no answer from the leader within {timeout} s"}
        names = {m["name"] for m in board.members()}
        if to not in names | {"leader", "all", "user"} or to == me:
            raise ValueError(f"unknown recipient {to!r}; use 'leader', 'user', 'all' or one of {sorted(names - {me})}")
        if to == "user":
            if not in_lang(text, lang := reply_lang(board.live_cfg())):
                raise ValueError(f"write to the user in {lang}: the user reads replies in {lang}")
            board.send(me, "user", "message", text, task, gist=short, report=report)
            board.send(me, "leader", "notice", f"(copy) {me} wrote to the user: {text}", task, gist=short, report=report)
            return {"sent": 1}
        targets = sorted(names - {me}) if to == "all" else [to]
        return {"sent": len([board.send(me, x, "message", text, task, gist=short, report=report) for x in targets])}
    if name == "approve":
        return policy.approve(board, me, a["tool_name"], a.get("input") or {}, a.get("tool_use_id"))
    if name == "screenshot":
        return screenshot(board, me, a)
    if tool := next((t for t in extension_tools(board) if t["name"] == name), None):
        return tool["call"](board, me, a)
    raise ValueError(f"unknown tool {name!r}")


def _save_place(board: Board, me: str, target: Path) -> tuple[str, list[str]]:
    absolute = target if target.is_absolute() else board.root / target
    rel = policy.relpath(board.root, str(absolute))
    mine = board.artifacts_dir / me
    if absolute.suffix.lower() == ".png" and rel is not None and board.write_block(me, rel) is None:
        return os.path.realpath(board.root), rel.split("/")
    if absolute.suffix.lower() == ".png" and board.own_file(me, str(absolute)) and not board.is_note(str(absolute)):
        mine.mkdir(parents=True, exist_ok=True)
        return os.path.realpath(mine), Path(os.path.relpath(os.path.realpath(absolute.parent), os.path.realpath(mine))).joinpath(absolute.name).parts
    raise ValueError(f"save is a .png path inside your region {board.write_area(me)} or under {mine}")


def screenshot(board: Board, me: str, a: dict) -> dict:
    from . import browser
    nums = {k: a.get(k) for k in ("width", "height", "wait_ms")}
    if any(v is not None and (isinstance(v, bool) or not isinstance(v, int)) for v in nums.values()):
        raise ValueError("width, height and wait_ms are whole numbers")
    w = min(max(nums["width"] or browser.SIZE[0], browser.MIN_SIZE[0]), browser.MAX_SIZE[0])
    h = min(max(nums["height"] or browser.SIZE[1], browser.MIN_SIZE[1]), browser.MAX_SIZE[1])
    wait = min(max(nums["wait_ms"] if nums["wait_ms"] is not None else browser.WAIT_MS, 0), browser.MAX_WAIT_MS)
    path, query, save = a.get("path") or "index.html", a.get("query") or "", a.get("save")
    if not all(isinstance(x, str) for x in (path, query)) or (save is not None and not isinstance(save, str)):
        raise ValueError("path, query and save are strings")
    sees = member_model(board, board.member(me))[0].split("[")[0] in (board.cfg["provider"].get("vision") or [])
    target = Path(save) if save else None if sees else board.artifacts_dir / me / f"screenshot-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}.png"
    if target is not None:
        _save_place(board, me, target)
    try:
        closed = (*policy.protected(board), *map(os.path.expanduser, policy.CLOSED_TO_LINKS))
        shot = browser.shoot(board.root, path, query, (w, h), wait, closed=closed)
    except OSError as e:
        raise ValueError(f"the screenshot failed: {e}") from None
    out = {k: shot[k] for k in ("url", "console", "errors", "missing") if shot[k] or k == "url"}
    if shot["dropped"]:
        out["console_dropped"] = shot["dropped"]
    if target is not None:
        top, parts = _save_place(board, me, target)
        try:
            browser.write_inside(top, parts, shot["png"])
        except OSError as e:
            raise ValueError(f"the picture was not saved: {e}") from None
        absolute = os.path.join(top, *parts)
        board.log_edit(me, "screenshot", policy.relpath(board.root, absolute) or absolute)
        out["saved"] = str(target)
    if sees:
        out["image_png"] = base64.b64encode(shot["png"]).decode("ascii")
    else:
        out["note"] = "your model does not take images: the picture is saved for others to look at"
    return out


def serve(board: Board, me: str) -> None:
    real, written = sys.stdout, threading.Lock()
    sys.stdout = sys.stderr
    running: list[threading.Thread] = []
    listed = False

    def reply(i, result=None, error=None) -> None:
        line = json.dumps({"jsonrpc": "2.0", "id": i, **({"error": error} if error else {"result": result})}, ensure_ascii=False) + "\n"
        with written:
            real.write(line)
            real.flush()

    def answer(i, params) -> None:
        try:
            p = params or {}
            result, is_error = call(board, me, p.get("name"), p.get("arguments") or {}), False
            image = result.pop("image_png", None) if isinstance(result, dict) else None
            text = json.dumps(result, ensure_ascii=False)
        except Exception as e:
            result, is_error, image = {"error": str(e) if isinstance(e, ValueError | KeyError | TypeError) else f"{type(e).__name__}: {e}"}, True, None
            text = json.dumps(result, ensure_ascii=False)
        with contextlib.suppress(OSError):
            reply(i, {"content": [*([{"type": "image", "data": image, "mimeType": "image/png"}] if image else []),
                                  {"type": "text", "text": text}], "isError": is_error})

    try:
        for line in sys.stdin:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            method, i = (m.get("method"), m.get("id")) if isinstance(m, dict) else (None, None)
            if i is None:
                continue
            if method == "initialize":
                reply(i, {"protocolVersion": (m.get("params") or {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                          "serverInfo": {"name": "deepcrew", "version": __version__}})
            elif method == "tools/list":
                try:
                    tools = tools_for(board)
                except Exception as e:
                    print(f"{providers.NAME}: listing the tools failed ({type(e).__name__}: {e}); only message and approve are listed", file=sys.stderr)
                    tools = TOOLS
                listed = True
                reply(i, {"tools": tools})
            elif method == "tools/call":
                if not listed:
                    listed = True
                    with contextlib.suppress(Exception):
                        extension_tools(board)
                running[:] = [t for t in running if t.is_alive()]
                thread = threading.Thread(target=answer, args=(i, m.get("params")), name=f"call-{i}", daemon=True)
                try:
                    thread.start()
                    running.append(thread)
                except RuntimeError:
                    answer(i, m.get("params"))
            elif method == "ping":
                reply(i, {})
            else:
                reply(i, error={"code": -32601, "message": f"method not found: {method}"})
        for t in running:
            t.join()
    finally:
        sys.stdout = real


def utf8_stdio() -> None:
    for stream, extra in ((sys.stdin, {}), (sys.stdout, {"newline": "\n"}), (sys.stderr, {})):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", **extra)


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    if providers.is_windows():
        utf8_stdio()
    me = os.environ.get("DEEPCREW_MEMBER")
    if not me or not os.environ.get("DEEPCREW_TEAM_DIR"):
        sys.stderr.write("deepcrew.member runs only inside a member started by deepcrew\n")
        return 2
    if args == ["hook", "pre"]:
        out = checked_pre(me, sys.stdin)
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0
    board = Board.from_env()
    if args[:1] == ["drive"] and len(args) in (2, 3):
        m = board.member(me)
        if m is None or m["session"] != args[1]:
            sys.stderr.write("this session is not the member's\n")
            return 2
        if providers.is_windows() and not hold_alive(alive_path(board, m["session"])):
            sys.stderr.write("another driver of this session is running\n")
            return 2
        drive(board, me, args[2:] == ["--resume"], sys.stdin.read())
        return 0
    if args == ["hook", "post"]:
        out = hook_post(board, me, json.load(sys.stdin))
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0
    if args == ["mcp"]:
        serve(board, me)
        return 0
    if args == ["key"]:
        found = providers.resolve_key(board.cfg["provider"])
        if sys.stdout.isatty() or found is None:
            sys.stderr.write("no key, or stdout is a terminal\n")
            return 1
        sys.stdout.write(found[1])
        return 0
    sys.stderr.write("usage: python -m deepcrew.member drive SESSION [--resume] | hook pre|post | mcp | key\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
