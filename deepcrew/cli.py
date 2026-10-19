from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__
from . import extensions, member, policy, presets, prompts, providers, review, route, toolkit
from .board import LANGS, MEMBER_NAME, Board, in_lang, read_json, read_jsonl, region_at, reply_lang, state_home, team_path, tools_dir, write_json
from .board import SUMMARY_PARTS

from .doctor import SANDBOX_TOOLS, check, conformance, fallback_checks, host_checks, provider_checks
from .inbox import ADD_DIR_HINT, QUIET, add_dir, failed_words, inbox_items, money, page_url, share_words, span, utc_at
from .inbox import widened_words, with_widening, _item_line
from .inbox import _proposal_lines
from .settings import DEFAULTS, DENY_READ, OPTIONAL_TOOLS, PAGE_KEYS, SETUP_FILE, UNTRACKED_AT_START, UNTRACKED_MAX
from .settings import allow_setting, claude_bin, create_team, default_provider, fallback_setting, fallback_tables, key_advice, key_place
from .settings import left_out, listed, member_config, on_provider, page_checked, positive, provider_view, refresh_install, refresh_key_copy
from .settings import sandbox_on, setting_text, setup_state, shared_folder, sync_team, untracked_files, user_settings
from .doctor import MEMBER_MB, REQUIRED_FLAGS, host_memory_mb, sandbox_check
from .inbox import price_notice
from .settings import DEFAULT_TOOLS, MAX_MEMBERS, key_copy, protected_paths, sandbox_settings, team_allow, tools_setting

CMD = providers.NAME

DESCRIPTION = f"{providers.NAME}: lead a team of Claude Code members driven by DeepSeek or another Anthropic-compatible model."


SHELL_WINDOW_S = 600.0


def audit(board: Board) -> dict:
    base = board.cfg["git"].get("base")
    if not base:
        return {"error": "the project was not a git repository with a commit when the team started"}

    def git(*args: str) -> set[str]:
        r = subprocess.run(["git", "-C", str(board.root), *args, "-z"], capture_output=True, text=True, check=False, **providers.SUBPROCESS_TEXT)
        return {p for p in r.stdout.split("\0") if p} if r.returncode == 0 else set()

    edits: dict[str, set[str]] = {}
    shells: list[dict] = []
    last: dict[str, float] = {}
    helped: dict[str, set[tuple[str, str]]] = {}
    members, files = board.members(), []
    by_name = {m["name"]: m for m in members}
    scopes = {t["id"]: t["scope"] or [] for t in board.tasks()}
    leased: dict[str, set[str]] = {}
    foreign: dict[str, set[str]] = {}
    for e in read_jsonl(board.dir / "edits.jsonl"):
        if e["tool"] == "Bash":
            shells.append(e)
            continue
        edits.setdefault(e["path"], set()).add(e["member"])
        last[e["path"]] = max(last.get(e["path"], 0), e["ts"])
        if e.get("task") and policy.covers(scopes.get(e["task"], []), e["path"]):
            leased.setdefault(e["path"], set()).add(e["member"])
        elif not ((m := by_name.get(e["member"])) and policy.covers(region_at(m, e["ts"]), e["path"])):
            foreign.setdefault(e["path"], set()).add(e["member"])
        if e.get("for_owner"):
            helped.setdefault(e["path"], set()).add((e["member"], e["for_owner"]))
    start = read_json(board.dir / UNTRACKED_AT_START) if (board.dir / UNTRACKED_AT_START).exists() else {}
    before = untracked_files(board.root) if start and "" not in start else {}
    too_many = "" in start or "" in before
    kept = {p for p, v in before.items() if p and start.get(p) == v}
    for path in sorted(git("diff", "--relative", "--name-only", base) | (git("ls-files", "--others", "--exclude-standard") - kept)):
        owner = next((m["name"] for m in members if policy.covers(m["region"], path)), None)
        by = sorted(edits.get(path, set()))
        allowed = {owner, *leased.get(path, ()), *(who for who, for_owner in helped.get(path, ()) if for_owner == owner)}
        found = _shell_writers(board.root / path, shells, after=last.get(path, 0))
        who = list(dict.fromkeys(e["member"] for e in found))
        unowned = owner is None and not leased.get(path)
        flags = ((["outside_every_region"] if unowned else []) + (["shell_edit"] if found else ["no_logged_edit"] if not by else [])
                 + (["edited_by_non_owner"] if owner and (foreign.get(path, set()) - allowed or who and not set(who) & allowed) else []))
        shell = {"member": who[0], "command": found[0]["command"], **({"others": who[1:]} if who[1:] else {})} if found else None
        files.append({"path": path, "owner": owner, "edited_by": by, "flags": flags, **({"shell": shell} if shell else {}),
                      **({"leased_to": sorted(leased[path])} if leased.get(path) else {})})
    return {"base": base, "dirty_at_start": board.cfg["git"].get("dirty", False), "files": files, "untracked_unchanged": len(kept),
            "untracked_too_many": too_many}


def _shell_writers(path: Path, shells: list[dict], after: float = 0) -> list[dict]:
    try:
        changed = path.stat().st_mtime
    except OSError:
        return []
    return [] if changed <= after else [e for e in shells if changed <= e["ts"] <= changed + SHELL_WINDOW_S]


def session_id() -> str | None:
    return re.sub(r"[^A-Za-z0-9._-]", "_", os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()) or None


def leader_records() -> list[tuple[Path, dict]]:
    out = []
    for path in sorted((state_home() / "leaders").glob("*.json")):
        try:
            out.append((path, read_json(path)))
        except (OSError, ValueError):
            continue
    return out


def session_refusal(team_dir: Path) -> str | None:
    sid = session_id()
    rec = next((r for p, r in leader_records() if p.stem == sid), None) if sid else None
    led = rec.get("team_dir") if rec else None
    if not led or led == str(team_dir) or not (Path(led) / "team.json").exists():
        return None
    return (f"this Claude Code session already leads team {rec['team']!r} of {rec['project']}; finish it first: "
            + shlex.join([CMD, "stop", "--all", "--project", rec["project"], "--team", rec["team"]]))


def record_leader(project: Path, team: str, team_dir: Path) -> None:
    sid = session_id()
    if sid:
        (state_home() / "leaders").mkdir(parents=True, exist_ok=True)
        write_json(state_home() / "leaders" / f"{sid}.json", {"project": str(project), "team": team, "team_dir": str(team_dir), "ts": time.time()})


LOCAL_MEMORY = "CLAUDE.local.md"


def _binding_marks(team: str) -> tuple[str, str]:
    return f"<!-- deepcrew team {team}: begin -->", f"<!-- deepcrew team {team}: end -->"


def _without_note(text: str, team: str) -> str:
    begin, end = _binding_marks(team)
    lines, out, inside = text.splitlines(), [], False
    for line in lines:
        if line.strip() == begin:
            inside = True
        elif inside and line.strip() == end:
            inside = False
        elif not inside:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")


def bind_claude(board: Board) -> None:
    path, team = board.root / LOCAL_MEMORY, board.cfg["team"]
    try:
        old = path.read_text(encoding="utf-8") if path.exists() else None
    except (OSError, UnicodeDecodeError):
        return
    begin, end = _binding_marks(team)
    rest = _without_note(old or "", team)
    note = f"{begin}\n{prompts.render('leader_binding', board.cfg, team=team, cmd=CMD)}\n{end}\n"
    new = (rest + "\n\n" if rest else "") + note
    if new != old:
        with contextlib.suppress(OSError):
            path.write_text(new, encoding="utf-8")
    if old is None and subprocess.run(["git", "-C", str(board.root), "check-ignore", "-q", LOCAL_MEMORY], capture_output=True,
                                      encoding="utf-8", errors="replace").returncode == 1:
        got = subprocess.run(["git", "-C", str(board.root), "rev-parse", "--git-path", "info/exclude"], capture_output=True, encoding="utf-8", errors="replace")
        if got.returncode == 0 and got.stdout.strip():
            exclude = Path(got.stdout.strip()) if os.path.isabs(got.stdout.strip()) else board.root / got.stdout.strip()
            with contextlib.suppress(OSError):
                exclude.parent.mkdir(parents=True, exist_ok=True)
                lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
                if f"/{LOCAL_MEMORY}" not in lines:
                    exclude.write_text("\n".join([*lines, f"/{LOCAL_MEMORY}"]) + "\n", encoding="utf-8")


def unbind_claude(board: Board) -> None:
    path, team = board.root / LOCAL_MEMORY, board.cfg["team"]
    try:
        old = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if _binding_marks(team)[0] not in old:
        return
    rest = _without_note(old, team)
    with contextlib.suppress(OSError):
        if rest.strip():
            path.write_text(rest + "\n", encoding="utf-8")
        else:
            path.unlink()


def release_leader(team_dir: Path) -> None:
    for path, rec in leader_records():
        if rec.get("team_dir") == str(team_dir):
            path.unlink(missing_ok=True)


