from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import providers
from .board import state_home, write_json

SAMPLE_S = 5.0
BUSY = {"cpu": 0.9, "mem": 0.20, "gpu": 0.90}
FULL = {"cpu": 1.5, "mem": 0.10, "gpu": 0.98}
EASE = 0.1
GPU_WEIGHT = 0.5
FILE = "load.json"


def guard_on() -> bool:
    return (providers.setting("DEEPCREW_LOAD_GUARD") or "on").lower() not in ("off", "0", "false", "no")


def _cpu() -> float | None:
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    except (OSError, AttributeError):
        return None


def _mem() -> float | None:
    try:
        if sys.platform == "darwin":
            libc = ctypes.CDLL(ctypes.util.find_library("c"))
            value, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
            if libc.sysctlbyname(b"kern.memorystatus_level", ctypes.byref(value), ctypes.byref(size), None, ctypes.c_size_t(0)) == 0:
                return max(0.0, min(1.0, value.value / 100))
            return None
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii") as f:
                info = dict(re.findall(r"^(\w+):\s+(\d+)", f.read(), re.M))
            return int(info["MemAvailable"]) / int(info["MemTotal"])
        if sys.platform == "win32":
            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("page_total", ctypes.c_ulonglong), ("page_avail", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                            ("virtual_avail", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]
            st = Status(length=ctypes.sizeof(Status))
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.avail / st.total
    except (OSError, KeyError, ValueError, ZeroDivisionError, AttributeError):
        pass
    return None


def _gpu() -> float | None:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["/usr/sbin/ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], capture_output=True, encoding="utf-8",
                                 errors="replace", timeout=2).stdout
            found = [int(x) for x in re.findall(r'"Device Utilization %"=(\d+)', out)]
        elif (smi := shutil.which("nvidia-smi")):
            out = subprocess.run([smi, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True, encoding="utf-8",
                                 errors="replace", timeout=2).stdout
            found = [int(x) for x in re.findall(r"\d+", out)]
        else:
            return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return max(found) / 100 if found else None


def level(look: dict, before: str = "ok") -> str:
    def over(limits: dict, margin: float) -> bool:
        cpu, mem, gpu = look.get("cpu"), look.get("mem"), look.get("gpu")
        return ((cpu is not None and cpu >= limits["cpu"] * (1 - margin)) or (mem is not None and mem <= limits["mem"] * (1 + margin))
                or (gpu is not None and gpu >= limits["gpu"] * (1 - margin)))
    if over(FULL, EASE if before == "full" else 0.0):
        return "full"
    return "busy" if over(BUSY, EASE if before in ("busy", "full") else 0.0) else "ok"


def sample(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    path = state_home() / FILE
    last = None
    with contextlib.suppress(OSError, ValueError):
        last = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(last, dict) and 0 <= now - float(last.get("ts") or 0) < SAMPLE_S and last.get("level") in ("ok", "busy", "full"):
        return last
    last = last if isinstance(last, dict) else {}
    gpu = _gpu()
    if gpu is not None and isinstance(last.get("gpu"), (int, float)):
        gpu = GPU_WEIGHT * gpu + (1 - GPU_WEIGHT) * last["gpu"]
    look = {"cpu": _cpu(), "mem": _mem(), "gpu": gpu, "ts": now}
    look = {k: round(v, 3) if isinstance(v, float) and k != "ts" else v for k, v in look.items()}
    look["level"] = level(look, last.get("level") if last.get("level") in ("ok", "busy", "full") else "ok")
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, look)
    return look


def limit(look: dict | None, max_active: int, running: int) -> int:
    if look is None or look.get("level") == "ok":
        return max_active
    return min(max_active, max(1, running) if look["level"] == "busy" else 1)
