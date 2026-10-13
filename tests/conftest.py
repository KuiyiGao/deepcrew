from __future__ import annotations

import inspect
import itertools
import os
import shutil
import subprocess
import sys
import urllib.request
from types import SimpleNamespace

import pytest

LEADER_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_EXECPATH", "FAKE_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY")


def leader_env() -> list[str]:
    return [v for v in os.environ if v.startswith("DEEPCREW_") or v in LEADER_ENV]


for var in set(leader_env()) - {"DEEPCREW_CLAUDE"}:
    del os.environ[var]

from deepcrew import board as board_module
from deepcrew import cli, host, inbox, member, providers

REAL_ALIVE = member.alive
REAL_PAGE = SimpleNamespace(start=cli.start_page, url=cli.page_url)
PROVIDER = {"name": "fake", "base_url": "http://127.0.0.1:9", "models": ["m"], "efforts": [], "fast_model": "m", "auto_compact_window": 100000,
            "timeout_ms": 1000, "key_sources": ["env:FAKE_KEY"], "key_pattern": ".+"}
FAKE_TOML = ('[fake]\nbase_url = "http://127.0.0.1:9"\nmodels = ["m"]\nefforts = []\nfast_model = "m"\nauto_compact_window = 100000\n'
             'timeout_ms = 1000\nkey_sources = ["env:FAKE_KEY"]\nkey_pattern = ".+"\n')
PAGE_URL = "http://127.0.0.1:9/?t=stand-in"
FORBIDDEN = {"claude", "true", "pkill", "killall", "taskkill", "osascript", "security"}
STAND_IN_EXE = "fake-claude.exe"
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
POSIX_ONLY = {"0o600": "POSIX file modes", "0o700": "POSIX file modes", "S_IMODE": "POSIX file modes", "#!/bin/sh": "an sh script as the claude stand-in",
              "symlink_to": "symlinks, which Windows creates only with a privilege", ".chmod(": "POSIX file modes"}


def pytest_configure(config):
    config.addinivalue_line("markers", "posix(why): needs a POSIX host (file modes, a script run by its #! line, symlinks, a FIFO); skipped on Windows")
    config.addinivalue_line("markers", "sandbox: the test's settings file turns the sandbox on (it is off by default); the test may turn it off again")


def needs_posix(item) -> str | None:
    if marker := item.get_closest_marker("posix"):
        return marker.args[0] if marker.args else "a POSIX host"
    fn = getattr(item, "function", None)
    text = inspect.getsource(fn) if fn else ""
    return next((why for needle, why in POSIX_ONLY.items() if needle in text), None)


def pytest_collection_modifyitems(config, items):
    if sys.platform != "win32":
        return
    for item in items:
        if why := needs_posix(item):
            item.add_marker(pytest.mark.skip(reason=f"needs {why}"))


class _GuardedPopen(subprocess.Popen):
    def __init__(self, args, *a, **kw):
        words = [str(x) for x in args] if isinstance(args, list | tuple) else str(args).split()
        if os.path.basename(words[0]) in FORBIDDEN or {"deepcrew.member", "drive"} <= set(words) or {"deepcrew", "--serve"} <= set(words):
            pytest.fail(f"a unit test tried to run {words[:4]}: tests never start claude, a member, a detached settings server, "
                        "taskkill, osascript or the Keychain", pytrace=False)
        if os.path.basename(words[0]) == STAND_IN_EXE:
            args = [os.path.splitext(words[0])[0] + ".cmd", *words[1:]]
        super().__init__(args, *a, **kw)


def _no_network(*a, **kw):
    pytest.fail("a unit test tried to use the network", pytrace=False)


@pytest.fixture(autouse=True)
def _isolated(request, tmp_path, monkeypatch):
    for var in leader_env():
        monkeypatch.delenv(var)
    for var, value in {"DEEPCREW_HOME": tmp_path / ".deepcrew", "GIT_CEILING_DIRECTORIES": tmp_path, **GIT_ENV}.items():
        monkeypatch.setenv(var, str(value))
    monkeypatch.setattr(providers, "CONFIG_DIR", tmp_path / ".config" / "deepcrew")
    monkeypatch.setattr(providers, "ENV_FILE", tmp_path / ".config" / "deepcrew" / ".env")
    monkeypatch.setattr(host, "is_windows", lambda: providers.is_windows())
    if request.node.get_closest_marker("sandbox"):
        providers.env_file_set({"DEEPCREW_SANDBOX": "on"})
    if request.node.get_closest_marker("e2e"):
        monkeypatch.setenv("DEEPCREW_CONFIG_DIR", str(providers.CONFIG_DIR))
        monkeypatch.setenv("DEEPCREW_TOOLS", str(tmp_path / ".cache" / "tools"))
        yield
        return
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if sys.platform == "win32":
        stub = bin_dir / STAND_IN_EXE
        (bin_dir / "fake-claude.cmd").write_text(f'@echo off\r\necho %1>>"{tmp_path / "claude.calls"}"\r\nif "%1"=="--version" echo 2.1.200 (stand-in)\r\n'
                                                 f'if "%1"=="--help" echo Usage: claude {" ".join(cli.REQUIRED_FLAGS)}\r\n')
    else:
        stub = bin_dir / "fake-claude"
        stub.write_text(f"#!/bin/sh\necho \"$1\" >> {tmp_path / 'claude.calls'}\ncase \"$1\" in\n  --version) echo '2.1.200 (stand-in)';;\n"
                        f"  --help) echo 'Usage: claude {' '.join(cli.REQUIRED_FLAGS)}';;\nesac\n")
        stub.chmod(0o755)
    tools = cli.SANDBOX_TOOLS.get(sys.platform) or (sys.executable,)
    monkeypatch.setitem(cli.SANDBOX_TOOLS, sys.platform, tools)
    for tool in [t for t in tools if not os.path.isabs(t)]:
        (bin_dir / tool).write_text("#!/bin/sh\n")
        (bin_dir / tool).chmod(0o755)
    drive, rest = os.path.splitdrive(str(tmp_path))
    env = {"HOME": tmp_path, "USERPROFILE": tmp_path, "HOMEDRIVE": drive, "HOMEPATH": rest,
           "PATH": os.pathsep.join(filter(None, [str(bin_dir), os.environ.get("PATH")])), "DEEPCREW_CLAUDE": stub,
           "DEEPCREW_BROWSER": "off"}
    for var, value in env.items():
        monkeypatch.setenv(var, str(value))
    monkeypatch.setattr(providers, "_keychain_get", lambda service: None)
    monkeypatch.setattr(providers, "can_prompt", lambda: False)
    monkeypatch.setattr(member, "alive", lambda m: bool(m and m.get("pid")))
    monkeypatch.setattr(cli, "start_page", lambda project, team, team_dir: PAGE_URL)
    monkeypatch.setattr(cli, "page_url", lambda team_dir: None)
    monkeypatch.setattr(inbox, "page_url", lambda team_dir: cli.page_url(team_dir))
    monkeypatch.setattr(cli, "ui_label", lambda project=None: "settings")
    monkeypatch.setattr(member, "_TALLY", {})
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
    yield


