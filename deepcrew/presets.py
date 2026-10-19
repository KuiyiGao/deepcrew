from __future__ import annotations

STRENGTHS = {
    "fast": {"label": "Fast", "summary": "The provider's first model at high effort. Cheapest per task. The default.", "model": 0, "effort": "high"},
    "strong": {"label": "Strong", "summary": "The provider's first model at maximum effort.", "model": 0, "effort": "max"},
    "strongest": {"label": "Strongest", "summary": "The provider's strongest model at maximum effort. Highest price per token.",
                  "model": -1, "effort": "max"},
}
SIZES = {
    "solo": {"label": "Solo", "summary": "One member; tasks run one after another.",
             "values": {"DEEPCREW_MAX_ACTIVE": "1", "DEEPCREW_HOLD_S": "120", "DEEPCREW_OFFER_SIZE": "3"}},
    "pair": {"label": "Pair", "summary": "Two members on separate regions, for example code and tests.",
             "values": {"DEEPCREW_MAX_ACTIVE": "2", "DEEPCREW_HOLD_S": "300", "DEEPCREW_OFFER_SIZE": "5"}},
    "squad": {"label": "Squad", "summary": "Up to four members in parallel.",
              "values": {"DEEPCREW_MAX_ACTIVE": "4", "DEEPCREW_HOLD_S": "300", "DEEPCREW_OFFER_SIZE": "5"}},
    "crowd": {"label": "Crowd", "summary": "Up to eight members, for wide, independent work. The default.",
              "values": {"DEEPCREW_MAX_ACTIVE": "8", "DEEPCREW_HOLD_S": "600", "DEEPCREW_OFFER_SIZE": "8"}},
    "large": {"label": "Large",
              "summary": "Up to sixteen members for big projects: work is posted in rounds and idle members help running tasks.",
              "values": {"DEEPCREW_MAX_ACTIVE": "16", "DEEPCREW_HOLD_S": "600", "DEEPCREW_OFFER_SIZE": "8"}},
}
SCOPES = {
    "locked": {"label": "Locked", "summary": "Sandbox, no network, no allowlist: only Claude Code's own read-only commands inside the "
                                             "workspace run without the leader.",
               "values": {"DEEPCREW_SANDBOX": "on", "DEEPCREW_SANDBOX_DOMAINS": "none", "DEEPCREW_ALLOW": ""}},
    "standard": {"label": "Standard", "summary": "No sandbox: read-only commands run without asking, any other command waits for the leader.",
                 "values": {"DEEPCREW_SANDBOX": "off", "DEEPCREW_SANDBOX_DOMAINS": "", "DEEPCREW_ALLOW": "ls,wc,du,head,tail,cat,diff"}},
    "builder": {"label": "Builder", "summary": "Sandbox, package registries reachable; tests and builds run without asking.",
                "values": {"DEEPCREW_SANDBOX": "on", "DEEPCREW_SANDBOX_DOMAINS": "",
                           "DEEPCREW_ALLOW": "ls,wc,du,head,tail,cat,diff,python3,pytest,node,npm test,npm run,make,cargo test,go test"}},
}
DEFAULTS = {"strength": "fast", "size": "crowd", "scope": "standard"}


SETUPS = {
    "standard": {"label": "Standard", "summary": "A first try: the cheap model and up to four members; read-only commands run without asking.",
                 "presets": ("fast", "squad", "standard")},
    "crowd": {"label": "Crowd", "summary": "A project with many tasks: eight members, and tests and builds run without asking.",
              "presets": ("fast", "crowd", "builder")},
    "strong": {"label": "Strong", "summary": "A hard bug or a hard task: the strongest model and four members. More members cost more.",
               "presets": ("strongest", "squad", "builder")},
    "locked": {"label": "Locked", "summary": "A repository you do not know yet: no network, and anything beyond reading asks first.",
               "presets": ("strong", "squad", "locked")},
}


def expand(names: list[str]) -> list[str]:
    return list(SETUPS[names[0]]["presets"]) if len(names) == 1 and names[0] in SETUPS else list(names)


def strength_values(name: str, provider: dict) -> dict[str, str]:
    s, efforts = STRENGTHS[name], list(provider.get("efforts") or [])
    effort = s["effort"] if s["effort"] in efforts else (efforts[-1] if efforts else "default")
    return {"DEEPCREW_MODEL": provider["models"][s["model"]], "DEEPCREW_EFFORT": effort}


def tables(provider: dict) -> dict[str, dict]:
    return {"strengths": {k: {**v, "values": strength_values(k, provider)} for k, v in STRENGTHS.items()}, "sizes": SIZES, "scopes": SCOPES,
            "setups": {k: {**v, "presets": list(v["presets"]), "values": values(list(v["presets"]), provider)} for k, v in SETUPS.items()}}


def values(names: list[str], provider: dict) -> dict[str, str]:
    out, seen = {}, set()
    for name in names:
        table = "strength" if name in STRENGTHS else "size" if name in SIZES else "scope" if name in SCOPES else None
        if table is None:
            raise ValueError(f"unknown preset {name!r}; strengths: {', '.join(STRENGTHS)}; sizes: {', '.join(SIZES)}; scopes: {', '.join(SCOPES)}")
        if table in seen:
            raise ValueError(f"choose one {table}, not two")
        seen.add(table)
        out.update(strength_values(name, provider) if table == "strength" else (SIZES if table == "size" else SCOPES)[name]["values"])
    return out
