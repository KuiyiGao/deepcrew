from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from . import policy, presets, providers
from .providers import replace_file, retry_busy

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

SCHEMA = "deepcrew.team.v1"
MEMBER_NAME = re.compile(r"[a-z][a-z0-9-]{1,23}")
RESERVED = {"leader", "all"}
REQUEST_ID = re.compile(r"[PQ][0-9a-f]{8}")
MAIL_KINDS = ("message", "answer", "notice")
MARKS = ("pass", "release", "skip")
NOTE_MAX_CHARS = 20000
REGION_CHANGES_KEPT = 50
NOTICE_MAX_CHARS = 20000
SUBDIRS = ("members", "mail", "questions", "perms", "logs", "run", "claude")
LOCK_STALE_S, LOCK_WAIT_S = 10.0, 5.0
LOCK_RETRY_S = 0.02
LOCK_POLL_S = 0.01
REQUEST_POLL_S = 0.25


def state_home() -> Path:
    return Path(os.environ.get("DEEPCREW_HOME") or Path.home() / f".{providers.NAME}").expanduser()


def team_path(project: Path, team: str) -> Path:
    return state_home() / f"{project.name}-{hashlib.sha256(policy.fold(str(project)).encode()).hexdigest()[:8]}" / team


def tools_dir() -> Path:
    return Path(os.environ.get("DEEPCREW_TOOLS") or Path.home() / ".cache" / providers.NAME / "tools").expanduser()


def default_shared_dir(project: Path, team: str) -> Path:
    return project.parent / f"{project.name}-{providers.NAME}" / team


def task_list_id(team_dir: Path) -> str:
    return "deepcrew-" + hashlib.sha256(str(team_dir).encode()).hexdigest()[:12]


