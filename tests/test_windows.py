from __future__ import annotations

import ast
import io
import json
import ntpath
import os
import posixpath
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import needs_posix
from deepcrew import board as board_module
from deepcrew import cli, host, member, policy, prompts, providers
from deepcrew.board import lock_backend, lock_file, read_json, team_path, unlock_file, write_json

REPO = Path(__file__).resolve().parents[1]
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="runs the POSIX branch for real")
WIN_PYTHON, WIN_PKG = "C:\\Program Files\\Python 312\\python.exe", "C:\\Users\\Jo Doe\\deepcrew"


class FakeMsvcrt:
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self, busy=0):
        self.busy, self.calls = busy, []

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes))
        if mode == self.LK_NBLCK and self.busy:
            self.busy -= 1
            raise OSError(13, "locked")


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(providers, "is_windows", lambda: True)
    monkeypatch.setattr(board_module, "msvcrt", FakeMsvcrt())


@POSIX
def test_posix_commands_are_unchanged(crew):
    crew.cfg.update(python="/opt/my python/bin/python3", pkg_parent="/srv/it's here")
    assert member._py(crew, "hook", "pre") == f"PYTHONPATH={shlex.quote(crew.cfg['pkg_parent'])} {shlex.quote(crew.cfg['python'])} -P -m deepcrew.member hook pre"
    assert shlex.split(member._py(crew, "key"))[-4:] == ["-P", "-m", "deepcrew.member", "key"]


def test_windows_commands_run_the_launcher_in_double_quotes_with_forward_slashes(crew, windows):
    crew.cfg.update(python=WIN_PYTHON, pkg_parent=WIN_PKG)
    cmd = member._py(crew, "hook", "pre")
    assert cmd == '"C:/Program Files/Python 312/python.exe" "C:/Users/Jo Doe/deepcrew/deepcrew/_run.py" hook pre' and "PYTHONPATH" not in cmd
    assert shlex.split(cmd) == ["C:/Program Files/Python 312/python.exe", "C:/Users/Jo Doe/deepcrew/deepcrew/_run.py", "hook", "pre"]
    assert member._py(crew, "key").endswith('_run.py" key') and "\\" not in member._py(crew, "key")


@pytest.mark.parametrize("bad", ['C:\\Users\\a"b', "C:\\Users\\$x", "C:\\Users\\`x", "C:\\Users\\100%"])
def test_windows_refuses_a_path_either_shell_would_expand(crew, windows, bad):
    crew.cfg.update(python=WIN_PYTHON, pkg_parent=bad)
    with pytest.raises(member.LaunchError, match="expand"):
        member._py(crew, "key")


def test_settings_use_the_windows_command_for_hooks_and_the_key_helper(crew, windows):
    crew.cfg.update(python=WIN_PYTHON, pkg_parent=WIN_PKG)
    s = member.settings(crew)
    commands = [s["apiKeyHelper"], *(h[0]["hooks"][0]["command"] for h in (s["hooks"]["PreToolUse"], s["hooks"]["PostToolUse"]))]
    assert all(c.startswith('"C:/Program Files/Python 312/python.exe" "C:/Users/Jo Doe/deepcrew/deepcrew/_run.py" ') for c in commands)
    assert [c.rsplit(" ", 1)[1] for c in commands] == ["key", "pre", "post"]


def test_the_launcher_runs_the_member_without_pythonpath(crew, tmp_path):
    env = {**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, "DEEPCREW_TEAM_DIR": str(crew.dir), "DEEPCREW_MEMBER": "alpha",
           "DEEPCREW_CONFIG_DIR": str(providers.CONFIG_DIR), "PYTHONDONTWRITEBYTECODE": "1"}
    hook = {"tool_name": "Write", "tool_input": {"file_path": str(crew.root / "docs" / "x.md"), "content": "x"}}
    launcher = str(REPO / "deepcrew" / "_run.py")
    p = subprocess.run([sys.executable, "-B", launcher, "hook", "pre"], input=json.dumps(hook), capture_output=True, text=True, cwd=tmp_path, env=env,
                       timeout=60, check=False)
    assert p.returncode == 0 and json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    p = subprocess.run([sys.executable, "-B", launcher, "bogus"], capture_output=True, text=True, cwd=tmp_path, env=env, timeout=60, check=False)
    assert p.returncode == 2 and "usage" in p.stderr


