from __future__ import annotations

import os
import sys
import time

BUSY_TRIES, BUSY_PAUSE_S = 8, 0.02


class ProviderError(Exception):
    pass


def is_windows() -> bool:
    return sys.platform == "win32"


def retry_busy(op):
    for attempt in range(BUSY_TRIES):
        try:
            return op()
        except PermissionError:
            if not is_windows() or attempt == BUSY_TRIES - 1:
                raise
            time.sleep(BUSY_PAUSE_S * (attempt + 1))


def replace_file(src, dst) -> None:
    retry_busy(lambda: os.replace(src, dst))


def split_list(raw: str | None) -> list[str]:
    return [x for x in (item.strip() for item in (raw or "").split(",")) if x]


def write_private(path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
