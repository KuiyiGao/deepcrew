from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import extensions, member, providers
from .board import state_home
from .settings import DEFAULT_TOOLS, claude_bin, create_team, git_state, key_advice, sandbox_on, sandbox_settings, user_settings
from .standin import StandIn

CMD = providers.NAME

MEMBER_MB = 400
CONFORMANCE_TIMEOUT_S = 120.0
CONFORMANCE_MARK = "deepcrew conformance: the project memory reached the member"
CONFORMANCE_OUTSIDE = "deepcrew conformance: a file outside the project reached the member"
MIN_CLAUDE = "2.1.133"
REQUIRED_FLAGS = ("--settings", "--setting-sources", "--mcp-config", "--strict-mcp-config", "--tools", "--session-id", "--resume",
                  "--append-system-prompt", "--input-format", "--output-format")
SANDBOX_TOOLS = {"darwin": ("/usr/bin/sandbox-exec",), "linux": ("bwrap", "socat")}


def check(id_: str, level: str, detail: str, fix: str = "") -> dict:
    return {"id": id_, "level": level, "detail": detail, "fix": fix}


def sandbox_check(platform: str) -> dict:
    tools, off = SANDBOX_TOOLS.get(platform), f"or run `{CMD} config set sandbox off`"
    if tools is None:
        return check("sandbox", "fail", f"Claude Code has no sandbox on {platform}", f"run deepcrew inside WSL2, {off}")
    missing = [t for t in tools if not shutil.which(t)]
    return check("sandbox", "fail" if missing else "ok", f"members' commands run in Claude Code's sandbox ({', '.join(tools)})"
                 + (f"; missing {', '.join(missing)}" if missing else ""), f"install {' and '.join(missing)}, {off}" if missing else "")


def host_memory_mb() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**20
    except (AttributeError, ValueError, OSError):
        return None


MARKETPLACE = "KuiyiGao/deepcrew"


def plugin_check(claude: str) -> dict | None:
    if os.environ.get("CLAUDECODE") != "1" or CMD != "deepcrew":
        return None
    try:
        r = subprocess.run([claude, "plugin", "list", "--json"], capture_output=True, text=True, check=False, timeout=30, **providers.SUBPROCESS_TEXT)
        listed = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(listed, list):
        return None
    mine = [p for p in listed if isinstance(p, dict) and str(p.get("id", "")).split("@")[0] == "deepcrew"]
    exe = f'"{claude}"'
    if any(p.get("enabled") for p in mine):
        return check("plugin", "ok", ", ".join(str(p["id"]) for p in mine if p.get("enabled")))
    if mine:
        return check("plugin", "warn", f"{mine[0]['id']} is installed but turned off", f"{exe} plugin enable {mine[0]['id']}")
    return check("plugin", "warn", "the DeepCrew plugin (the /deepcrew skill and the mod) is not in this Claude Code",
                 f"{exe} plugin marketplace add {MARKETPLACE}, then {exe} plugin install deepcrew@deepcrew; Claude Code loads it in the next session")


def host_checks(project: Path) -> list[dict]:
    out = [check("python", "ok" if sys.version_info >= (3, 11) else "fail", sys.version.split()[0], "install Python 3.11 or newer")]
    claude = claude_bin()
    if not claude:
        out.append(check("claude", "fail", "no claude CLI: none on PATH, and this command was not started from Claude Code",
                         "install Claude Code, or set DEEPCREW_CLAUDE to its path"))
    elif providers.is_windows() and Path(claude).suffix.lower() in (".cmd", ".bat"):
        out.append(check("claude", "fail", f"{claude} is a batch shim, which cannot pass the members' multi-line prompts",
                         "install Claude Code with the native installer (claude.exe), or set DEEPCREW_CLAUDE to claude.exe"))
    else:
        version = subprocess.run([claude, "--version"], capture_output=True, text=True, check=False, **providers.SUBPROCESS_TEXT).stdout.strip()
        helptext = subprocess.run([claude, "--help"], capture_output=True, text=True, check=False, **providers.SUBPROCESS_TEXT).stdout
        missing = [f for f in REQUIRED_FLAGS if f not in helptext]
        found = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        ver = tuple(map(int, found.groups())) if found else None
        if missing:
            out.append(check("claude", "fail", f"{claude} {version}; missing {', '.join(missing)}", "update Claude Code"))
        elif ver is not None and ver < tuple(map(int, MIN_CLAUDE.split("."))):
            out.append(check("claude", "warn", f"{claude} {version}; deepcrew needs {MIN_CLAUDE} or newer", "update Claude Code"))
        else:
            out.append(check("claude", "ok", f"{claude} {version}"))
        out.extend(filter(None, [plugin_check(claude)]))
    home = state_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / f".probe-{os.getpid()}").write_text("x", encoding="utf-8")
        (home / f".probe-{os.getpid()}").unlink()
        inside = str(home.resolve()).startswith(str(project.resolve()) + os.sep)
        out.append(check("state", "fail" if inside else "ok", str(home), "set DEEPCREW_HOME outside the project" if inside else ""))
    except OSError as e:
        out.append(check("state", "fail", f"{home}: {e}", "make it writable, or set DEEPCREW_HOME; an agent sandbox may block it"))
    g = git_state(project)
    if not g["base"]:
        out.append(check("git", "warn", f"no git repository with a commit: `{CMD} audit` is unavailable", "git init and commit first"))
    else:
        notes = ([] if Path(g["repo"]).resolve() == project.resolve() else [f"inside the repository at {g['repo']}"]) + \
                (["uncommitted changes (the audit will include them)"] if g["dirty"] else [])
        out.append(check("git", "warn" if notes else "ok", "; ".join(notes) or g["base"][:12], "commit or stash first" if g["dirty"] else ""))
    if sandbox_on():
        out.append(sandbox_check(sys.platform))
    size = user_settings()["max_active"]
    if (ram := host_memory_mb()) and size * MEMBER_MB > ram / 2:
        out.append(check("memory", "warn", f"{size} members take about {size * MEMBER_MB} MB, more than half of this host's {ram} MB",
                         f"choose a smaller team size: {CMD} config preset squad"))
    if specs := extensions.declared(project):
        bad = extensions.failures(project)
        every = set(bad) & set(extensions.everywhere())
        out.append(check("extensions", "warn" if bad else "ok", ", ".join(specs) + "".join(f"; not loaded: {s} ({why})" for s, why in bad.items()),
                         f"put them in the project or install them, or change this project's list: {CMD} config set extensions ... (none "
                         "removes it)" + (f"; {', '.join(sorted(every))} {'is' if len(every) == 1 else 'are'} declared for every project: "
                                          f"{CMD} config set extensions ... --global" if every else "") if bad else ""))
    who = "claude-code" if os.environ.get("CLAUDECODE") == "1" else "terminal" if providers.can_prompt() else "generic"
    out.append(check("leader", "ok", f"{who}; terminal prompts {'yes' if providers.can_prompt() else 'no'}"))
    return out