def test_the_environment_names_follow_the_host(windows, monkeypatch):
    assert "SYSTEMROOT" in member.pass_env_names() and "CLAUDE_CODE_GIT_BASH_PATH" in member.pass_env_names() and "PATH" in member.pass_env_names()
    assert member.host_env() == {"PYTHONUTF8": "1"}
    monkeypatch.setattr(providers, "is_windows", lambda: False)
    assert member.pass_env_names() == member.COMMON_ENV and member.host_env() == {}


def test_a_windows_member_gets_its_system_variables_and_utf8_and_never_a_key(make_board, add_member, windows, monkeypatch):
    for var, value in {"SYSTEMROOT": "C:\\Windows", "CLAUDE_CODE_GIT_BASH_PATH": "C:\\Git\\bin\\bash.exe", "FAKE_KEY": "sk-secret", "UNRELATED": "x"}.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setattr(member, "PASS_ENV", member.pass_env_names())
    monkeypatch.setattr(member, "FIXED_ENV", {**member.FIXED_ENV, **member.host_env()})
    board = make_board()
    add_member(board, "alpha", ["src/"], "active")
    e = member.env(board, board.member("alpha"))
    assert e["SYSTEMROOT"] == "C:\\Windows" and e["CLAUDE_CODE_GIT_BASH_PATH"] == "C:\\Git\\bin\\bash.exe" and e["PYTHONUTF8"] == "1"
    assert "sk-secret" not in json.dumps(e) and "UNRELATED" not in e


def test_the_driver_starts_in_its_own_group_without_a_window_on_windows_and_its_own_session_elsewhere(windows, monkeypatch):
    assert member.detach_options() == {"creationflags": 0x00000200 | 0x08000000}
    monkeypatch.setattr(providers, "is_windows", lambda: False)
    assert member.detach_options() == {"start_new_session": True}


def launch(board, m, monkeypatch):
    seen = SimpleNamespace(kw=None, stdin=io.BytesIO(), waited=[])

    class Proc:
        pid, returncode = 4711, None
        stdin = seen.stdin
        wait = poll = lambda self, *a: None

    def popen(args, **kw):
        seen.args, seen.kw = args, kw
        return Proc()

    monkeypatch.setenv("FAKE_KEY", "k")
    monkeypatch.setattr(member, "subprocess", SimpleNamespace(Popen=popen, PIPE=-1, run=None))
    monkeypatch.setattr(member, "await_alive", lambda proc, path, name: seen.waited.append((path, name)))
    seen.stdin.close = lambda: None
    member._launch(board, m, "hello", resume=False)
    return seen


def test_launching_on_windows_detaches_with_flags_sets_utf8_and_waits_for_the_alive_lock(crew, windows, monkeypatch):
    m = crew.member("alpha")
    seen = launch(crew, m, monkeypatch)
    assert seen.kw["creationflags"] == 0x08000200 and "start_new_session" not in seen.kw
    assert seen.kw["env"]["PYTHONUTF8"] == "1" and seen.kw["env"]["PYTHONPATH"] == crew.cfg["pkg_parent"] and seen.stdin.getvalue() == b"hello"
    assert seen.waited == [(member.alive_path(crew, m["session"]), "alpha")]


@POSIX
def test_launching_on_posix_is_as_before(crew, monkeypatch):
    seen = launch(crew, crew.member("alpha"), monkeypatch)
    assert seen.kw["start_new_session"] is True and "creationflags" not in seen.kw and "PYTHONUTF8" not in seen.kw["env"] and seen.waited == []


@POSIX
def test_start_and_wake_record_the_alive_lock_only_on_windows(crew, procs, windows, monkeypatch):
    m = member.start(crew, "gamma", ["README.md"])
    assert m["lock"] == str(crew.dir / "run" / f"{m['session']}.alive")
    monkeypatch.setattr(providers, "is_windows", lambda: False)
    assert "lock" not in member.start(crew, "delta", ["LICENSE"])