@pytest.fixture(scope="session")
def _template(tmp_path_factory):
    root = tmp_path_factory.mktemp("template") / "proj"
    for rel, text in {"README.md": "# demo\n", "src/a.py": "A = 1\n", "src/pkg/b.py": "B = 1\n", "docs/guide.md": "guide\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false",
           "-c", "maintenance.auto=false", "-c", "gc.auto=0"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run([*git, *args], check=True, capture_output=True, env={**os.environ, **GIT_ENV})
    return root


@pytest.fixture
def project(tmp_path, _template):
    shutil.copytree(_template, tmp_path / "proj", symlinks=True)
    return tmp_path / "proj"


@pytest.fixture
def provider_table():
    return dict(PROVIDER)


@pytest.fixture
def make_board(project, tmp_path):
    def make(name="t", *, provider=None, default_location=False, **options):
        options = {"question_timeout_s": 0.3, "approval_timeout_s": 0.3, **options}
        where = {} if default_location else {"team_dir": tmp_path / "team"}
        return cli.create_team(project, name, provider or PROVIDER, {"model": "m", "effort": None}, claude="/usr/bin/true", **where, **options)
    return make


@pytest.fixture
def board(make_board):
    return make_board()


@pytest.fixture
def add_member():
    def add(board, name, region, state="idle", **fields):
        with board.locked():
            board.new_member(name, region, f"session-{name}")
        fields.setdefault("pid", 4242 if state in ("starting", "active") else None)
        return board.update_member(name, state=state, **fields)
    return add


@pytest.fixture
def post_task():
    return lambda board, title="t", scope=(), deps=(), body="": board.post(title, body, list(scope), list(deps), "leader")


@pytest.fixture
def task_edit():
    def edit(board, tid, *, status=None, **metadata):
        raw = board_module.read_json(board.tasks_dir / f"{tid}.json")
        raw.update({"status": status} if status else {})
        raw["metadata"] = {**raw.get("metadata", {}), **metadata}
        board_module.write_json(board.tasks_dir / f"{tid}.json", raw)
    return edit


@pytest.fixture
def finish(task_edit):
    def done(board, name, tid, summary="done"):
        task_edit(board, tid, status="completed", summary=summary)
        board.sync_member(name)
    return done


@pytest.fixture
def crew(make_board, add_member):
    board = make_board(bash_allow=["du -sh", "wc -l"])
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    return board


@pytest.fixture
def lead(make_board, add_member):
    board = make_board("main", default_location=True)
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    return board


@pytest.fixture
def real_alive():
    return REAL_ALIVE


@pytest.fixture
def real_page():
    return REAL_PAGE


@pytest.fixture
def procs(monkeypatch):
    rec = SimpleNamespace(calls=[], fail=None)
    monkeypatch.setenv("FAKE_KEY", "sk-" + "b" * 32)

    def launch(board, m, prompt, *, resume):
        if rec.fail:
            raise rec.fail
        rec.calls.append({"member": dict(m), "prompt": prompt, "resume": resume})
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(member, "_launch", launch)
    return rec


@pytest.fixture
def deepcrew(capsys, project):
    def run(*argv):
        code = cli.main([*map(str, argv), "--project", str(project)])
        out, err = capsys.readouterr()
        return code, out, err
    return run


@pytest.fixture
def providers_toml():
    def write(text=FAKE_TOML):
        providers.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (providers.CONFIG_DIR / "providers.toml").write_text(text, encoding="utf-8")
    return write


@pytest.fixture
def clock(monkeypatch):
    ticks = itertools.count()
    c = SimpleNamespace(now=0.0, on_sleep=None, time=lambda: 1_700_000_000 + next(ticks) * 0.001, monotonic=lambda: c.now)

    def sleep(seconds):
        c.now += seconds
        if c.on_sleep:
            c.on_sleep()

    c.sleep = sleep
    monkeypatch.setattr(board_module, "time", c)
    return c
