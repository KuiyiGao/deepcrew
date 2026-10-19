from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from pathlib import Path

from . import host


def pathmod():
    return ntpath if host.is_windows() else posixpath


def fold(path: str) -> str:
    return pathmod().normcase(path)


def ignores_case() -> bool:
    return host.is_windows() or sys.platform == "darwin"


def inside(path: str, top: str) -> bool:
    path, top = fold(path), fold(top)
    return path == top or path.startswith(top + pathmod().sep)


def rule_path(path) -> str:
    real = os.path.realpath(path)
    if not host.is_windows():
        return real
    drive, rest = ntpath.splitdrive(real)
    return (f"/{drive[0].lower()}" if drive[1:2] == ":" else drive.replace("\\", "/")) + rest.replace("\\", "/")


def git_bash_native(arg: str) -> str | None:
    drive, sep, rest = arg[1:].partition("/")
    if not arg.startswith("/") or len(drive) != 1 or not drive.isalpha():
        return None
    return f"{drive.upper()}:\\" + rest.replace("/", "\\") if sep or not rest else None


def norm(entry) -> str:
    if not isinstance(entry, str) or not entry.strip() or entry.strip().startswith(("/", "~")) or "\\" in entry:
        raise ValueError(f"region entries are project-relative paths, got {entry!r}")
    entry = entry.strip()
    parts = [p for p in entry.split("/") if p not in ("", ".")]
    if host.is_windows() and ":" in entry:
        raise ValueError(f"region entries are project-relative paths without ':' on Windows, got {entry!r}")
    if not parts or ".." in parts:
        raise ValueError(f"region entries must stay inside the project, got {entry!r}")
    return "/".join(parts) + ("/" if entry.endswith("/") else "")


def norm_all(entries) -> list[str]:
    return sorted({norm(e) for e in entries or []})


def covers(region: list[str], path: str) -> bool:
    if host.is_windows():
        region, path = [r.lower() for r in region], path.lower()
    return any(r == path or (r.endswith("/") and path.startswith(r)) for r in region)


def within(scope: list[str], region: list[str]) -> bool:
    return all(covers(region, s) for s in scope)


def overlap(a: list[str], b: list[str]) -> bool:
    return any(covers([x], y) or covers([y], x) for x in a for y in b)


def minimal(entries) -> list[str]:
    entries = norm_all(entries)
    return [e for e in entries if not any(o != e and covers([o], e) for o in entries)]


def relpath(root: Path, file_path: str) -> str | None:
    p = Path(file_path)
    try:
        rel = Path(os.path.realpath(p if p.is_absolute() else root / p)).relative_to(os.path.realpath(root))
    except ValueError:
        return None
    return None if rel == Path(".") else rel.as_posix()