def test_stdio_is_utf8_with_plain_newlines_on_windows(windows, monkeypatch):
    raw = {name: io.BytesIO() for name in ("out", "err")}
    streams = {"in": io.TextIOWrapper(io.BytesIO("é\r\n".encode("cp1252")), encoding="cp1252"),
               "out": io.TextIOWrapper(raw["out"], encoding="cp1252", newline="\r\n"), "err": io.TextIOWrapper(raw["err"], encoding="cp1252")}
    for name, stream in zip(("stdin", "stdout", "stderr"), streams.values(), strict=True):
        monkeypatch.setattr(sys, name, stream)
    member.utf8_stdio()
    sys.stdout.write("é\n")
    sys.stdout.flush()
    assert raw["out"].getvalue() == "é\n".encode() and streams["in"].encoding == "utf-8"


def test_the_lock_call_follows_the_host(windows, monkeypatch):
    assert lock_backend() == "msvcrt"
    monkeypatch.setattr(providers, "is_windows", lambda: False)
    assert lock_backend() == "fcntl"


def test_windows_locks_the_first_byte_and_tries_again_until_it_gets_it(tmp_path, windows, clock, monkeypatch):
    fake = FakeMsvcrt(busy=3)
    monkeypatch.setattr(board_module, "msvcrt", fake)
    with open(tmp_path / "lock", "ab") as f:
        assert lock_file(f) is True
        unlock_file(f)
    assert fake.calls == [(2, 1)] * 4 + [(0, 1)] and clock.now == pytest.approx(3 * board_module.LOCK_RETRY_S)


def test_windows_probe_without_waiting_reports_a_lock_held_elsewhere(tmp_path, windows, clock, monkeypatch):
    monkeypatch.setattr(board_module, "msvcrt", FakeMsvcrt(busy=99))
    with open(tmp_path / "lock", "ab") as f:
        assert lock_file(f, wait=False) is False and clock.now == 0


def test_the_board_lock_is_taken_and_released_through_msvcrt_on_windows(crew, windows, monkeypatch):
    fake = FakeMsvcrt()
    monkeypatch.setattr(board_module, "msvcrt", fake)
    with crew.locked():
        assert fake.calls == [(2, 1)]
    assert fake.calls == [(2, 1), (0, 1)]


@POSIX
def test_the_board_lock_excludes_a_second_holder_on_posix(crew):
    with crew.locked(), open(crew.dir / "lock", "ab") as other:
        assert lock_file(other, wait=False) is False
    with open(crew.dir / "lock", "ab") as other:
        assert lock_file(other, wait=False) is True


def test_a_lock_directory_being_deleted_is_waited_for_on_windows_and_an_error_elsewhere(tmp_path, windows, clock, monkeypatch):
    real, tries = os.mkdir, []

    def mkdir(path, *a, **kw):
        tries.append(path)
        if len(tries) < 3:
            raise PermissionError(5, "delete pending")
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "mkdir", mkdir)
    with board_module.file_lock(tmp_path / "1.json"):
        assert (tmp_path / "1.json.lock").is_dir()
    tries.clear()
    monkeypatch.setattr(providers, "is_windows", lambda: False)
    with pytest.raises(PermissionError), board_module.file_lock(tmp_path / "2.json"):
        pass
    assert len(tries) == 1


def test_alive_on_windows_asks_the_lock_not_ps_and_never_a_reused_pid(windows, monkeypatch, real_alive):
    monkeypatch.setattr(member, "subprocess", SimpleNamespace(run=lambda *a, **kw: pytest.fail("ps must not be asked on Windows")))
    asked = []
    monkeypatch.setattr(member, "lock_held", lambda path: asked.append(path) or path == "held.alive")
    assert real_alive({"pid": 4242, "session": "s", "lock": "held.alive"}) is True and real_alive({"pid": 4242, "session": "s", "lock": "free.alive"}) is False
    assert [real_alive(x) for x in (None, {}, {"pid": None, "session": "s", "lock": "held.alive"})] == [False] * 3 and asked == ["held.alive", "free.alive"]
    assert real_alive({"pid": 4242, "session": "s"}) is False and asked[-1] is None