def ui_label(project=None) -> str:
    from . import ui
    return ", ".join(["team call", *ui.tab_names(project)])


def start_page(project: Path, team: str, team_dir: Path) -> str | None:
    from . import ui
    try:
        return ui.start_background(project, team, team_dir)
    except Exception as e:
        print(f"{CMD}: the settings page did not start ({e}); `{CMD} ui` tries again", file=sys.stderr)
        return None


def prices_line(p: dict, now: float | None = None) -> str | None:
    now = time.time() if now is None else now
    if (t := providers.tariff_now(p, now)) is None:
        return None
    return (f"prices    {'peak' if t['peak'] else 'off-peak'}, {share_words(t['share'])}, until {utc_at(t['until'], now)} "
            f"(in {span(t['until'] - now)}); then {share_words(t['next'])}")


def memory_line(board: Board) -> str | None:
    text, where = member.instructions(board)
    files = [p.relative_to(board.root).as_posix() for p, _ in member.project_memory(board)]
    parts = ([f"{where} instructions ({len(text):,} chars)"] if text else []) + [f"the project's {f}" for f in files]
    return f"memory    members read {' and '.join(parts)}" if parts else None


def cmd_instructions(a) -> int:
    board = _board(a) if not a.default else None
    path = (providers.CONFIG_DIR if a.default else board.dir) / member.INSTRUCTIONS
    text = Path(a.file).expanduser().read_text(encoding="utf-8") if a.file else a.text
    if a.clear:
        path.unlink(missing_ok=True)
    elif text is not None:
        text = text.strip()
        if len(text) > member.INSTRUCTIONS_MAX_CHARS:
            raise ValueError(f"instructions are at most {member.INSTRUCTIONS_MAX_CHARS:,} characters, not {len(text):,}")
        if a.default:
            providers.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        providers.write_private(tmp, text + "\n")
        providers.replace_file(tmp, path)
    scope = "every team without its own" if a.default else f"team {board.cfg['team']}"
    if board is None:
        now = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        _print(a, {"default": now}, [f"default instructions ({scope}): " + (f"{len(now):,} chars" if now else "none"), *([now] if now else [])])
        return 0
    now, where = member.instructions(board)
    files = [p.relative_to(board.root).as_posix() for p, _ in member.project_memory(board)]
    changed = a.clear or text is not None
    _print(a, {"team": board.cfg["team"], "instructions": now, "source": where, "project_memory": files},
           [f"instructions for {scope}: " + (f"{where}, {len(now):,} chars" if now else "none")
            + ("; members take them when they next start or wake" if changed else ""),
            *([now] if now else []), f"project CLAUDE.md for members: {', '.join(files) or 'none'}"
            + ("" if board.cfg.get("project_memory") is not False else f" (off: {CMD} config set project_memory on)")])
    return 0


def budget_line(b: dict) -> str:
    if b["usd"] and b["spent"] is None:
        return f"budget    {money(b['usd'])}, not counted: the model has no prices"
    if b["usd"]:
        return f"budget    {money(b['spent'])} of {money(b['usd'])}" + (": reached, the crew rests" if b["reached"] else "")
    return "budget    none set" + (f", {money(b['spent'])} spent" if b["spent"] is not None else "") + "; the user can set one on the page"


def summary(board: Board, url: str | None) -> list[str]:
    c, on = board.cfg, bool(board.cfg.get("sandbox"))
    running = sum(m["state"] in ("starting", "active") for m in board.members())
    added = [e for e in c.get("pack_allow", []) if e in c["bash_allow"]]
    own = [e for e in c["bash_allow"] if e not in c.get("pack_allow", [])]
    scope = next((k for k, v in presets.SCOPES.items() if v["values"]["DEEPCREW_ALLOW"] and set(own) == set(v["values"]["DEEPCREW_ALLOW"].split(","))), None)
    commands = scope or ", ".join(own) or ("none" if added else "none, every shell command waits for you")
    if added:
        commands += f"   (+ from pack {Path(c['pack']).name or c['pack']}: {', '.join(added)})"
    if left := left_out(c):
        commands += (f"   (not simple commands, left out: {', '.join(left)})" if on else
                     f"   (the sandbox is off, so these wait for approval: {', '.join(left)})")
    place, setup, paused = key_place(c["provider"]), setup_state(board), board.paused()
    what = {"key": f"save the {c['provider']['name']} API key", "confirm": "check the settings and press Done"}
    rest = ("" if not paused else f"; resting since {time.strftime('%H:%M', time.localtime(paused['ts']))}"
            + (": the budget is reached" if paused.get("by") == "budget" else f", as the {paused.get('by', 'leader')} asked"))
    lines = [f"{ui_label(board.root):<9} {url or 'not running: ' + CMD + ' ui'}", f"team      {c['team']}   ({providers.shown(board.root)})",
             f"members   up to {c['max_active']}, {running} running{rest}", f"model     {member_phrase(c['member'])}",
             *filter(None, [provider_line(c)]), f"commands  {commands}", f"sandbox   {'on' if on else 'off'}",
             f"key       {'saved in ' + place if place else 'not saved yet'}; it"
             + ("" if "this computer" in place else " stays on this computer and") + f" goes only to {c['provider']['base_url']}"]
    lines += [reviewer_line(board), budget_line(member.budget_state(board))]
    if (lang := reply_lang(board.live_cfg())) != LANGS[0]:
        lines.append(f"replies   in {lang} for the user, on the page and in this chat; the crew works in English ({CMD} lang NAME to change)")
    if line := prices_line(c["provider"]):
        lines.append(line)
    if line := memory_line(board):
        lines.append(line)
    lines += [f"folders   {', '.join(f['path'] + (' (write)' if f.get('write') else '') for f in board.folders())}; for this chat:",
              *(add_dir(f["path"]) for f in board.folders())] if board.folders() else []
    if bad := extensions.failures(board.root):
        every = set(bad) & set(extensions.everywhere())
        off = ("takes them off this project" + (", and with --global off every project" if every else "")) if set(bad) - every else \
            "--global takes them off every project"
        lines.append(f"extensions not loaded: {', '.join(f'{s} ({why})' for s, why in bad.items())}; {CMD} config set extensions none {off}")
    if setup and setup["missing"]:
        lines.append(f"setup     waiting for the user on the page: {'; '.join(what[k] for k in setup['missing'])}. "
                     "Start members after the inbox says it is done")
    return lines


def reviewer_line(board: Board) -> str:
    if board.live_cfg().get("review") is False:
        return f"reviewer  off: every shell command off the allowlist waits for you ({CMD} config set review on)"
    t = review.tally(board)
    return (f"reviewer  on: {board.live_cfg()['provider']['fast_model']} looks at members' shell commands first and lets routine work on the "
            "project run without you" + (f"; {t['allowed']} of the {t['reviews']} it saw so far" if t["reviews"] else ""))


def member_phrase(m: dict) -> str:
    return f"{m['model']}, effort {m.get('effort') or 'default'}, thinking {'off' if m.get('thinking') is False else 'on'}"


def provider_line(c: dict) -> str | None:
    v = provider_view(c)
    if not v["fallback"] and not v["backups"]:
        return None
    line, moved = f"provider  {v['name']}", v["fallback"]
    if moved:
        line += (f" since {time.strftime('%H:%M', time.localtime(moved['ts']))}: {moved['from']} {failed_words(moved.get('kind'))}; once it works "
                 f"again, {CMD} config set provider {moved['from']} moves the team back")
    return line + (f"; backups: {', '.join(v['backups'])}" if v["backups"] else "; no backup left")


def show_value(key: str, value) -> str:
    if key == "sandbox":
        if not value:
            return "off"
        net, fs = value.get("network", {}), value.get("filesystem", {})
        domains = net.get("allowedDomains", [])
        return ("on, " + (f"network {', '.join(domains)}" if domains else "no network") + (", local ports" if net.get("allowLocalBinding") else "")
                + f", {len(fs.get('denyRead', []))} places closed to reading" + (f", writes also in {providers.shown(', '.join(fs['allowWrite']))}"
                                                                                 if fs.get("allowWrite") else ""))
    if key in ("bash_allow", "fallback"):
        return ", ".join(value) or "none"
    if key == "member":
        return member_phrase(value)
    if key == "tools":
        return ", ".join(t for t in value if t in OPTIONAL_TOOLS) or "no optional tools"
    if key == "review" and isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def change_lines(team: str, changed: dict) -> list[str]:
    return [f"team {team}: {k} {show_value(k, old)} -> {show_value(k, new)}" for k, (old, new) in changed.items()] or [f"team {team}: no change"]


def _project(a) -> Path:
    return Path(a.project or os.getcwd()).resolve()


def _board(a) -> Board:
    d = team_path(_project(a), a.team)
    if not (d / "team.json").exists():
        raise SystemExit(f"no team {a.team!r} for {_project(a)}: run `{CMD} up` there first")
    return Board(d)


