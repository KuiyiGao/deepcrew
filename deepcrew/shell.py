from __future__ import annotations

import ntpath
import os
import re
import shlex

from . import host

SHELL_META = set(";&|`$<>()\n\\*?[]{}!")
RUNS_CODE = frozenset({
    "python", "python3", "pypy", "pypy3", "node", "deno", "bun", "ruby", "perl", "php", "lua", "R", "Rscript", "julia", "java", "dotnet",
    "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "pwsh", "osascript",
    "make", "cmake", "ninja", "bazel", "gradle", "gradlew", "mvn", "ant", "cargo", "rustc", "go", "gcc", "g++", "cc", "c++", "clang", "clang++", "swift",
    "pytest", "tox", "nox", "jest", "vitest", "mocha", "rspec",
    "pip", "pip3", "pipx", "uv", "uvx", "poetry", "pdm", "hatch", "conda", "mamba", "npm", "npx", "pnpm", "yarn", "gem", "bundle", "composer",
    "docker", "podman", "kubectl", "env", "xargs", "find", "awk", "gawk", "sed", "git", "nohup", "timeout", "watch", "time", "sudo",
    "ipython", "jupyter", "tsx", "ts-node", "nice", "stdbuf", "caffeinate", "command", "exec", "doas", "ionice", "chrt", "taskset", "script",
    "unbuffer",
})
VERSIONED = re.compile(r"(?:python|pypy|ipython|ruby|perl|php|lua|node|deno|bun|java|julia)[0-9][0-9._-]*")
READ_ONLY = frozenset({"ls", "cat", "head", "tail", "wc", "du", "df", "diff", "cmp", "stat", "file", "pwd", "echo", "cut", "tr",
                       "grep", "which", "date", "basename", "dirname", "realpath", "md5", "md5sum", "shasum", "sha256sum"})
WINDOWS_RUNS_CODE = frozenset({c.lower() for c in RUNS_CODE} | {"cmd", "powershell", "py", "pyw", "pythonw", "wsl", "wscript", "cscript", "mshta",
                                                              "rundll32", "regsvr32", "msbuild", "start"})
WINDOWS_PROGRAM_EXTENSIONS = (".exe", ".com", ".bat", ".cmd", ".ps1")


