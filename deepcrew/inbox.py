from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

from . import extensions, member, policy, providers, route
from .board import Board
from .settings import fallback_now, setup_state

CMD = providers.NAME

ADD_DIR_HINT = "Unless you did already, write this line in the chat for the user to type, so this session reaches it too:"
QUIET = ("note:",)
PRICE_NOTICE_S = 600.0


def money(usd: float | None) -> str:
    return f"${usd:,.2f}" if usd is not None else "an unknown amount"


def span(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h" + (f" {m} min" if m else "")
    d, h = divmod(h, 24)
    return f"{d} d" + (f" {h} h" if h else "")


def utc_at(ts: float, now: float) -> str:
    g = time.gmtime(ts)
    return time.strftime("%H:%M UTC" if g[:3] == time.gmtime(now)[:3] else "%a %H:%M UTC", g)


def share_words(share: float) -> str:
    return "list prices" if share >= 1 else "half price" if share == 0.5 else f"{share:.0%} of list prices"


def failed_words(kind: str | None) -> str:
    return {"balance": "had no balance", "rate_limit": "kept hitting its rate limit", "overload": "stayed overloaded"}.get(kind or "", "failed")


def add_dir(path: str) -> str:
    return f"/add-dir {path}"


def page_url(team_dir: Path) -> str | None:
    from . import ui
    found = ui.running(team_dir)
    return found["url"] if found else None


def with_widening(board: Board, requests: list[dict]) -> list[dict]:
    return [{**r, "widening": policy.widening(board, r)} for r in requests]


def widened_words(w: dict) -> str:
    if w.get("kind") == "site":
        return f"members reach {w.get('value')} without asking"
    if w.get("kind") == "tool":
        return f"members use {w.get('value')} without asking"
    return "added to the team allowlist"


def inbox_items(board: Board) -> dict:
    reqs, asks, plan, tasks = with_widening(board, board.pending("perms")), board.pending("questions"), route.plan(board), board.tasks()
    by_user = [r for kind in ("perms", "questions") for r in _answered_by_user(board, kind)]
    finished = bool(tasks) and all(t["state"] in ("done", "cancelled") for t in tasks) and not any(m.get("task") for m in board.members())
    items = {f"perm:{r['id']}": ("perm", r) for r in reqs} | {f"question:{q['id']}": ("question", q) for q in asks}
    items |= {f"blocked:{b['task']}:{b['reason']}": ("blocked", b) for b in plan["blocked"]}
    items |= {f"note:{n}": ("note", n) for n in plan["notes"]}
    items |= {f"answer:{r['id']}": ("answer", r) for r in by_user}
    if finished:
        items[f"finished:{len(tasks)}"] = ("finished", {"tasks": len(tasks), "done": sum(t["state"] == "done" for t in tasks)})
    if pn := price_notice(board):
        items[f"prices:{pn['until']:.0f}"] = ("prices", pn)
    if moved := fallback_now(board.live_cfg()):
        items[f"provider:{moved['ts']}"] = ("provider", moved)
    for got in extensions.call("inbox", board, project=board.root):
        items |= {f"ext:{k}": ("ext", str(line)) for k, line in (got or {}).items()} if isinstance(got, dict) else {}
    if setup := setup_state(board):
        items[f"setup:{setup['since']}:{','.join(setup['missing']) or 'done'}"] = ("setup", {**setup, "page_url": page_url(board.dir)})
    cfg = board.live_cfg()
    for key in ("paused", "resumed"):
        if (x := cfg.get(key)) and x.get("by") == "user":
            items[f"crew:{key}:{x['ts']}"] = ("crew", {"what": key, **x})
    items |= {f"link:{f['path']}:{f['ts']}": ("link", f) for f in board.folders() if f.get("by") == "user"}
    if (p := cfg.get("paused")) and p.get("by") == "budget":
        items[f"budget:reached:{p['ts']}"] = ("budget", {"what": "reached", **p})
    if isinstance(b := cfg.get("budget"), dict) and b.get("by") == "user":
        items[f"budget:set:{b.get('ts')}"] = ("budget", {"what": "set", **b, "spent": member.spent(board)})
        if b.get("near") and not (p and p.get("by") == "budget"):
            items[f"budget:near:{b.get('ts')}"] = ("budget", {"what": "near", "usd": b.get("usd"), "spent": b["near"].get("spent")})
    return items


def price_notice(board: Board, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    t = providers.tariff_now(board.cfg["provider"], now)
    if not t or t["next"] <= t["share"] or t["until"] - now > PRICE_NOTICE_S:
        return None
    if not any(m["state"] in ("starting", "active") for m in board.members()):
        return None
    after = providers.tariff_now(board.cfg["provider"], t["until"])
    return {"until": t["until"], "share": t["share"], "next": t["next"], "then": after["until"] if after else None,
            "in": t["until"] - now, "at": utc_at(t["until"], now), "back": utc_at(after["until"], now) if after else None}


def _answered_by_user(board: Board, kind: str) -> list[dict]:
    from .board import _load
    return [r for r in _load(sorted((board.dir / kind).glob("*.json"))) if r.get("by") == "user" and r["state"] != "pending"]


def _item_line(kind: str, x: dict) -> str:
    if kind == "perm":
        answers = ["[--deny]", *([f"[--always: {providers.shown(x['widening']['text'])}]"] if x.get("widening") else []),
                   *(["[--link]"] if x.get("folder") else [])]
        line = (f"permission {x['id']} from {x['member']}: {x['tool']} {json.dumps(x['input'], ensure_ascii=False)}"
                + (f"\n  reviewer: {x['review']}" if x.get("review") else "")
                + f"\n  answer: {CMD} approve {shlex.quote(x['id'])} {' '.join(answers)}")
        if x.get("folder"):
            line += (f"\n  outside the project: {x['folder']} ({'writing' if x.get('write') else 'reading'}). Tell the user that "
                     f"Link folder on the page lets the members in. {ADD_DIR_HINT}\n  {add_dir(x['folder'])}")
        return line
    if kind == "question":
        return (f"question {x['id']} from {x['member']}" + (f" on {x['task']}" if x["task"] else "") + f" (the member is waiting): {x['question']}"
                + (f"\n  options: {' | '.join(x['options'])}" if x.get("options") else "") + f"\n  answer: {CMD} answer {shlex.quote(x['id'])} \"...\"")
    if kind == "blocked":
        return f"blocked {x['task']}: {x['reason']}"
    if kind == "note":
        return f"note: {x}"
    if kind == "setup":
        what = {"key": "save the API key (it stays on this computer)", "confirm": "check the settings and press Done"}
        if not x["missing"]:
            return "setup: done on the page (the key is saved on this computer and the settings are confirmed): start the members"
        return f"setup: waiting for the user on the page {x.get('page_url') or ''}: " + "; ".join(what[k] for k in x["missing"])
    if kind == "crew":
        at = time.strftime("%H:%M", time.localtime(x["ts"]))
        if x["what"] == "paused":
            return f"crew: the user rested the crew on the page (Rest crew) at {at}; it rests until the user asks for more ({CMD} resume)"
        return (f"crew: the user raised the budget to {money(x['budget'])} on the page at {at}; the crew works again" if x.get("budget")
                else f"crew: the user resumed the crew on the page at {at}")
    if kind == "link":
        return (f"link: the user linked {x['path']} for {'reading and writing' if x.get('write') else 'reading'}; members reach it now. "
                f"{ADD_DIR_HINT}\n  {add_dir(x['path'])}")
    if kind == "ext":
        return x
    if kind == "provider":
        return (f"provider: {x['from']} {failed_words(x.get('kind'))} (API error {x.get('status')} for {x.get('member')}: {x.get('said', '')[:200]}), "
                f"so the team switched to {x['to']}; members go on there with the same sessions and files"
                + (f" ({'; '.join(x['skipped'])})" if x.get("skipped") else "")
                + f". Tell the user; once {x['from']} works again, {CMD} config set provider {x['from']} moves the team back")
    if kind == "prices":
        return (f"prices: {share_words(x['next'])} from {x['at']} (in {span(x['in'])}), {share_words(x['share'])} until then"
                + (f"; {share_words(x['share'])} again from {x['back']}" if x.get("back") else "")
                + f". Tell the user; to wait for the lower price, {CMD} rest now and {CMD} resume then")
    if kind == "budget":
        if x["what"] == "reached":
            return (f"budget: the team has spent {money(x.get('spent'))} of the {money(x.get('usd'))} the user set; each member stops after its "
                    "current turn and the crew rests. Only the user can raise the budget, on the page: tell them")
        if x["what"] == "near":
            share = f" ({x['spent'] / x['usd']:.0%})" if x.get("spent") is not None and x.get("usd") else ""
            return (f"budget: the team has spent {money(x.get('spent'))} of the {money(x.get('usd'))} the user set{share}; at the budget each "
                    "member stops after its turn. Tell the user now, in case they want to raise it on the page")
        if not x.get("usd"):
            return "budget: the user removed the team's budget"
        return f"budget: the user set the team's budget to {money(x['usd'])}" + (f", {money(x['spent'])} spent so far" if x.get("spent") is not None else "")
    if kind == "answer":
        what = x.get("answer") if x["id"].startswith("Q") else f"{x['tool']} {json.dumps(x.get('input'), ensure_ascii=False)[:120]}"
        return (f"the user answered {x['id']} from {x['member']} on the page: {x['state']}"
                + (f" ({providers.shown(widened_words(x.get('widened') or {}))})" if x.get("always") else "") + f": {what}")
    return (f"finished: {x['done']} of {x['tasks']} tasks done, nobody holds one; read the summaries, run the checks, {CMD} audit. "
            f"Keep the team: the crew rests by itself, and the next request needs no new {CMD} up")
