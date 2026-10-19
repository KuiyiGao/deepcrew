from __future__ import annotations

import math
import re
import threading
import time

from . import extensions, member, policy, providers
from . import load as host_load
from . import presets
from .board import RESERVED, Board

UNTAKEN_S = 60
PITCH_CHARS = 300
ASK_WAKE = 2
STAGGER_TIMEOUT_S = 60.0
FILLER = frozenset(("a", "an", "the", "and", "or", "of", "for", "to", "with", "in", "on", "at", "by", "from", "into", "as", "is"))
MIN_NAME, MAX_NAME = 3, 20
ROLE_CHARS = 300


def _waking_mail(board: Board, name: str) -> bool:
    senders = {msg.get("from") for _, msg in board.peek_mail(name)}
    return "leader" in senders or board.partner_wake(name, senders)


def _own_offer(board: Board, m: dict) -> list[dict]:
    return [t for t in member.offer(board, m) if not t["scope"] or policy.within(t["scope"], m["region"])]


def _deciding(t: dict, members: list[dict]) -> list[str]:
    answered = {*t["volunteers"], *t["passed_by"], *t["released_by"], *t["ignored_by"]}
    return [m["name"] for m in members if t["id"] in (m.get("offered") or []) and m["state"] in ("starting", "active")
            and not m.get("task") and m["name"] not in answered]


def _asked(t: dict, members: list[dict]) -> dict[str, float]:
    working = {m["name"] for m in members if m["state"] != "failed"}
    return {n: when for n, when in t["volunteers"].items() if n in working}


def plan(board: Board) -> dict:
    with board.still():
        return _plan(board)