def _words(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def program(word: str) -> str:
    if not host.is_windows():
        return os.path.basename(word)
    name = ntpath.basename(word).lower()
    stem, ext = ntpath.splitext(name)
    return stem if ext in WINDOWS_PROGRAM_EXTENSIONS else name


def builtin_runs_code() -> frozenset[str]:
    return WINDOWS_RUNS_CODE if host.is_windows() else RUNS_CODE


def runs_code(command: str) -> bool:
    words = _words(command) if isinstance(command, str) else None
    while words and ASSIGNMENT.fullmatch(words[0]):
        words = words[1:]
    if not words:
        return False
    first = words[0]
    by_path = first.startswith(".") or "/" in first or "\\" in first or (host.is_windows() and ":" in first)
    return by_path or program(first) in builtin_runs_code() or bool(VERSIONED.fullmatch(program(first)))


ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
RISKY_VARS = re.compile(r"(?:LD|DYLD)_[A-Za-z0-9_]*|PATH|PYTHONPATH|PYTHONSTARTUP|PYTHONHOME|NODE_OPTIONS|NODE_PATH|BASH_ENV|ENV|PERL5OPT|PERL5LIB"
                        r"|RUBYOPT|RUBYLIB|GIT_[A-Za-z0-9_]*|IFS|PS4|PROMPT_COMMAND|SHELLOPTS|BASHOPTS")
SUBCOMMANDED = frozenset({"npm", "pnpm", "yarn", "bun", "cargo", "go", "uv", "pip", "pip3", "pipx", "poetry", "pdm", "hatch", "conda", "mamba", "gem",
                          "bundle", "composer", "make", "cmake", "gradle", "mvn", "dotnet", "swift", "docker", "podman", "kubectl", "helm", "brew",
                          "gh", "git", "terraform", "deno", "jupyter"})
INTERPRETERS = frozenset({"python", "python3", "pypy", "pypy3", "node", "deno", "bun", "ruby", "perl", "php", "lua", "Rscript", "julia",
                          "sh", "bash", "zsh", "dash", "ksh", "ipython", "tsx", "ts-node"})
NO_PREFIX = frozenset({"sudo", "su", "doas", "pkexec", "rm", "rmdir", "unlink", "shred", "srm", "trash", "kill", "pkill", "killall", "dd",
                       "mkfs", "chown", "chgrp", "chmod", "env", "xargs", "find", "awk", "gawk", "sed", "nohup", "timeout", "watch", "time",
                       "command", "exec", "script", "eval", "nice", "stdbuf", "caffeinate", "unbuffer", "osascript", "open", "tee", "mv", "cp"})
GIT_ONE_BY_ONE = frozenset({"reset", "clean", "checkout", "restore", "rebase", "push", "stash", "branch", "switch", "rm", "filter-branch",
                            "filter-repo", "gc", "prune", "update-ref", "reflog", "worktree", "submodule", "config", "remote", "tag"})
SUBCOMMAND = re.compile(r"[a-z][a-z0-9:._-]*")


def unassigned(words: list[str]) -> list[str] | None:
    while words and ASSIGNMENT.fullmatch(words[0]):
        if RISKY_VARS.fullmatch(words[0].split("=", 1)[0]):
            return None
        words = words[1:]
    return words


def approval_prefix(words: list[str]) -> list[str] | None:
    words = unassigned(words)
    if not words:
        return None
    first, args = words[0], words[1:]
    name = program(first)
    if name in NO_PREFIX:
        return None
    if name in INTERPRETERS or VERSIONED.fullmatch(name):
        for i, arg in enumerate(args):
            if arg == "-m" and i + 1 < len(args):
                return [first, *args[: i + 2]]
            if arg in ("-c", "-e", "-p", "-", "--eval", "--print", "-i"):
                return None
            if not arg.startswith("-"):
                return [first, *args[: i + 1]]
        return None
    if name in SUBCOMMANDED:
        lead = 0
        while lead < len(args) and args[lead].startswith("-"):
            lead += 2 if args[lead] in ("-C", "-c", "--git-dir", "--work-tree", "-f", "--file", "--directory") else 1
        sub = args[lead] if lead < len(args) else None
        if sub is None or not SUBCOMMAND.fullmatch(sub) or (name == "git" and sub in GIT_ONE_BY_ONE):
            return None if name == "git" or sub is not None else [first]
        if sub == "run" and lead + 1 < len(args) and SUBCOMMAND.fullmatch(args[lead + 1]):
            return [first, *args[: lead + 2]]
        return [first, *args[: lead + 1]]
    return None if name in builtin_runs_code() or "/" in first else [first]
UNQUOTED_BANNED = set("$`*?[]{}!()")
DISCARD = re.compile(r">&[12](?=$|[\s|&;])|>>?[ \t]*/dev/null(?=$|[\s|&;])")
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n$`\\]+)\")(?=$|[ \t\n|&;])")


def parts(command: str) -> list[list[str]] | None:
    found = joined_parts(command)
    return [words for words, _ in found] if found else None


def joined_parts(command: str) -> list[tuple[list[str], str]] | None:
    if not isinstance(command, str) or "\0" in command or "\r" in command:
        return None
    out, ops, cur, word = [], [], [], []
    started, joined, pending, i, n = False, False, [], 0, len(command)
    plain = True

    def flush() -> None:
        nonlocal word, started, joined, plain
        if started:
            cur.append("".join(word))
            joined = False
        word, started, plain = [], False, True

    while i < n:
        c = command[i]
        if c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                return None
            word.append(command[i + 1:j])
            started, plain, i = True, False, j + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and command[j] != '"':
                if command[j] in "$`":
                    return None
                if command[j] == "\\" and j + 1 < n and command[j + 1] in '$`"\\\n':
                    buf.append("" if command[j + 1] == "\n" else command[j + 1])
                    j += 2
                    continue
                buf.append(command[j])
                j += 1
            if j >= n:
                return None
            word.append("".join(buf))
            started, plain, i = True, False, j + 1
        elif c == "\\":
            if i + 1 >= n:
                return None
            if command[i + 1] != "\n":
                word.append(command[i + 1])
                started, plain = True, False
            i += 2
        elif c in " \t":
            flush()
            i += 1
        elif c == "#" and not started:
            j = command.find("\n", i)
            i = n if j < 0 else j
        elif c == "\n":
            flush()
            i += 1
            for delim, tabs in pending:
                while True:
                    if i >= n:
                        return None
                    j = command.find("\n", i)
                    line, i = command[i:n if j < 0 else j], n if j < 0 else j + 1
                    if (line.lstrip("\t") if tabs else line) == delim:
                        break
            pending = []
            if cur and not joined:
                out.append(cur)
                ops.append("\n")
                cur = []
        elif c in "|&;":
            flush()
            op = command[i:i + 2] if command[i:i + 2] in ("||", "&&") else c
            if op == "&" or not cur or joined:
                return None
            out.append(cur)
            ops.append(op)
            cur, joined = [], True
            i += len(op)
        elif c == ">":
            if started and ("".join(word) not in ("1", "2") or not plain):
                return None
            m = DISCARD.match(command, i)
            if not m:
                return None
            word, started, i = [], False, m.end()
        elif c == "<":
            m = HEREDOC.match(command, i)
            if not m or started or not cur:
                return None
            pending.append((m.group(2) or m.group(3), m.group(1) == "-"))
            i = m.end()
        elif c in UNQUOTED_BANNED:
            return None
        else:
            word.append(c)
            started = True
            i += 1
    flush()
    if pending or joined:
        return None
    found = list(zip([*out, cur] if cur else out, [*ops, ""] if cur else ops, strict=True))
    return found or None


def allowlisted(command, allow: list[str], sandboxed: bool = False, exact=(), approved=()) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    entries, fixed = [e for e in (_words(a) for a in allow) if e], [e for e in (_words(a) for a in exact) if e]
    granted = [e for e in (_words(a) for a in approved) if e]
    found = [words[1:] if words[0] == "time" and len(words) > 1 else words for words in parts(command) or []]

    def ok(words: list[str]) -> bool:
        if words in fixed:
            return True
        bare = unassigned(words)
        if not bare:
            return any(words[: len(e)] == e for e in entries) and (sandboxed or not runs_code(shlex.join(words)))
        def led(e: list[str]) -> bool:
            return words[: len(e)] == e or bare[: len(e)] == e
        return any(led(e) for e in granted) or (any(led(e) for e in entries) and (sandboxed or not runs_code(shlex.join(bare))))
    return bool(found) and all(ok(words) for words in found)


def allow_refusal(command: str, sandboxed: bool, exact: bool = False) -> str | None:
    found = parts(command) if isinstance(command, str) else None
    if not found or len(found) != 1 or SHELL_META & set(command):
        return f"{command!r} is not one simple command"
    if exact:
        return None
    if not sandboxed and runs_code(command):
        return f"{command!r} could run code and the sandbox is off, so it is approved call by call"
    if not sandboxed and program(found[0][0]) not in READ_ONLY:
        return f"{command!r} is not one of the read-only programs the allowlist takes while the sandbox is off, so it is approved call by call"
    return None
