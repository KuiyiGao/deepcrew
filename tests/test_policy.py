from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from deepcrew import member, policy, providers
from deepcrew.board import Board, read_json, state_home, write_json

ALLOW = ["du -sh", "wc -l", "ls"]


def decide(board, tool, member="alpha", **tool_input):
    return policy.decide(board, member, tool, tool_input)


def test_norm_gives_the_canonical_form_and_rejects_what_leaves_the_project():
    assert [policy.norm(e) for e in ("src//a/./b/", "  src/x/ ", "src/a.py", "a/..b", "./src/")] == ["src/a/b/", "src/x/", "src/a.py", "a/..b", "src/"]
    for bad in ("/etc", " /etc", "~", "~/x", "../x", "a/../b", "..", "a\\b", "", "  ", ".", "./", None, 5, ["a"]):
        with pytest.raises(ValueError):
            policy.norm(bad)
    assert policy.norm_all(["b/", "a//", "a/./"]) == ["a/", "b/"] and policy.norm_all(None) == []


def test_covers_within_overlap_and_minimal():
    assert policy.covers(["src/"], "src/pkg/b.py") and policy.covers(["src/"], "src/pkg/") and policy.covers(["a.py"], "a.py")
    assert not any([policy.covers(["src/"], "src"), policy.covers(["src/"], "srcx/a.py"), policy.covers(["a.py"], "a.py/x"), policy.covers([], "a")])
    assert policy.within([], ["src/"]) and policy.within(["src/a.py", "src/b/"], ["src/"])
    assert not policy.within(["src/a.py", "docs/x"], ["src/"]) and not policy.within(["a"], [])
    assert policy.overlap(["src/"], ["src/a.py"]) and policy.overlap(["src/pkg/"], ["src/"]) and policy.overlap(["a/", "b/"], ["c/", "b/x"])
    assert not any([policy.overlap(["src/"], ["src2/"]), policy.overlap(["src/a.py"], ["src/b.py"]), policy.overlap([], ["src/"])])
    assert policy.minimal(["a/", "a/x.py", "b", "b", "a//y/"]) == ["a/", "b"]


def test_relpath_resolves_symlinks_and_is_none_outside_the_project(project, tmp_path):
    (tmp_path / "outside").mkdir()
    (project / "src" / "out").symlink_to(tmp_path / "outside")
    (project / "alias").symlink_to(project / "src")
    paths = [project / "src" / "a.py", "src/../docs/guide.md", project / "src" / "new" / "deep.py", project / "alias" / "a.py",
             project / "src" / "out" / "f", tmp_path / "proj-evil" / "x", "/etc/passwd", "../x", project, "."]
    assert [policy.relpath(project, p) for p in paths] == ["src/a.py", "docs/guide.md", "src/new/deep.py", "src/a.py"] + [None] * 6


def test_allowlisted_takes_one_simple_command_that_starts_with_an_entry():
    for ok in ("du -sh", "du -sh -s src/a.py", "  ls  ", 'ls "a file"', "wc -l -q tests/"):
        assert policy.allowlisted(ok, ALLOW), ok
    for no in ("", "  ", "du", "du -shx", "dux -sh", "du -a", "/usr/bin/du -sh", "FOO=1 du -sh", "du -sh; rm x", "du -sh && rm x",
               "du -sh | cat", "du -sh `id`", "du -sh $(id)", "du -sh $HOME", "du -sh > out", "du -sh\nrm x", "du -sh *",
               'du -sh "open', None, 5):
        assert not policy.allowlisted(no, ALLOW), no
    assert policy.allowlisted("ls -la", ["echo 'unterminated", "", "ls"]) and not policy.allowlisted("echo hi", ["echo 'unterminated"])


def test_a_hash_inside_a_word_does_not_hide_the_rest_of_the_command_from_the_allowlist(crew):
    assert policy.parts("wc -l a#b; touch /tmp/x") == [["wc", "-l", "a#b"], ["touch", "/tmp/x"]] and policy.parts("wc -l a#b") == [["wc", "-l", "a#b"]]
    assert policy.allowlisted("wc -l a#b", ["wc -l"]) and not policy.allowlisted("wc -l a#b; touch x", ["wc -l"])
    assert decide(crew, "Bash", command="wc -l a#b")["behavior"] == "allow"
    for command in ("wc -l a#b; touch /tmp/x", "wc -l a#b; touch x", "wc -l a#b && touch x", "wc -l a#b | touch x"):
        assert decide(crew, "Bash", command=command) is None, command


@pytest.mark.sandbox
def test_a_user_allow_covers_exactly_the_words_approved_and_not_the_commands_that_start_with_them(crew):
    r = policy.answer_permission(crew, ask(crew, "rm build/x.o"), True, by="user")
    cfg = read_json(crew.dir / "team.json")
    assert r["always"] is True and cfg["exact_allow"] == ["rm build/x.o"] and "rm build/x.o" not in cfg["bash_allow"] + cfg["approved_allow"]
    exact = lambda command: policy.allowlisted(command, cfg["bash_allow"], True, cfg["exact_allow"])
    assert exact("rm build/x.o") and exact("  rm   build/x.o ") and exact("rm build/x.o && wc -l a")
    assert not any(exact(c) for c in ("rm build/x.o src/a.py", "rm -rf build/x.o", "rm build/x.o; rm src/a.py", "rm build", "rm", "rm build/x.o2"))
    assert decide(crew, "Bash", command="rm build/x.o")["behavior"] == "allow"
    assert decide(crew, "Bash", command="rm build/x.o src/a.py") is None and decide(crew, "Bash", command="rm build/x.o; rm src/a.py") is None
    assert policy.allowlisted("ls -la", ["ls"], exact=["rm x"]) and not policy.allowlisted("rm y", ["ls"], exact=["rm x"])


def test_decide_denies_every_spelling_of_team_state_and_key_locations(crew):
    home = str(Path.home())
    for place in (state_home(), crew.dir, providers.CONFIG_DIR):
        for spelling in (str(place), str(place).replace(home, "~", 1), str(place).replace(home, "$HOME", 1), str(place).replace(home, "${HOME}", 1)):
            for tool, tool_input in (("Bash", {"command": f"cat {spelling}/x"}), ("Read", {"file_path": f"{spelling}/x"}),
                                     ("Edit", {"file_path": "src/a.py", "new_string": spelling})):
                out = decide(crew, tool, **tool_input)
                assert out["behavior"] == "deny" and "team state or keys" in out["message"], (tool, spelling)
    assert decide(crew, "Bash", command="security find-generic-password -s x -w")["behavior"] == "deny"


def test_decide_denies_the_command_that_prints_the_key(crew):
    assert (decide(crew, "Bash", command="python3 -m deepcrew.member key") or {}).get("behavior") == "deny"