def write_json(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    replace_file(tmp, path)


def read_text(path: Path) -> str:
    return retry_busy(lambda: path.read_text(encoding="utf-8"))


def read_json(path: Path):
    return json.loads(read_text(path))


def _load(files) -> list[dict]:
    out = []
    for p in files:
        try:
            out.append(read_json(p))
        except ValueError as e:
            (p.parent / "bad").mkdir(exist_ok=True)
            replace_file(p, p.parent / "bad" / p.name)
            with open(p.parent.parent / "errors.jsonl", "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"ts": time.time(), "file": str(p), "moved_to": str(p.parent / "bad" / p.name), "error": str(e)}) + "\n")
        except FileNotFoundError:
            continue
    return out


def read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return [r for r in out if isinstance(r, dict) and "ts" in r]


def _strings(name: str, value) -> list[str]:
    if isinstance(value, str) or not isinstance(value or [], list) or not all(isinstance(v, str) for v in value or []):
        raise ValueError(f"{name} must be a list of strings")
    return list(value or [])


def _text(name: str, value, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be 1-{limit} characters")
    return value


def lock_backend() -> str:
    return "msvcrt" if providers.is_windows() else "fcntl"


def lock_file(f, wait: bool = True) -> bool:
    if lock_backend() == "msvcrt":
        while True:
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if not wait:
                    return False
                time.sleep(LOCK_RETRY_S)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return True
    except BlockingIOError:
        return False


def unlock_file(f) -> None:
    if lock_backend() == "msvcrt":
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path: Path):
    lock, deadline = Path(f"{path}.lock"), time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            os.mkdir(lock)
            break
        except (FileExistsError, PermissionError) as e:
            if isinstance(e, PermissionError) and not providers.is_windows():
                raise
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                    os.rmdir(lock)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"{lock} is held") from None
            time.sleep(LOCK_POLL_S)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(lock)


def view(raw: dict) -> dict:
    md = raw.get("metadata") or {}
    owner = raw.get("owner") or None
    done = "cancelled" if md.get("outcome") == "cancelled" else "done"
    state = done if raw.get("status") == "completed" else "claimed" if owner else "open"
    try:
        scope = policy.norm_all(_strings("scope", md.get("scope") or []))
    except ValueError:
        scope = None

    def marked(kind: str) -> list[str]:
        return sorted(k.split(":", 1)[1] for k in md if k.startswith(kind + ":") and len(k) > len(kind) + 1)

    return {"id": raw["id"], "title": raw.get("subject", ""), "body": raw.get("description", ""), "scope": scope,
            "deps": list(raw.get("blockedBy") or []), "state": state, "owner": owner, "summary": md.get("summary"),
            "posted_by": md.get("by"), "passed_by": marked("pass"), "released_by": marked("release"), "ignored_by": marked("skip"),
            "strength": md.get("strength") if md.get("strength") in presets.STRENGTHS else None,
            "reasons": {k: v for k, v in md.items() if ":" in k and k.split(":", 1)[0] in MARKS and k.split(":", 1)[1]}}


def _own(t: dict) -> dict:
    return {k: list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v for k, v in t.items()}


def region_at(m: dict, when: float) -> list[str]:
    for change in m.get("region_changes") or []:
        if change.get("ts", 0) > when:
            return change.get("was", [])
    return m["region"]


def region_notice(by: str, was: list[str], region: list[str]) -> str:
    gained, lost = [p for p in region if not policy.covers(was, p)], [p for p in was if not policy.covers(region, p)]
    sentences = [f"Your region was changed by the {by}. It is now: {', '.join(region) or 'no files'}.",
                 f"You may now also change: {', '.join(gained)}." if gained else "",
                 f"You no longer own: {', '.join(lost)}." if lost else "",
                 "Change files only inside your region." if region else "You can read everything and change no files."]
    return " ".join(s for s in sentences if s)[:NOTICE_MAX_CHARS]


class Board:
    def __init__(self, team_dir: Path):
        self.dir = Path(team_dir)
        self.cfg = read_json(self.dir / "team.json")
        self.root = Path(self.cfg["project"])
        self.tasks_dir = self.dir / "claude" / "tasks" / self.cfg["task_list"]
        self.shared_dir = Path(self.cfg.get("shared_dir") or default_shared_dir(self.root, self.cfg["team"]))
        self.notes_dir, self.artifacts_dir = self.shared_dir / "notes", self.shared_dir / "artifacts"
        self._views: dict[str, tuple[tuple[int, int], dict]] = {}

    @classmethod
    def create(cls, team_dir: Path, cfg: dict) -> Board:
        for sub in SUBDIRS:
            (team_dir / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(team_dir, 0o700)
        write_json(team_dir / "team.json", {"schema": SCHEMA, "task_list": task_list_id(team_dir), **cfg})
        if not (team_dir / "version").exists():
            (team_dir / "version").write_text("0", encoding="utf-8")
        board = cls(team_dir)
        board.tasks_dir.mkdir(parents=True, exist_ok=True)
        (board.tasks_dir / ".lock").touch()
        board.notes_dir.mkdir(parents=True, exist_ok=True)
        board.artifacts_dir.mkdir(parents=True, exist_ok=True)
        return board

    @classmethod
    def from_env(cls) -> Board:
        return cls(Path(os.environ["DEEPCREW_TEAM_DIR"]))

    @contextlib.contextmanager
    def locked(self):
        with open(self.dir / "lock", "ab") as f:
            lock_file(f)
            try:
                yield
            finally:
                unlock_file(f)

    def version(self) -> str:
        files = []
        with contextlib.suppress(FileNotFoundError), os.scandir(self.tasks_dir) as it:
            files = [(e.name, (st := e.stat()).st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size) for e in it if e.name.endswith(".json")]
        return f"{read_text(self.dir / 'version').strip()}:{hashlib.sha256(repr(sorted(files)).encode()).hexdigest()[:12]}"

    def bump(self) -> None:
        n = int(read_text(self.dir / "version") or 0)
        tmp = self.dir / f".version.{os.getpid()}.tmp"
        tmp.write_text(str(n + 1), encoding="utf-8")
        replace_file(tmp, self.dir / "version")

    def _raw(self, tid) -> dict | None:
        if not isinstance(tid, str) or not tid.isdigit():
            return None
        try:
            return read_json(self.tasks_dir / f"{tid}.json")
        except (FileNotFoundError, ValueError):
            return None

    def tasks(self) -> list[dict]:
        out = []
        for p in sorted(self.tasks_dir.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else -1):
            if p.stem.isdigit() and (t := self.task(p.stem)) is not None:
                out.append(t)
        return out

    def task(self, tid) -> dict | None:
        if not isinstance(tid, str) or not tid.isdigit():
            return None
        try:
            st = (self.tasks_dir / f"{tid}.json").stat()
        except OSError:
            return None
        stamp = (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
        if (hit := self._views.get(tid)) and hit[0] == stamp:
            return _own(hit[1])
        raw = self._raw(tid)
        if not raw:
            return None
        t = {**view(raw), "changed": st.st_mtime}
        self._views[tid] = (stamp, t)
        return _own(t)

    def _update(self, tid: str, change) -> dict | None:
        path = self.tasks_dir / f"{tid}.json"
        if not path.exists():
            return None
        with file_lock(path):
            raw = self._raw(tid)
            if raw is None:
                return None
            change(raw)
            write_json(path, raw)
        return view(raw)

    def post(self, title, body, scope, deps, by: str, strength: str | None = None, to: str | None = None) -> dict:
        if not isinstance(body, str) or len(body) > 20000:
            raise ValueError("body must be a string of at most 20000 characters")
        if strength is not None and strength not in presets.STRENGTHS:
            raise ValueError(f"strength is one of {', '.join(presets.STRENGTHS)}, not {strength!r}")
        title = _text("title", title, 200).strip()
        scope, deps = policy.norm_all(_strings("scope", scope)), sorted(set(_strings("deps", deps)), key=lambda d: (len(d), d))
        with self.locked(), file_lock(self.tasks_dir / ".lock"):
            missing = [d for d in deps if self._raw(d) is None]
            if missing:
                raise ValueError(f"unknown dependency: {', '.join(missing)}")
            if to is not None and (why := self._cannot_give(to, scope, deps, strength)):
                raise ValueError(f"cannot give the task to {to}: {why}; post it without --to")
            mark = self.tasks_dir / ".highwatermark"
            n = 1 + max([int(p.stem) for p in self.tasks_dir.glob("*.json") if p.stem.isdigit()] +
                        [int(read_text(mark).strip() or 0) if mark.exists() else 0])
            raw = {"id": str(n), "subject": title, "description": body, "status": "pending", "blocks": [], "blockedBy": deps,
                   "metadata": {"scope": scope, "by": by, **({"strength": strength} if strength else {})}}
            if to is not None:
                raw.update(owner=to, status="in_progress")
            write_json(self.tasks_dir / f"{n}.json", raw)
            if to is not None:
                m = self.member(to)
                m.update(task=str(n), assigned=str(n), updated=time.time())
                self.put_member(m)
            for d in deps:
                self._update(d, lambda r: r.__setitem__("blocks", sorted({*r.get("blocks", []), str(n)}, key=lambda x: (len(x), x))))
            self.bump()
        return view(raw)

    def _cannot_give(self, name: str, scope: list[str], deps: list[str], strength: str | None) -> str | None:
        m = self.member(name)
        if m is None or m["state"] == "failed":
            return f"{name!r} is not a working member of this team"
        held = self.task(m.get("task"))
        if held and held["state"] == "claimed" and held["owner"] == name:
            return f"it holds task {held['id']}, and a task with a scope inside its region is offered to it alone once task {held['id']} ends"
        if pending := [d for d in deps if (self.task(d) or {}).get("state") != "done"]:
            return f"the task would wait for {', '.join(pending)}"
        if not policy.within(scope, m["region"]):
            return f"the scope {scope} is outside its region {m['region']}"
        if not self.fits(m, {"strength": strength}):
            return f"it runs {self.model_of(m)}, and the task needs {self.needs({'strength': strength})}"
        return None

    def prerequisites(self, t: dict) -> list[dict]:
        deps = [self.task(d) for d in t["deps"]]
        return [{k: d[k] for k in ("id", "title", "state", "owner", "summary")} for d in deps if d]

    def eligible(self, member: dict) -> list[dict]:
        tasks = self.tasks()
        done = {t["id"] for t in tasks if t["state"] == "done"}
        name = member["name"]
        mine = [t for t in tasks if t["state"] == "open" and t["scope"] is not None and set(t["deps"]) <= done
                and name not in [*t["released_by"], *t["passed_by"], *t["ignored_by"]] and policy.within(t["scope"], member["region"])
                and self.fits(member, t)]
        return mine

    def why_not(self, name: str, tid: str) -> str | None:
        m, t = self.member(name), self.task(tid)
        if m is None:
            return f"{name!r} is not a member of this team"
        if m["state"] == "failed":
            return f"{name!r} is not a working member of this team (it failed; resume the crew once the cause is fixed)"
        if t is None:
            return f"there is no task {tid}"
        held = self.task(m.get("task"))
        if held and held["state"] == "claimed" and held["owner"] == name and held["id"] != tid:
            return f"{name} holds task {held['id']} and takes another only once that one is done or given back"
        if t["state"] == "claimed" and t["owner"] == name:
            return None
        if t["state"] != "open":
            return f"task {tid} is {t['state']}" + (f" by {t['owner']}" if t["owner"] and t["state"] != "cancelled" else "")
        holder = next((x["name"] for x in self.members() if x.get("task") == tid and x["name"] != name), None)
        if holder:
            return f"task {tid} is being taken by {holder}"
        pending = sorted(set(t["deps"]) - {d["id"] for d in self.tasks() if d["state"] == "done"})
        if pending:
            return f"task {tid} waits for {', '.join(pending)}"
        if t["scope"] is None:
            return f"task {tid} has no usable scope: its metadata.scope is not a list of project paths"
        if not policy.within(t["scope"], m["region"]):
            return f"task {tid} changes {t['scope']}, outside {name}'s region {m['region']}"
        if not self.fits(m, t):
            return f"task {tid} needs a member on {self.needs(t)} (strength {t['strength']}); {name} runs {self.model_of(m)}"
        return None

    def model_of(self, m: dict | None) -> str:
        return (m or {}).get("model") or self.cfg["member"]["model"]

    def needs(self, t: dict) -> str | None:
        return presets.strength_values(t["strength"], self.cfg["provider"])["DEEPCREW_MODEL"] if t.get("strength") else None

    def fits(self, m: dict | None, t: dict) -> bool:
        need = self.needs(t)
        if need is None:
            return True
        models = self.cfg["provider"]["models"]
        return (models.index(self.model_of(m)) if self.model_of(m) in models else 0) >= models.index(need)

    def live_cfg(self) -> dict:
        return read_json(self.dir / "team.json")

    def folders(self) -> list[dict]:
        return [f for f in self.live_cfg().get("folders", []) if isinstance(f, dict) and isinstance(f.get("path"), str)]

    def sites(self) -> list[str]:
        return [x["domain"] for x in self.live_cfg().get("sites", []) if isinstance(x, dict) and isinstance(x.get("domain"), str)]

    def reachable(self, path: str, write: bool = False) -> bool:
        real = os.path.realpath(os.path.expanduser(path))
        places = [(str(tools_dir()), True), *((f["path"], bool(f.get("write"))) for f in self.folders())]
        return any(policy.inside(real, os.path.realpath(p)) and (w or not write) for p, w in places)

    def paused(self) -> dict | None:
        return self.live_cfg().get("paused") or None

    def budget(self) -> dict | None:
        b = self.live_cfg().get("budget")
        return b if isinstance(b, dict) and isinstance(b.get("usd"), int | float) and b["usd"] > 0 else None

    def update_cfg(self, change) -> dict:
        with self.locked():
            cfg = self.live_cfg()
            change(cfg)
            write_json(self.dir / "team.json", cfg)
            self.cfg = cfg
            self.bump()
        return cfg

    def claim(self, name: str, tid: str) -> tuple[dict | None, str]:
        with self.locked():
            return self._claim(name, tid)

    def _claim(self, name: str, tid: str) -> tuple[dict | None, str]:
        why = self.why_not(name, tid)
        if why:
            return None, why
        t = self._update(tid, lambda r: r.update(owner=name, status="in_progress"))
        if t is None:
            return None, f"task {tid} could not be read just now; try again"
        m = self.member(name)
        m.update(task=tid, updated=time.time())
        self.put_member(m)
        self.bump()
        return t, "claimed"

    def release(self, name: str, tid: str, reason: str = "", by_choice: bool = True) -> dict | None:
        with self.locked():
            return self._release(name, tid, reason, by_choice)

    def _release(self, name: str, tid: str, reason: str = "", by_choice: bool = True) -> dict | None:
        def change(r: dict) -> None:
            if r.get("owner") == name and r.get("status") != "completed":
                r.pop("owner", None)
                r["status"] = "pending"
                if by_choice:
                    r.setdefault("metadata", {})[f"release:{name}"] = reason
        t = self._update(tid, change)
        m = self.member(name)
        if m and m.get("task") == tid:
            m.update(task=None, updated=time.time())
            self.put_member(m)
        self.bump()
        return t

    def mark(self, name: str, ids: list[str], kind: str, reason: str) -> None:
        with self.locked():
            self._mark(name, ids, kind, reason)

    def _mark(self, name: str, ids: list[str], kind: str, reason: str) -> None:
        if kind not in MARKS:
            raise ValueError(kind)
        for tid in ids:
            self._update(tid, lambda r: r.setdefault("metadata", {}).setdefault(f"{kind}:{name}", reason)
                         if not r.get("owner") and r.get("status") == "pending" else None)
        self.bump()

    def _assign(self, name: str, tid: str) -> tuple[dict | None, str]:
        t, why = self._claim(name, tid)
        if t is not None:
            m = self.member(name)
            m["assigned"] = tid
            self.put_member(m)
        return t, why

    def sync_member(self, name: str) -> None:
        with self.locked():
            m = self.member(name)
            if m is None:
                return
            t = self.task(m.get("task"))
            if m.get("task") and not (t and t["state"] == "claimed" and t["owner"] == name):
                m.update(task=None, updated=time.time())
                self.put_member(m)
            self.bump()

    def cancel(self, tid: str, by: str, reason: str) -> tuple[dict | None, str]:
        with self.locked():
            t = self.task(tid)
            if t is None:
                return None, f"there is no task {tid}"
            if t["state"] in ("done", "cancelled"):
                return None, f"task {tid} is {t['state']}"

            def change(r: dict) -> None:
                r["status"] = "completed"
                r.setdefault("metadata", {}).update(outcome="cancelled", summary=f"cancelled by {by}: {reason}")
            t = self._update(tid, change)
            for m in self.members():
                if m.get("task") == tid:
                    m.update(task=None, updated=time.time())
                    self.put_member(m)
            self.bump()
            return t, "cancelled"

    def members(self) -> list[dict]:
        return _load(sorted((self.dir / "members").glob("*.json")))

    def member(self, name) -> dict | None:
        p = self.dir / "members" / f"{name}.json"
        return read_json(p) if isinstance(name, str) and MEMBER_NAME.fullmatch(name) and p.exists() else None

    def write_area(self, name: str) -> list[str]:
        m = self.member(name) or {}
        return list(m.get("region", []))

    def put_member(self, m: dict) -> None:
        write_json(self.dir / "members" / f"{m['name']}.json", m)

    def new_member(self, name: str, region, session: str, role: str | None = None) -> dict:
        if not isinstance(name, str) or not MEMBER_NAME.fullmatch(name) or name in RESERVED:
            raise ValueError(f"member names are 2-24 characters of a-z, 0-9 and '-', starting with a letter, not {' or '.join(sorted(RESERVED))}: "
                             f"{name!r}")
        region = policy.norm_all(_strings("region", region))
        if self.member(name) is not None:
            raise ValueError(f"member {name!r} exists")
        for other in self.members():
            if policy.overlap(region, other["region"]):
                raise ValueError(f"region {region} overlaps the region of {other['name']}: {other['region']}")
        now = time.time()
        m = {"name": name, "region": region, "role": role, "session": session, "state": "starting", "pid": None, "task": None,
             "offered": [], "failure": None, "runs": 0, "created": now, "updated": now}
        self.put_member(m)
        return m

    def set_region(self, name: str, region, by: str = "leader") -> dict:
        region = policy.norm_all(_strings("region", region))
        with self.locked():
            m = self.member(name)
            if m is None:
                raise ValueError(f"{name!r} is not a member of this team")
            if region == m["region"]:
                return m
            for other in (o for o in self.members() if o["name"] != name):
                if policy.overlap(region, other["region"]):
                    clash = [x for x in region if policy.overlap([x], other["region"])]
                    raise ValueError(f"{', '.join(clash)} overlaps the region of {other['name']}: {other['region']}. Take it off {other['name']} first")
            held = self.task(m.get("task"))
            if held and held["state"] == "claimed" and held["owner"] == name and held["scope"] is not None and not policy.within(held["scope"], region):
                out = [s for s in held["scope"] if not policy.covers(region, s)]
                raise ValueError(f"{name} holds task {held['id']}, which changes {out}: outside the new region {region}. "
                                 "Keep those paths in the region, or end the task first")
            was, now = m["region"], time.time()
            changes = [*(m.get("region_changes") or []), {"ts": now, "by": by, "was": was}]
            m.update(region=region, region_changes=changes[-REGION_CHANGES_KEPT:], updated=now)
            self.put_member(m)
            self.bump()
        self.send(by, name, "notice", region_notice(by, was, region))
        return m

    def used_names(self) -> list[str]:
        path = self.dir / "names.json"
        return read_json(path) if path.exists() else []

    def use_name(self, name: str) -> None:
        used = self.used_names()
        if name not in used:
            write_json(self.dir / "names.json", [*used, name])

    def update_member(self, name: str, **fields) -> dict | None:
        with self.locked():
            m = self.member(name)
            if m is None:
                return None
            m.update(fields, updated=time.time())
            self.put_member(m)
            self.bump()
            return m

    def reap(self, alive) -> list[str]:
        gone = []
        with self.locked():
            for m in self.members():
                if m["state"] not in ("starting", "active") or alive(m):
                    continue
                if m.get("task"):
                    self._release(m["name"], m["task"], by_choice=False)
                    m = self.member(m["name"])
                if m["runs"] == 0:
                    (self.dir / "members" / f"{m['name']}.json").unlink()
                else:
                    m.update(state="idle", pid=None, task=None, updated=time.time())
                    self.put_member(m)
                gone.append(m["name"])
            if gone:
                self.bump()
        return gone

    def forget(self, name: str) -> dict | None:
        with self.locked():
            m = self.member(name)
            if m is None:
                return None
            if m.get("task"):
                self._release(name, m["task"], by_choice=False)
            (self.dir / "members" / f"{name}.json").unlink()
            self.bump()
            return m

    def log_edit(self, name: str, tool: str, path: str, **extra) -> None:
        m = self.member(name) or {}
        row = {"ts": time.time(), "member": name, "task": m.get("task"), "tool": tool, "path": path}
        with open(self.dir / "edits.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({**row, **extra}, ensure_ascii=False) + "\n")

    def send(self, frm: str, to: str, kind, text, task=None) -> dict:
        if kind not in MAIL_KINDS:
            raise ValueError(f"kind must be one of {', '.join(MAIL_KINDS)}")
        msg = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "from": frm, "to": to, "kind": kind, "text": _text("text", text, 20000),
               "task": task}
        box = self.dir / "mail" / to
        box.mkdir(parents=True, exist_ok=True)
        tmp = box / f".{msg['id']}.tmp"
        tmp.write_text(json.dumps(msg, ensure_ascii=False), encoding="utf-8")
        replace_file(tmp, box / f"{int(msg['ts'] * 1e6):016d}-{msg['id']}.json")
        with self.locked():
            self.bump()
        return msg

    def mail_pending(self, who: str) -> list[Path]:
        return sorted((self.dir / "mail" / who).glob("*.json"))

    def peek_mail(self, who: str, only_from: str | None = None) -> list[tuple[Path, dict]]:
        out = []
        for p in self.mail_pending(who):
            try:
                msg = read_json(p)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                (p.parent / "bad").mkdir(exist_ok=True)
                replace_file(p, p.parent / "bad" / p.name)
                continue
            if only_from is None or msg.get("from") == only_from:
                out.append((p, msg))
        return out

    def ack_mail(self, items: list[tuple[Path, dict]]) -> list[dict]:
        out = []
        for p, msg in items:
            (p.parent / "read").mkdir(exist_ok=True)
            try:
                replace_file(p, p.parent / "read" / p.name)
            except FileNotFoundError:
                continue
            except PermissionError:
                if providers.is_windows():
                    continue
                raise
            out.append(msg)
        return out

    def take_mail(self, who: str, only_from: str | None = None) -> list[dict]:
        with self.locked():
            return self.ack_mail(self.peek_mail(who, only_from))

    def note_max_chars(self) -> int:
        return int(self.cfg.get("note_max_chars") or NOTE_MAX_CHARS)

    def in_shared(self, path: str) -> bool:
        return policy.inside(os.path.realpath(path), os.path.realpath(self.shared_dir))

    def own_file(self, name: str, path: str) -> bool:
        sep, real = policy.pathmod().sep, policy.fold(os.path.realpath(path))
        notes, mine = (policy.fold(os.path.realpath(d)) for d in (self.notes_dir, self.artifacts_dir / name))
        return bool(re.fullmatch(re.escape(notes + sep + name) + r"-[A-Za-z0-9._-]{1,80}\.md", real)) or real.startswith(mine + sep)

    def is_note(self, path: str) -> bool:
        return policy.fold(policy.pathmod().dirname(os.path.realpath(path))) == policy.fold(os.path.realpath(self.notes_dir))

    def note_write(self, by: str, topic: str, text: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", _text("topic", topic, 80).strip()).strip("-") or "note"
        slug = slug.removesuffix(".md").removeprefix(f"{by}-") or "note"
        path = self.notes_dir / f"{by}-{slug}.md"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(_text("text", text, self.note_max_chars()), encoding="utf-8")
        return path

    def request(self, kind: str, member: str, payload: dict) -> dict:
        r = {"id": {"questions": "Q", "perms": "P"}[kind] + uuid.uuid4().hex[:8], "ts": time.time(), "member": member,
             "state": "pending", "by": None, **payload}
        write_json(self.dir / kind / f"{r['id']}.json", r)
        return r

    def pending(self, kind: str) -> list[dict]:
        return [r for r in _load(sorted((self.dir / kind).glob("*.json"))) if r["state"] == "pending"]

    def answer(self, kind: str, rid: str, state: str, by: str = "leader", **fields) -> dict:
        with self.locked():
            path = self.dir / kind / f"{rid}.json"
            if not REQUEST_ID.fullmatch(rid or "") or not path.exists():
                raise ValueError(f"no request {rid!r}")
            r = read_json(path)
            if r["state"] != "pending":
                raise ValueError(f"{rid} is already {r['state']}")
            r.update(fields, state=state, by=by, answered=time.time())
            write_json(path, r)
            return r

    def wait(self, kind: str, rid: str, timeout: float) -> dict:
        path = self.dir / kind / f"{rid}.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = read_json(path)
            if r["state"] != "pending":
                return r
            time.sleep(REQUEST_POLL_S)
        with self.locked():
            r = read_json(path)
            if r["state"] == "pending":
                r.update(state="expired", answered=time.time())
                write_json(path, r)
            return r