def _print(a, obj, lines: list[str] | None = None) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1) if a.json or lines is None else "\n".join(lines))


def check_line(r: dict, fix: bool = True) -> str:
    return (f"{r['level']:4}  {r['id']:12} {providers.shown(r['detail'])}"
            + (f"\n      fix: {providers.shown(r['fix'])}" if fix and r["fix"] and r["level"] != "ok" else ""))


def _show_checks(a, results: list[dict], url: str | None = None) -> int:
    _print(a, {"checks": results, **({"page_url": url} if url else {})}, ([f"{ui_label(_project(a)):<9} {url}"] if url else [])
           + [check_line(r) for r in results])
    return 1 if any(r["level"] == "fail" for r in results) else 0


def _needs(questions: list[dict], url: str | None = None) -> int:
    print(json.dumps({"schema": providers.QUESTIONS_SCHEMA, "questions": questions, **({"page_url": url} if url else {})}, ensure_ascii=False, indent=1))
    return 3


def obtain_key(p: dict) -> str | None:
    return providers.ask_tty(p) if providers.can_prompt() else None


def key_question(p: dict) -> dict:
    var = next((s.partition(":")[2] for s in p["key_sources"] if s.startswith("dotenv:")), "the key variable")
    return {"id": "key", "prompt": f"{CMD} needs a {p['name']} API key; it is never shown to any agent", "options": [],
            "answer_with": f"tell the user: {key_advice(p['name'])}; or write {var}=<key> into {providers.shown(providers.ENV_FILE)} "
                           "(mode 0600, outside every repository)"}


def sandbox_question() -> dict:
    return {"id": "sandbox", "prompt": f"Claude Code has no sandbox on {sys.platform}: run members without it? Every command that runs "
                                      "code then waits for the leader's approval", "options": ["off"],
            "answer_with": f"{CMD} config set sandbox off, or run {CMD} inside WSL2; then run `{CMD} up` again"}


def cmd_up(a) -> int:
    project = _project(a)
    flags = {k: positive(k, getattr(a, k)) for k in DEFAULTS if getattr(a, k) is not None}
    d = team_path(project, a.team)
    if refusal := session_refusal(d):
        print(f"{CMD}: {refusal}", file=sys.stderr)
        return 4
    url = page_url(d) if a.no_ui else start_page(project, a.team, d)
    if sandbox_on() and sys.platform not in SANDBOX_TOOLS:
        return _needs([sandbox_question()], url)
    board = Board(d) if (d / "team.json").exists() else None
    if board is None:
        shared = shared_folder(project, a.team, a.shared)
        if refused := [why for c in a.allow or [] if (why := policy.allow_refusal(c, sandbox_on()))]:
            raise ValueError("; ".join(refused))
    results = host_checks(project)
    if any(r["level"] == "fail" for r in results):
        return _show_checks(a, results, url)
    if board is not None:
        refresh_key_copy(board)
        p, member_cfg, chosen = board.cfg["provider"], board.cfg["member"], True
    else:
        member_cfg, p, chosen = member_config(a)
    has_key = providers.resolve_key(p) is not None
    results += provider_checks(p, a.offline, member_cfg["model"]) if has_key else [check("key", "skip", "not saved yet", key_advice(p["name"]))]
    results += fallback_checks(board.cfg.get("fallback") or [] if board is not None else fallback_tables(fallback_setting()), p["name"])
    if any(r["level"] == "fail" for r in results):
        return _show_checks(a, results, url)
    if board is not None:
        refresh_install(board)
        given = [f"--{k.replace('_', '-')}" for k in ("provider", "model", "effort", "allow", "shared", *DEFAULTS) if getattr(a, k, None)]
        if given:
            print(f"{CMD}: team {a.team!r} exists; {', '.join(given)} only apply when a team is created "
                  f"(it runs {board.cfg['member']['model']})", file=sys.stderr)
    else:
        board = create_team(project, a.team, p, member_cfg, claude=claude_bin(), bash_allow=a.allow or allow_setting(),
                            pack=providers.setting("DEEPCREW_PROMPT_PACK"), shared_dir=shared, **flags)
        providers.env_file_set({"DEEPCREW_PROVIDER": p["name"], "DEEPCREW_MODEL": member_cfg["model"],
                                "DEEPCREW_EFFORT": member_cfg["effort"] or "default"})
    board.reap(member.living)
    record_leader(project, a.team, board.dir)
    bind_claude(board)
    before = setup_state(board)
    needs = {"key": not has_key, "confirm": not chosen or bool(before and "confirm" in before["missing"])}
    if any(needs.values()):
        write_json(board.dir / "run" / SETUP_FILE, {"since": before["since"] if before else time.time(), **needs})
    else:
        (board.dir / "run" / SETUP_FILE).unlink(missing_ok=True)
    block = summary(board, url)
    playbook = prompts.render("leader", board.cfg, cmd=CMD, team=a.team, project=str(project), provider=board.cfg["provider"]["name"],
                              model=board.cfg["member"]["model"])
    playbook = "\n\n".join([playbook, *(t.strip() for t in extensions.call("playbook", board, project=board.root) if isinstance(t, str) and t.strip())])
    _print(a, {"summary": block, "page_url": url, "setup": setup_state(board), "team_dir": str(board.dir), "shared_dir": str(board.shared_dir),
               "checks": results, "playbook": playbook},
           [*block, ""] + [check_line(r, fix=False) for r in results]
           + [f"team folder (notes, artifacts; members may read it): {providers.shown(board.shared_dir)}", "", playbook])
    return 0


def cmd_check(a) -> int:
    results = host_checks(_project(a))
    name = providers.choices()["provider"]
    try:
        results += provider_checks(providers.load(name), a.offline) if name else [check("provider", "skip", "no provider chosen yet",
                                                                                         f"the user chooses one on the page; until then "
                                                                                         f"`{CMD} up` uses {default_provider()}")]
        results += fallback_checks(fallback_tables(fallback_setting()), name or default_provider())
    except providers.ProviderError as e:
        results.append(check("provider", "fail", str(e), f"fix {providers.CONFIG_DIR / 'providers.toml'}"))
    if a.deep:
        results.append(conformance(claude_bin()) if claude_bin() else check("conformance", "skip", "no claude CLI"))
    return _show_checks(a, results)


def provider_choice(p: dict) -> dict:
    ch = providers.choices()
    if not ch["model"] or ch["provider"] in (None, p["name"]):
        return {}
    try:
        old = providers.load(ch["provider"])
    except providers.ProviderError:
        return presets.strength_values(presets.DEFAULTS["strength"], p)
    model, effort = on_provider(ch["model"], None if ch["effort"] in (None, "default") else ch["effort"], old, p)
    return {"DEEPCREW_MODEL": model, "DEEPCREW_EFFORT": effort or "default"}


def provider_notes(p: dict, found: bool = False) -> list[str]:
    src = providers.key_source(p)
    notes = [f"{p['name']}: key found in {providers.shown(src)}"] if src and found else [] if src else [f"{p['name']}: no key yet: {key_advice(p['name'])}"]
    if not p.get("prices"):
        notes.append(f"{p['name']}: costs are not tracked, as its entry has no prices in USD; the page shows no cost and a budget cannot be counted")
    return notes