def _plan(board: Board) -> dict:
    tasks, members, now = board.tasks(), board.members(), time.time()
    leases = board.leases()
    done = {t["id"]: t for t in tasks if t["state"] == "done"}
    dead = {t["id"] for t in tasks if t["state"] == "cancelled"}
    running = {m["name"] for m in members if m["state"] in ("starting", "active")}
    failed = {m["name"] for m in members if m["state"] == "failed"}
    helps = {m["name"]: h for m in members if (h := board.helping(m))}
    idle = {m["name"] for m in members if m["name"] not in failed and not m.get("task") and m["name"] not in helps}
    blocked = [{"task": t["id"], "reason": "dependency " + ", ".join(sorted(set(t["deps"]) & dead)) + " was cancelled"}
               for t in tasks if t["state"] == "open" and set(t["deps"]) & dead]
    blocked += [{"task": t["id"], "reason": "its metadata.scope is not a list of project paths"} for t in tasks if t["state"] == "open" and t["scope"] is None]
    unowned, awards, choices, nudge = [], [], [], set()
    verbs = {"pass": "declined it", "release": "gave it back", "skip": "left it undecided"}
    for t in (t for t in tasks if t["state"] == "open" and t["scope"] is not None and set(t["deps"]) <= set(done)):
        gave_up = {*t["released_by"], *t["passed_by"], *t["ignored_by"]} | failed
        fit = {m["name"] for m in members if board.fits(m, t)} - failed
        if not t["scope"]:
            if asked := _asked(t, members):
                if not _deciding(t, members) and len(asked) == 1:
                    awards.append(t["id"])
                elif not _deciding(t, members):
                    names = sorted(asked, key=asked.get)
                    choices.append({"task": t["id"], "title": t["title"], "volunteers": names, "suggested": best_match(board, t, names),
                                    "pitches": {n: t["pitches"][n] for n in names if n in t["pitches"]}})
                continue
            if (idle & fit) - gave_up:
                continue
            if t["strength"] and not fit:
                blocked.append({"task": t["id"], "reason": f"it needs a member on {board.needs(t)}: start one with {providers.NAME} spawn NAME "
                                                           f"--strength {t['strength']} --task {t['id']}"})
                continue
        else:
            if board.lease_clash("", t["scope"], leases):
                continue
            owners = [m for m in members if policy.within(t["scope"], m["region"])]
            live = [m for m in owners if m["name"] not in gave_up]
            if any(m["name"] in fit for m in live):
                continue
            if asked := _asked(t, members):
                if not _deciding(t, members):
                    names = sorted(asked, key=asked.get)
                    choices.append({"task": t["id"], "title": t["title"], "volunteers": names, "suggested": best_match(board, t, names),
                                    "pitches": {n: t["pitches"][n] for n in names if n in t["pitches"]}})
                continue
            if near := ranked(board, t, sorted((idle & fit) - gave_up))[:ASK_WAKE]:
                nudge.update(near)
                continue
            if live:
                blocked.append({"task": t["id"], "reason": f"it needs a member on {board.needs(t)}, and its region belongs to "
                                                           f"{live[0]['name']} on {board.model_of(live[0])}: give its paths to a member on that model "
                                                           f"(region), cancel it and post it again with another --scope, or forget "
                                                           f"{live[0]['name']} and start a member with --strength {t['strength']} for that region"})
                continue
            if owners:
                why = [f"{k.split(':', 1)[1]} {verbs[k.split(':', 1)[0]]}: {v}" for k, v in t["reasons"].items()]
                blocked.append({"task": t["id"], "reason": "no member left to take it (" + "; ".join(why[-3:] or ["its only owner failed"]) + ")"})
                continue
            if any(policy.overlap(t["scope"], m["region"]) for m in members):
                blocked.append({"task": t["id"], "reason": "scope crosses a region boundary"})
                continue
        ready = max([t["changed"], *(done[d]["changed"] for d in t["deps"])])
        unowned.append({**t, "waited": now - ready, "able": fit - gave_up, "refused": bool(fit) and not fit - gave_up})
    groups: list[dict] = []
    for t in [t for t in unowned if t["scope"] or t["posted_by"] == "leader"]:
        joined = [g for g in groups if (policy.overlap(g["region"], t["scope"]) if t["scope"] else not g["region"])]
        merged = {"region": policy.minimal([e for g in joined for e in g["region"]] + t["scope"]),
                  "tasks": [x for g in joined for x in g["tasks"]] + [t["id"]], "waited": min([t["waited"], *(g["waited"] for g in joined)]),
                  "strength": max([t["strength"], *(g["strength"] for g in joined)], key=lambda s: -1 if s is None else list(presets.STRENGTHS).index(s))}
        groups = [g for g in groups if g not in joined] + [merged]
    proposed = {tid for g in groups for tid in g["tasks"]}
    if any(g["region"] for g in groups):
        groups = [g for g in groups if g["region"]]
    after = board.cfg.get("propose_after_s", UNTAKEN_S)
    for t in unowned:
        if (t["waited"] < after and not t["refused"]) or (not t["scope"] and t["able"]):
            continue
        if t["id"] in proposed:
            continue
        start = f"{providers.NAME} spawn NAME" + (" --region PATH" if t["scope"] else "") + (f" --strength {t['strength']}" if t["strength"] else "")
        blocked.append({"task": t["id"], "reason": ("no member's region covers its scope" if t["scope"] else "no member left to take it")
                                                   + f"; start one with {start} --task {t['id']}"})
    titles, taken, refused = {t["id"]: t for t in tasks}, set(), {t["id"] for t in unowned if t["refused"]}
    for g in groups:
        first = titles[g["tasks"][0]]
        g["name"] = member_name(board, g["region"], taken, first["title"])
        g["role"] = (first["title"] + ". " + (first["body"].strip().splitlines() or [""])[0]).strip(". ")[:ROLE_CHARS]
        g["due"] = g["waited"] >= after or set(g["tasks"]) <= refused
        g["declined"] = [f"{k.split(':', 1)[1]} {verbs[k.split(':', 1)[0]]}: {v}" for tid in g["tasks"] for k, v in titles[tid]["reasons"].items()]
        taken.add(g["name"])
    wake = [] if board.paused() else [m["name"] for m in members if m["name"] not in running | failed
                                      and (_own_offer(board, m) or _waking_mail(board, m["name"]) or m["name"] in nudge)]
    look = host_load.sample() if host_load.guard_on() else None
    slots = max(0, host_load.limit(look, board.cfg["max_active"], len(running)) - len(running))
    ready = sum(t["state"] == "open" and t["scope"] is not None and set(t["deps"]) <= set(done) for t in tasks)
    held = [{"task": t["id"], "title": t["title"], "owner": t["owner"], "helpers": sorted(n for n, h in helps.items() if h["task"] == t["id"])}
            for t in tasks if t["state"] == "claimed"]
    room = len(idle) + max(0, board.cfg["max_active"] - len(members)) - ready
    capacity = {"idle": sorted(idle), "free": max(0, board.cfg["max_active"] - len(members)), "ready": ready, "room": room,
                "running": held, "done": len(done)} if idle and room > 0 and held else None
    p = {"wake": wake[:slots], "waiting": wake[slots:], "blocked": blocked, "notes": turn_notes(board, members), "load": look}
    p.update(awards=awards, choices=choices, proposals=groups, capacity=capacity, helpers=helps)
    return extensions.chain("route", p, board, project=board.root)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 2 and w not in FILLER}


def ranked(board: Board, t: dict, names: list[str]) -> list[str]:
    members, did = board.members(), {}
    for x in board.tasks():
        if x["state"] == "done" and x["owner"]:
            did[x["owner"]] = did.get(x["owner"], "") + " " + x["title"]
    docs = {m["name"]: _words(" ".join([m.get("role") or "", *m["region"], did.get(m["name"], "")])) for m in members}
    want = _words(t["title"] + " " + t["body"])

    def score(name: str) -> float:
        return sum(math.log((1 + len(docs)) / (1 + sum(w in d for d in docs.values()))) for w in want & docs.get(name, set()))
    return sorted(sorted(names, key=lambda n: t["volunteers"].get(n, 0.0)), key=score, reverse=True)


