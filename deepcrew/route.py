from __future__ import annotations

import threading
import time

from . import extensions, member, policy, providers
from .board import Board

UNTAKEN_S = 60


def _waking_mail(board: Board, name: str) -> bool:
    senders = {msg.get("from") for _, msg in board.peek_mail(name)}
    return "leader" in senders


def plan(board: Board) -> dict:
    tasks, members, now = board.tasks(), board.members(), time.time()
    done = {t["id"]: t for t in tasks if t["state"] == "done"}
    dead = {t["id"] for t in tasks if t["state"] == "cancelled"}
    running = {m["name"] for m in members if m["state"] in ("starting", "active")}
    failed = {m["name"] for m in members if m["state"] == "failed"}
    idle = {m["name"] for m in members if m["name"] not in failed and not m.get("task")}
    blocked = [{"task": t["id"], "reason": "dependency " + ", ".join(sorted(set(t["deps"]) & dead)) + " was cancelled"}
               for t in tasks if t["state"] == "open" and set(t["deps"]) & dead]
    blocked += [{"task": t["id"], "reason": "its metadata.scope is not a list of project paths"} for t in tasks if t["state"] == "open" and t["scope"] is None]
    unowned = []
    verbs = {"pass": "declined it", "release": "gave it back", "skip": "left it undecided"}
    for t in (t for t in tasks if t["state"] == "open" and t["scope"] is not None and set(t["deps"]) <= set(done)):
        gave_up = {*t["released_by"], *t["passed_by"], *t["ignored_by"]} | failed
        fit = {m["name"] for m in members if board.fits(m, t)} - failed
        if not t["scope"]:
            if (idle & fit) - gave_up:
                continue
            if t["strength"] and not fit:
                blocked.append({"task": t["id"], "reason": f"it needs a member on {board.needs(t)}: start one with {providers.NAME} spawn NAME "
                                                           f"--strength {t['strength']} --task {t['id']}"})
                continue
        else:
            owners = [m for m in members if policy.within(t["scope"], m["region"])]
            live = [m for m in owners if m["name"] not in gave_up]
            if any(m["name"] in fit for m in live):
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
        unowned.append({**t, "waited": now - ready, "able": fit - gave_up})
    after = UNTAKEN_S
    for t in unowned:
        if t["waited"] < after or (not t["scope"] and t["able"]):
            continue
        start = f"{providers.NAME} spawn NAME" + (" --region PATH" if t["scope"] else "") + (f" --strength {t['strength']}" if t["strength"] else "")
        blocked.append({"task": t["id"], "reason": ("no member's region covers its scope" if t["scope"] else "no member left to take it")
                                                   + f"; start one with {start} --task {t['id']}"})
    wake = [] if board.paused() else [m["name"] for m in members if m["name"] not in running | failed
                                      and (member.offer(board, m) or _waking_mail(board, m["name"]))]
    slots = max(0, board.cfg["max_active"] - len(running))
    p = {"wake": wake[:slots], "waiting": wake[slots:], "blocked": blocked, "notes": turn_notes(board, members)}
    return extensions.chain("route", p, board, project=board.root)


def ask(board: Board, me: str, tid: str) -> str | None:
    t, why = board.claim(me, tid)
    return None if t else why


def assign(board: Board, name: str, tid: str) -> str | None:
    with board.locked():
        got, why = board._assign(name, tid)
    if got is None:
        return why
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
    board.reap(member.alive)
    p = plan(board)
    p["woken"], p["errors"] = [], []
    for name in p["wake"]:
        try:
            if member.wake(board, name):
                p["woken"].append(name)
        except Exception as e:
            p["errors"].append(f"wake {name}: {e}")
    return p


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