def provider_checks(p: dict, offline: bool, model: str | None = None) -> list[dict]:
    found = providers.resolve_key(p)
    if not found:
        return [check("key", "fail", f"no {p['name']} key in {', '.join(p['key_sources'])}", key_advice(p["name"]))]
    out = [check("key", "ok", f"found in {found[0]}")]
    model = model or providers.choices()["model"] or p["models"][0]
    if p.get("prices") and model.split("[")[0] not in p["prices"]:
        out.append(check("prices", "warn", f"no prices for {model}: the page shows no cost and the budget is not counted",
                         f"use one of {', '.join(p['models'])}, or add its prices to providers.toml"))
    elif not p.get("prices"):
        out.append(check("prices", "warn", f"costs are not tracked for {p['name']}: its entry has no prices in USD, so the page shows no cost "
                                           "and a budget cannot be counted", "add prices in USD to its entry in providers.toml to track them"))
    if offline:
        return out + [check("api", "skip", "offline")]
    if not p.get("balance_url"):
        return out + [check("api", "skip", "not checked: the provider has no balance endpoint to ask")]
    b = providers.balance(p, found[1])
    if b["ok"]:
        return out + [check("api", "ok", f"balance {b.get('balances') or b.get('detail')}")]
    rejected, empty = f"the key was rejected: {key_advice(p['name'])}", "the account has no balance: top it up"
    fix = {401: rejected, 403: rejected, 402: empty, 200: empty}.get(b.get("status"), "check the network; an agent sandbox may block it")
    return out + [check("api", "fail", b.get("detail") or "the provider refused the balance request", fix)]


def fallback_checks(tables: list[dict], own: str) -> list[dict]:
    backups = [p for p in tables if p["name"] != own]
    if not backups:
        return []
    missing = [p["name"] for p in backups if providers.resolve_key(p) is None]
    detail = "; ".join(f"{p['name']} " + ("without a key" if p["name"] in missing else "ready") + ("" if p.get("prices") else ", costs not tracked")
                       for p in backups)
    return [check("fallback", "warn" if missing else "ok", f"backups in order: {detail}",
                  "; ".join(f"{CMD} auth set {name} in your own terminal" for name in missing))]