def cmd_config(a) -> int:
    if a.every_project and (a.action, a.key) != ("set", "extensions"):
        raise ValueError(f"--global is for `{CMD} config set extensions` only: the other settings hold for every project already")
    if a.action == "show":
        values = {**providers.choices(), "thinking": "off" if providers.setting("DEEPCREW_THINKING") == "off" else "on",
                  "project_memory": "off" if providers.setting("DEEPCREW_PROJECT_MEMORY") == "off" else "on",
                  "review": "off" if providers.setting("DEEPCREW_REVIEW") == "off" else "on", **user_settings(),
                  "allow": allow_setting(), "sandbox": "on" if sandbox_on() else "off", "deny_read": listed("DEEPCREW_SANDBOX_DENY_READ", DENY_READ),
                  "local_ports": "on" if providers.setting("DEEPCREW_SANDBOX_LOCAL_PORTS") == "on" else "off",
                  "browser": providers.setting("DEEPCREW_BROWSER") or "auto", "pack": providers.setting("DEEPCREW_PROMPT_PACK") or "basic",
                  "extensions": ",".join(extensions.declared(_project(a))) or None, "global_extensions": ",".join(extensions.everywhere()) or None,
                  "fallback": fallback_setting()}
        _print(a, values, [f"{k}: {setting_text(v)}" for k, v in values.items()])
        return 0
    if a.action == "preset":
        names = [n for n in (a.key, a.value, *(a.more or [])) if n]
        if not names:
            raise ValueError(f"usage: {CMD} config preset {'|'.join(presets.SETUPS)}, or [{'|'.join(presets.STRENGTHS)}] "
                             f"[{'|'.join(presets.SIZES)}] [{'|'.join(presets.SCOPES)}]")
        values = page_checked(presets.values(presets.expand(names), providers.load(providers.choices()["provider"] or default_provider())))
        providers.env_file_set(values)
        _written(a, "\n".join(f"{k}={v}" for k, v in values.items()), values, "\n")
        return 0
    if a.key in PAGE_KEYS and a.value is not None:
        var, extra, notes = PAGE_KEYS[a.key], {}, []
        if a.key in DEFAULTS:
            positive(a.key, a.value)
        elif a.key in ("thinking", "project_memory", "review", "sandbox") and a.value not in ("on", "off"):
            raise ValueError(f"{a.key} is on or off")
        elif a.key in ("provider", "model", "effort"):
            if a.key == "provider" and providers.add_template(a.value):
                notes.append(f"added the ready {a.value} entry to {providers.shown(providers.CONFIG_DIR / 'providers.toml')}")
            p = providers.load(a.value if a.key == "provider" else providers.choices()["provider"] or default_provider())
            allowed = {"provider": providers.known(), "model": p["models"], "effort": [*p["efforts"], "default"]}[a.key]
            if a.value not in allowed:
                raise ValueError(f"{a.key} must be one of: {', '.join(allowed)}")
            if a.key == "provider":
                extra = provider_choice(p)
                notes += provider_notes(p)
        values = page_checked({var: a.value, **extra})
        providers.env_file_set(values)
        _written(a, f"{a.key} = {providers.shown(values[var])}", [var, *extra])
        if notes:
            print("\n".join(notes))
        return 0
    if a.key == "fallback" and a.value is not None:
        names = [] if a.value.strip().lower() in ("", "none") else providers.split_list(a.value)
        if len(set(names)) < len(names):
            raise ValueError("fallback names each provider once, in the order to try them")
        for name in names:
            if name not in providers.known() and name not in providers.TEMPLATES:
                providers.load(name)
        toml = providers.shown(providers.CONFIG_DIR / "providers.toml")
        notes = [f"added the ready {name} entry to {toml}" for name in names if providers.add_template(name)]
        tables = [providers.load(name) for name in names]
        providers.env_file_set({"DEEPCREW_FALLBACK": ",".join(names) or "none"})
        _written(a, f"fallback = {', '.join(names) or 'none'}", ["DEEPCREW_FALLBACK"])
        notes += [line for p in tables for line in provider_notes(p, found=True)]
        if notes:
            print("\n".join(notes))
        return 0
    if a.key == "local_ports" and a.value is not None:
        if a.value not in ("on", "off"):
            raise ValueError("local_ports is on or off")
        providers.env_file_set({"DEEPCREW_SANDBOX_LOCAL_PORTS": a.value})
        _written(a, f"local_ports = {a.value}", ["DEEPCREW_SANDBOX_LOCAL_PORTS"])
        return 0
    if a.key == "browser" and a.value is not None:
        value = "" if a.value == "auto" else a.value
        if value not in ("", "off") and not (os.path.isfile(os.path.expanduser(value)) or shutil.which(value)):
            raise ValueError(f"browser is a path to a Chromium-based browser, auto or off; {a.value!r} was not found")
        providers.env_file_set({"DEEPCREW_BROWSER": os.path.expanduser(value) if value not in ("", "off") else value})
        _written(a, f"browser = {providers.shown(a.value)}", ["DEEPCREW_BROWSER"], team=False)
        return 0
    if a.key == "pack" and a.value is not None:
        value = prompts.pack_value(a.value)
        providers.env_file_set({"DEEPCREW_PROMPT_PACK": value})
        folder, files = prompts.pack_dir({"pack": value}), prompts.pack_files(value)
        texts, d = [f for f in files if f != "pack.json"], team_path(_project(a), a.team)
        kept = (read_json(d / "team.json").get("pack") or "") if (d / "team.json").exists() else value
        lines = [f"pack = {providers.shown(value) or 'basic'}  ({providers.shown(providers.ENV_FILE)}; applies to teams created from now on)"]
        if folder:
            lines.append(f"  {providers.shown(folder)}: {', '.join(files)}"
                         + ("" if len(texts) == len(prompts.NAMES) else " (the other texts come from basic)" if texts else " (every text comes from basic)"))
        if kept != value:
            lines.append(f"team {a.team} keeps pack {providers.shown(kept) or 'basic'}, the one it was created with")
        _print(a, {"pack": value or "basic", "folder": str(folder or prompts.BASIC), "files": files,
                   **({"team_pack": kept or "basic"} if kept != value else {})}, lines)
        return 0
    if a.key == "extensions" and a.value is not None:
        specs = extensions.declare(_project(a), a.value, every_project=a.every_project)
        if a.every_project:
            _print(a, {"global_extensions": specs}, [f"extensions of every project: {', '.join(specs) or 'none'} "
                                                     f"({providers.shown(providers.ENV_FILE)}; each project loads its own too)"])
        else:
            _print(a, {"project": str(_project(a)), "extensions": specs},
                   [f"extensions of {providers.shown(_project(a))}: {', '.join(specs) or 'none'} (other projects do not load them)"])
        return 0
    raise ValueError(f"usage: {CMD} config set provider|model|effort|thinking|project_memory|review|sandbox|local_ports|deny_read|browser|pack|extensions|"
                     f"fallback|{'|'.join(DEFAULTS)} VALUE")


def _written(a, head: str, env_keys, sep: str = "  ", team: bool = True) -> None:
    where = providers.shown(providers.ENV_FILE)
    if not team:
        print(f"{head}{sep}({where}; members take it when they next start)")
        return
    d = team_path(_project(a), a.team)
    lines = change_lines(a.team, sync_team(Board(d), env_keys)["changed"]) if (d / "team.json").exists() else []
    print("\n".join([f"{head}{sep}({where}" + ("" if lines else "; applies to teams created from now on") + ")", *lines]))


def cmd_auth(a) -> int:
    name = a.provider or providers.choices()["provider"] or default_provider()
    if a.action == "set" and providers.add_template(name):
        print(f"{CMD}: added the ready {name} entry to {providers.shown(providers.CONFIG_DIR / 'providers.toml')}", file=sys.stderr)
    p = providers.load(name)
    if a.action == "status":
        src = providers.key_source(p)
        _print(a, {"provider": p["name"], "source": src}, [f"{p['name']}: {providers.shown(src or 'no key found')}"])
        return 0 if src else 1
    if a.action == "clear":
        print("removed: " + (", ".join(providers.clear_keys(p)) or "nothing"))
        return 0
    source = obtain_key(p)
    if source is None:
        providers.env_template(p)
        return _needs([key_question(p)])
    print(f"{p['name']} key stored in {source}")
    return 0


def _routing_lines(r: dict | None) -> list[str]:
    if not r:
        return []
    lines = [f"  resumed {n}" for n in r["woken"]]
    lines += [f"  task {t} went to {n}" for t, n in r.get("given", {}).items()]
    lines += [f"  blocked {b['task']}: {b['reason']}" for b in r["blocked"]] + [f"  note: {n}" for n in r.get("notes", [])]
    lines += [f"  waiting for a free slot: {w}" for w in r["waiting"]] + [f"  error: {e}" for e in r["errors"]]
    lines += _proposal_lines([g for g in r["proposals"] if g["due"]])
    return lines


def cmd_post(a) -> int:
    board = _board(a)
    t = board.post(a.title, a.body or "", a.scope or [], a.dep or [], by="leader", strength=a.strength, to=a.to)
    routing = None if a.no_route and not a.to else route.run(board)
    _print(a, {"task": t, "routing": routing}, [f"posted {t['id']}: {t['title']}  scope={t['scope']}"
                                                + (f"  strength={t['strength']} ({board.needs(t)})" if t["strength"] else "")
                                                + (f"  given to {a.to}" if a.to else "")] + _routing_lines(routing))
    return 0