def best_match(board: Board, t: dict, names: list[str]) -> str:
    return ranked(board, t, names)[0]


def _award(board: Board, tid: str) -> str | None:
    t = board.task(tid)
    if t is None or t["state"] != "open" or not t["volunteers"]:
        return None
    free = [n for n in t["volunteers"] if (m := board.member(n)) and m["state"] != "failed" and not m.get("task") and not board.helping(m)]
    if not free:
        return None
    winner = best_match(board, t, free)
    return winner if board._assign(winner, tid)[0] else None


def ask(board: Board, me: str, tid: str, pitch: str | None = None) -> str | None:
    with board.locked():
        t, m = board.task(tid), board.member(me)
        if t and t["scope"] and m and not policy.within(t["scope"], m["region"]):
            if why := board.why_not(me, tid, chosen=True):
                return why
            if not board.open_to_ask(t):
                return f"task {tid} is for the member whose region holds {', '.join(t['scope'])}; ask the leader by message if you should take it"
            board._mark(me, [tid], "volunteer", f"{time.time():.6f}")
            if pitch and pitch.strip():
                board._mark(me, [tid], "pitch", " ".join(pitch.split())[:PITCH_CHARS])
            return (f"Noted: you asked for task {tid}, outside your region. The leader picks who takes it and briefs them; if it is you, "
                    "it comes as your next message. End your turn now.")
        if why := board.why_not(me, tid):
            return why
        t, members = board.task(tid), board.members()
        deciding = [n for n in _deciding(t, members) if n != me]
        asked = [n for n in _asked(t, members) if n != me]
        if deciding or asked:
            board._mark(me, [tid], "volunteer", f"{time.time():.6f}")
            return (f"Noted: you asked for task {tid}. "
                    + ("Other members asked for it too, so the leader picks one of those who asked." if asked else
                       "Other members are deciding on it too: if one of them asks for it as well, the leader picks one of those who "
                       "asked; if none does, it is yours.")
                    + " If it is you, it comes as your next message. End your turn now.")
        got, why = board._claim(me, tid)
        return None if got else why


def assign(board: Board, name: str, tid: str, note: str | None = None) -> str | None:
    with board.locked():
        got, why = board._assign(name, tid)
    if got is None:
        return why
    brief(board, name, tid, note)
    run(board)
    return None


def brief(board: Board, name: str, tid: str, note: str | None = None) -> None:
    t = board.task(tid)
    if t is None:
        return
    others = sorted({o["name"] for o in board.members() if o["name"] != name and t["scope"] and policy.overlap(t["scope"], o["region"])})
    lines = [f"The leader chose you for task {tid} ({t['title']})."]
    if t["scope"]:
        told = f"; it lies in {', '.join(others)}'s region, and {'they were' if len(others) > 1 else 'it was'} told to leave those files to you"
        lines.append(f"Your lease: you may change {', '.join(t['scope'])} until the task is done{told if others else ''}.")
    if note and note.strip():
        lines.append("From the leader: " + note.strip())
    lines.append("Start with a kickoff report: message the leader with kind kickoff, your plan in a few lines and a one-sentence gist.")
    board.send("leader", name, "notice", "\n".join(lines), tid)
    for o in others:
        board.send("leader", o, "notice", f"{name} holds {', '.join(x for x in t['scope'] if policy.overlap([x], board.member(o)['region']))} for task "
                   f"{tid} ({t['title']}) until it is done: leave those files to {name}, and send it a message for any change you need there.", tid)


def add_helper(board: Board, tid: str, name: str, paths: list[str] | None = None) -> str | None:
    with board.locked():
        t, m = board.task(tid), board.member(name)
        if t is None or t["state"] != "claimed":
            return f"task {tid} is not being worked on" if t else f"there is no task {tid}"
        if m is None or m["state"] == "failed":
            return f"{name!r} is not a working member of this team"
        if name == t["owner"] or m.get("task") or board.helping(m):
            return f"{name} is busy with " + ("that task" if name == t["owner"] else f"task {m.get('task') or board.helping(m)['task']}")
        try:
            area = policy.norm_all(paths) if paths else []
        except ValueError as e:
            return str(e)
        if not policy.within(area, t["scope"]):
            return f"{area} is not inside task {tid}'s scope {t['scope']}"
        m["helps"] = {"task": tid, "owner": t["owner"], "paths": area, "since": time.time()}
        board.put_member(m)
        board.bump()
    can = "may change " + ", ".join(area) if area else "changes no files: it reads, checks and advises by message"
    board.send("leader", name, "notice", f"Help {t['owner']} with task {tid} ({t['title']}): you {can} until the task is done. "
               f"Agree with {t['owner']} by message on who does which part and tell them when yours is done; send questions and results, "
               "not acknowledgements.", tid)
    board.send("leader", t["owner"], "notice", f"{name} now helps you with task {tid} and {can}. Give it a part by message; "
               "you still complete the task.", tid)
    run(board)
    return None


