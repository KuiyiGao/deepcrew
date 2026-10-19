from __future__ import annotations

import json
import os
from pathlib import Path
from string import Template

BASIC = Path(__file__).parent / "prompts" / "basic"
NAMES = ("member_system", "member_start", "member_wake", "deliver_offer", "deliver_mail", "task_open", "deny_outside", "leader", "reviewer")
FILLED = {
    "member_system": {"team", "notes", "artifacts", "tools"}, "member_start": {"name", "team", "region", "role", "task"},
    "member_wake": {"name", "team", "region", "role", "task", "body"}, "deliver_offer": {"name", "tasks"}, "deliver_mail": {"messages"},
    "task_open": {"id", "name"}, "deny_outside": {"path", "region", "why"}, "leader": {"team", "project", "provider", "model", "cmd"}, "reviewer": set()}


def _bare_name(chosen: str) -> bool:
    return "/" not in chosen and os.sep not in chosen and not Path(chosen).drive and not chosen.startswith("~")


def pack_dir(cfg: dict | None = None) -> Path | None:
    from .providers import CONFIG_DIR, setting
    chosen = cfg.get("pack") if cfg is not None else setting("DEEPCREW_PROMPT_PACK")
    if not chosen:
        return None
    return CONFIG_DIR / "packs" / chosen if _bare_name(chosen) else Path(chosen).expanduser()


def pack_files(pack: str | None) -> list[str]:
    custom = pack_dir({"pack": pack}) if pack else None
    return [f for f in (*(f"{n}.md" for n in NAMES), "pack.json") if custom and (custom / f).is_file()]


def pack_value(raw: str) -> str:
    from .providers import shown
    raw = raw.strip()
    if raw in ("", "basic"):
        return ""
    value = raw if _bare_name(raw) else os.path.abspath(os.path.expanduser(raw))
    folder = pack_dir({"pack": value})
    if folder is None or not folder.is_dir():
        raise ValueError(f"no prompt pack at {shown(folder)}: start one with a copy of the basic pack, {shown(BASIC)}")
    if not pack_files(value):
        raise ValueError(f"{shown(folder)} holds none of a pack's files ({', '.join(f'{n}.md' for n in NAMES)}, pack.json)")
    check_pack(value)
    pack_settings(value)
    return value


def pack_settings(pack: str | None) -> dict:
    from .providers import shown
    custom = pack_dir({"pack": pack}) if pack else None
    path = custom / "pack.json" if custom else None
    if path is None or not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise ValueError(f"{shown(path)} is not valid JSON: {e}") from None
    if not isinstance(settings, dict):
        raise ValueError(f"{shown(path)} must hold an object: allow, tools, each a list of non-empty strings")
    for key in settings:
        if key not in ("allow", "tools") or not isinstance(settings[key], list) or not all(isinstance(v, str) and v.strip() for v in settings[key]):
            raise ValueError(f"{shown(path)}: {key!r} must be one of allow, tools, each a list of non-empty strings")
    return settings


def check_pack(pack: str | None) -> None:
    custom = pack_dir({"pack": pack}) if pack else None
    for name in NAMES if custom else ():
        path = custom / f"{name}.md"
        if not path.exists():
            continue
        raw, known = path.read_text(encoding="utf-8-sig"), FILLED[name]
        for m in Template.pattern.finditer(raw):
            word = m.group("named") or m.group("braced")
            if m.group("invalid") is None and (word is None or word in known):
                continue
            line = raw.count("\n", 0, m.start()) + 1
            why = (f"invalid placeholder {raw[m.start():m.start() + 2].rstrip()!r}; write $$ for a literal $" if word is None else
                   f"${word} is not filled here (this template gets {', '.join('$' + k for k in sorted(known)) or 'none'})")
            raise ValueError(f"prompt pack {Path(pack).name or pack}, {path.name} line {line}: {why}")


def text(name: str, cfg: dict | None = None) -> str:
    if name not in NAMES:
        raise KeyError(name)
    custom = pack_dir(cfg)
    path = custom / f"{name}.md" if custom and (custom / f"{name}.md").exists() else BASIC / f"{name}.md"
    return path.read_text(encoding="utf-8-sig").strip()


def render(template: str, cfg: dict | None = None, /, **values) -> str:
    return Template(text(template, cfg)).substitute(values)