def _release():
    while member._HELD:
        member._HELD.pop().close()


def test_the_alive_lock_is_held_by_exactly_one_driver_and_is_gone_when_it_ends(tmp_path):
    path = tmp_path / "run" / "s.alive"
    code = ("import sys, time\nfrom pathlib import Path\nfrom deepcrew.member import hold_alive\n"
            "assert hold_alive(Path(sys.argv[1]))\nprint('ready', flush=True)\ntime.sleep(60)\n")
    env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.Popen([sys.executable, "-B", "-c", code, str(path)], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert child.stdout.readline().strip() == "ready"
        assert member.lock_held(path) is True and member.hold_alive(path, timeout=0.2) is False
    finally:
        child.terminate()
        child.wait(timeout=30)
        _release()
    assert member.lock_held(path) is False and member.hold_alive(path) is True
    _release()


def test_lock_held_treats_an_unreadable_file_as_alive_and_a_missing_one_as_dead(tmp_path, monkeypatch):
    assert member.lock_held(None) is False and member.lock_held(tmp_path / "none") is False
    (tmp_path / "x.alive").write_bytes(b"")
    monkeypatch.setattr("builtins.open", lambda *a, **kw: (_ for _ in ()).throw(PermissionError(13, "denied")))
    assert member.lock_held(tmp_path / "x.alive") is True


def test_a_new_driver_is_awaited_until_it_holds_its_lock_and_one_that_ends_or_never_takes_it_is_an_error(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=0.0, monotonic=lambda: clock.now, sleep=lambda s: setattr(clock, "now", clock.now + s))
    monkeypatch.setattr(member, "time", clock)
    probes = iter([False, False, True])
    monkeypatch.setattr(member, "lock_held", lambda path: next(probes))
    running = SimpleNamespace(poll=lambda: None)
    member.await_alive(running, tmp_path / "s.alive", "alpha")
    assert clock.now == pytest.approx(0.2)
    monkeypatch.setattr(member, "lock_held", lambda path: False)
    with pytest.raises(member.LaunchError, match="driver.log"):
        member.await_alive(SimpleNamespace(poll=lambda: 1, returncode=1), tmp_path / "s.alive", "alpha")
    with pytest.raises(member.LaunchError, match="did not start within 0.3 s"):
        member.await_alive(running, tmp_path / "s.alive", "alpha", timeout=0.3)


def test_the_end_command_follows_the_host(windows, monkeypatch):
    m = {"pid": 4242, "session": "sess-1"}
    assert member.end_command(m, False) == ["taskkill", "/PID", "4242", "/T"] and member.end_command(m, True) == ["taskkill", "/PID", "4242", "/T", "/F"]
    monkeypatch.setattr(providers, "is_windows", lambda: False)
    assert member.end_command(m, False) == ["pkill", "-TERM", "-f", "sess-1"] and member.end_command(m, True) == ["pkill", "-KILL", "-f", "sess-1"]


def stop_on(board, monkeypatch, *, dies_on, returncode=0, running=True):
    asked, clock = [], SimpleNamespace(now=0.0)
    clock.monotonic, clock.sleep = (lambda: clock.now), (lambda s: setattr(clock, "now", clock.now + s))
    monkeypatch.setattr(member, "subprocess", SimpleNamespace(run=lambda argv, **kw: asked.append(argv) or SimpleNamespace(returncode=returncode)))
    monkeypatch.setattr(member, "time", clock)
    monkeypatch.setattr(member, "alive", lambda m: running and not any(dies_on in a for a in asked))
    return asked, member.stop(board, "alpha", timeout=1.0)


def test_stop_on_windows_ends_the_tree_gently_first_and_forces_after_the_timeout(crew, windows, monkeypatch):
    asked, ran = stop_on(crew, monkeypatch, dies_on="/F")
    assert ran is True and asked == [["taskkill", "/PID", "4242", "/T"], ["taskkill", "/PID", "4242", "/T", "/F"]]


def test_stop_on_windows_is_done_when_the_gentle_request_worked(crew, windows, monkeypatch):
    asked, ran = stop_on(crew, monkeypatch, dies_on="/T")
    assert ran is True and asked == [["taskkill", "/PID", "4242", "/T"]]


def test_stop_on_windows_forces_at_once_when_taskkill_says_the_process_can_only_be_forced(crew, windows, monkeypatch):
    asked, ran = stop_on(crew, monkeypatch, dies_on="/F", returncode=128)
    assert ran and [a[-1] for a in asked] == ["/T", "/F"] and member.time.now < 1.0


def test_stop_on_windows_stops_nothing_that_does_not_run(crew, windows, monkeypatch):
    asked, ran = stop_on(crew, monkeypatch, dies_on="/F", running=False)
    assert ran is False and asked == []


@POSIX
def test_stop_on_posix_still_uses_pkill_by_session(crew, monkeypatch):
    asked, ran = stop_on(crew, monkeypatch, dies_on="-KILL")
    assert ran and asked == [["pkill", "-TERM", "-f", "session-alpha"], ["pkill", "-KILL", "-f", "session-alpha"]]


def flaky_replace(monkeypatch, failures):
    real, calls = os.replace, []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= failures:
            raise PermissionError(32, "in use by another process")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(host.time, "sleep", lambda s: None)
    return calls


def test_write_json_waits_on_windows_for_a_file_another_process_has_open(tmp_path, windows, monkeypatch):
    calls = flaky_replace(monkeypatch, 3)
    (tmp_path / "d").mkdir()
    write_json(tmp_path / "d" / "t.json", {"a": 1})
    assert len(calls) == 4 and read_json(tmp_path / "d" / "t.json") == {"a": 1} and [p.name for p in (tmp_path / "d").iterdir()] == ["t.json"]


def test_write_json_gives_up_on_windows_after_the_tries(tmp_path, windows, monkeypatch):
    calls = flaky_replace(monkeypatch, 99)
    with pytest.raises(PermissionError):
        write_json(tmp_path / "t.json", {"a": 1})
    assert len(calls) == host.BUSY_TRIES


@POSIX
def test_write_json_raises_at_once_off_windows(tmp_path, monkeypatch):
    calls = flaky_replace(monkeypatch, 3)
    with pytest.raises(PermissionError):
        write_json(tmp_path / "t.json", {"a": 1})
    assert len(calls) == 1


def test_a_read_that_meets_a_file_being_replaced_is_tried_again_on_windows(tmp_path, windows, monkeypatch):
    (tmp_path / "t.json").write_text('{"a": 1}')
    real, opened = Path.read_text, []

    def read_text(self, *a, **kw):
        opened.append(self)
        if len(opened) < 3:
            raise PermissionError(13, "sharing violation")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(host.time, "sleep", lambda s: None)
    assert read_json(tmp_path / "t.json") == {"a": 1} and len(opened) == 3


def test_a_mail_the_other_reader_is_moving_stays_with_that_reader_on_windows(crew, windows, monkeypatch):
    crew.send("beta", "alpha", "message", "hi")
    items = crew.peek_mail("alpha")
    monkeypatch.setattr(board_module, "replace_file", lambda *a: (_ for _ in ()).throw(PermissionError(32, "moving")))
    assert crew.ack_mail(items) == []
    monkeypatch.setattr(providers, "is_windows", lambda: False)
    with pytest.raises(PermissionError):
        crew.ack_mail(items)


def test_fold_and_inside_ignore_case_and_slash_direction_on_windows(windows):
    assert policy.fold("C:/Users/Jo/Proj") == policy.fold("c:\\users\\jo\\PROJ") == "c:\\users\\jo\\proj"
    assert policy.inside("C:\\Proj-deepcrew\\Notes\\a.md", "c:\\proj-deepcrew") and policy.inside("c:\\proj-deepcrew", "C:\\Proj-Deepcrew")
    assert not policy.inside("c:\\proj-deepcrew-evil\\a.md", "c:\\proj-deepcrew")


@POSIX
def test_the_team_folder_checks_ignore_case_on_windows_and_not_elsewhere(crew, monkeypatch):
    monkeypatch.setattr(os.path, "realpath", lambda p: str(p))
    note, art = str(crew.notes_dir / "alpha-api.md"), str(crew.artifacts_dir / "alpha" / "out.txt")
    loud = {k: v.upper() for k, v in (("note", note), ("art", art), ("other", str(crew.notes_dir / "beta-api.md")), ("shared", str(crew.shared_dir)))}
    assert not crew.own_file("alpha", loud["note"]) and not crew.in_shared(loud["shared"]) and not crew.is_note(loud["note"])
    monkeypatch.setattr(providers, "is_windows", lambda: True)
    assert crew.own_file("alpha", loud["note"]) and crew.own_file("alpha", loud["art"]) and not crew.own_file("alpha", loud["other"])
    assert crew.in_shared(loud["shared"]) and crew.is_note(loud["note"]) and not crew.in_shared(str(crew.shared_dir) + "-evil")


def test_a_team_folder_is_one_team_however_the_drive_letter_is_written(windows, tmp_path):
    assert team_path(Path("C:\\Proj"), "t").parent.name.rsplit("-", 1)[1] == team_path(Path("c:\\proj"), "t").parent.name.rsplit("-", 1)[1]


def test_regions_cover_paths_without_regard_to_case_on_windows(windows):
    assert policy.covers(["src/", "README.md"], "SRC/A.py") and policy.covers(["src/"], "Src/") and policy.covers(["README.md"], "readme.MD")
    assert policy.overlap(["src/"], ["SRC/x.py"]) and not policy.covers(["src/"], "srcx/a.py")


@POSIX
def test_regions_stay_case_sensitive_on_posix():
    assert not policy.covers(["src/"], "SRC/A.py") and not policy.overlap(["src/"], ["SRC/x.py"])


@POSIX
def test_region_entries_with_a_drive_or_colon_are_refused_on_windows_only(monkeypatch):
    assert policy.norm("C:/x") == "C:/x"
    monkeypatch.setattr(providers, "is_windows", lambda: True)
    for bad in ("C:/x", "dir/file.txt:stream", "C:"):
        with pytest.raises(ValueError, match="':'"):
            policy.norm(bad)
    assert policy.norm("src\\x".replace("\\", "/")) == "src/x"


def test_a_program_is_known_by_its_stem_whatever_its_case_or_extension_on_windows(windows):
    assert policy.program("C:\\Python312\\PYTHON.EXE") == "python" and policy.program("/usr/bin/Node.cmd") == "node" and policy.program("ls") == "ls"
    for cmd in ("C:/Python312/python.exe x.py", '"C:\\Python312\\python.exe" x.py', "Rscript.exe a.R", "powershell -c x", "cmd /c dir", "py -3 x.py", "NPM.CMD test"):
        assert policy.runs_code(cmd), cmd
    assert not policy.runs_code("ls -la") and not policy.runs_code("du.exe -sh")


@POSIX
def test_a_program_is_known_by_its_basename_off_windows():
    assert policy.program("/usr/bin/python3") == "python3" and not policy.runs_code("powershell -c x") and not policy.runs_code("C:\\Python312\\python.exe x.py")


def test_rule_paths_name_drives_the_way_claude_code_reads_them(windows, monkeypatch):
    monkeypatch.setattr(os.path, "realpath", lambda p: str(p))
    assert policy.rule_path("C:\\Users\\Jo Doe\\.deepcrew") == "/c/Users/Jo Doe/.deepcrew"
    assert "Read(/" + policy.rule_path("D:\\keys") + "/**)" == "Read(//d/keys/**)"
    assert policy.rule_path("/tmp/x") == "/tmp/x"


@POSIX
def test_rule_paths_are_the_resolved_path_off_windows(tmp_path):
    assert policy.rule_path(tmp_path) == os.path.realpath(tmp_path)


def test_the_places_a_command_may_not_name_include_every_windows_spelling(windows):
    spelled = providers.windows_spellings("C:\\Users\\Jo Doe\\.config\\deepcrew")
    assert spelled == {"C:\\Users\\Jo Doe\\.config\\deepcrew", "C:/Users/Jo Doe/.config/deepcrew", "C:\\\\Users\\\\Jo Doe\\\\.config\\\\deepcrew",
                       "/c/Users/Jo Doe/.config/deepcrew"}
    assert "_run.py" in providers.secret_locations({"key_sources": []})


@POSIX
def test_the_places_are_unchanged_off_windows():
    assert "_run.py" not in providers.secret_locations({"key_sources": []})


def test_protected_places_match_without_regard_to_case_on_windows(crew, monkeypatch):
    command = {"command": f"type {providers.CONFIG_DIR}".upper()}
    monkeypatch.setattr(sys, "platform", "linux")
    assert policy.decide(crew, "alpha", "Bash", command) is None
    monkeypatch.setattr(providers, "is_windows", lambda: True)
    assert policy.decide(crew, "alpha", "Bash", command)["behavior"] == "deny"
    monkeypatch.setattr(providers, "CONFIG_DIR", Path("C:\\Users\\Jo\\.config\\deepcrew"))
    for spelling in ("type c:\\users\\jo\\.config\\deepcrew\\.env", "cat C:/Users/JO/.config/deepcrew/.env", "cat /c/users/jo/.config/deepcrew/.env"):
        assert policy.decide(crew, "alpha", "Bash", {"command": spelling})["behavior"] == "deny", spelling


def unnamed_encodings(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        method = isinstance(node.func, ast.Attribute)
        name = node.func.attr if method else getattr(node.func, "id", "")
        keywords = {k.arg for k in node.keywords}
        if name in ("read_text", "write_text") and method:
            bad = "encoding" not in keywords
        elif name in ("open", "fdopen") and not (method and name == "open" and getattr(node.func.value, "id", "") in ("os", "webbrowser", "tarfile")):
            mode = next((a.value for a in node.args[1:2] if isinstance(a, ast.Constant)), "r")
            mode = next((k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)), mode)
            bad = "b" not in mode and "encoding" not in keywords
        elif name in ("run", "Popen", "check_output"):
            bad = any(k.arg in ("text", "universal_newlines") for k in node.keywords) and "encoding" not in keywords and None not in keywords
        else:
            bad = False
        if bad:
            found.append(f"{path.name}:{node.lineno} {name}")
    return found


def test_every_text_file_and_subprocess_call_names_its_encoding():
    assert [bad for path in sorted((REPO / "deepcrew").glob("*.py")) for bad in unnamed_encodings(path)] == []


def test_non_ascii_text_survives_the_files_deepcrew_writes(tmp_path, crew):
    write_json(tmp_path / "u.json", {"summary": "\u6309\u9879\u76ee\u6574\u7406 \u2014 \u00e9"})
    assert (tmp_path / "u.json").read_bytes().decode("utf-8").count("\u6309") == 1 and read_json(tmp_path / "u.json")["summary"].endswith("\u00e9")
    note = crew.note_write("leader", "caf\u00e9", "\u2014 na\u00efve")
    assert note.read_bytes() == "\u2014 na\u00efve".encode()


def test_a_byte_order_mark_does_not_hide_the_first_setting(providers_toml):
    providers.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    providers.ENV_FILE.write_bytes(b"\xef\xbb\xbfDEEPCREW_MODEL=m\r\nDEEPCREW_EFFORT=high\r\n")
    assert providers.env_file_get("DEEPCREW_MODEL") == "m" and providers.env_file_get("DEEPCREW_EFFORT") == "high" and providers.env_file_has("DEEPCREW_MODEL")
    providers.env_file_set({"DEEPCREW_MODEL": "n"})
    assert providers.ENV_FILE.read_bytes() == b"DEEPCREW_MODEL=n\nDEEPCREW_EFFORT=high\n"


def test_a_prompt_pack_named_by_a_path_is_a_path(tmp_path, monkeypatch):
    assert prompts.pack_dir({"pack": "mine"}) == providers.CONFIG_DIR / "packs" / "mine"
    assert prompts.pack_dir({"pack": str(tmp_path / "pack")}) == tmp_path / "pack"
    monkeypatch.setattr(os, "sep", "\\")
    assert prompts.pack_dir({"pack": "packs\\mine"}) == Path("packs\\mine")


def test_subprocess_text_is_decoded_as_utf8_with_replacement(crew, monkeypatch):
    seen = {}

    class Proc:
        pid, stdin, stdout = 1, io.StringIO(), iter(())

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(member, "subprocess", SimpleNamespace(Popen=lambda args, **kw: seen.update(kw) or Proc(), PIPE=-1, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(member, "argv", lambda *a, **kw: ["claude"])
    Proc.stdin.close = lambda: None
    member.drive(crew, "alpha", False, "go")
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace" and seen["text"] is True


def test_claude_exe_comes_before_an_npm_shim_and_a_shim_fails_the_check(monkeypatch, tmp_path):
    found = {"claude.exe": str(tmp_path / "claude.exe"), "claude": str(tmp_path / "claude.cmd")}
    monkeypatch.delenv("DEEPCREW_CLAUDE", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: found.get(name))
    monkeypatch.setattr(providers, "is_windows", lambda: True)
    assert cli.claude_bin() == found["claude.exe"]
    del found["claude.exe"]
    assert cli.claude_bin() == found["claude"]
    [claude] = [c for c in cli.host_checks(tmp_path) if c["id"] == "claude"]
    assert claude["level"] == "fail" and "claude.exe" in claude["fix"]
    monkeypatch.setattr(providers, "is_windows", lambda: False)
    assert cli.claude_bin() == found["claude"]


@pytest.mark.sandbox
def test_up_asks_before_running_without_a_sandbox_and_config_turns_it_off(deepcrew, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delitem(cli.SANDBOX_TOOLS, "win32", raising=False)
    code, out, _ = deepcrew("up")
    (question,) = json.loads(out)["questions"]
    assert code == 3 and question["id"] == "sandbox" and question["options"] == ["off"] and "deepcrew config set sandbox off" in question["answer_with"]
    assert deepcrew("config", "set", "sandbox", "off")[0] == 0 and providers.setting("DEEPCREW_SANDBOX") == "off" and not cli.sandbox_on()


def test_the_shell_that_runs_the_tests_does_not_rename_deepcrew(tmp_path):
    env = {**os.environ, "DEEPCREW_NAME": "deepcrew-other", "DEEPCREW_CONFIG_DIR": str(tmp_path / "theirs"), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join([str(REPO), str(REPO / "tests")])}
    code = "import conftest\nfrom deepcrew import cli, providers\nprint(cli.CMD, providers.CONFIG_DIR.name)\n"
    p = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, cwd=tmp_path, env=env, timeout=60, check=False)
    assert p.stdout.split() == ["deepcrew", "deepcrew"], p.stderr


def test_the_home_is_a_throw_away_folder_wherever_the_host_looks_for_it(tmp_path):
    assert Path.home() == tmp_path and posixpath.expanduser("~") == ntpath.expanduser("~") == str(tmp_path)
    assert ntpath.join(os.environ["HOMEDRIVE"], os.environ["HOMEPATH"]) == str(tmp_path)


def test_no_test_ends_a_process():
    for argv in (["pkill", "-TERM", "-f", "session-alpha"], ["taskkill", "/PID", "4242", "/T"], ["killall", "claude"]):
        with pytest.raises(pytest.fail.Exception, match="tests never start"):
            subprocess.Popen(argv)


def test_claude_codes_sandbox_is_there_whatever_the_platform():
    assert cli.sandbox_check(sys.platform)["level"] == "ok"


@pytest.mark.posix("symlinks")
def test_the_posix_marker_says_what_windows_lacks(request):
    assert needs_posix(request.node) == "symlinks"