def turn_notes(board: Board, members: list[dict]) -> list[str]:
    busy = {m["name"] for m in members if m["state"] in ("starting", "active") and m.get("turn") and not m.get("task")}
    if not busy:
        return []
    takers: dict[str, list[str]] = {}
    for m in members:
        if m["state"] != "failed":
            for t in member.offer(board, m):
                takers.setdefault(t["id"], []).append(m["name"])
    return [f"task {tid} goes to {', '.join(names)} when {'its' if len(names) == 1 else 'their'} current turn ends"
            for tid, names in sorted(takers.items(), key=lambda x: (len(x[0]), x[0])) if set(names) <= busy]


def run(board: Board) -> dict:
    board.reap(member.living)
    p = plan(board)
    p["woken"], p["errors"] = [], []
    p["given"] = {}
    if p["awards"]:
        with board.locked():
            p["given"] = {tid: w for tid in p["awards"] if (w := _award(board, tid))}
        p = {**p, **{k: v for k, v in plan(board).items() if k in ("wake", "waiting")}}
    for name in p["wake"]:
        try:
            if member.wake(board, name):
                p["woken"].append(name)
        except Exception as e:
            p["errors"].append(f"wake {name}: {e}")
    p["eased"] = ease(board, p.get("load"))
    return p


def ease(board: Board, look: dict | None) -> list[str]:
    if not look or look.get("level") != "full":
        return []
    names = [m["name"] for m in board.members() if m["state"] == "active" and not m.get("turn") and not m.get("task") and member.alive(m)]
    for name in names:
        member._end(board.member(name), False)
    return names


def rest(board: Board, by: str = "leader", wait: bool = True) -> list[str]:
    board.update_cfg(lambda cfg: cfg.__setitem__("paused", {"by": by, "ts": time.time()}))
    names = [m["name"] for m in board.members() if member.alive(m)]
    for name in names:
        member._end(board.member(name), False)

    def finish() -> None:
        for name in names:
            if not board.paused():
                break
            member.stop(board, name)
    if wait:
        finish()
    else:
        threading.Thread(target=finish, name="rest", daemon=True).start()
    return names


def resume(board: Board, by: str = "leader", wait: bool = True, **why) -> dict:
    st = member.budget_state(board)
    if st["reached"]:
        raise ValueError(f"the team has spent ${st['spent']:.2f} of the ${st['usd']:.2f} budget the user set; only the user can raise it, "
                         "on the team's page")
    board.update_cfg(lambda cfg: cfg.update(paused=None, resumed={"by": by, "ts": time.time(), **why}))
    with board.locked():
        failed = [m for m in board.members() if m["state"] == "failed"]
        for m in failed:
            m.update(state="idle", failure=None, updated=time.time())
            board.put_member(m)
        if failed:
            board.bump()
    if wait:
        return run(board)
    threading.Thread(target=run, args=(board,), name="resume", daemon=True).start()
    return {}


def member_name(board: Board, region: list[str], taken: set[str] = frozenset(), title: str | None = None) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if w not in FILLER]
    base = "-".join(words[:2])
    if not base and region:
        base = re.sub(r"[^a-z0-9-]+", "-", region[0].rstrip("/").rsplit("/", 1)[-1].split(".")[0].lower()).strip("-") or "member"
    base = base or "reader"
    if len(base) < MIN_NAME or not base[0].isalpha() or base in RESERVED:
        base = "m-" + base
    base = base[:MAX_NAME].rstrip("-")
    names = {m["name"] for m in board.members()} | set(taken) | set(board.used_names())
    name, n = base, 2
    while name in names:
        name, n = f"{base}-{n}", n + 1
    return name


def start_proposals(board: Board, roles: dict[str, str] | None = None) -> list[dict]:
    started, proposals = [], plan(board)["proposals"]
    room = board.cfg["max_active"] - len(board.members())
    if room > 0:
        proposals = proposals[:room]
    stagger = board.cfg["provider"].get("prefix_cache") and len(proposals) > 1
    for i, g in enumerate(proposals):
        log = board.dir / "logs" / f"{g['name']}.ndjson"
        offset = log.stat().st_size if log.exists() else 0
        started.append(member.start(board, g["name"], g["region"], g["tasks"][0], (roles or {}).get(g["name"]) or g["role"], g.get("strength")))
        if stagger and i == 0:
            member.first_reply(board, g["name"], offset, STAGGER_TIMEOUT_S)
    return started