def test_decide_keeps_reads_and_edits_inside_the_workspace_and_edits_inside_the_region(crew, project):
    for tool, key in (("Read", "file_path"), ("Glob", "path"), ("Grep", "path"), ("Edit", "file_path"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")):
        for where in ("/etc/passwd", "../outside.txt", "src/../../outside.txt"):
            assert decide(crew, tool, **{key: where}) is None, (tool, where)
    guide = {"file_path": str(project / "docs" / "guide.md")}
    assert decide(crew, "Read", **guide) == {"behavior": "allow", "updatedInput": guide}
    for tool in ("Grep", "Glob"):
        root = {"path": str(project), "pattern": "x"}
        assert decide(crew, tool, **root) == {"behavior": "allow", "updatedInput": root}
    assert decide(crew, "Edit", file_path=str(project))["behavior"] == "deny"
    assert decide(crew, "Edit", file_path=str(project / "src" / "new" / "x.py"))["behavior"] == "allow"
    out = decide(crew, "Write", **guide)
    assert out["behavior"] == "deny" and "it is in beta's region: ask beta by message" in out["message"]
    assert decide(crew, "Write", member="beta", **guide)["behavior"] == "allow"
    assert decide(crew, "Edit", member="ghost", file_path=str(project / "src" / "a.py"))["behavior"] == "deny"


def test_decide_allows_an_allowlisted_command_and_rereads_the_allowlist(crew):
    assert decide(crew, "Bash", command="du -sh -s") == {"behavior": "allow", "updatedInput": {"command": "du -sh -s"}}
    assert all(decide(crew, "Bash", command=c) is None for c in ("rm -rf x", "du -sh; rm x", "wc -c")) and decide(crew, "WebFetch", url="https://x.example") is None
    cfg = read_json(crew.dir / "team.json")
    cfg["bash_allow"].append("wc -c")
    write_json(crew.dir / "team.json", cfg)
    assert decide(crew, "Bash", command="wc -c -j4")["behavior"] == "allow"


def test_an_allowlisted_command_that_names_a_path_outside_the_workspace_goes_to_the_leader(crew, project):
    inside, folder = project / "src", crew.shared_dir / "notes"
    for there in (Path.home() / ".ssh", project.parent / "elsewhere", project.parent / "x"):
        there.mkdir(exist_ok=True)
    for command in ("du -sh src", f"du -sh {inside.as_posix()}", f"du -sh {folder.as_posix()}", "wc -l src/a.py docs/b.md"):
        assert decide(crew, "Bash", command=command)["behavior"] == "allow", command
    for command in ("du -sh ~/.ssh", "du -sh /etc", "du -sh ../elsewhere", "wc -l --files0-from=/etc/passwd", "du -sh src && du -sh /var"):
        assert decide(crew, "Bash", command=command) is None, command
    assert policy.outside(crew, "du -sh src/../../x") == ["src/../../x"] and policy.outside(crew, "du -sh src/../docs") == []


def test_relative_paths_count_from_where_cd_went_and_every_cd_is_checked(crew, project):
    crew.update_cfg(lambda cfg: cfg.__setitem__("sandbox", {"enabled": True}))
    (crew.shared_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (project.parent / "elsewhere").mkdir()
    assert policy.outside(crew, f"cd {crew.shared_dir}/artifacts && cd .. && ls") == []
    assert policy.outside(crew, "cd src && du -sh ../docs") == []
    assert policy.outside(crew, "cd src && du -sh ../../elsewhere") == ["../../elsewhere"]
    assert policy.outside(crew, "cd && cat notes.txt") == ["~"] and policy.outside(crew, "cd - && ls") == ["-"]
    (project / "away").symlink_to(project.parent / "elsewhere")
    assert policy.outside(crew, "cd away && cat x") == ["away"]


def test_with_the_sandbox_a_path_that_does_not_exist_is_harmless(crew):
    test_string = "node --test tests/ -- ../../etc/no-such-file"
    crew.update_cfg(lambda cfg: cfg.__setitem__("sandbox", {"enabled": True}))
    assert policy.outside(crew, test_string) == []
    crew.update_cfg(lambda cfg: cfg.__setitem__("sandbox", None))
    assert policy.outside(crew, test_string) == ["../../etc/no-such-file"]


def test_approve_reuses_the_leaders_answer_for_the_same_tool_use_id(crew):
    tool_input = {"url": "https://x.example"}
    for allow, expected in ((True, {"behavior": "allow", "updatedInput": tool_input}), (False, {"behavior": "deny", "message": "the leader denied this call"})):
        r = crew.request("perms", "alpha", {"tool": "WebFetch", "input": tool_input, "tool_use_id": f"toolu_{allow}", "always": False})
        policy.answer_permission(crew, r["id"], allow)
        assert policy.approve(crew, "alpha", "WebFetch", tool_input, f"toolu_{allow}") == expected
    assert len(list((crew.dir / "perms").glob("P*.json"))) == 2
    assert policy.approve(crew, "alpha", "Bash", {"command": "du -sh"})["behavior"] == "allow" and len(list((crew.dir / "perms").glob("P*.json"))) == 2


def test_approve_denies_when_no_answer_arrives_in_time(crew, clock):
    out = policy.approve(crew, "alpha", "WebFetch", {"url": "https://x.example"}, "toolu_1")
    assert out["behavior"] == "deny" and "no answer from the leader" in out["message"] and clock.now >= crew.cfg["approval_timeout_s"]
    (path,) = (crew.dir / "perms").glob("P*.json")
    assert read_json(path)["state"] == "expired" and crew.pending("perms") == []


def test_an_expired_request_denies_that_call_when_claude_code_asks_again(crew, clock):
    first = policy.approve(crew, "alpha", "WebFetch", {"url": "x"}, "toolu_1")
    again = policy.approve(crew, "alpha", "WebFetch", {"url": "x"}, "toolu_1")
    assert first["behavior"] == again["behavior"] == "deny" and len(list((crew.dir / "perms").glob("P*.json"))) == 1


def test_answer_permission_always_adds_one_simple_bash_command_word_for_word(crew):
    def ask(tool, tool_input):
        return crew.request("perms", "alpha", {"tool": tool, "input": tool_input, "tool_use_id": None, "always": False})["id"]

    def allowed():
        cfg = read_json(crew.dir / "team.json")
        return cfg.get("exact_allow", []), cfg["bash_allow"], cfg["approved_allow"]

    prefixes = allowed()[1:]
    rid = ask("Bash", {"command": "  wc -c  "})
    r = policy.answer_permission(crew, rid, True, always=True)
    assert r["always"] is True and r["state"] == "allowed" and allowed()[0] == ["wc -c"] and crew.cfg["exact_allow"] == ["wc -c"]
    assert read_json(crew.dir / "perms" / f"{rid}.json")["always"] is True
    assert allowed()[1:] == prefixes
    assert decide(crew, "Bash", command="wc -c")["behavior"] == "allow"
    assert decide(crew, "Bash", command="wc -c -j4") is None and decide(crew, "Bash", command="wc -c a.py") is None
    before = allowed()
    for tool, tool_input, allow in (("Bash", {"command": "wc a && wc b"}, True), ("Bash", {"command": "make c"}, False), ("Bash", {"command": " "}, True),
                                    ("Bash", {"command": "echo it's"}, True), ("Edit", {"file_path": "src/a.py"}, True)):
        refused = ask(tool, tool_input)
        assert policy.answer_permission(crew, refused, allow, always=True)["always"] is False
        stored = read_json(crew.dir / "perms" / f"{refused}.json")
        assert (stored["always"], stored["widened"]) == (False, None)
    assert policy.answer_permission(crew, ask("Bash", {"command": "wc -c"}), True, always=True)["always"] is True
    assert allowed() == before
    with pytest.raises(ValueError):
        policy.answer_permission(crew, "P12345678", True)


@pytest.mark.sandbox
def test_commands_that_run_code_join_by_prefix_only_under_the_sandbox_and_word_for_word_once_approved(crew):
    for cmd in ("python3 -m pytest", "/usr/bin/python3 x.py", "make test", "npm test", "bash -c ls", "git status", "env ls"):
        assert policy.runs_code(cmd) and not policy.allowlisted(cmd, [cmd]) and policy.allowlisted(cmd, [cmd], sandboxed=True)
        assert policy.allowlisted(cmd, [], exact=[cmd]) and policy.allowlisted(cmd, [], sandboxed=True, exact=[cmd])
        assert not policy.allowlisted(cmd + " --again", [], exact=[cmd])
    assert not policy.runs_code("ls -la") and not policy.runs_code("du -sh src")

    def always(cmd):
        rid = crew.request("perms", "alpha", {"tool": "Bash", "input": {"command": cmd}, "tool_use_id": None, "always": False})["id"]
        return policy.answer_permission(crew, rid, True, always=True)["always"]
    assert crew.cfg["sandbox"] and always("make test") is True and "make test" in read_json(crew.dir / "team.json")["exact_allow"]
    without_sandbox(crew)
    assert always("npm test") is True and "npm test" in read_json(crew.dir / "team.json")["exact_allow"]


@pytest.mark.parametrize("command, ok", [
    ("node --test tests 2>&1 | tail -5", True), ("cd sim && node check.mjs 2>/dev/null", True), ("node a.js && node b.js || ls", True),
    ("ls > out.txt", False), ("ls; rm -rf x", False), ("a=1; ls", False), ("echo $HOME", False), ("ls $(whoami)", False),
    ("wc -l *.py", False), ("node x &", False), ("echo hi\nrm x", False), ("echo hi\rrm x", False), ("ls |", False), ("| ls", False),
])
def test_allowlisted_commands_may_be_joined_but_every_part_must_be_on_the_list(command, ok):
    assert policy.allowlisted(command, ["ls", "wc", "node", "tail", "cd", "echo"], sandboxed=True) is ok


@pytest.mark.parametrize("command", ["python3.12 x.py", "FOO=1 python3 x.py", "nice python3 x.py", "rg foo", "node22 x.js", "stdbuf -oL ls", "command ls", "A=1 B=2 make",
                                     "rm build/x.o", "./build.sh"])
def test_without_the_sandbox_only_read_only_programs_may_be_listed(command):
    runs = "could run code and the sandbox is off, so it is approved call by call"
    other = "is not one of the read-only programs the allowlist takes while the sandbox is off, so it is approved call by call"
    expected = f"{command!r} {other}" if command in ("rg foo", "rm build/x.o") else f"{command!r} {runs}"
    assert policy.allow_refusal(command, sandboxed=False) == expected
    assert policy.allow_refusal(command, sandboxed=True) is None
    assert policy.allowlisted(command, [command], sandboxed=True) and policy.runs_code(command) is (command not in ("rg foo", "rm build/x.o"))


@pytest.mark.parametrize("command", ["sort -o .claude/settings.json notes.txt", "sort -o.claude/settings.json notes.txt", "sort --output=.git/hooks/pre-commit x",
                                     "sort --compress-program=./evil.sh big.txt", "uniq notes.txt .git/hooks/pre-commit", "tree -o .claude/settings.json",
                                     "sort", "uniq", "tree"])
def test_sort_uniq_and_tree_write_files_or_run_programs_so_without_the_sandbox_they_are_not_listed(crew, command):
    assert "is not one of the read-only programs" in policy.allow_refusal(command, sandboxed=False)
    assert policy.allow_refusal(command, sandboxed=True) is None
    without_sandbox(crew)
    assert "is not one of the read-only programs" in policy.set_allow(crew, command, "leader")
    assert command not in read_json(crew.dir / "team.json")["bash_allow"]


def test_a_program_given_by_a_path_runs_code(monkeypatch):
    for command in ("./build.sh", "bin/rails server", ".venv/bin/pytest -q", "../tools/x", "FOO=1 ./x.sh", "/bin/ls", "'C:\\tools\\x.exe' /q", ".hidden"):
        assert policy.runs_code(command), command
        assert not policy.allowlisted(command, [command]) and policy.allowlisted(command, [command], sandboxed=True)
    for command in ("ls ./src", "wc -l bin/x", "ls"):
        assert not policy.runs_code(command), command
    monkeypatch.setattr(providers, "is_windows", lambda: True)
    assert policy.runs_code("C:x.exe") and not policy.runs_code("findstr x a.txt")


def test_without_the_sandbox_a_read_only_simple_command_may_be_listed_and_nothing_else_may():
    assert policy.allow_refusal("wc -l", sandboxed=False) is None and policy.allow_refusal("ls -la src", sandboxed=False) is None
    assert policy.allow_refusal("wc -l; ls", sandboxed=True) == "'wc -l; ls' is not one simple command" and policy.allow_refusal(" ", sandboxed=False) == "' ' is not one simple command"
    for runs in ("python3.12 x.py", "FOO=1 python3 x.py", "A=1 B=2 make", "nice ls", "/usr/bin/node22 x.js", "stdbuf -oL ls", "command ls"):
        assert policy.runs_code(runs), runs
    for plain in ("wc -l", "FOO=1 ls", "echo python3", "FOO=python3 ls", "rg foo", "python3x.py"):
        assert not policy.runs_code(plain), plain


def test_a_joined_command_that_runs_code_needs_the_sandbox():
    assert policy.allowlisted("ls && node x.mjs", ["ls", "node"], sandboxed=True)
    assert not policy.allowlisted("ls && node x.mjs", ["ls", "node"], sandboxed=False)


def ask(board, command, member="alpha", tool="Bash", **tool_input):
    return board.request("perms", member, {"tool": tool, "input": tool_input or {"command": command}, "tool_use_id": None, "always": False})["id"]


def allow_log(board):
    path = board.dir / "run" / policy.ALLOW_LOG
    return [(r["command"], r["by"], r["on"]) for r in map(json.loads, path.read_text().splitlines())] if path.exists() else []


def allow_rows(board):
    return [json.loads(line) for line in (board.dir / "run" / policy.ALLOW_LOG).read_text().splitlines()]


def without_sandbox(board):
    cfg = read_json(board.dir / "team.json")
    cfg["sandbox"] = None
    write_json(board.dir / "team.json", cfg)
    board.cfg = cfg


def test_a_user_allow_of_one_simple_command_allowlists_it_and_the_log_says_who(crew):
    write_json(crew.dir / "run" / "settings.json", {})
    version = crew.version()
    r = policy.answer_permission(crew, ask(crew, "  wc -c  "), True, by="user")
    assert crew.version() != version
    cfg = read_json(crew.dir / "team.json")
    assert (r["state"], r["by"], r["always"]) == ("allowed", "user", True) and cfg["exact_allow"] == ["wc -c"]
    assert r["widened"] == {"kind": "command", "value": "wc -c", "text": "`wc -c` runs without asking from now on"}
    assert "wc -c" not in cfg["bash_allow"] and "wc -c" not in cfg["approved_allow"]
    assert crew.cfg["exact_allow"] == cfg["exact_allow"] and decide(crew, "Bash", command="wc -c")["behavior"] == "allow"
    assert decide(crew, "Bash", command="wc -c -j4") is None
    assert allow_log(crew) == [("wc -c", "user", True)] and [row["exact"] for row in allow_rows(crew)] == [True]
    assert read_json(crew.dir / "run" / "settings.json") == {}


def test_a_leader_approval_allowlists_a_command_only_with_always(crew):
    r = policy.answer_permission(crew, ask(crew, "wc -m"), True)
    cfg = read_json(crew.dir / "team.json")
    assert (r["state"], r["by"], r["always"]) == ("allowed", "leader", False) and "wc -m" not in [*cfg["bash_allow"], *cfg.get("exact_allow", [])]
    assert allow_log(crew) == [] and decide(crew, "Bash", command="wc -m") is None
    r = policy.answer_permission(crew, ask(crew, "wc -m"), True, always=True)
    assert r["always"] is True and allow_log(crew) == [("wc -m", "leader", True)] and decide(crew, "Bash", command="wc -m")["behavior"] == "allow"
    assert read_json(crew.dir / "team.json")["exact_allow"] == ["wc -m"] and decide(crew, "Bash", command="wc -m x") is None


def test_an_answer_adds_to_the_lists_only_what_widening_names(crew):
    lists = lambda: {k: read_json(crew.dir / "team.json").get(k, []) for k in ("bash_allow", "approved_allow", "exact_allow")}
    before = lists()
    for by, allow, command in (("user", False, "wc -c"), ("user", True, "wc a && wc b"), ("user", True, "wc a | wc"), ("user", True, "wc a; rm b"), ("user", True, "echo $HOME"),
                               ("user", True, " "), ("user", True, "echo it's")):
        r = policy.answer_permission(crew, ask(crew, command), allow, by=by)
        assert r["always"] is False and r["state"] == ("allowed" if allow else "denied"), command
    r = policy.answer_permission(crew, ask(crew, "", tool="Edit", file_path="src/a.py"), True, by="user")
    assert (r["always"], r["widened"]) == (False, None)
    assert lists() == before and allow_log(crew) == [] and crew.sites() == []


def test_a_user_allow_of_a_command_that_runs_code_lists_it_word_for_word_without_the_sandbox_too(crew):
    without_sandbox(crew)
    r = policy.answer_permission(crew, ask(crew, "make test"), True, by="user")
    cfg = read_json(crew.dir / "team.json")
    assert (r["state"], r["always"]) == ("allowed", True) and "make test" in cfg["exact_allow"] and "make test" not in cfg["bash_allow"]
    assert decide(crew, "Bash", command="make test")["behavior"] == "allow"
    assert decide(crew, "Bash", command="make test -j8") is None
    assert policy.set_allow(crew, "make", "leader") == "'make' could run code and the sandbox is off, so it is approved call by call"


def test_a_user_allow_puts_a_removed_command_back_word_for_word(crew):
    cfg = read_json(crew.dir / "team.json")
    write_json(crew.dir / "team.json", {**cfg, "allow_scope": ["du -sh", "wc -l"]})
    assert policy.set_allow(crew, "wc -l", "leader", on=False) is None and "wc -l" not in read_json(crew.dir / "team.json")["bash_allow"]
    assert read_json(crew.dir / "team.json")["allow_off"] == ["wc -l"] and decide(crew, "Bash", command="wc -l") is None
    policy.answer_permission(crew, ask(crew, "wc -l"), True, by="user")
    cfg = read_json(crew.dir / "team.json")
    assert cfg["exact_allow"] == ["wc -l"] and "wc -l" not in cfg["bash_allow"] and cfg["allow_off"] == []
    assert allow_log(crew) == [("wc -l", "leader", False), ("wc -l", "user", True)] and [row["exact"] for row in allow_rows(crew)] == [False, True]
    assert decide(crew, "Bash", command="wc -l")["behavior"] == "allow" and decide(crew, "Bash", command="wc -l a.py") is None
    assert policy.set_allow(crew, "wc -l", "leader", on=False) is None and read_json(crew.dir / "team.json")["exact_allow"] == []


def test_a_denied_member_is_told_who_denied_the_call(crew, clock):
    def deny_by(who):
        clock.on_sleep = lambda: [policy.answer_permission(crew, r["id"], False, by=who) for r in crew.pending("perms")]
        first = policy.approve(crew, "alpha", "WebFetch", {"url": f"https://{who}.example"}, f"toolu_{who}")
        again = policy.approve(crew, "alpha", "WebFetch", {"url": f"https://{who}.example"}, f"toolu_{who}")
        return first, again

    for who in ("user", "leader"):
        first, again = deny_by(who)
        assert first == again == {"behavior": "deny", "message": f"the {who} denied this call"}
    assert len(list((crew.dir / "perms").glob("P*.json"))) == 2
    clock.on_sleep = None
    expired = policy.approve(crew, "alpha", "WebFetch", {"url": "https://late.example"}, "toolu_late")
    assert "no answer from the leader" in expired["message"]
    assert policy.approve(crew, "alpha", "WebFetch", {"url": "https://late.example"}, "toolu_late")["behavior"] == "deny"


def test_a_user_allow_is_reused_for_the_same_tool_use_id(crew, clock):
    clock.on_sleep = lambda: [policy.answer_permission(crew, r["id"], True, by="user") for r in crew.pending("perms")]
    tool_input = {"command": "wc -w"}
    assert policy.approve(crew, "alpha", "Bash", tool_input, "toolu_1")["behavior"] == "allow"
    clock.on_sleep = None
    assert policy.approve(crew, "alpha", "Bash", tool_input, "toolu_1") == {"behavior": "allow", "updatedInput": tool_input}
    assert len(list((crew.dir / "perms").glob("P*.json"))) == 1


def test_a_heredoc_delimiter_must_end_its_word_and_a_quoted_digit_is_an_argument():
    for command in ("cat <<'EOF'x\nEOFx\ntouch PWNED\nEOF\n", 'cat <<"EOF"y\nx\nEOF\n', "cat <<'E''X'\nx\nE\n", "cat <<'E'\\x\nx\nE\n"):
        assert policy.parts(command) is None, command
    assert policy.parts("cat <<'EOF' | wc -l\nhi\nEOF\n") == [["cat"], ["wc", "-l"]]
    assert policy.parts("echo \\2>&1") is None and policy.parts('echo ""2>&1') is None and policy.parts("ls 2>&1") == [["ls"]]


def test_a_cd_that_may_fail_or_not_last_keeps_the_old_folder_in_view(crew, project):
    crew.update_cfg(lambda cfg: cfg.__setitem__("sandbox", {"enabled": True}))
    sib = project.parent / "sib"
    sib.mkdir()
    (sib / "secret.txt").write_text("x")
    for command in ("cd nosuch; cat ../sib/secret.txt", "cd src | cat ../sib/secret.txt", "cd src || cat ../sib/secret.txt"):
        assert policy.outside(crew, command) == ["../sib/secret.txt"], command
    assert policy.outside(crew, "cd src && cat ../sib/secret.txt") == []
    assert policy.outside(crew, "cd proj sib") == ["proj sib"]
    (project / "lib").symlink_to(sib)
    assert policy.outside(crew, "cat lib/secret.txt") == ["lib/secret.txt"]
    (project / "src" / "deep").mkdir()
    (project / "src" / "up").symlink_to(project / "src" / "deep")
    assert policy.outside(crew, "cd src/up/../../..") == ["src/up/../../.."]


@pytest.mark.sandbox
def test_a_command_is_decided_by_the_sandbox_the_member_started_with(crew):
    cfg = read_json(crew.dir / "team.json")
    write_json(crew.dir / "team.json", {**cfg, "bash_allow": [*cfg["bash_allow"], "make test"]})
    crew.update_member("alpha", sandbox=False)
    crew.update_member("beta", sandbox=True)
    assert crew.cfg["sandbox"] and decide(crew, "Bash", command="make test") is None
    assert decide(crew, "Bash", member="beta", command="make test")["behavior"] == "allow"
    without_sandbox(crew)
    assert decide(crew, "Bash", member="beta", command="make test")["behavior"] == "allow" and decide(crew, "Bash", command="make test") is None
    assert policy.outside(crew, "du -sh ../../nowhere", "alpha") == ["../../nowhere"] and policy.outside(crew, "du -sh ../../nowhere", "beta") == []
    crew.update_member("beta", sandbox=None)
    assert decide(crew, "Bash", member="beta", command="make test") is None


def test_approve_waits_as_long_as_team_json_says_now(crew, clock):
    cfg = read_json(crew.dir / "team.json")
    write_json(crew.dir / "team.json", {**cfg, "approval_timeout_s": 7})
    out = policy.approve(crew, "alpha", "WebFetch", {"url": "https://x.example"}, "toolu_1")
    assert clock.now >= 7 and out == {"behavior": "deny", "message": "no answer from the leader within 7 s; denied"}
    assert policy.approve(crew, "alpha", "WebFetch", {"url": "https://x.example"}, "toolu_1") == out


def test_an_answer_links_a_folder_only_for_a_request_it_answers(crew, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()

    def wants(folder):
        return crew.request("perms", "alpha", {"tool": "Read", "input": {"file_path": str(folder / "x")}, "tool_use_id": None, "always": False,
                                               "folder": str(folder), "write": False})["id"]

    real = crew.answer

    def late(kind, rid, *args, **fields):
        write_json(crew.dir / kind / f"{rid}.json", {**read_json(crew.dir / kind / f"{rid}.json"), "state": "expired"})
        return real(kind, rid, *args, **fields)

    monkeypatch.setattr(crew, "answer", late)
    with pytest.raises(ValueError, match="already expired"):
        policy.answer_permission(crew, wants(data), True, link=True)
    monkeypatch.setattr(crew, "answer", real)
    assert crew.folders() == []
    answered = wants(data)
    policy.answer_permission(crew, answered, False)
    for rid in (answered, "../team", "P12345678", None):
        with pytest.raises(ValueError, match="no request|only an allowed request"):
            policy.answer_permission(crew, rid, True, link=True)
    too_wide = wants(Path.home())
    with pytest.raises(ValueError, match="too wide"):
        policy.answer_permission(crew, too_wide, True, link=True)
    assert crew.folders() == [] and [r["id"] for r in crew.pending("perms")] == [too_wide]
    r = policy.answer_permission(crew, wants(data), True, link=True)
    assert r["state"] == "allowed" and [f["path"] for f in crew.folders()] == [str(data.resolve())]


@pytest.mark.skipif(sys.platform == "win32", reason="runs the macOS and Linux branches for real")
@pytest.mark.parametrize("platform, closed", [("darwin", True), ("linux", False)])
def test_on_macos_a_place_with_team_state_is_closed_whatever_the_case_of_its_name(crew, monkeypatch, platform, closed):
    monkeypatch.setattr(sys, "platform", platform)
    other_case = str(state_home()).replace(".deepcrew", ".DeepCrew")
    for tool, tool_input in (("Bash", {"command": "cat ~/.DeepCrew/x"}), ("Bash", {"command": f"cat {other_case}/x"}), ("Read", {"file_path": f"{other_case}/x"})):
        out = decide(crew, tool, **tool_input) or {}
        assert (out.get("behavior") == "deny" and "team state or keys" in out["message"]) is closed, (tool, tool_input)


def test_a_site_must_be_a_host_name(crew):
    with pytest.raises(ValueError, match=r"'not a host' is not a host name such as api\.example\.com or \*\.example\.com"):
        policy.set_site(crew, "not a host")


def test_a_link_holds_neither_the_team_folder_nor_the_tools_folder_nor_another_link(make_board, tmp_path):
    from deepcrew.board import tools_dir
    board = make_board("main", default_location=True)
    tools = tools_dir()
    tools.mkdir(parents=True, exist_ok=True)
    board.shared_dir.mkdir(parents=True, exist_ok=True)
    for bad, why in ((board.shared_dir.parent, "holds the team folder"), (tools.parent, "holds the tools folder")):
        with pytest.raises(ValueError, match=why):
            policy.check_folder(board, str(bad))
    data = tmp_path / "data"
    (data / "out").mkdir(parents=True)
    policy.set_folder(board, str(data), by="user")
    with pytest.raises(ValueError, match="linked already"):
        policy.set_folder(board, str(data / "out"), write=True, by="user")
    assert policy.set_folder(board, str(data), write=True, by="user")["write"] is True


def fetch(board, url, member="alpha"):
    return decide(board, "WebFetch", member=member, url=url)


def test_a_web_fetch_to_a_site_of_the_team_is_allowed_at_once_and_a_look_alike_is_not(crew):
    assert fetch(crew, "https://docs.example.org/a") is None
    policy.set_site(crew, "docs.example.org", by="leader")
    policy.set_site(crew, "*.github.com", by="user")
    for url in ("https://docs.example.org/a?b=1#c", "http://DOCS.example.org:8080/", "https://docs.example.org./",
                "https://docs.example.org", "https://api.github.com/repos", "https://a.b.github.com/", "  https://docs.example.org/x\n"):
        assert fetch(crew, url) == {"behavior": "allow", "updatedInput": {"url": url}}, url
    for url in ("https://example.org/", "https://github.com/", "https://api.docs.example.org/x", "https://evildocs.example.org/", "https://docs.example.org.evil.com/",
                "https://docs.example.org@evil.com/", "https://evil.com\\@docs.example.org/", "https://evil.com#@docs.example.org", "https://evil.com/docs.example.org",
                "ftp://docs.example.org/", "docs.example.org", "https://docs.example.org /x", "https://docs.exa\nmple.org/", "https://docs.example.org\\x",
                "https://localhost/", "https://127.0.0.1/", "https://[::1]/", "", None, 5, ["https://docs.example.org/"]):
        assert fetch(crew, url) is None, url
    assert decide(crew, "WebFetch") is None and decide(crew, "WebSearch", url="https://docs.example.org/") is None


def test_the_sites_are_read_on_every_call_so_running_members_follow_a_change(crew):
    url = "https://docs.example.org/guide"
    assert fetch(crew, url) is None
    policy.set_site(Board(crew.dir), "docs.example.org", by="leader")
    assert fetch(crew, url)["behavior"] == "allow"
    policy.set_site(Board(crew.dir), "docs.example.org", by="leader", on=False)
    assert fetch(crew, url) is None


def test_a_site_does_not_open_a_call_that_names_team_state_or_keys(crew):
    policy.set_site(crew, "docs.example.org")
    out = fetch(crew, f"https://docs.example.org/?q={state_home()}")
    assert out["behavior"] == "deny" and "team state or keys" in out["message"]
    policy.set_tool(crew, "mcp__srv__search", "leader")
    out = decide(crew, "mcp__srv__search", query=f"{crew.dir}/team.json")
    assert out["behavior"] == "deny" and "team state or keys" in out["message"]


def test_an_mcp_tool_on_the_tool_list_is_allowed_at_once_and_deepcrews_own_tools_never_come_from_it(crew):
    tool = "mcp__github__create_issue"
    assert decide(crew, tool, title="x") is None
    assert policy.set_tool(crew, tool, "leader") is None
    assert decide(crew, tool, title="x") == {"behavior": "allow", "updatedInput": {"title": "x"}}
    assert all(decide(crew, other) is None for other in ("mcp__github__delete_repo", "mcp__github", "mcp__githubx__create_issue", "Bash", "WebFetch"))
    cfg = read_json(crew.dir / "team.json")
    write_json(crew.dir / "team.json", {**cfg, "tool_allow": ["mcp__deepcrew__approve", "mcp__deepcrew__message", tool]})
    assert decide(crew, "mcp__deepcrew__message") is None and decide(crew, tool, title="x")["behavior"] == "allow"
    write_json(crew.dir / "team.json", {**cfg, "tool_allow": tool})
    assert decide(crew, tool, title="x") is None and decide(crew, "mcp__github", title="x") is None


def test_set_tool_lists_an_mcp_tool_logs_who_did_it_and_takes_it_off(crew):
    version = crew.version()
    assert policy.set_tool(crew, "  mcp__srv__search  ", "leader") is None
    assert read_json(crew.dir / "team.json")["tool_allow"] == ["mcp__srv__search"] and crew.cfg["tool_allow"] == ["mcp__srv__search"]
    assert crew.version() != version
    assert policy.set_tool(crew, "mcp__srv__search", "user") is None and policy.set_tool(crew, "mcp__srv__other-tool", "user") is None
    assert read_json(crew.dir / "team.json")["tool_allow"] == ["mcp__srv__search", "mcp__srv__other-tool"]
    rows = allow_rows(crew)
    assert [(r["kind"], r["tool"], r["by"], r["on"]) for r in rows] == [("tool", "mcp__srv__search", "leader", True), ("tool", "mcp__srv__search", "user", True),
                                                                        ("tool", "mcp__srv__other-tool", "user", True)]
    assert all("command" not in r for r in rows)
    assert policy.set_tool(crew, "mcp__srv__search", "leader", on=False) is None
    assert read_json(crew.dir / "team.json")["tool_allow"] == ["mcp__srv__other-tool"] and allow_rows(crew)[-1]["on"] is False
    assert policy.set_tool(crew, "mcp__srv__search", "leader", on=False) == "'mcp__srv__search' is not on the list of tools members use without asking"
    assert read_json(crew.dir / "team.json")["bash_allow"] == ["du -sh", "wc -l"]


@pytest.mark.parametrize("name", ["Bash", "WebFetch", "github", "mcp__srv", "mcp__srv__", "mcp____x", "mcp__a b__c", "mcp__srv__a;b", "", None, 5])
def test_only_the_name_of_an_mcp_tool_goes_on_the_tool_list(crew, name):
    assert "is not the name of an MCP tool, such as mcp__github__create_issue" in policy.set_tool(crew, name, "leader")
    assert read_json(crew.dir / "team.json").get("tool_allow", []) == [] and not (crew.dir / "run" / policy.ALLOW_LOG).exists()


def test_deepcrews_own_tools_are_not_put_on_the_tool_list(crew):
    assert policy.set_tool(crew, "mcp__deepcrew__message", "leader") == "'mcp__deepcrew__message' is one of deepcrew's own tools, which members use without asking already"
    assert read_json(crew.dir / "team.json").get("tool_allow", []) == []


def request_of(board, tool, member="alpha", **tool_input):
    return board.request("perms", member, {"tool": tool, "input": tool_input, "tool_use_id": None, "always": False})


@pytest.mark.sandbox
def test_widening_names_what_an_always_answer_would_add(crew):
    def widening(tool, **tool_input):
        return policy.widening(crew, {"tool": tool, "member": "alpha", "input": tool_input})

    assert widening("Bash", command="  npm run build ") == {"kind": "command", "value": "npm run build", "text": "`npm run build` runs without asking from now on"}
    assert widening("WebFetch", url="https://Docs.Python.org/3/") == {
        "kind": "site", "value": "docs.python.org",
        "text": "members reach docs.python.org without asking"}
    assert widening("mcp__github__create_issue", title="x") == {"kind": "tool", "value": "mcp__github__create_issue",
                                                               "text": "members use mcp__github__create_issue without asking"}
    for tool, tool_input in (("Bash", {"command": "wc a && wc b"}), ("Bash", {"command": "echo $HOME"}), ("Bash", {"command": " "}), ("Bash", {}), ("Bash", {"command": ["ls"]}),
                             ("Bash", "ls"), ("WebFetch", {"url": "https://127.0.0.1/x"}), ("WebFetch", {"url": "http://localhost/"}), ("WebFetch", {"url": "https://1.2.3.0x7f/"}),
                             ("WebFetch", {"url": "https://evil.com\\@docs.python.org/"}), ("WebFetch", {"url": "not a url"}), ("WebFetch", {}),
                             ("mcp__deepcrew__message", {"to": "leader"}), ("mcp__github", {}), ("Edit", {"file_path": "src/a.py"}), ("Read", {"file_path": "/etc/hosts"}),
                             ("WebSearch", {"query": "x"}), ("Agent", {}), (None, {})):
        assert policy.widening(crew, {"tool": tool, "member": "alpha", "input": tool_input}) is None, (tool, tool_input)
    assert policy.widening(crew, {"tool": "Bash", "member": "alpha"}) is None and policy.widening(crew, {}) is None


@pytest.mark.sandbox
def test_widening_leaves_out_what_is_listed_already_and_lists_a_command_word_for_word_with_or_without_the_sandbox(crew):
    policy.set_site(crew, "*.python.org")
    policy.set_tool(crew, "mcp__github__create_issue", "leader")
    assert policy.widening(crew, request_of(crew, "WebFetch", url="https://docs.python.org/3/")) is None
    assert policy.widening(crew, request_of(crew, "WebFetch", url="https://python.org/"))["value"] == "python.org"
    assert policy.widening(crew, request_of(crew, "mcp__github__create_issue")) is None
    assert policy.widening(crew, request_of(crew, "Bash", command="make test"))["value"] == "make test"
    without_sandbox(crew)
    assert policy.widening(crew, request_of(crew, "Bash", command="make test"))["value"] == "make test"
    assert policy.widening(crew, request_of(crew, "Bash", command="wc -c"))["value"] == "wc -c"


def test_a_command_that_names_a_place_outside_the_project_has_nothing_to_widen_since_it_would_still_ask(crew):
    r = request_of(crew, "Bash", command="du -sh /etc")
    assert decide(crew, "Bash", command="du -sh /etc") is None and policy.widening(crew, r) is None
    assert policy.widening_refusal(crew, r) == "'du -sh /etc' names a place outside the project (/etc), so it is approved call by call"
    answered = policy.answer_permission(crew, r["id"], True, by="user")
    assert (answered["always"], answered["widened"]) == (False, None) and read_json(crew.dir / "team.json").get("exact_allow", []) == []
    assert decide(crew, "Bash", command="du -sh /etc") is None
    linkable = crew.request("perms", "alpha", {"tool": "Bash", "input": {"command": "du -sh /etc"}, "tool_use_id": None, "always": False, "folder": "/etc"})
    assert policy.widening_refusal(crew, linkable).endswith("so it is approved call by call, unless the folder is linked (--link)")


def test_widening_says_why_it_adds_nothing(crew):
    def why(tool, **tool_input):
        return policy.widening_refusal(crew, {"tool": tool, "member": "alpha", "input": tool_input})

    assert why("Bash", command="wc a && wc b") == "'wc a && wc b' is not one simple command"
    assert why("WebFetch", url="https://127.0.0.1/x") == "127.0.0.1 is an address, not a domain name, so it is approved call by call"
    assert why("WebFetch", url="gopher://x") == "the address is not a plain http or https URL with a host name"
    assert why("mcp__deepcrew__message") == "'mcp__deepcrew__message' is one of deepcrew's own tools, which members use without asking already"
    assert why("Edit", file_path="src/a.py") == "Edit calls are answered one by one"
    assert why("Bash", command="wc -c") == "" and why("WebFetch", url="https://docs.python.org/") == ""
    policy.set_site(crew, "docs.python.org")
    policy.set_tool(crew, "mcp__srv__search", "leader")
    assert why("WebFetch", url="https://docs.python.org/3") == "docs.python.org is among the team's sites already"
    assert why("mcp__srv__search") == "mcp__srv__search is on the list of tools members use without asking already"
    assert policy.widening_refusal(crew, {"tool": "Read", "input": {"file_path": "/data/x"}, "folder": "/data"}) == "Read calls are answered one by one (--link opens its folder for every member)"


def test_an_always_answer_to_a_fetch_adds_the_site_and_the_record_says_so(crew):
    rid = request_of(crew, "WebFetch", url="https://docs.python.org/3/library/")["id"]
    r = policy.answer_permission(crew, rid, True, always=True)
    expected = {"kind": "site", "value": "docs.python.org",
                "text": "members reach docs.python.org without asking"}
    assert (r["state"], r["by"], r["always"], r["widened"]) == ("allowed", "leader", True, expected)
    stored = read_json(crew.dir / "perms" / f"{rid}.json")
    assert (stored["always"], stored["widened"]) == (True, expected)
    (site,) = read_json(crew.dir / "team.json")["sites"]
    assert (site["domain"], site["by"]) == ("docs.python.org", "leader") and crew.sites() == ["docs.python.org"]
    assert fetch(crew, "https://docs.python.org/3/tutorial/")["behavior"] == "allow" and fetch(crew, "https://pypi.org/") is None
    assert fetch(crew, "https://www.docs.python.org/") is None
    assert read_json(crew.dir / "team.json").get("exact_allow", []) == [] and allow_log(crew) == []


def test_a_user_allow_of_a_fetch_or_an_mcp_tool_widens_as_a_simple_command_does(crew):
    fetched = policy.answer_permission(crew, request_of(crew, "WebFetch", url="https://api.render.com/v1/services")["id"], True, by="user")
    assert (fetched["always"], fetched["widened"]["kind"], fetched["widened"]["value"]) == (True, "site", "api.render.com")
    assert [(s["domain"], s["by"]) for s in read_json(crew.dir / "team.json")["sites"]] == [("api.render.com", "user")]
    used = policy.answer_permission(crew, request_of(crew, "mcp__render__list_services")["id"], True, by="user")
    assert (used["always"], used["widened"]) == (True, {"kind": "tool", "value": "mcp__render__list_services",
                                                         "text": "members use mcp__render__list_services without asking"})
    assert read_json(crew.dir / "team.json")["tool_allow"] == ["mcp__render__list_services"]
    assert [(r["kind"], r["tool"], r["by"], r["on"]) for r in allow_rows(crew)] == [("tool", "mcp__render__list_services", "user", True)]
    assert fetch(crew, "https://api.render.com/v1/x")["behavior"] == "allow" and decide(crew, "mcp__render__list_services", id="s1")["behavior"] == "allow"


def test_a_plain_leader_allow_and_every_deny_widen_nothing(crew):
    for tool, tool_input in (("WebFetch", {"url": "https://docs.python.org/"}), ("mcp__srv__search", {"q": "x"}), ("Bash", {"command": "wc -c"})):
        allowed = policy.answer_permission(crew, request_of(crew, tool, **tool_input)["id"], True)
        denied = policy.answer_permission(crew, request_of(crew, tool, **tool_input)["id"], False, always=True, by="user")
        for r in (allowed, denied):
            assert (r["always"], r["widened"]) == (False, None), (tool, r["state"])
            stored = read_json(crew.dir / "perms" / f"{r['id']}.json")
            assert (stored["always"], stored["widened"]) == (False, None)
    cfg = read_json(crew.dir / "team.json")
    assert cfg.get("sites", []) == [] and cfg.get("tool_allow", []) == [] and cfg.get("exact_allow", []) == [] and allow_log(crew) == []


def test_an_address_that_is_no_domain_name_and_a_tool_of_deepcrews_are_never_added_by_an_answer(crew):
    for tool, tool_input in (("WebFetch", {"url": "https://169.254.169.254/latest/meta-data/"}), ("WebFetch", {"url": "http://localhost:8000/"}),
                             ("mcp__deepcrew__message", {"to": "leader"})):
        r = policy.answer_permission(crew, request_of(crew, tool, **tool_input)["id"], True, always=True, by="user")
        assert (r["state"], r["always"], r["widened"]) == ("allowed", False, None), tool_input
    assert crew.sites() == [] and read_json(crew.dir / "team.json").get("tool_allow", []) == []


def test_a_region_change_reaches_the_permission_answer_and_the_pre_hook_of_a_member_that_runs(crew, project):
    running = Board(crew.dir)
    src, docs = {"file_path": str(project / "src" / "a.py")}, {"file_path": str(project / "docs" / "guide.md")}

    def pre(tool_input, who="alpha"):
        return member.hook_pre(running, who, {"tool_name": "Write", "tool_input": tool_input})

    assert decide(running, "Write", **src)["behavior"] == "allow" and pre(src) is None
    assert decide(running, "Write", **docs)["behavior"] == "deny" and pre(docs)["hookSpecificOutput"]["permissionDecision"] == "deny"
    crew.set_region("beta", ["docs/other/"])
    crew.set_region("alpha", ["lib/", "docs/guide.md"])
    assert decide(running, "Write", **src)["behavior"] == "deny" and "no member's region holds it: ask the leader" in decide(running, "Write", **src)["message"]
    assert pre(src)["hookSpecificOutput"]["permissionDecision"] == "deny" and decide(running, "Write", **docs)["behavior"] == "allow" and pre(docs) is None
    assert decide(running, "Write", member="beta", **docs)["behavior"] == "deny"


def test_one_answer_ends_the_asking_for_a_site_and_for_a_tool_for_every_member(crew, clock):
    def answered(**how):
        clock.on_sleep = lambda: [policy.answer_permission(crew, r["id"], True, **how) for r in crew.pending("perms")]

    def requests():
        return len(list((crew.dir / "perms").glob("P*.json")))

    answered(by="user")
    assert policy.approve(crew, "alpha", "WebFetch", {"url": "https://docs.python.org/3/"}, "toolu_1")["behavior"] == "allow" and requests() == 1
    answered(always=True)
    assert policy.approve(crew, "alpha", "mcp__render__list", {"id": "s1"}, "toolu_2")["behavior"] == "allow" and requests() == 2
    clock.on_sleep = None
    for member_name, tool, tool_input in (("beta", "WebFetch", {"url": "https://docs.python.org/3/library/os.html"}), ("alpha", "WebFetch", {"url": "https://docs.python.org/"}),
                                          ("beta", "mcp__render__list", {"id": "s2"}), ("alpha", "mcp__render__list", {})):
        assert policy.approve(crew, member_name, tool, tool_input, f"toolu_{member_name}")["behavior"] == "allow", (member_name, tool)
    assert requests() == 2
    assert policy.approve(crew, "beta", "WebFetch", {"url": "https://pypi.org/"}, "toolu_x")["behavior"] == "deny" and requests() == 3
    assert policy.approve(crew, "beta", "mcp__render__delete", {"id": "s2"}, "toolu_y")["behavior"] == "deny" and requests() == 4


def test_link_folder_links_the_folder_and_adds_nothing_an_allow_would_not(crew, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "notes.txt").write_text("x\n", encoding="utf-8")
    ti = {"command": f"wc {outside.as_posix()}/notes.txt"}
    r = crew.request("perms", "alpha", {"tool": "Bash", "input": ti, "tool_use_id": None, "always": False, **policy.folder_wanted(crew, "Bash", ti, "alpha")})
    assert r["folder"] == os.path.realpath(outside) and policy.widening(crew, r) is None
    answered = policy.answer_permission(crew, r["id"], True, by="user", link=True)
    assert (answered["state"], answered["always"], answered["widened"]) == ("allowed", False, None)
    assert [f["path"] for f in crew.folders()] == [os.path.realpath(outside)]
    assert read_json(crew.dir / "team.json").get("exact_allow", []) == []


def test_the_null_device_and_the_standard_streams_are_no_place_outside(make_board):
    board = make_board()
    assert policy.outside(board, "curl -sS -o /dev/null https://example.com") == []
    assert policy.outside(board, "pytest --junitxml=/dev/stdout tests") == []
    assert policy.outside(board, "cat /dev/zero") == ["/dev/zero"]
