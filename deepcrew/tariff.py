from __future__ import annotations

import re
import time

from .host import ProviderError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WINDOW = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-(?:([01]\d|2[0-3]):([0-5]\d)|(24):(00))")


def peak_windows(tariff: dict) -> list[tuple[int, int]]:
    out = []
    for w in tariff.get("peak_utc") or []:
        m = WINDOW.fullmatch(str(w).replace(" ", ""))
        a, b = (int(m[1]) * 60 + int(m[2]), int(m[3] or m[5]) * 60 + int(m[4] or m[6])) if m else (0, 0)
        if a >= b:
            raise ProviderError(f"tariff: peak hours are HH:MM-HH:MM within one UTC day, not {w!r}")
        out.append((a, b))
    return out


def check_tariff(tariff) -> None:
    if not isinstance(tariff, dict):
        raise ProviderError("tariff must be a table")
    peak_windows(tariff)
    days = tariff.get("peak_days", DAYS)
    if not isinstance(days, list | tuple) or not all(isinstance(d, str) and d in DAYS for d in days):
        raise ProviderError(f"tariff: peak_days are among {', '.join(DAYS)}")
    share = tariff.get("off_peak", 1)
    if isinstance(share, bool) or not isinstance(share, int | float) or not 0 <= share <= 1:
        raise ProviderError("tariff: off_peak is the share of the list prices paid off-peak, from 0 to 1")
    if not all(isinstance(d, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", d) for d in tariff.get("holidays") or []):
        raise ProviderError("tariff: holidays are dates written YYYY-MM-DD")


def price_share(p: dict, ts: float) -> float:
    tariff = p.get("tariff")
    if not tariff:
        return 1.0
    off = float(tariff.get("off_peak", 1))
    day = time.gmtime(ts)
    if time.strftime("%Y-%m-%d", day) in (tariff.get("holidays") or []) or DAYS[day.tm_wday] not in tariff.get("peak_days", DAYS):
        return off
    minute = day.tm_hour * 60 + day.tm_min
    return 1.0 if any(a <= minute < b for a, b in peak_windows(tariff)) else off


def tariff_now(p: dict, now: float | None = None) -> dict | None:
    if not p.get("tariff"):
        return None
    now = time.time() if now is None else now
    share = price_share(p, now)
    edges = sorted({0, *(x for w in peak_windows(p["tariff"]) for x in w)})
    midnight = now - now % 86400
    for day in range(8):
        for minute in edges:
            at = midnight + day * 86400 + minute * 60
            if at > now and (after := price_share(p, at)) != share:
                return {"share": share, "peak": share >= 1, "until": at, "next": after}
    return None
