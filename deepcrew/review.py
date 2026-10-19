from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from urllib.parse import urlparse

from . import paths, policy, prompts, providers, shell
from .board import Board, read_json, tools_dir, write_json

REVIEW_FILE = "reviewer.json"
TIMEOUT_S = 20
MAX_COMMAND = 4000
MAX_TOKENS = 200
ESCALATE = frozenset({
    "sudo", "su", "doas", "pkexec",
    "ssh", "scp", "sftp", "rsync", "nc", "ncat", "netcat", "telnet", "ftp", "tftp", "curl", "wget", "aria2c",
    "security", "launchctl", "systemctl", "service", "crontab", "osascript", "open", "xdg-open", "defaults",
    "chown", "chgrp", "kill", "pkill", "killall", "shutdown", "reboot", "halt", "dd", "mkfs", "diskutil", "mount", "umount",
    "docker", "podman", "kubectl", "helm", "gh", "aws", "gcloud", "az", "terraform", "twine",
    "psql", "mysql", "mongo", "mongosh", "redis-cli",
})
DELETE = frozenset({"rm", "rmdir", "unlink", "shred", "srm", "trash"})
GIT_READS = frozenset({"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "grep", "describe"})
PACKAGERS = frozenset({"npm", "pnpm", "yarn", "bun", "cargo", "poetry", "uv", "gem", "pip", "pip3"})
SENDS = frozenset({"publish", "upload", "push", "login", "adduser", "owner", "token"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
WRAPPERS = frozenset({"env", "time", "nohup", "nice", "timeout", "exec", "command", "builtin", "stdbuf", "xargs"})
LOCAL_VARS = frozenset({"TMPDIR", "PWD", "OLDPWD", "PIPESTATUS", "RANDOM", "SECONDS"})
ASSIGNED = re.compile(r"(?:^|[\s;&|(])(?:export\s+|local\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)=|\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b"
                      r"|\bread\s+(?:-\w+\s+)*([A-Za-z_][A-Za-z0-9_]*)")
WORD = re.compile(r"[^\s;&|()<>`\"'{}]+")
VARIABLE = re.compile(r"`|\$\(|\$\{?([A-Za-z_][A-Za-z0-9_]*)")
QUOTED_HEREDOC = re.compile(r"<<-?[ \t]*(['\"])([^'\"\n]+)\1[^\n]*\n.*?^[ \t]*\2[ \t]*$", re.S | re.M)


def screen(board: Board, member: str, command: str) -> str | None:
    if len(command) > MAX_COMMAND:
        return "too long to review"
    if far := policy.outside(board, command, member):
        return f"names {providers.shown(far[0])}, outside the project"
    bare = QUOTED_HEREDOC.sub("", command)
    own = LOCAL_VARS | {n for m in ASSIGNED.finditer(bare) for n in m.groups() if n}
    for m in VARIABLE.finditer(bare):
        if m.group(1) is None:
            return "runs a command substitution"
        if m.group(1) not in own:
            return f"uses ${m.group(1)}"
    commands = programs_of(command)
    if commands is None:
        if bad := far_word(board, WORD.findall(command)):
            return f"names {providers.shown(bad)}, outside the project"
        words = {w.rsplit("/", 1)[-1] for w in WORD.findall(bare)}
        if hit := sorted(words & ESCALATE):
            return f"runs {hit[0]}"
        if words & {*DELETE, "git", "eval", "-delete"}:
            return "deletes files or runs git in a command too intricate to check"
        return None
    for argv in commands:
        name, args = shell.program(argv[0]), argv[1:]
        if bad := far_word(board, args):
            return f"names {providers.shown(bad)}, outside the project"
        if name in ESCALATE:
            return f"runs {name}"
        if name == "git" and not git_reads(args):
            return f"runs git {git_command(args) or ''}".rstrip()
        if name in PACKAGERS and SENDS & set(args):
            return f"runs {name} {next(a for a in args if a in SENDS)}"
        if name in DELETE and (theirs := not_own(board, member, [a for a in args if not a.startswith("-")])):
            return f"deletes {providers.shown(theirs)}, which is not {member}'s"
        if name == "find" and ({"-delete", "-exec", "-execdir", "-ok", "-okdir"} & set(args)):
            return "runs find with -delete or -exec"
    return None


SANDBOX_FETCH = frozenset({"curl", "wget", "aria2c"})


def sandbox_screen(board: Board, member: str, command: str) -> str | None:
    commands = programs_of(command)
    if commands is None:
        words = {w.rsplit("/", 1)[-1] for w in WORD.findall(QUOTED_HEREDOC.sub("", command))}
        if hit := sorted(words & (ESCALATE - SANDBOX_FETCH)):
            return f"runs {hit[0]}"
        if words & {*DELETE, "git", "eval"}:
            return "deletes files or runs git in a command too intricate to check"
        return None
    for argv in commands:
        name, args = shell.program(argv[0]), argv[1:]
        if name in ESCALATE - SANDBOX_FETCH:
            return f"runs {name}"
        if name == "git" and not git_reads(args):
            return f"runs git {git_command(args) or ''}".rstrip()
        if name in PACKAGERS and SENDS & set(args):
            return f"runs {name} {next(a for a in args if a in SENDS)}"
        if name in DELETE and (theirs := not_own(board, member, [a for a in args if not a.startswith("-")])):
            return f"deletes {providers.shown(theirs)}, which is not {member}'s"
        if name == "find" and "-delete" in args:
            return "runs find with -delete"
    return None


def programs_of(command: str, depth: int = 0) -> list[list[str]] | None:
    found = shell.parts(command) if depth < 3 else None
    if found is None:
        return None
    out = []
    for argv in found:
        while argv and (shell.ASSIGNMENT.fullmatch(argv[0]) or shell.program(argv[0]) in WRAPPERS):
            argv = argv[1:]
            while argv and (argv[0].startswith("-") or argv[0].replace(".", "", 1).isdigit()):
                argv = argv[1:]
        if not argv:
            continue
        name = shell.program(argv[0])
        if name == "eval":
            return None
        if name in SHELLS and "-c" in argv[1:-1]:
            inner = programs_of(argv[argv.index("-c", 1) + 1], depth + 1)
            if inner is None:
                return None
            out += inner
            continue
        out.append(argv)
    return out


def far_word(board: Board, words: list[str]) -> str | None:
    root = os.path.realpath(board.root)
    for w in [v for word in words for v in (word, word.partition("=")[2]) if v]:
        if w in policy.NULL_DEVICES or not (w.startswith(("/", "~")) or ".." in w.replace("\\", "/").split("/")):
            continue
        head = re.split(r"[*?\[]", w, maxsplit=1)[0] or "/"
        full = os.path.realpath(os.path.join(root, os.path.expanduser(head)))
        if not (policy.inside(full, root) or board.in_shared(full) or board.reachable(full) or policy.inside(full, os.path.realpath(tools_dir()))):
            return w
    return None


GIT_LISTS = {"branch": ("-a", "--all", "-r", "--remotes", "-l", "--list", "-v", "-vv", "--verbose", "--show-current", "--contains", "--merged",
                        "--no-merged", "--sort", "--format", "--color", "--no-color", "--column", "--no-column", "--points-at"),
             "tag": ("-l", "--list", "-n", "--contains", "--merged", "--no-merged", "--sort", "--format", "--points-at"),
             "remote": ("-v", "--verbose", "show", "get-url"), "stash": ("list", "show"),
             "config": ("--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope")}


def git_reads(args: list[str]) -> bool:
    sub = git_command(args)
    if sub in GIT_READS:
        return True
    if sub not in GIT_LISTS:
        return False
    rest = args[args.index(sub) + 1:]
    kinds = GIT_LISTS[sub]
    if sub in ("branch", "tag"):
        return all(a.split("=", 1)[0] in kinds or (not a.startswith("-") and sub == "tag" and "-l" in rest) for a in rest)
    if sub == "remote":
        return not rest or rest[0] in kinds
    return bool(rest) and rest[0] in kinds


def git_command(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        if args[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            return args[i]
    return None


def not_own(board: Board, member: str, targets: list[str]) -> str | None:
    if not targets:
        return "files"
    area = board.write_area(member)
    for t in targets:
        if not os.path.isabs(os.path.expanduser(t)):
            return t
        full = os.path.realpath(os.path.expanduser(re.split(r"[*?\[]", t, maxsplit=1)[0] or "/"))
        rel = paths.relpath(board.root, full)
        if not ((rel is not None and rel not in ("", ".") and paths.covers(area, rel)) or board.own_file(member, full)
                or policy.inside(full, os.path.realpath(tools_dir()))):
            return t
    return None


def review(board: Board, member: str, tool: str, tool_input: dict) -> dict | None:
    cfg, command = board.live_cfg(), tool_input.get("command")
    if tool != "Bash" or cfg.get("review") is False or not isinstance(command, str) or not command.strip():
        return None
    if why := screen(board, member, command):
        return {"verdict": "ask", "reason": why}
    p = cfg["provider"]
    found = providers.resolve_key(p)
    if found is None:
        return {"verdict": "ask", "reason": "the reviewer has no key"}
    m = board.member(member) or {}
    facts = {"command": command, "description": str(tool_input.get("description") or ""), "member": member, "role": m.get("role") or "",
             "region": board.write_area(member), "project": str(board.root), "team_folder": str(board.shared_dir), "tools_folder": str(tools_dir())}
    try:
        said, used = ask_model(p, found[1], prompts.render("reviewer", cfg), json.dumps(facts, ensure_ascii=False, indent=1))
    except (OSError, ValueError) as e:
        count(board, p, None, "ask")
        return {"verdict": "ask", "reason": f"the reviewer gave no answer ({type(e).__name__})"}
    out = verdict(said)
    count(board, p, used, out["verdict"])
    return out


def ask_model(p: dict, key: str, system: str, facts: str) -> tuple[str, dict]:
    body = {"model": p["fast_model"].split("[")[0], "max_tokens": MAX_TOKENS, "temperature": 0, "system": system,
            "messages": [{"role": "user", "content": facts}]}
    if p.get("thinking_off_body"):
        body["thinking"] = {"type": "disabled"}
    url = urlparse(p["base_url"])
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost")):
        raise ValueError(f"the provider's address is not https: {p['base_url']}")
    request = urllib.request.Request(p["base_url"].rstrip("/") + "/v1/messages", data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"content-type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": key,
                                              "authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
        got = json.loads(reply.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in got.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
    return text, got.get("usage") if isinstance(got.get("usage"), dict) else {}


def verdict(said: str) -> dict:
    s = said.strip()
    if s.startswith("```"):
        s = s.strip("`").strip().removeprefix("json").strip()
    try:
        got = json.loads(s)
    except ValueError:
        got = None
    if not isinstance(got, dict) or got.get("verdict") not in ("allow", "ask"):
        return {"verdict": "ask", "reason": "the reviewer's answer was not the line of JSON asked for"}
    reason = " ".join(str(got.get("reason") or "").split())[:160]
    return {"verdict": got["verdict"], "reason": reason or ("routine work on the project" if got["verdict"] == "allow" else "not sure")}


def count(board: Board, p: dict, used: dict | None, said: str) -> None:
    with board.locked():
        path = board.dir / "run" / REVIEW_FILE
        t = {"reviews": 0, "allowed": 0, "asked": 0, "no_answer": 0, "input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0,
             "cost_usd": 0.0, **(read_json(path) if path.exists() else {})}
        t["reviews"] += 1
        t["no_answer" if used is None else "allowed" if said == "allow" else "asked"] += 1
        if used:
            n = {k: int(used.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
            t["input_tokens"] += n["input_tokens"] + n["cache_creation_input_tokens"]
            t["cache_read_input_tokens"] += n["cache_read_input_tokens"]
            t["output_tokens"] += n["output_tokens"]
            price = (p.get("prices") or {}).get(p["fast_model"].split("[")[0])
            if price is None or t["cost_usd"] is None:
                t["cost_usd"] = None
            else:
                listed = ((n["input_tokens"] + n["cache_creation_input_tokens"]) * price["cache_miss"] + n["cache_read_input_tokens"] * price["cache_hit"]
                          + n["output_tokens"] * price["output"]) / 1e6
                t["cost_usd"] = round(t["cost_usd"] + listed * providers.price_share(p, time.time()), 6)
        write_json(path, t)


def tally(board: Board) -> dict:
    path = board.dir / "run" / REVIEW_FILE
    zero = {"reviews": 0, "allowed": 0, "asked": 0, "no_answer": 0, "input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    return {**zero, **(read_json(path) if path.exists() else {})}