def turn_text(m: dict) -> str:
    t = m.get("turn")
    if not isinstance(t, dict) or m["state"] not in ("starting", "active"):
        return ""
    minutes, tokens = int((time.time() - t.get("since", time.time())) // 60), int(t.get("chars", 0)) // 4
    return f"  turn={minutes}m {t.get('doing', '')}" + (f", about {tokens // 1000}k tokens so far" if tokens >= 1000 else "")


def load_line(look: dict) -> str:
    parts = [f"{k} {round(v * 100)}%" + (" available" if k == "memory" else "") for k, v in
             (("processor", look.get("cpu")), ("memory", look.get("mem")), ("GPU", look.get("gpu"))) if isinstance(v, (int, float))]
    what = ("no member starts beyond those running" if look["level"] == "busy"
            else "one member runs at a time, and members waiting between turns are ended until it eases")
    return f"host      {look['level']} ({', '.join(parts)}): {what}"


def cmd_status(a) -> int:
    board = _board(a)
    board.reap(member.living)
    tasks, members, plan = board.tasks(), board.members(), route.plan(board)
    usage = {m["name"]: member.usage(board, m["name"]) for m in members}
    obj = {"tasks": tasks, "members": members, "usage": usage, "blocked": plan["blocked"], "waiting": plan["waiting"], "notes": plan["notes"],
           "permission_requests": with_widening(board, board.pending("perms")), "questions": board.pending("questions"),
           "unread": len(board.mail_pending("leader")), "user_messages": len(user_messages(board)),
           "budget": member.budget_state(board), "paused": board.paused(), "provider": provider_view(board.cfg), "reviewer": review.tally(board)}
    obj["proposals"] = plan["proposals"]
    url = page_url(board.dir)
    obj["summary"], obj["page_url"] = summary(board, url), url
    lines = [*obj["summary"], "", "tasks:"] + [f"  {t['id']} {t['state']:9} {t['owner'] or '-':12} {t['title']}  scope={t['scope']}" for t in tasks]
    lines += ["members:"] + [f"  {m['name']:12} {m['state']:8} task={m['task'] or '-':6} region={m['region']}  requests={usage[m['name']]['requests']}"
                             + turn_text(m)
                             + (f"  helps={plan['helpers'][m['name']]['task']}" if m["name"] in plan["helpers"] else "")
                             + (f"  cost=${usage[m['name']]['cost_usd']:.4f}" if "cost_usd" in usage[m["name"]] else "")
                             for m in members]
    lines += [f"blocked {b['task']}: {b['reason']}" for b in plan["blocked"]] + [f"note: {n}" for n in plan["notes"]]
    lines += _proposal_lines(plan["proposals"])
    lines += [_item_line("capacity", plan["capacity"])] if plan["capacity"] else []
    lines += [load_line(plan["load"])] if plan.get("load") and plan["load"]["level"] != "ok" else []
    obj["capacity"], obj["helpers"], obj["leader_summary"] = plan["capacity"], plan["helpers"], (board.summaries(1) or [None])[0]
    obj["load"] = plan.get("load")
    lines += [f"summary   {obj['leader_summary']['text'].splitlines()[0][:160]}"] if obj["leader_summary"] else []
    lines += [f"{len(obj['permission_requests'])} permission request(s), {len(obj['questions'])} question(s), {obj['unread']} unread "
              f"message(s): {CMD} inbox"]
    _print(a, obj, lines)
    return 0


def cmd_prices(a) -> int:
    d = team_path(_project(a), a.team)
    p = Board(d).cfg["provider"] if (d / "team.json").exists() else providers.load(providers.choices()["provider"] or "deepseek")
    now, rows = time.time(), p.get("prices") or {}
    t, changes, at = providers.tariff_now(p, now), [], now
    while t and len(changes) < a.changes:
        changes.append({"at": t["until"], "share": t["next"]})
        at = t["until"]
        t = providers.tariff_now(p, at)
    tariff = p.get("tariff") or {}
    obj = {"provider": p["name"], "prices": rows, "tariff": tariff or None, "now": providers.tariff_now(p, now), "changes": changes}
    untracked = f"costs are not tracked for {p['name']}: its entry has no prices in USD, so no cost is counted and a budget cannot be"
    lines = [prices_line(p, now) or (f"prices    {p['name']} has no tariff: its list prices hold at every hour" if rows else f"prices    {untracked}")]
    if tariff:
        days = [d for d in providers.DAYS if d in tariff.get("peak_days", providers.DAYS)]
        run = len(days) > 2 and "".join(days) in "".join(providers.DAYS)
        lines.append(f"peak      {' and '.join(tariff.get('peak_utc') or [])} UTC, {f'{days[0]}-{days[-1]}' if run else ', '.join(days) or 'no day'}"
                     + (f"; off-peak {share_words(float(tariff.get('off_peak', 1)))}" if tariff.get("off_peak", 1) < 1 else "")
                     + (f"; holidays off-peak: {', '.join(tariff['holidays'])}" if tariff.get("holidays") else ""))
    if rows:
        lines += [f"list      {'USD per 1M tokens':18} {'cache hit':>10} {'cache miss':>11} {'output':>8}"]
        lines += [f"{'':9} {name:18} {r['cache_hit']:>10g} {r['cache_miss']:>11g} {r['output']:>8g}" for name, r in rows.items()]
    elif tariff:
        lines.append(f"list      {untracked}")
    lines += [f"{'next' if not i else '':9} {utc_at(c['at'], now):14} {share_words(c['share'])}" for i, c in enumerate(changes)]
    _print(a, obj, lines)
    return 0


def user_messages(board: Board) -> list[tuple]:
    return [(p, m) for p, m in board.peek_mail("leader", only_from="user") if m.get("kind") == "message"]


def cmd_inbox(a) -> int:
    board = _board(a)
    if a.from_user:
        taken = board.ack_mail(user_messages(board))
        _print(a, {"messages": [{k: m.get(k) for k in ("ts", "text", "task")} for m in taken]}, [m["text"] for m in taken])
        return 0
    seen_file = board.dir / "run" / "inbox-seen.json"
    seen = set(read_json(seen_file)) if seen_file.exists() else set()
    deadline = time.monotonic() + (a.wait or 0)
    while True:
        items, msgs = inbox_items(board), board.take_mail("leader")
        if msgs or {k for k in set(items) - seen if not k.startswith(QUIET)} or time.monotonic() >= deadline:
            break
        time.sleep(member.POLL_S)
    write_json(seen_file, sorted(items))
    new = set(items) - seen
    lines = [_item_line(kind, x) + ("" if key in new else "   (shown before)") for key, (kind, x) in items.items() if kind != "answer" or key in new]
    lines += [f"{m['kind']} from {m['from']}" + (f" on {m['task']}" if m["task"] else "") + f": {m['text']}" for m in msgs]
    url = page_url(board.dir)
    kinds = ("perm", "question", "proposal", "blocked", "note", "answer", "choose", "capacity", "summary", "setup", "crew", "link", "budget",
             "prices", "provider", "ext")
    grouped = {k: [x for key, (kind, x) in items.items() if kind == k and (k != "answer" or key in new)] for k in kinds}
    obj = {"permission_requests": grouped["perm"], "questions": grouped["question"], "messages": msgs, "blocked": grouped["blocked"],
           "notes": grouped["note"], "answered_on_page": grouped["answer"], "finished": any(k.startswith("finished:") for k in items),
           "setup": (grouped["setup"] or [None])[0], "crew": grouped["crew"], "links": grouped["link"], "budget": grouped["budget"],
           "prices": (grouped["prices"] or [None])[0], "provider": (grouped["provider"] or [None])[0], "extensions": grouped["ext"],
           "new": sorted(new), "page_url": url}
    if (setup := obj["setup"]) and not setup["missing"]:
        (board.dir / "run" / SETUP_FILE).unlink(missing_ok=True)
    obj.update(proposals=grouped["proposal"], choices=grouped["choose"], capacity=(grouped["capacity"] or [None])[0],
               summary=(grouped["summary"] or [None])[0])
    _print(a, obj, (lines or ["nothing new"]) + ([f"{ui_label(board.root):<9} {url}"] if url else []))
    return 0


def cmd_approve(a) -> int:
    board, failed = _board(a), False
    for rid in dict.fromkeys(a.request):
        try:
            r = policy.answer_permission(board, rid, allow=not a.deny, always=a.always, link=a.link)
        except ValueError as e:
            print(f"{CMD}: {e}", file=sys.stderr)
            failed = True
            continue
        if r["always"]:
            widened = f" ({providers.shown(widened_words(r.get('widened') or {}))})"
        elif a.always and r["state"] == "allowed":
            widened = f" (nothing to widen: {providers.shown(policy.widening_refusal(board, r) or 'it could not be added just now')})"
        else:
            widened = ""
        print(f"{r['id']} {r['state']}{widened}"
              + (f"; linked {r['folder']} for {'writing' if r.get('write') else 'reading'}. {ADD_DIR_HINT}\n{add_dir(r['folder'])}"
                 if a.link and r.get("folder") else ""))
    return 1 if failed else 0


def cmd_answer(a) -> int:
    if not a.text.strip():
        raise ValueError("the answer must not be empty")
    q = _board(a).answer("questions", a.question, "answered", answer=a.text)
    print(f"{q['id']} answered; {q['member']} continues")
    return 0


def cmd_note(a) -> int:
    board = _board(a)
    if a.action == "add":
        if not a.title or not a.text:
            raise ValueError(f"usage: {CMD} note add TOPIC --text TEXT")
        print(providers.shown(board.note_write("leader", a.title, a.text)))
        return 0
    notes = sorted(board.notes_dir.glob("*.md"))
    if a.action == "list":
        _print(a, {"notes": [str(n) for n in notes]}, [n.name for n in notes] or ["no notes"])
        return 0
    path = next((n for n in notes if a.title and n.name in (a.title, a.title + ".md")), None)
    if path is None:
        raise ValueError(f"no note {a.title!r} in {providers.shown(board.notes_dir)}")
    if a.action == "show":
        print(path.read_text(encoding="utf-8"))
    else:
        path.unlink()
        print(f"{path.name}: removed")
    return 0


def cmd_ui(a) -> int:
    from . import ui
    project, team_dir = _project(a), team_path(_project(a), a.team)
    if a.serve:
        ui.serve_team(project, a.team, a.timeout)
        return 0
    if not (team_dir / "team.json").exists():
        result = ui.serve(port=a.port or 0, open_browser=a.open, timeout_s=a.timeout, project=project, team=a.team,
                          on_ready=lambda url: print(json.dumps({"url": url}) if a.json else url, flush=True))
        _print(a, result, [f"{'saved' if result.get('done') else 'timed out without Done'}: {providers.shown(providers.ENV_FILE)}"])
        return 0
    try:
        url = ui.start_background(project, a.team, team_dir)
    except Exception as e:
        raise ValueError(f"the settings server did not start: {e}") from e
    print(json.dumps({"url": url}) if a.json else url, flush=True)
    if a.open:
        webbrowser.open(url)
    if a.no_wait:
        return 0
    done = ui.wait_done(team_dir, a.timeout)
    values = {k: v for k, v in ((done or {}).get("values") or {}).items() if not re.search(r"KEY|TOKEN|SECRET", k, re.I)}
    _print(a, {"url": url, "done": bool(done), "values": values, "changed": (done or {}).get("changed") or {}},
           ["timed out without Done"] if not done else ["settings confirmed", *[f"  {k}={v}" for k, v in values.items()],
                                                        *change_lines(a.team, done.get("changed") or {})])
    return 0


def cmd_send(a) -> int:
    board = _board(a)
    names = [m["name"] for m in board.members()]
    if a.to not in [*names, "all", "user"]:
        raise ValueError(f"unknown member {a.to!r}; members: {', '.join(names) or 'none'} ('all' for every member, 'user' for the page)")
    if a.to == "user":
        if not in_lang(a.text, lang := reply_lang(board.live_cfg())):
            raise ValueError(f"the user reads replies in {lang}: write it in {lang}")
        board.send("leader", "user", "message", a.text, a.task)
        print("on the page for the user")
        return 0
    for to in names if a.to == "all" else [a.to]:
        board.send("leader", to, a.kind, a.text, a.task)
    print("\n".join([f"sent to {a.to}", *_routing_lines(route.run(board))]))
    return 0


def cmd_lang(a) -> int:
    board = _board(a)
    if a.language:
        lang = next((x for x in LANGS if x.lower() == a.language.strip().lower()), None)
        if lang is None:
            raise ValueError(f"the languages on offer: {', '.join(LANGS)}")
        board.update_cfg(lambda cfg: cfg.update(reply_lang=lang))
    now = reply_lang(board.live_cfg())
    _print(a, {"lang": now, "langs": list(LANGS)}, [f"the user reads replies in {now}; the crew works in English"
                                                    + ("; members follow with their next message" if a.language else "")])
    return 0


def cmd_assign(a) -> int:
    board = _board(a)
    if why := route.assign(board, a.member, a.task, getattr(a, "note", None)):
        raise ValueError(why)
    print(f"task {a.task} assigned to {a.member}; it comes as its next message")
    return 0


def allow_sources(board: Board) -> list[dict]:
    cfg, by = read_json(board.dir / "team.json"), {}
    for row in read_jsonl(board.dir / "run" / policy.ALLOW_LOG):
        if row.get("on") and row.get("kind") != "tool":
            by[row.get("command")] = row.get("by")
    return [{"command": c, "source": f"approved by {by.get(c) or 'leader'}" if c in cfg.get("approved_allow", [])
             else "pack" if c in cfg.get("pack_allow", []) else "settings"} for c in cfg.get("bash_allow", [])] + \
        [{"command": c, "source": f"approved by {by.get(c) or 'leader'}, exactly this command"} for c in cfg.get("exact_allow", [])] + \
        [{"command": c, "source": f"approved by {by.get(c) or 'leader'}, every command it starts"} for c in cfg.get("prefix_allow", [])]


def tool_sources(board: Board) -> list[dict]:
    by = {row.get("tool"): row.get("by") for row in read_jsonl(board.dir / "run" / policy.ALLOW_LOG) if row.get("kind") == "tool" and row.get("on")}
    return [{"tool": t, "source": f"approved by {by.get(t) or 'leader'}"} for t in policy.listed_tools(read_json(board.dir / "team.json"))]


def cmd_allow(a) -> int:
    board = _board(a)
    problems = [why for c in a.command if (why := policy.set_allow(board, c, "leader", on=not a.remove))]
    problems += [why for t in a.tool or [] if (why := policy.set_tool(board, t, "leader", on=not a.remove))]
    rows, tools = allow_sources(board), tool_sources(board)
    lines = [f"  {r['command']:28} {r['source']}" for r in rows] + [f"  {t['tool']:28} {t['source']}" for t in tools]
    _print(a, {"allow": rows, "tools": tools, "problems": problems}, (lines or ["  (empty)"]) + problems)
    return 1 if problems else 0


def cmd_assist(a) -> int:
    board = _board(a)
    if why := route.add_helper(board, a.task, a.member, a.paths or None):
        raise ValueError(why)
    print(f"{a.member} now helps with task {a.task}; both were told by mail")
    return 0


def cmd_summary(a) -> int:
    board = _board(a)
    parts = {k: getattr(a, k) for k in SUMMARY_PARTS}
    if a.text or any(parts.values()):
        board.add_summary(a.text or "", **parts)
    rows = board.summaries(1 if a.text or any(parts.values()) else 5)
    _print(a, {"summaries": rows}, [f"{time.strftime('%H:%M', time.localtime(r['ts']))}  {r['text']}" for r in rows] or ["no summary yet"])
    return 0


def cmd_spawn(a) -> int:
    board = _board(a)
    if providers.resolve_key(board.cfg["provider"]) is None:
        raise ValueError(f"no {board.cfg['provider']['name']} key yet: the user saves it on the team's page ({page_url(board.dir) or CMD + ' ui'}); "
                         "it stays on this computer. Wait for the inbox to report the setup done")
    if a.proposals:
        started = route.start_proposals(board)
        print("\n".join(f"started {m['name']} region={m['region']} task={m['task']}" for m in started) or "no proposals")
        if left := route.plan(board)["proposals"]:
            print(f"{len(left)} proposal(s) not started: the team is at its limit of {board.cfg['max_active']} members")
        return 0
    if not a.name:
        raise ValueError(f"{CMD} spawn NAME --region PATH ... [--task T] [--role TEXT], or {CMD} spawn --proposals")
    if not MEMBER_NAME.fullmatch(a.name):
        raise ValueError(f"member names are 2-24 characters of a-z, 0-9 and '-', starting with a letter, such as \"physics-engine\": not {a.name!r}")
    m = member.start(board, a.name, a.region, a.task, a.role, a.strength)
    print(f"started {m['name']} region={m['region']} task={m['task']}" + (f" model={m['model']}" if m.get("model") else ""))
    return 0


def cmd_rest(a) -> int:
    names = route.rest(_board(a), by="leader")
    ran = ", ".join(names) + (" was" if len(names) == 1 else " were") + " running" if names else "nobody was running"
    print(f"the crew rests ({ran}); `{CMD} resume` starts it again")
    return 0


def cmd_resume(a) -> int:
    r = route.resume(_board(a), by="leader")
    print("\n".join(["the crew may work again", *_routing_lines(r)]))
    return 0


def _folder_line(f: dict) -> str:
    return f"{f['path']}  ({'read and write' if f.get('write') else 'read'}, linked by {f.get('by', 'leader')})"


def cmd_link(a) -> int:
    board = _board(a)
    if a.path:
        entry = policy.set_folder(board, a.path, write=a.write, by="leader", on=not a.remove)
        lines = [f"{'unlinked' if a.remove else 'linked'} {_folder_line(entry)}"]
        if not a.remove:
            lines += [f"members reach it now. {ADD_DIR_HINT}", add_dir(entry["path"])]
    else:
        lines = [_folder_line(f) for f in board.folders()] or ["no folders linked"]
        lines.append(f"tools folder (every member, read and write; bin/ on their PATH): {providers.shown(tools_dir())}")
    _print(a, {"folders": board.folders(), "tools": str(tools_dir()), **({"add_dir": add_dir(entry["path"])} if a.path and not a.remove else {})},
           lines)
    return 0


def cmd_site(a) -> int:
    board = _board(a)
    if a.domain:
        entry = policy.set_site(board, a.domain, by="leader", on=not a.remove)
        lines = [f"removed {entry['domain']}: members ask before they fetch from it or connect to it (a member process started before keeps "
                 "its sandbox's sites until it ends)" if a.remove else
                 f"added {entry['domain']}: members fetch from it and connect to it without asking, from now on"]
    else:
        net = (board.cfg.get("sandbox") or {}).get("network", {}).get("allowedDomains", [])
        lines = ([f"settings: {', '.join(net) or 'no network'}"] if board.cfg.get("sandbox")
                 else ["the sandbox is off: members' commands are not limited to sites"])
        lines.append(f"added: {', '.join(board.sites()) or 'none'}")
    _print(a, {"sites": board.sites()}, lines)
    return 0


def _region_text(region: list[str]) -> str:
    return ", ".join(region) or "no files (it reads, and changes nothing)"


def cmd_region(a) -> int:
    board = _board(a)
    m = board.member(a.name)
    if m is None:
        names = [x["name"] for x in board.members()]
        raise ValueError(f"unknown member {a.name!r}; members: {', '.join(names) or 'none'}")
    if a.paths and (a.add or a.drop):
        raise ValueError("give the whole region as PATH ..., or change it with --add and --drop, not both")
    region, new = m["region"], m["region"]
    if a.paths:
        new = policy.norm_all(a.paths)
    elif a.add or a.drop:
        dropped = policy.norm_all(a.drop or [])
        if missing := [d for d in dropped if d not in region]:
            raise ValueError(f"{a.name}'s region ({', '.join(region) or 'no files'}) has no entry {', '.join(missing)}. A region lists whole "
                             f"folders and files, so to cut a part out give the parts to keep: {CMD} region {a.name} PATH ...")
        kept = [e for e in region if e not in dropped]
        new = policy.norm_all([*kept, *(p for p in policy.norm_all(a.add or []) if not policy.covers(kept, p))])
    changed = new != region
    if changed:
        m = board.set_region(a.name, new, by="leader")
    routing = route.run(board) if changed else None
    last = (m.get("region_changes") or [None])[-1]
    obj = {"member": a.name, "region": m["region"], "was": region, "changed": changed, "last_change": last, "routing": routing}
    if changed:
        lines = [f"{a.name}'s region is now {_region_text(m['region'])} (it was {', '.join(region) or 'no files'}); {a.name} was told by mail"]
    elif a.paths or a.add or a.drop:
        lines = [f"{a.name}'s region is unchanged: {_region_text(region)}"]
    else:
        lines = [f"{a.name}: {_region_text(region)}"] + ([f"changed by {last['by']} at {time.strftime('%H:%M', time.localtime(last['ts']))}, it was "
                                                          f"{', '.join(last['was']) or 'no files'}"] if last else [])
    _print(a, obj, lines + _routing_lines(routing))
    return 0


def cmd_stop(a) -> int:
    board = _board(a)
    for n in [m["name"] for m in board.members()] if a.all else [a.name]:
        print(f"{n}: {'stopped' if member.stop(board, n) else 'was not running'}")
    if a.all:
        for copy in (board.dir / "secret").glob("*.key"):
            copy.unlink(missing_ok=True)
        release_leader(board.dir)
        unbind_claude(board)
        from . import ui
        if ui.stop_page(board.dir):
            print("page: closed")
    return 0


def cmd_forget(a) -> int:
    board = _board(a)
    member.stop(board, a.name)
    cost = member.usage(board, a.name).get("cost_usd") if board.member(a.name) else None
    m = board.forget(a.name)
    if m and cost:
        board.update_cfg(lambda cfg: cfg.__setitem__("spent_removed", round(float(cfg.get("spent_removed") or 0) + cost, 6)))
    print(f"{a.name}: " + (f"removed; region {m['region']} is free for a new member" if m else "no such member"))
    return 0 if m else 1


def cmd_cancel(a) -> int:
    t, why = _board(a).cancel(a.task, "leader", a.reason or "")
    print(f"{a.task}: {why}")
    return 0 if t else 1


def cmd_audit(a) -> int:
    r = audit(_board(a))
    if "error" in r:
        print(r["error"])
        return 1
    _print(a, r, [f"since {r['base'][:12]}" + (" (the tree was dirty then)" if r["dirty_at_start"] else "")
                  + (f"; {r['untracked_unchanged']} untracked files from before the team started (unchanged) left out" if r["untracked_unchanged"] else "")
                  + (f"; more than {UNTRACKED_MAX:,} untracked files, so every untracked file is listed" if r["untracked_too_many"] else "")] +
           [f"  {f['path']:40} owner={f['owner'] or '-':12} edited_by={','.join(f['edited_by']) or '-'}  {' '.join(f['flags'])}"
            + (f"  lease={','.join(f['leased_to'])}" if f.get("leased_to") else "")
            + (f" (likely {f['shell']['member']}: {f['shell']['command'][:80]}" + "".join(f"; or {o}" for o in f["shell"].get("others", []))
               + ")" if f.get("shell") else "") for f in r["files"]])
    return 0


def cmd_route(a) -> int:
    print("\n".join(_routing_lines(route.run(_board(a)))) or "nothing to do")
    return 0


def _usage_error(a) -> None:
    if a.remove and a.source:
        raise ValueError("--from is for adding and --remove for taking off: use one of them")
    if a.source and not a.name:
        raise ValueError(f"--from goes with a name: {CMD} {a.cmd} NAME --from {'FILE' if a.cmd == 'mcp' else 'DIR'}")


def _files(n: int) -> str:
    return f"{n} file" + ("" if n == 1 else "s")


def cmd_mcp(a) -> int:
    project = _project(a)
    _usage_error(a)
    if a.name and a.remove:
        toolkit.remove_mcp(project, a.name)
        _print(a, {"removed": a.name}, [f"removed MCP server {a.name}. Member processes started from now on do not get it."])
        return 0
    if a.name:
        replaced = any(r["name"] == a.name for r in toolkit.mcp_list(project))
        got = toolkit.add_mcp(project, a.name, a.source)
        lines = [providers.shown(f"{'replaced' if replaced else 'added'} MCP server {got['name']} (from {got['source']}): {got['command']}")]
        if got["pinned"]:
            lines.append(f"pinned by SHA-256: {', '.join(got['pinned'])}. The server is left out if one of these files changes.")
        lines += [*map(providers.shown, got["notes"]),
                  "Member processes started from now on get it. It runs outside the members' sandbox, with the user's rights. Its environment is "
                  f"the member's and holds no key. Every call of its tools asks you or the user until a tool is allowed with `{CMD} approve ID --always`."]
        _print(a, got, lines)
        return 0
    rows = toolkit.mcp_list(project)
    lines = ["MCP servers given to this project's teams:" if rows else "no MCP servers are given to this project's teams"]
    for r in rows:
        lines.append(providers.shown(f"  {r['name']:<14} {r['command']}" + (f"   pinned: {', '.join(r['pinned'])}" if r["pinned"] else "")
                                     + ("" if r["ok"] else f"   left out: {r['why']}. Add it again with `{CMD} mcp {r['name']}`.")))
    lines.append("Member processes started from now on get these. They run outside the members' sandbox, with the user's rights. Their environment is "
                 f"the member's and holds no key. Every call of their tools asks you or the user until a tool is allowed with `{CMD} approve ID --always`."
                 if rows else
                 f"Add one with `{CMD} mcp NAME`: its definition comes from the project's .mcp.json or your Claude Code settings, or from FILE with --from.")
    _print(a, {"mcp": rows}, lines)
    return 0


def cmd_skill(a) -> int:
    project = _project(a)
    _usage_error(a)
    if a.name and a.remove:
        toolkit.remove_skill(project, a.name)
        _print(a, {"removed": a.name}, [f"removed skill {a.name}. Member processes started from now on do not get it."])
        return 0
    if a.name:
        replaced = any(r["name"] == a.name for r in toolkit.skill_list(project))
        got = toolkit.add_skill(project, a.name, a.source)
        size = f"{max(1, round(got['bytes'] / 1024))} KB"
        lines = [providers.shown(f"{'replaced' if replaced else 'added'} skill {got['name']} (from {got['source']}): {_files(got['files'])}, {size}")]
        if got["dropped"]:
            lines.append(f"Members do not get these settings of the skill: {', '.join(got['dropped'])}. A skill could use them to skip the team's "
                         "approvals or to run commands outside the sandbox.")
        lines.append("Member processes started from now on get it, and the Skill tool with it. A copy is kept in the settings folder, so a change to "
                     f"the skill reaches members only when you add it again with `{CMD} skill {got['name']}`.")
        _print(a, got, lines)
        return 0
    rows = toolkit.skill_list(project)
    lines = ["Skills given to this project's teams:" if rows else "no skills are given to this project's teams"]
    for r in rows:
        status = f" ({_files(r['files'])})" if r["ok"] else f"   left out: {r['why']}. Add it again with `{CMD} skill {r['name']}`."
        lines.append(f"  {r['name']:<14} {' '.join(r['description'].split())[:100].rstrip()}{status}")
    lines.append("Member processes started from now on get these, and the Skill tool with them." if rows else
                 f"Add one with `{CMD} skill NAME`: it is taken from the project's .claude/skills or ~/.claude/skills, or from DIR with --from.")
    _print(a, {"skills": rows}, lines)
    return 0


def parser(project=None) -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog=CMD, description=DESCRIPTION)
    top.add_argument("--version", action="version", version=f"{CMD} {__version__}")
    sub = top.add_subparsers(dest="cmd", required=True)

    def cmd(name, fn, help_text, *args):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--team", default="main")
        p.add_argument("--project")
        p.add_argument("--json", action="store_true")
        for names, kw in args:
            p.add_argument(*names, **kw)
        p.set_defaults(fn=fn)
        return p

    def one(*names, **kw):
        return names, kw

    def many(*names):
        return names, {"action": "append"}

    def flag(*names):
        return names, {"action": "store_true"}

    p = cmd("up", cmd_up, "start the team's page, self-check, create or open the team, print the summary and the leader playbook (the user "
                         "saves the key and confirms the settings on the page)",
            one("--provider"), one("--model"), one("--effort"),
            one("--shared", help=f"team folder for notes and artifacts (default: <project>-{CMD}/<team>)"),
            one("--allow", action="append", help="a Bash command members may run (repeatable)"),
            flag("--offline"), flag("--no-ui"))
    for k in DEFAULTS:
        p.add_argument(f"--{k.replace('_', '-')}", dest=k, type=int)
    cmd("check", cmd_check, "self-check; --deep runs one member against the local stand-in model (no tokens)", flag("--deep"), flag("--offline"))
    cmd("config", cmd_config,
        f"show or set settings in {providers.shown(providers.ENV_FILE)}; preset NAME ... applies ready-made choices (a setup: "
        f"{', '.join(presets.SETUPS)}; or a strength, a size and a scope); set provider NAME switches provider, DeepSeek by default, "
        f"and adds a ready entry ({', '.join(providers.TEMPLATES)}) the first time; set fallback NAME,... lists backup providers",
        one("action", choices=["show", "set", "preset"]), one("key", nargs="?"), one("value", nargs="?"), one("more", nargs="*"),
        one("--global", dest="every_project", action="store_true",
            help="with set extensions: declare installed modules for every project, apart from each project's own"))
    cmd("ui", cmd_ui, "the settings page (key, model, permissions, team size); waits until the user clicks Done or --timeout seconds pass",
        one("--port", type=int), flag("--open"), flag("--no-wait"), one("--timeout", type=float, default=1800.0),
        one("--serve", action="store_true", help=argparse.SUPPRESS))
    cmd("auth", cmd_auth, "store, show the source of, or clear a provider key: the chosen provider's, or PROVIDER's (a backup's)",
        one("action", choices=["set", "status", "clear"]), one("provider", nargs="?"))
    cmd("post", cmd_post, "post a task; --strength makes it wait for a member on that strength's model", one("title"), one("--body"),
        many("--scope"), many("--dep"), one("--strength", choices=list(presets.STRENGTHS)), flag("--no-route"),
        one("--to", help="give the task to this member at once: nobody else is offered it"))
    cmd("status", cmd_status, "tasks, members, proposals, blocked tasks")
    cmd("inbox", cmd_inbox, "permission requests, questions, messages and proposals for the leader", one("--wait", type=float),
        flag("--from-user"))
    cmd("prices", cmd_prices, "the provider's list prices, what they cost now and the next changes (UTC), to choose when the crew works",
        one("--changes", type=int, default=4, help="how many coming changes to list"))
    cmd("approve", cmd_approve, "answer a permission request; --always also lets members do the same without asking (the inbox says what it adds), "
                                "--link also links the folder outside the project it reaches into",
        one("request", nargs="+"), flag("--deny"), flag("--always"), flag("--link"))
    cmd("answer", cmd_answer, "answer a member's question (the member is waiting)", one("question"), one("text"))
    cmd("instructions", cmd_instructions, "extra instructions every member reads (in its CLAUDE.md) for this team, or --default for every team",
        one("text", nargs="?"), one("--file"), flag("--clear"), flag("--default"))
    cmd("note", cmd_note, "team notes (Markdown files members read): add TOPIC --text T, list, show NAME, rm NAME",
        one("action", choices=["add", "list", "show", "rm"]), one("title", nargs="?"), one("--text"))
    cmd("send", cmd_send, "send a message to a member or 'all', or to 'user': the team's page shows it to the user", one("to"), one("text"),
        one("--kind", default="notice", choices=["notice", "answer"]), one("--task"))
    cmd("lang", cmd_lang, f"the language the user reads replies in ({', '.join(LANGS)}); the crew works in English", one("language", nargs="?"))
    cmd("assign", cmd_assign, "give a task to a member (the leader picks among members who asked for the same task); "
        "--note is your brief: what to weigh, the access you gave it; a task outside its region becomes its lease",
        one("task"), one("member"), one("--note"))
    cmd("allow", cmd_allow, "the team's allowlist and the MCP tools members use without asking, with where each entry came from; COMMAND ... and --tool NAME "
                            "... put commands and tools the user approved on them, --remove takes them off",
        one("command", nargs="*"), one("--tool", action="append", metavar="NAME", help="an MCP tool, such as mcp__github__create_issue (repeatable)"),
        flag("--remove"))
    cmd("assist", cmd_assist, "put an idle member on a task another member holds; PATH ... (inside the task's scope) become its lease and the holder "
        "stops writing them until the task closes; without PATH it reads, checks and advises", one("task"), one("member"), one("paths", nargs="*"))
    cmd("summary", cmd_summary, "record where the work stands for the user (the pages show the latest as a round report); without text, print the "
        "last ones", one("text", nargs="?"), one("--finished", action="append", default=[], help="a result of this round, one per flag"),
        one("--running", action="append", default=[], help="what is in progress"), one("--risks", action="append", default=[], help="a risk"),
        one("--decide", action="append", default=[], help="a decision the user should make"))
    cmd("spawn", cmd_spawn, "start a member with its region, first task and role; --strength runs it on a stronger model than the team's",
        one("name", nargs="?"), one("--region", action="append", default=[]), one("--task"), one("--role"),
        one("--strength", choices=list(presets.STRENGTHS)))
    sub.choices["spawn"].add_argument("--proposals", action="store_true", help="start every proposed member")
    cmd("rest", cmd_rest, "rest the crew: every member process ends and nobody starts until resume; the team, its page and its key stay")
    cmd("resume", cmd_resume, "end a rest: members with work or mail start again")
    cmd("link", cmd_link, "link a folder outside the project to the team (members reach it at once); without PATH, list the links",
        one("path", nargs="?"), flag("--write"), flag("--remove"))
    cmd("site", cmd_site, "let members reach DOMAIN: fetches and sandboxed shell commands (downloads) go through at once; without DOMAIN, "
                          "list the sites",
        one("domain", nargs="?"), flag("--remove"))
    cmd("region", cmd_region, "show a member's region; with PATH ... set it, with --add and --drop change it, to move a boundary when two members "
                              "need the same file (the member is told and follows at once)",
        one("name"), one("paths", nargs="*", metavar="PATH", help="the whole region: folders end in /, anything else is one file"),
        one("--add", action="append", metavar="PATH", help="add PATH to the region (repeatable)"),
        one("--drop", action="append", metavar="PATH", help="take the entry PATH off the region (repeatable)"))
    cmd("stop", cmd_stop, "end a member's process (its session stays resumable); --all ends the team: every member stops, its key copies are "
                          "deleted, its page closes and this session may lead another team",
        one("name", nargs="?"), flag("--all"))
    cmd("forget", cmd_forget, "stop and remove a member; its task goes back to open and its region is freed", one("name"))
    cmd("cancel", cmd_cancel, "cancel a task", one("task"), one("--reason"))
    cmd("audit", cmd_audit, "files changed since the team started, by region and member")
    cmd("route", cmd_route, "resume idle members that have work now")
    cmd("mcp", cmd_mcp, "give every member of the project's teams an MCP server the user asked for (from the project's .mcp.json, the user's Claude Code "
                        "settings or --from FILE), take it off with --remove, or list them",
        one("name", nargs="?"), one("--from", dest="source", metavar="FILE"), flag("--remove"))
    cmd("skill", cmd_skill, "give every member of the project's teams a skill the user asked for (from the project's .claude/skills, ~/.claude/skills or "
                            "--from DIR), take it off with --remove, or list them",
        one("name", nargs="?"), one("--from", dest="source", metavar="DIR"), flag("--remove"))
    extensions.call("commands", cmd, project=project)
    return top


def project_arg(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == "--project" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--project="):
            return arg.partition("=")[2]
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    a = parser(project_arg(argv)).parse_args(argv)
    if a.cmd == "stop" and not (a.name or a.all):
        raise SystemExit(f"{CMD} stop NAME | --all")
    try:
        return a.fn(a)
    except (ValueError, providers.ProviderError, member.LaunchError) as e:
        print(f"{CMD}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
