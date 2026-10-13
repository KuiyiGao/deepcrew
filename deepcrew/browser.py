from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import policy, providers

SIZE = (960, 540)
MAX_SIZE = (1920, 1080)
MIN_SIZE = (200, 150)
WAIT_MS = 3000
MAX_WAIT_MS = 30000
TIMEOUT_S = 90.0
POLL_S = 0.25
MAX_LINES = 60
NOISE = re.compile(r"GL Driver Message|GPU stall due to ReadPixels|Automatic fallback to software WebGL")
CONSOLE = re.compile(r":CONSOLE(?::\d+)?\] \"(.*)\", source: (\S*) \((\d+)\)$")
CANDIDATES = {
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
               "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    "linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"],
    "win32": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
              r"%LocalAppData%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"],
}


def find() -> str | None:
    chosen = (providers.setting("DEEPCREW_BROWSER") or "").strip()
    if chosen.lower() == "off":
        return None
    for c in [chosen] if chosen else CANDIDATES.get(sys.platform, CANDIDATES["linux"]):
        path = os.path.expandvars(c)
        found = path if os.path.isfile(path) and os.access(path, os.X_OK) else shutil.which(path)
        if found:
            return found
    return None


TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json", ".wasm": "application/wasm",
         ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".ktx2": "image/ktx2"}


def _no_dir_fds() -> bool:
    return not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd


def _dir_fd(root: str, names: list[str], create: bool = False) -> int:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in names:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o755, dir_fd=fd)
            nxt = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_inside(root: str, parts: list[str]) -> int | None:
    if _no_dir_fds():
        real = os.path.realpath(os.path.join(root, *parts))
        try:
            return os.open(real, os.O_RDONLY | getattr(os, "O_BINARY", 0)) if policy.inside(real, root) else None
        except OSError:
            return None
    try:
        d = _dir_fd(root, parts[:-1])
    except OSError:
        return None
    try:
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
    except OSError:
        return None
    finally:
        os.close(d)


def write_inside(root: str, parts: list[str], data: bytes) -> None:
    tmp = f".{parts[-1]}.{os.getpid()}.{threading.get_ident()}.tmp"
    if _no_dir_fds():
        folder = os.path.realpath(os.path.join(root, *parts[:-1]))
        if not policy.inside(folder, root):
            raise OSError(f"{'/'.join(parts)} leaves {root}")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, tmp), "xb") as f:
            f.write(data)
        os.replace(os.path.join(folder, tmp), os.path.join(folder, parts[-1]))
        return
    d = _dir_fd(root, parts[:-1], create=True)
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=d), "wb") as f:
            f.write(data)
        os.replace(tmp, parts[-1], src_dir_fd=d, dst_dir_fd=d)
    finally:
        os.close(d)


class _Files(SimpleHTTPRequestHandler):
    def __init__(self, *a, root: str, closed: tuple[str, ...], missing: list[str], **kw):
        self.real_root, self.closed, self.missing = root, closed, missing
        super().__init__(*a, directory=root, **kw)

    def send_head(self):
        rel = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        parts = [x for x in rel.split("/") if x not in ("", ".")] + (["index.html"] if rel.endswith("/") else [])
        full = os.path.join(self.real_root, *parts)
        fd = None if not parts or any(x == ".." or "\\" in x or "\0" in x for x in parts) or \
            any(policy.inside(_fold(full), _fold(c)) for c in self.closed) else _open_inside(self.real_root, parts)
        if fd is None:
            self.send_error(404)
            return None
        f = os.fdopen(fd, "rb")
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            f.close()
            self.send_error(404)
            return None
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(os.path.splitext(full)[1].lower()) or self.guess_type(full))
        self.send_header("Content-Length", str(st.st_size))
        self.end_headers()
        return f

    def send_error(self, code, message=None, explain=None):
        if code >= 400 and len(self.missing) < MAX_LINES:
            self.missing.append(f"{urllib.parse.unquote(self.path)} ({code})")
        super().send_error(code, message, explain)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass


def shoot(root: Path, path: str = "index.html", query: str = "", size: tuple[int, int] = SIZE, wait_ms: int = WAIT_MS,
          closed: tuple[str, ...] = (), browser: str | None = None) -> dict:
    exe = browser or find()
    if exe is None:
        raise ValueError("no Chromium-based browser found (Chrome, Chromium, Edge, Brave): the user can name one with "
                         "`deepcrew config set browser PATH`")
    real_root = os.path.realpath(root)
    page = os.path.realpath(os.path.join(real_root, path))
    if not policy.inside(page, real_root) or not os.path.isfile(page):
        raise ValueError(f"{path!r} is not a file of the project")
    if query and not query.startswith(("?", "#")):
        raise ValueError("query starts with ? or #, as in ?t=2.5")
    missing: list[str] = []
    handler = functools.partial(_Files, root=real_root, closed=tuple(os.path.realpath(c) for c in closed), missing=missing)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/{urllib.parse.quote(Path(page).relative_to(real_root).as_posix())}{query}"
        with tempfile.TemporaryDirectory(prefix="deepcrew-shot-", ignore_cleanup_errors=True) as tmp:
            png = Path(tmp) / "shot.png"
            prefs = Path(tmp) / "profile" / "Default" / "Preferences"
            prefs.parent.mkdir(parents=True)
            prefs.write_text(json.dumps({"download": {"default_directory": tmp, "prompt_for_download": False}}), encoding="utf-8")
            args = [exe, "--headless=new", f"--user-data-dir={tmp}/profile", "--no-first-run", "--no-default-browser-check",
                    "--disable-extensions", "--disable-background-networking", "--disable-sync", "--disable-component-update",
                    "--mute-audio", "--hide-scrollbars", f"--window-size={size[0]},{size[1]}", f"--virtual-time-budget={wait_ms}",
                    f"--screenshot={png}", "--proxy-server=http://127.0.0.1:9", f"--proxy-bypass-list=<-loopback>;127.0.0.1:{port}",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--enable-logging=stderr", "--log-level=0", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", url]
            env = {**os.environ, "MAC_CHROMIUM_TMPDIR": tmp, "TMPDIR": tmp}
            try:
                proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, start_new_session=sys.platform != "win32",
                                        **providers.SUBPROCESS_TEXT)
            except OSError as e:
                raise ValueError(f"the browser {exe} did not start: {e}") from None
            lines: list[str] = []
            reader = threading.Thread(target=lambda: lines.extend(proc.stderr), daemon=True)
            reader.start()
            deadline, size = time.monotonic() + TIMEOUT_S, -1
            while proc.poll() is None and time.monotonic() < deadline:
                now = png.stat().st_size if png.exists() else -1
                if now > 0 and now == size:
                    break
                size = now
                time.sleep(POLL_S)
            if proc.returncode is None:
                _end(proc)
            proc.wait()
            reader.join(timeout=5)
            err = "".join(lines)
            if not png.exists():
                raise ValueError("the browser took no picture: " + (err.strip().splitlines() or ["no output"])[-1][:300])
            console = [f"{m[1]}  ({_short(m[2], port)}:{m[3]})" for m in map(CONSOLE.search, err.splitlines()) if m and not NOISE.search(m[1])]
            return {"png": png.read_bytes(), "console": console[:MAX_LINES], "errors": [c for c in console if c.startswith("Uncaught")][:MAX_LINES],
                    "missing": missing, "dropped": max(0, len(console) - MAX_LINES), "url": url.split(f":{port}", 1)[1]}
    finally:
        server.shutdown()
        server.server_close()


def _fold(path: str) -> str:
    return path.casefold() if sys.platform in ("darwin", "win32") else path


def _short(source: str, port: int) -> str:
    return source.split(f"127.0.0.1:{port}", 1)[-1] or "/"


def _end(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        if sys.platform != "win32":
            os.killpg(proc.pid, 9)
        else:
            proc.kill()