def conformance(claude: str) -> dict:
    pack = providers.setting("DEEPCREW_PROMPT_PACK")
    with tempfile.TemporaryDirectory(prefix="deepcrew-check-") as tmp, StandIn() as si:
        root = Path(tmp).resolve()
        proj = root / "proj"
        (proj / "src").mkdir(parents=True)
        (proj / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
        (proj / "docs").mkdir()
        (proj / "docs" / "notes.md").write_text(f"{CONFORMANCE_MARK}\n", encoding="utf-8")
        (root / "outside.md").write_text(f"{CONFORMANCE_OUTSIDE}\n", encoding="utf-8")
        outside = (root / "outside.md").as_posix().replace(" ", "\\ ")
        (proj / "CLAUDE.md").write_text(f"Notes: @docs/notes.md\nNot a note: @{outside}\n", encoding="utf-8")
        for args in (("init", "-q"), ("add", "-A"), ("-c", "user.name=deepcrew", "-c", "user.email=deepcrew@localhost", "commit", "-qm", "base")):
            subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True)
        (root / "key").write_text("sk-standin-key-0000", encoding="utf-8")
        p = {"name": "standin", "base_url": si.base_url, "models": ["standin"], "efforts": ["high"], "fast_model": "standin",
             "auto_compact_window": 100000, "timeout_ms": 60000, "key_sources": [f"file:{root / 'key'}"], "key_pattern": ".+"}
        try:
            board = create_team(proj, "check", p, {"model": "standin", "effort": "high", "thinking": True}, claude=claude, team_dir=root / "team",
                                hold_s=0, max_turns=20, approval_timeout_s=5, bash_allow=["python3"] if sandbox_settings(p) else [],
                                tools=list(DEFAULT_TOOLS), allow_off=[], project_memory=True, pack=pack, fallback=[])
        except ValueError as e:
            return check("conformance", "fail", str(e), f"fix the pack, or go back to the shipped one: {CMD} config set pack basic")
        t = board.post("conformance", "write src/ok.py, try a file outside the region", ["src/"], [], by="leader")
        si.actions[t["id"]] = [("TaskList", {}), ("Write", {"file_path": str(proj / "src" / "ok.py"), "content": "OK = 1\n"}),
                               ("Write", {"file_path": str(proj / "outside.py"), "content": "X = 1\n"})]
        if board.cfg.get("sandbox"):
            si.actions[t["id"]].append(("Bash", {"command": f"python3 -c \"print(open('{root / 'key'}').read())\"", "description": "probe"}))
        problems = []
        try:
            member.start(board, "probe", ["src/"], t["id"], "conformance probe")
        except member.LaunchError as e:
            problems.append(str(e))
        try:
            deadline = time.monotonic() + CONFORMANCE_TIMEOUT_S
            while time.monotonic() < deadline and any(m["state"] in ("starting", "active") for m in board.members()):
                time.sleep(member.POLL_S)
            if any(m["state"] in ("starting", "active") for m in board.members()):
                problems.append(f"the member was still working after {CONFORMANCE_TIMEOUT_S:.0f} s")
        finally:
            member.stop(board, "probe")
        if board.task(t["id"])["state"] != "done":
            problems.append(f"task ended {board.task(t['id'])['state']}")
        if not (proj / "src" / "ok.py").exists():
            problems.append("the write inside the region did not happen")
        if (proj / "outside.py").exists():
            problems.append("a write outside the region was not refused")
        reqs = [r for r in si.requests if r["body"].get("tools")]
        if not reqs:
            problems.append("no model request arrived")
        elif any(r["authorization"] != "Bearer sk-standin-key-0000" for r in reqs):
            problems.append("a request carried a different credential")
        elif any("x-anthropic-billing-header" in str(r["body"].get("system")) for r in reqs):
            problems.append("the attribution header is still in the system prompt")
        elif not {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet"} <= {x.get("name") for x in reqs[0]["body"]["tools"]}:
            problems.append("this Claude Code version does not give members the Task tools (the team's task list)")
        elif "mcp__deepcrew__approve" in {x.get("name") for x in reqs[0]["body"]["tools"]}:
            problems.append("the permission tool is visible to the model")
        elif board.cfg.get("sandbox") and any("sk-standin-key-0000" in json.dumps(r["body"]) for r in si.requests):
            problems.append("the sandbox did not keep the key file unreadable")
        sent = reqs[0]["body"] if reqs else {}
        if reqs and (sent.get("output_config") or {}).get("effort") != "high":
            problems.append("this Claude Code version does not send the effort level to the provider")
        if reqs and CONFORMANCE_MARK not in json.dumps(sent):
            problems.append("the project's CLAUDE.md that deepcrew hands its members, or the file it imports, did not reach the model")
        if reqs and CONFORMANCE_OUTSIDE in json.dumps(sent):
            problems.append("a file outside the project, named in the project's CLAUDE.md, reached the model")
        name = (Path(pack).name or pack) if pack else "basic"
        system = sent.get("system") or []
        said = [system] if isinstance(system, str) else [b["text"] for b in system if isinstance(b, dict) and isinstance(b.get("text"), str)]
        if reqs and not any(member.system_prompt(board) in s for s in said):
            problems.append(f"the team protocol of prompt pack {name} did not reach the model")
        if problems:
            tail = " ".join(e.read_text(encoding="utf-8", errors="replace")[-300:] for e in (board.dir / "logs").glob("*.err")).strip()
            return check("conformance", "fail", "; ".join(problems) + (f" (stderr: {tail})" if tail else ""),
                         "this Claude Code version behaves differently from the one deepcrew was tested with")
        thinking = (sent.get("thinking") or {}).get("type") or "no"
        return check("conformance", "ok", f"{len(reqs)} model requests; region, hooks, MCP tools, key helper"
                                          + (" and sandbox" if board.cfg.get("sandbox") else "") + " behave as expected; requests carry "
                                          f"effort high, {thinking} thinking, max_tokens {sent.get('max_tokens')}, the team protocol of prompt pack {name} "
                                          "and the project's CLAUDE.md with its import")
