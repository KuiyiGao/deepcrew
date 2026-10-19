from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import sys
import threading
import time
from pathlib import Path
from string import Template

import pytest

from conftest import FAKE_TOML, PAGE_URL, PROVIDER
from deepcrew import cli, doctor, member, policy, presets, prompts, providers, settings, ui
from deepcrew.board import Board, read_json, state_home, team_path

KEY = "sk-" + "a" * 32
UP = ("up", "--provider", "fake", "--model", "m", "--effort", "default", "--offline")


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_inbox_names_the_page_through_inbox_page_url_which_the_tests_redirect_to_cli_page_url(deepcrew, project, providers_toml, monkeypatch):
    providers_toml()
    monkeypatch.setattr(cli, "page_url", lambda team_dir: PAGE_URL)
    assert deepcrew(*UP)[0] == 0
    assert f"setup: waiting for the user on the page {PAGE_URL}: " in deepcrew("inbox")[1]
    assert json.loads(deepcrew("inbox", "--json")[1])["setup"]["page_url"] == PAGE_URL


def test_up_asks_nothing_starts_the_page_first_and_waits_for_the_user_there(deepcrew, project, monkeypatch):
    started = []
    monkeypatch.setattr(cli, "start_page", lambda *args: started.append(args) or PAGE_URL)
    code, out, err = deepcrew("up", "--offline")
    lines, d, p = out.splitlines(), team_path(project, "main"), providers.load("deepseek")
    assert (code, err) == (0, "") and lines[0] == f"settings  {PAGE_URL}" and started == [(project, "main", d)]
    assert providers.choices() == {"provider": "deepseek", "model": p["models"][0], "effort": "high", "thinking": None}
    assert read_json(d / "team.json")["member"] == {"model": p["models"][0], "effort": "high", "thinking": True}
    assert f"key       not saved yet; it stays on this computer and goes only to {p['base_url']}" in lines
    assert "setup     waiting for the user on the page: save the deepseek API key; check the settings and press Done. Start members after the inbox says it is done" in lines
    board = Board(d)
    assert cli.setup_state(board)["missing"] == ["key", "confirm"]
    assert "setup: waiting for the user on the page" in deepcrew("inbox")[1]
    code, _, err = deepcrew("spawn", "alpha", "--region", "src/")
    assert code == 1 and "no deepseek key yet: the user saves it on the team's page" in err
    assert deepcrew("up", "--offline")[0] == 0 and cli.setup_state(board)["missing"] == ["key", "confirm"]
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    assert cli.setup_state(board)["missing"] == ["confirm"]
    with open(d / "run" / "ui-events.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time() + 1, "kind": "done", "values": {}}) + "\n")
    out = deepcrew("inbox")[1]
    assert "setup: done on the page" in out and cli.setup_state(board) is None
    lines = deepcrew("up", "--offline")[1].splitlines()
    assert not any(line.startswith("setup") for line in lines) and "key       saved in this team's private copy on this computer" in lines[6]


def test_up_creates_the_team_and_restores_the_members_key_copy_after_stop_all(deepcrew, project, providers_toml, add_member, monkeypatch):
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", KEY)
    code, out, err = deepcrew(*UP, "--allow", "du -sh", "--max-active", "2")
    d = team_path(project, "main")
    copy, cfg = d / "secret" / "fake.key", read_json(d / "team.json")
    assert (code, err) == (0, "") and "# deepcrew leader playbook" in out and cfg["bash_allow"] == ["du -sh"] and cfg["max_active"] == 2
    assert cfg["member"] == {"model": "m", "effort": None, "thinking": True} and cfg["provider"]["key_sources"] == [f"file:{copy}", "env:FAKE_KEY"]
    assert copy.read_text() == KEY and mode(copy) == 0o600 and KEY not in out + err + (d / "team.json").read_text()
    assert (d / "claude" / "tasks" / cfg["task_list"]).is_dir()
    assert cfg["shared_dir"] == str(project.parent / f"{project.name}-deepcrew" / "main") and (Path(cfg["shared_dir"]) / "notes").is_dir()
    add_member(Board(d), "alpha", ["src/"])
    assert deepcrew("stop", "--all") == (0, "alpha: was not running\n", "") and not copy.exists()
    assert deepcrew(*UP)[0] == 0 and copy.read_text() == KEY
    monkeypatch.delenv("FAKE_KEY")
    assert providers.resolve_key(Board(d).cfg["provider"]) == (f"file:{copy}", KEY)


def test_up_on_an_existing_team_checks_the_provider_it_runs_and_leaves_the_settings_alone(deepcrew, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    assert deepcrew(*UP)[0] == 0
    providers.env_file_set({"DEEPCREW_PROVIDER": "deepseek"})
    before = providers.ENV_FILE.read_text()
    code, out, _ = deepcrew("up", "--offline", "--json")
    data = json.loads(out)
    assert code == 0 and providers.ENV_FILE.read_text() == before
    assert {c["id"]: c["level"] for c in data["checks"]}["key"] == "ok" and data["setup"]["missing"] == ["confirm"]
    assert "model     m, effort default, thinking on" in data["summary"]
    code, _, err = deepcrew("up", "--offline", "--model", "deepseek-flash[1m]", "--effort", "max")
    assert code == 0 and "--model, --effort only apply when a team is created (it runs m)" in err and providers.ENV_FILE.read_text() == before


def test_a_first_up_that_fails_writes_no_settings_and_a_confirmed_team_is_not_asked_again(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    providers_toml(FAKE_TOML + 'balance_url = "https://fake.example/balance"\n')
    monkeypatch.setattr(providers, "balance", lambda p, key: {"ok": False, "status": 401, "detail": "invalid key"})
    code, out, _ = deepcrew(*UP[:-1])
    assert code == 1 and "invalid key" in out
    assert "fix: the key was rejected: save a valid key on the team page, or run `deepcrew auth set fake` in your own terminal" in out
    assert not providers.ENV_FILE.exists() and not team_path(project, "main").exists()
    assert json.loads(deepcrew(*UP, "--json")[1])["setup"]["missing"] == ["confirm"]
    with open(team_path(project, "main") / "run" / "ui-events.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time() + 1, "kind": "done", "values": {}}) + "\n")
    assert "setup: done on the page" in deepcrew("inbox")[1]
    providers.ENV_FILE.unlink()
    assert json.loads(deepcrew(*UP, "--json")[1])["setup"] is None


def test_up_points_an_existing_team_at_the_claude_python_and_package_it_runs_with(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    assert deepcrew(*UP)[0] == 0
    d = team_path(project, "main")
    stale = {**read_json(d / "team.json"), "claude": "/old/claude", "python": "/old/python3", "pkg_parent": "/old/site-packages"}
    cli.write_json(d / "team.json", stale)
    assert deepcrew(*UP)[0] == 0
    cfg = read_json(d / "team.json")
    assert (cfg["claude"], cfg["python"], cfg["pkg_parent"]) == (cli.claude_bin(), sys.executable, str(Path(cli.__file__).resolve().parent.parent))
    assert member.argv(Board(d), {"name": "alpha", "session": "s"}, resume=False)[0] == cli.claude_bin()


def test_up_refuses_a_team_folder_inside_the_project(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    for inside in (project / "notes", project):
        code, out, err = deepcrew(*UP, "--shared", inside)
        assert (code, out) == (1, "") and "is inside the project; give --shared a folder outside it" in err and not team_path(project, "main").exists()
    assert deepcrew(*UP, "--shared", project.parent / "elsewhere")[0] == 0
    assert read_json(team_path(project, "main") / "team.json")["shared_dir"] == str((project.parent / "elsewhere").resolve())
    code, _, err = deepcrew(*UP, "--shared", project / "notes")
    assert code == 0 and "--shared only apply when a team is created" in err
    with pytest.raises(ValueError, match="inside the project"):
        cli.create_team(project, "direct", PROVIDER, {"model": "m", "effort": None}, claude="/usr/bin/true", shared_dir=project / "x")


def test_up_fixes_the_prompt_pack_named_in_the_env_file_for_the_whole_team(deepcrew, project, providers_toml, monkeypatch, tmp_path):
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", KEY)
    (providers.CONFIG_DIR / "packs" / "concise").mkdir(parents=True)
    (providers.CONFIG_DIR / "packs" / "concise" / "leader.md").write_text("# concise playbook for $team")
    providers.env_file_set({"DEEPCREW_PROMPT_PACK": "concise"})
    code, out, _ = deepcrew(*UP)
    cfg = read_json(team_path(project, "main") / "team.json")
    assert (code, cfg["pack"]) == (0, "concise") and "# concise playbook for main" in out
    monkeypatch.setenv("DEEPCREW_PROMPT_PACK", str(tmp_path / "elsewhere"))
    assert prompts.text("leader", cfg) == "# concise playbook for $team" and prompts.text("member_system", cfg) == prompts.text("member_system", {})


def test_the_key_copy_exists_only_while_the_key_is_in_the_leaders_environment(board, monkeypatch):
    copy = cli.key_copy(board.dir, board.cfg["provider"])
    assert cli.refresh_key_copy(board) is False and not copy.exists()
    monkeypatch.setenv("FAKE_KEY", KEY)
    assert cli.refresh_key_copy(board) is True and copy.read_text() == KEY and mode(copy.parent) == 0o700
    monkeypatch.delenv("FAKE_KEY")
    assert cli.refresh_key_copy(board) is False and not copy.exists()


def check_of(project, check_id):
    return next(r for r in cli.host_checks(project) if r["id"] == check_id)


@pytest.mark.sandbox
def test_the_host_check_reports_claude_flags_the_state_home_and_the_git_state(project, tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "SANDBOX_TOOLS", dict.fromkeys(("darwin", "linux", sys.platform), (sys.executable,)))
    assert {r["id"]: r["level"] for r in cli.host_checks(project)} == {"python": "ok", "claude": "ok", "state": "ok", "git": "ok", "sandbox": "ok",
                                                                       "leader": "ok"}
    (project / "src" / "a.py").write_text("changed\n")
    assert check_of(project, "git")["level"] == "warn" and "uncommitted changes" in check_of(project, "git")["detail"]
    assert "inside the repository at" in check_of(project / "src", "git")["detail"]
    monkeypatch.setenv("DEEPCREW_HOME", str(project / ".deepcrew"))
    assert check_of(project, "state")["level"] == "fail" and "outside the project" in check_of(project, "state")["fix"]
    old = tmp_path / "old-claude"
    old.write_text("#!/bin/sh\ncase \"$1\" in --version) echo 0.0.1;; --help) echo 'Usage: claude --settings --tools';; esac\n")
    old.chmod(0o755)
    monkeypatch.setenv("DEEPCREW_CLAUDE", str(old))
    claude = check_of(project, "claude")
    assert claude["level"] == "fail" and "missing --setting-sources" in claude["detail"] and "--settings," not in claude["detail"] and claude["fix"] == "update Claude Code"
    monkeypatch.setattr(doctor, "claude_bin", lambda: None)
    assert check_of(project, "claude")["level"] == "fail" and "DEEPCREW_CLAUDE" in check_of(project, "claude")["fix"]


def test_the_sandbox_check_names_what_claude_codes_sandbox_needs_on_each_platform(project, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda tool: None if tool == "socat" else tool)
    assert cli.sandbox_check("darwin")["level"] == "ok"
    linux = cli.sandbox_check("linux")
    assert linux["level"] == "fail" and linux["detail"].endswith("missing socat") and linux["fix"].startswith("install socat, or run `deepcrew config set sandbox off`")
    monkeypatch.delitem(cli.SANDBOX_TOOLS, "win32", raising=False)
    assert cli.sandbox_check("win32")["level"] == "fail" and "WSL2" in cli.sandbox_check("win32")["fix"]
    monkeypatch.setenv("DEEPCREW_SANDBOX", "off")
    assert "sandbox" not in {r["id"] for r in cli.host_checks(project)}


def test_the_host_check_asks_claude_for_its_help_once(project, tmp_path):
    cli.host_checks(project)
    assert (tmp_path / "claude.calls").read_text().split().count("--help") == 1


def test_deepcrew_claude_may_name_claude_in_the_settings_file(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPCREW_CLAUDE")
    providers.env_file_set({"DEEPCREW_CLAUDE": str(tmp_path / "my-claude")})
    assert cli.claude_bin() == str(tmp_path / "my-claude")
    monkeypatch.setenv("DEEPCREW_CLAUDE", str(tmp_path / "other"))
    assert cli.claude_bin() == str(tmp_path / "other")


def test_members_start_with_the_claude_code_the_leader_runs_under(monkeypatch, tmp_path):
    paths = {}
    for name in ("leader", "recorded", "on-path", "node"):
        paths[name] = tmp_path / name / ("node" if name == "node" else "claude")
        paths[name].parent.mkdir()
        paths[name].write_text("#!/bin/sh\n")
        paths[name].chmod(0o755)
    leader, recorded, on_path, node = (str(paths[n]) for n in ("leader", "recorded", "on-path", "node"))
    monkeypatch.delenv("DEEPCREW_CLAUDE")
    monkeypatch.setenv("PATH", str(paths["on-path"].parent))
    assert (cli.claude_bin(), member.claude_command(recorded)) == (on_path, recorded)
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", leader)
    assert (cli.claude_bin(), member.claude_command(recorded)) == (leader, leader)
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", node)
    assert member.claude_command(recorded) == recorded
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", str(tmp_path / "gone" / "claude"))
    assert (member.claude_command(recorded), member.claude_command(str(tmp_path / "gone" / "claude"))) == (recorded, on_path)
    monkeypatch.setenv("DEEPCREW_CLAUDE", "/chosen/claude")
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", leader)
    assert member.claude_command(recorded) == "/chosen/claude"


def test_check_says_whether_the_leaders_claude_code_has_the_plugin_and_how_to_add_it(monkeypatch, tmp_path):
    claude, listing = tmp_path / "app dir" / "my-claude", tmp_path / "plugins.json"
    claude.parent.mkdir()
    claude.write_text(f"#!/bin/sh\n[ \"$1 $2 $3\" = 'plugin list --json' ] && cat '{listing}'\n")
    claude.chmod(0o755)
    listing.write_text("[]")
    assert doctor.plugin_check(str(claude)) is None
    monkeypatch.setenv("CLAUDECODE", "1")
    row = doctor.plugin_check(str(claude))
    assert row["level"] == "warn" and row["fix"].startswith(f'"{claude}" plugin marketplace add KuiyiGao/deepcrew, then "{claude}" plugin install')
    listing.write_text(json.dumps([{"id": "deepcrew@deepcrew", "enabled": False}]))
    assert doctor.plugin_check(str(claude))["fix"] == f'"{claude}" plugin enable deepcrew@deepcrew'
    listing.write_text(json.dumps([{"id": "other@x", "enabled": True}, {"id": "deepcrew@deepcrew", "enabled": True}]))
    assert doctor.plugin_check(str(claude)) == doctor.check("plugin", "ok", "deepcrew@deepcrew")
    listing.write_text("Unknown command: plugin")
    assert doctor.plugin_check(str(claude)) is None


def test_a_member_starts_with_the_leaders_claude_code_rather_than_the_one_the_team_recorded(crew, monkeypatch, tmp_path):
    leader = tmp_path / "app" / "claude"
    leader.parent.mkdir()
    leader.write_text("#!/bin/sh\n")
    leader.chmod(0o755)
    monkeypatch.delenv("DEEPCREW_CLAUDE")
    assert member.argv(crew, {"name": "alpha", "session": "s"}, resume=False)[0] == crew.cfg["claude"]
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", str(leader))
    assert member.argv(crew, {"name": "alpha", "session": "s"}, resume=False)[0] == str(leader)


@pytest.mark.sandbox
def test_check_deep_runs_its_probe_with_neutral_options_and_stops_it_when_it_does_not_finish(monkeypatch):
    providers.env_file_set({"DEEPCREW_PROJECT_MEMORY": "off", "DEEPCREW_ALLOW_OFF": "python3", "DEEPCREW_TOOLS_OFF": "WebSearch,Agent"})
    seen, stopped = {}, []

    def start(board, name, region, task=None, role=None, strength=None):
        seen.update(board.cfg)
        with board.locked():
            board.new_member(name, region, f"session-{name}")
        return board.update_member(name, state="active", pid=4242)

    monkeypatch.setattr(member, "start", start)
    monkeypatch.setattr(member, "stop", lambda board, name: stopped.append(name) or True)
    monkeypatch.setattr(member, "POLL_S", 0.05)
    monkeypatch.setattr(doctor, "CONFORMANCE_TIMEOUT_S", 0.4)
    r = cli.conformance("/usr/bin/true")
    assert r["level"] == "fail" and "the member was still working after" in r["detail"] and stopped == ["probe"]
    assert seen["project_memory"] is True and seen["allow_off"] == [] and seen["tools"] == list(cli.DEFAULT_TOOLS)
    assert seen["sandbox"] and seen["bash_allow"] == ["python3"]


def test_the_leader_playbook_names_only_commands_that_exist():
    root = Path(__file__).resolve().parent.parent
    texts = [prompts.text("leader")] + [p.read_text(encoding="utf-8") for p in (root / "mod/skills/deepcrew/SKILL.md",) if p.exists()]
    names = {n for t in texts for n in re.findall(r"(?:\$cmd|`deepcrew) ([a-z][a-z-]*)\b(?! playbook)", t)}
    assert {"post", "spawn", "approve", "region", "mcp", "skill"} <= names
    unknown = []
    for name in sorted(names):
        try:
            cli.parser().parse_args([name, "--help"])
        except SystemExit as e:
            unknown += [name] if e.code == 2 else []
    assert unknown == []


def test_config_show_and_set_validate_against_the_provider_and_store_in_the_env_file(deepcrew, providers_toml):
    shown = json.loads(deepcrew("config", "show", "--json")[1])
    assert {k: shown[k] for k in ("provider", "model", "effort")} == {"provider": None, "model": None, "effort": None} and shown["max_active"] == cli.DEFAULTS["max_active"]
    for argv in (("provider", "deepseek"), ("model", "deepseek-v4-pro[1m]"), ("effort", "default")):
        assert deepcrew("config", "set", *argv)[0] == 0
    assert providers.choices() == {"provider": "deepseek", "model": "deepseek-v4-pro[1m]", "effort": "default", "thinking": None} and mode(providers.ENV_FILE) == 0o600
    before = providers.ENV_FILE.read_text()
    for argv, message in ((("model", "gpt-9"), "model must be one of"), (("effort", "extreme"), "effort must be one of"), (("provider", "nope"), "unknown provider 'nope'"),
                          (("colour", "red"), "usage: deepcrew config set"), (("model",), "usage: deepcrew config set"), (("max_active", "0"), "positive whole number"),
                          (("max_active", "many"), "positive whole number")):
        code, out, err = deepcrew("config", "set", *argv)
        assert (code, out) == (1, "") and err.startswith("deepcrew: ") and message in err, argv
    assert providers.ENV_FILE.read_text() == before
    providers_toml()
    assert deepcrew("config", "set", "provider", "fake")[0] == 0
    assert "model must be one of: m" in deepcrew("config", "set", "model", "deepseek-flash[1m]")[2]


def test_team_settings_come_from_the_env_file_unless_given_explicitly(deepcrew, make_board):
    assert cli.DEFAULTS["note_max_chars"] == 20000
    assert deepcrew("config", "set", "max_active", "2")[0] == 0 and deepcrew("config", "set", "note_max_chars", "300")[0] == 0
    assert providers.env_file_get("DEEPCREW_MAX_ACTIVE") == "2" and json.loads(deepcrew("config", "show", "--json")[1])["max_active"] == 2
    board = make_board(max_active=7)
    assert (cli.user_settings()["max_active"], board.cfg["max_active"], board.note_max_chars()) == (2, 7, 300)
    assert deepcrew("config", "set", "note_max_chars", "0")[0] == 1


def test_a_team_has_up_to_eight_members_by_default_and_the_readme_marks_the_crowd_size_as_the_default(deepcrew):
    root = Path(__file__).resolve().parent.parent
    assert cli.DEFAULTS["max_active"] == 8 == int(presets.SIZES["crowd"]["values"]["DEEPCREW_MAX_ACTIVE"]) and presets.DEFAULTS["size"] == "crowd"
    assert json.loads(deepcrew("config", "show", "--json")[1])["max_active"] == 8
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert re.search(r"\bcrowd\b[^\n]{0,24}\bdefault\b", readme)
    assert [k for k, v in presets.SIZES.items() if "The default" in v["summary"]] == ["crowd"]


def test_deepcrew_allow_is_a_comma_separated_list_that_up_uses_unless_given_explicitly(deepcrew, project, providers_toml, monkeypatch):
    assert cli.allow_setting() == presets.SCOPES["standard"]["values"]["DEEPCREW_ALLOW"].split(",")
    providers.env_file_set({"DEEPCREW_ALLOW": ""})
    assert cli.allow_setting() == []
    monkeypatch.setenv("DEEPCREW_ALLOW", "ls, wc ,du -sh,,")
    assert cli.allow_setting() == ["ls", "wc", "du -sh"] and json.loads(deepcrew("config", "show", "--json")[1])["allow"] == ["ls", "wc", "du -sh"]
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", KEY)
    assert deepcrew(*UP)[0] == 0
    assert read_json(team_path(project, "main") / "team.json")["bash_allow"] == ["ls", "wc", "du -sh"]
    assert deepcrew(*UP, "--team", "other", "--allow", "cat")[0] == 0
    assert read_json(team_path(project, "other") / "team.json")["bash_allow"] == ["cat"]


def test_a_preset_writes_its_values_to_the_env_file_for_teams_created_afterwards(deepcrew):
    code, out, err = deepcrew("config", "preset", "crowd", "builder")
    assert (code, err) == (0, "") and "DEEPCREW_MAX_ACTIVE=8" in out and "DEEPCREW_ALLOW=ls,wc" in out
    assert providers.env_file_get("DEEPCREW_MAX_ACTIVE") == "8" and providers.env_file_get("DEEPCREW_OFFER_SIZE") == "8" and mode(providers.ENV_FILE) == 0o600
    assert cli.user_settings()["max_active"] == 8 and "python3" in cli.allow_setting()
    assert deepcrew("config", "preset", "fast", "locked")[0] == 0
    assert providers.env_file_get("DEEPCREW_MAX_ACTIVE") == "8" and providers.env_file_get("DEEPCREW_SANDBOX_DOMAINS") == "none" and cli.allow_setting() == []
    assert deepcrew("config", "preset", "solo")[0] == 0 and cli.user_settings()["max_active"] == 1
    assert cli.allow_setting() == [] and providers.env_file_get("DEEPCREW_SANDBOX_DOMAINS") == "none"


def test_a_setup_given_alone_sets_the_strength_the_size_and_the_scope_it_names(deepcrew):
    assert set(presets.SETUPS) == {"standard", "crowd", "strong", "locked"}
    assert presets.expand(["crowd"]) == ["fast", "crowd", "builder"] and presets.expand(["crowd", "builder"]) == ["crowd", "builder"]
    for name, setup in presets.SETUPS.items():
        assert deepcrew("config", "preset", name)[0] == 0, name
        want = presets.values(list(setup["presets"]), providers.load("deepseek"))
        assert all(providers.env_file_get(k) == (v or None) for k, v in want.items()), name
    assert providers.env_file_get("DEEPCREW_SANDBOX") == "on" and cli.allow_setting() == []
    assert "config preset standard|crowd|strong|locked, or" in deepcrew("config", "preset")[2]


def test_a_preset_that_does_not_exist_changes_nothing(deepcrew):
    deepcrew("config", "preset", "squad", "standard")
    before = providers.ENV_FILE.read_text()
    for argv, message in ((("preset",), "usage: deepcrew config preset"), (("preset", "huge"), "unknown preset 'huge'"),
                          (("preset", "solo", "wide-open"), "unknown preset 'wide-open'"), (("preset", "solo", "crowd"), "choose one size")):
        code, out, err = deepcrew("config", *argv)
        assert (code, out) == (1, "") and err.startswith("deepcrew: ") and message in err, argv
    assert providers.ENV_FILE.read_text() == before


@pytest.mark.sandbox
def test_the_sandbox_reaches_package_registries_unless_the_domain_list_is_changed(provider_table, monkeypatch):
    assert cli.sandbox_settings(provider_table)["network"]["allowedDomains"] == ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]
    monkeypatch.setenv("DEEPCREW_SANDBOX_DOMAINS", "none")
    assert cli.sandbox_settings(provider_table)["network"]["allowedDomains"] == []
    monkeypatch.setenv("DEEPCREW_SANDBOX_DOMAINS", "example.org, mirror.example.net")
    assert cli.sandbox_settings(provider_table)["network"]["allowedDomains"] == ["example.org", "mirror.example.net"]
    monkeypatch.setenv("DEEPCREW_SANDBOX", "off")
    assert cli.sandbox_settings(provider_table) is None


@pytest.mark.sandbox
def test_a_key_file_is_closed_to_members_commands_and_the_folder_that_holds_it_is_not(project, provider_table):
    p = {**provider_table, "key_sources": ["file:~/deepseek.key", "env:FAKE_KEY"]}
    closed = cli.protected_paths(p)
    assert str(Path.home() / "deepseek.key") in closed and str(Path.home()) not in closed
    assert str(Path.home()) not in cli.sandbox_settings(p, project)["filesystem"]["denyRead"]


def _age(board, tid, seconds):
    old = time.time() - seconds
    os.utime(board.tasks_dir / f"{tid}.json", (old, old))


def _seen_by_all(board, *tids):
    for m in board.members():
        board.mark(m["name"], list(tids), "skip", "offered; neither taken nor declined")


def test_post_prints_the_task_and_starts_nothing(lead, deepcrew, procs):
    code, out, err = deepcrew("post", "write parser", "--body", "details", "--scope", "lib/parser/", "--scope", "lib/x.py")
    assert (code, err) == (0, "") and out.startswith("posted 1: write parser  scope=['lib/parser/', 'lib/x.py']") and lead.task("1")["body"] == "details"
    assert "proposal" not in out
    _seen_by_all(lead, "1")
    _age(lead, "1", 61)
    status = deepcrew("status")[1]
    assert "proposal: nobody took 1 for 6" in status and "start write-parser for ['lib/parser/', 'lib/x.py']" in status
    assert "deepcrew spawn write-parser --region lib/parser/ --region lib/x.py --task 1 --role 'write parser. details'" in status
    md = {k: v for k, v in read_json(lead.tasks_dir / "1.json")["metadata"].items() if k != "posted" and not k.startswith("skip:")}
    assert md == {"scope": ["lib/parser/", "lib/x.py"], "by": "leader"}
    assert [m["name"] for m in lead.members()] == ["alpha", "beta"]
    assert sorted(c["member"]["name"] for c in procs.calls) == ["alpha", "beta"]
    assert json.loads(deepcrew("post", "u", "--no-route", "--json")[1])["routing"] is None
    for argv, message in ((["post", "t", "--dep", "99"], "unknown dependency: 99"), (["post", "  "], "title"), (["post", "t", "--scope", "/abs"], "region entries")):
        code, out, err = deepcrew(*argv, "--no-route")
        assert (code, out) == (1, "") and err.startswith("deepcrew: ") and message in err and len(lead.tasks()) == 2


def test_status_and_inbox_show_what_waits_for_the_leader(lead, deepcrew, post_task):
    post_task(lead, "new area", ["lib/"])
    post_task(lead, "crosses", ["src/a.py", "docs/x.md"])
    perm = lead.request("perms", "alpha", {"tool": "Bash", "input": {"command": "make all"}, "tool_use_id": None, "always": False})
    ask = lead.request("questions", "beta", {"question": "Ship it?", "options": ["yes", "no"], "task": None, "answer": None})
    lead.send("alpha", "leader", "message", "dispute: beta edits src/")
    _seen_by_all(lead, "1", "2")
    _age(lead, "1", 61)
    data = json.loads(deepcrew("status", "--json")[1])
    assert [g["name"] for g in data["proposals"]] == ["new-area"] and [b["reason"] for b in data["blocked"]] == ["scope crosses a region boundary"]
    assert [r["id"] for r in data["permission_requests"]] == [perm["id"]] and [q["id"] for q in data["questions"]] == [ask["id"]] and data["unread"] == 1
    text = deepcrew("status")[1]
    assert "start new-area for ['lib/']" in text and "blocked 2:" in text and "1 permission request(s), 1 question(s), 1 unread" in text
    assert "  1 open " in text and "  2 open " in text
    out = deepcrew("inbox")[1]
    assert f"permission {perm['id']} from alpha: Bash" in out and f"deepcrew approve {perm['id']}" in out and f"question {ask['id']} from beta" in out
    assert "options: yes | no" in out and "message from alpha: dispute: beta edits src/" in out and "start new-area" in out
    again = json.loads(deepcrew("inbox", "--json")[1])
    assert again["messages"] == [] and again["new"] == [] and len(lead.pending("perms")) == 1
    assert "(shown before)" in deepcrew("inbox", "--wait", "0")[1]


def test_the_inbox_asks_the_leader_to_choose_and_assign_gives_the_task(lead, deepcrew, post_task):
    t = post_task(lead, "Review everything")
    for name in ("alpha", "beta"):
        lead.update_member(name, state="active", pid=4242, offered=[t["id"]])
    for name in ("alpha", "beta"):
        member.task_rules(lead, name, "TaskUpdate", {"taskId": t["id"], "status": "in_progress"})
    lead.update_member("alpha", state="idle", pid=None)
    lead.update_member("beta", state="idle", pid=None)
    out = deepcrew("inbox")[1]
    assert f"choose: task {t['id']} (Review everything) was asked for by alpha, beta" in out and f"deepcrew assign {t['id']} NAME" in out
    code, out, err = deepcrew("assign", t["id"], "beta")
    assert (code, out) == (0, f"task {t['id']} assigned to beta; it comes as its next message\n") and lead.task(t["id"])["owner"] == "beta"
    code, out, err = deepcrew("assign", t["id"], "alpha")
    assert code == 1 and "claimed by beta" in err


def test_inbox_wait_returns_for_new_items_only_and_says_when_the_board_finished(lead, deepcrew, post_task, finish, monkeypatch):
    t = post_task(lead, "parse", ["src/p.py"])
    perm = lead.request("perms", "alpha", {"tool": "Bash", "input": {"command": "make"}, "tool_use_id": None, "always": False})
    assert json.loads(deepcrew("inbox", "--json")[1])["new"] == [f"perm:{perm['id']}"]
    started = time.monotonic()
    assert json.loads(deepcrew("inbox", "--wait", "1", "--json")[1])["new"] == [] and time.monotonic() - started >= 1
    policy.answer_permission(lead, perm["id"], allow=True, by="user")
    out = deepcrew("inbox")[1]
    assert f"the user answered {perm['id']} from alpha on the page: allowed" in out
    assert "the user answered" not in deepcrew("inbox")[1]
    lead.claim("alpha", t["id"])
    finish(lead, "alpha", t["id"])
    assert json.loads(deepcrew("inbox", "--wait", "5", "--json")[1])["finished"] is True and "finished: 1 of 1 tasks done" in deepcrew("inbox")[1]


def test_approve_and_answer_resolve_the_requests(lead, deepcrew):
    a, b, c = (lead.request("perms", "alpha", {"tool": "Bash", "input": {"command": cmd}, "tool_use_id": None, "always": False})
               for cmd in ("wc -c", "rm -rf x", "wc a && wc b"))
    assert deepcrew("approve", a["id"], "--always") == (0, f"{a['id']} allowed (added to the team allowlist)\n", "")
    assert deepcrew("approve", b["id"], "--deny")[1] == f"{b['id']} denied\n"
    assert deepcrew("approve", c["id"], "--always")[1] == f"{c['id']} allowed (nothing to widen: 'wc a && wc b' is not one simple command)\n"
    cfg = read_json(lead.dir / "team.json")
    assert cfg["exact_allow"] == ["wc -c"] and cfg["bash_allow"] == [] and cfg["approved_allow"] == [] and lead.pending("perms") == []
    assert read_json(lead.dir / "perms" / f"{a['id']}.json")["always"] is True and read_json(lead.dir / "perms" / f"{c['id']}.json")["always"] is False
    code, _, err = deepcrew("approve", a["id"])
    assert code == 1 and "already allowed" in err and deepcrew("approve", "P0000dead")[0] == 1
    q = lead.request("questions", "alpha", {"question": "Ship?", "options": [], "task": None, "answer": None})
    assert deepcrew("answer", q["id"], "   ")[0] == 1 and len(lead.pending("questions")) == 1
    assert deepcrew("answer", q["id"], "yes") == (0, f"{q['id']} answered; alpha continues\n", "")
    assert lead.pending("questions") == [] and read_json(lead.dir / "questions" / f"{q['id']}.json")["answer"] == "yes"


def test_note_commands_work_on_markdown_files_in_the_project(lead, deepcrew):
    code, out, err = deepcrew("note", "add", "API contract", "--text", "parse() returns tokens")
    path = lead.notes_dir / "leader-API-contract.md"
    assert (code, out, err) == (0, f"{providers.shown(path)}\n", "") and path.read_text() == "parse() returns tokens"
    (lead.notes_dir / "alpha-parser.md").write_text("written by a member with Write\n")
    assert deepcrew("note", "add", "no text")[0] == 1 and deepcrew("note", "add", "--text", "no topic")[0] == 1
    assert deepcrew("note", "list")[1] == "alpha-parser.md\nleader-API-contract.md\n"
    assert json.loads(deepcrew("note", "list", "--json")[1]) == {"notes": [str(lead.notes_dir / "alpha-parser.md"), str(path)]}
    assert deepcrew("note", "show", "leader-API-contract")[1] == "parse() returns tokens\n" and deepcrew("note", "show", "alpha-parser.md")[1].startswith("written by a member")
    code, out, err = deepcrew("note", "show", "nope")
    assert (code, out) == (1, "") and "no note 'nope'" in err and deepcrew("note", "show")[0] == 1
    assert deepcrew("note", "rm", "leader-API-contract")[1] == "leader-API-contract.md: removed\n" and not path.exists() and deepcrew("note", "rm", "leader-API-contract")[0] == 1
    assert deepcrew("note", "list")[1] == "alpha-parser.md\n" and deepcrew("note", "rm", "alpha-parser.md")[0] == 0 and deepcrew("note", "list")[1] == "no notes\n"


def test_send_mails_the_member_and_wakes_it_through_route_run(lead, deepcrew, monkeypatch):
    woken = []
    monkeypatch.setattr(member, "wake", lambda b, name: woken.append(name) or b.member(name))
    code, out, err = deepcrew("send", "alpha", "please look at src/", "--kind", "answer", "--task", "1")
    assert (code, out, err) == (0, "sent to alpha\n  resumed alpha\n", "") and woken == ["alpha"]
    (msg,) = lead.take_mail("alpha")
    assert (msg["from"], msg["kind"], msg["text"], msg["task"]) == ("leader", "answer", "please look at src/", "1")
    assert deepcrew("send", "all", "heads up")[1].startswith("sent to all") and [len(lead.mail_pending(n)) for n in ("alpha", "beta")] == [1, 1]
    code, _, err = deepcrew("send", "ghost", "hi")
    assert code == 1 and "unknown member 'ghost'; members: alpha, beta" in err
    assert deepcrew("send", "all", "  ")[0] == 1 and [len(lead.mail_pending(n)) for n in ("alpha", "beta")] == [1, 1]


def test_send_to_the_user_puts_it_on_the_page_in_the_language_the_user_reads(lead, deepcrew):
    code, out, err = deepcrew("send", "user", "Two of four tasks are done.")
    assert (code, out) == (0, "on the page for the user\n")
    (m,) = lead.take_mail("user")
    assert (m["from"], m["to"], m["text"]) == ("leader", "user", "Two of four tasks are done.")
    code, out, _ = deepcrew("lang", "simplified chinese")
    assert code == 0 and "the user reads replies in Simplified Chinese" in out and lead.live_cfg()["reply_lang"] == "Simplified Chinese"
    code, _, err = deepcrew("send", "user", "Two of four tasks are done.")
    assert code == 1 and "write it in Simplified Chinese" in err
    assert deepcrew("lang", "Klingon")[0] == 1 and "Simplified Chinese" in deepcrew("lang")[1]
    assert any(x.startswith("replies   in Simplified Chinese") for x in cli.summary(lead, None))


def test_spawn_starts_a_member_with_its_region_task_and_role(lead, deepcrew, post_task, procs):
    t = post_task(lead, "parse", ["lib/"])
    code, out, err = deepcrew("spawn", "libby", "--region", "lib/", "--task", t["id"], "--role", "owns the parser")
    assert (code, out, err) == (0, f"started libby region=['lib/'] task={t['id']}\n", "") and "owns the parser" in procs.calls[0]["prompt"]
    assert lead.member("libby")["state"] == "active" and lead.task(t["id"])["owner"] == "libby"
    code, _, err = deepcrew("spawn", "other", "--region", "lib/x/")
    assert code == 1 and "overlaps" in err and deepcrew("spawn")[0] == 1
    post_task(lead, "elsewhere", ["web/"])
    _seen_by_all(lead, "2")
    assert deepcrew("spawn", "--proposals")[1] == "started elsewhere region=['web/'] task=2\n" and deepcrew("spawn", "--proposals")[1] == "no proposals\n"


def test_forget_stops_and_removes_a_member_and_frees_its_region(lead, deepcrew, post_task):
    post_task(lead, "t", ["src/a.py"])
    lead.claim("alpha", "1")
    assert deepcrew("forget", "alpha") == (0, "alpha: removed; region ['src/'] is free for a new member\n", "")
    assert lead.member("alpha") is None and lead.task("1")["state"] == "open" and deepcrew("forget", "alpha")[:2] == (1, "alpha: no such member\n")


def test_cancel_and_route(lead, deepcrew, post_task):
    post_task(lead, "t", ["src/a.py"])
    assert deepcrew("cancel", "1", "--reason", "not needed") == (0, "1: cancelled\n", "")
    assert lead.task("1")["state"] == "cancelled" and lead.task("1")["summary"] == "cancelled by leader: not needed"
    assert deepcrew("cancel", "1")[:2] == (1, "1: task 1 is cancelled\n") and deepcrew("cancel", "99")[:2] == (1, "99: there is no task 99\n")
    assert deepcrew("route") == (0, "nothing to do\n", "")


def test_commands_need_an_existing_team_and_stop_needs_a_name(deepcrew):
    for argv in (["status"], ["inbox"], ["post", "t"], ["forget", "a"], ["stop", "--all"]):
        with pytest.raises(SystemExit) as e:
            deepcrew(*argv)
        assert "no team 'main'" in str(e.value) and "deepcrew up" in str(e.value), argv
    with pytest.raises(SystemExit, match="deepcrew stop NAME"):
        deepcrew("stop")


def test_audit_flags_what_the_edit_log_cannot_explain_including_non_ascii_file_names(lead, deepcrew, project):
    for rel, text in {"src/a.py": "A = 2\n", "src/héllo.py": "x\n", "docs/guide.md": "changed\n", "README.md": "changed\n", "scratch.txt": "x\n", "src/pkg/b.py": "B = 2\n"}.items():
        (project / rel).write_text(text)
    lead.note_write("leader", "plan", "notes live in the project but are not changes to it")
    for who, rel in (("alpha", "src/a.py"), ("alpha", "src/héllo.py"), ("alpha", "docs/guide.md"), ("beta", "README.md")):
        lead.log_edit(who, "Edit", rel)
    code, out, err = deepcrew("audit", "--json")
    data = json.loads(out)
    assert (code, err, data["base"], data["dirty_at_start"]) == (0, "", lead.cfg["git"]["base"], False)
    assert {f["path"]: (f["owner"], f["edited_by"], f["flags"]) for f in data["files"]} == {
        "README.md": (None, ["beta"], ["outside_every_region"]), "docs/guide.md": ("beta", ["alpha"], ["edited_by_non_owner"]),
        "scratch.txt": (None, [], ["outside_every_region", "no_logged_edit"]), "src/a.py": ("alpha", ["alpha"], []),
        "src/héllo.py": ("alpha", ["alpha"], []), "src/pkg/b.py": ("alpha", [], ["no_logged_edit"])}
    text = deepcrew("audit")[1]
    assert text.startswith(f"since {lead.cfg['git']['base'][:12]}\n") and "src/héllo.py" in text and "no_logged_edit" in text


def test_audit_skips_a_torn_line_of_the_edit_log_and_says_when_untracked_files_were_too_many(lead, make_board, deepcrew, project, monkeypatch):
    (project / "src" / "a.py").write_text("A = 2\n")
    lead.log_edit("alpha", "Edit", "src/a.py")
    with open(lead.dir / "edits.jsonl", "a", encoding="utf-8") as f:
        f.write('{"ts": 1, "member": "beta", "tool": "Ed')
    data = json.loads(deepcrew("audit", "--json")[1])
    assert [(f["path"], f["edited_by"]) for f in data["files"]] == [("src/a.py", ["alpha"])] and data["untracked_too_many"] is False
    monkeypatch.setattr(settings, "UNTRACKED_MAX", 1)
    monkeypatch.setattr(cli, "UNTRACKED_MAX", 1)
    for name in ("x.tmp", "y.tmp"):
        (project / name).write_text("made before the team")
    many = make_board("many", default_location=True)
    assert read_json(many.dir / cli.UNTRACKED_AT_START) == {"": "too many"}
    data = json.loads(deepcrew("audit", "--team", "many", "--json")[1])
    assert data["untracked_too_many"] is True and {"x.tmp", "y.tmp"} <= {f["path"] for f in data["files"]}
    assert "; more than 1 untracked files, so every untracked file is listed" in deepcrew("audit", "--team", "many")[1].splitlines()[0]


@pytest.mark.sandbox
def test_up_refuses_an_allowlist_entry_that_runs_code_only_without_the_sandbox(deepcrew, project, monkeypatch, providers_toml):
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", "sk-" + "a" * 32)
    monkeypatch.setenv("DEEPCREW_SANDBOX", "off")
    code, out, err = deepcrew(*UP, "--allow", "python3 -m pytest", "--allow", "./build.sh")
    assert (code, out) == (1, "") and "'python3 -m pytest' could run code and the sandbox is off" in err and "'./build.sh' could run code" in err
    assert not team_path(project, "main").exists()
    monkeypatch.delenv("DEEPCREW_SANDBOX")
    result = deepcrew(*UP, "--allow", "python3 -m pytest")
    assert result[0] == 0, result
    assert deepcrew(*UP, "--allow", "ls; rm -rf x", "--team", "other")[2] == "deepcrew: 'ls; rm -rf x' is not one simple command\n"


def test_the_sandbox_is_off_by_default_and_only_the_builder_and_locked_scopes_turn_it_on(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    assert not cli.sandbox_on() and cli.sandbox_settings(PROVIDER, project) is None
    assert {k: v["values"]["DEEPCREW_SANDBOX"] for k, v in presets.SCOPES.items()} == {"locked": "on", "standard": "off", "builder": "on"}
    assert deepcrew(*UP)[0] == 0
    cfg = read_json(team_path(project, "main") / "team.json")
    assert cfg["sandbox"] is None and "ls" in cfg["bash_allow"] and "python3" not in cfg["bash_allow"]
    assert deepcrew(*UP, "--allow", "python3 -m pytest", "--team", "other")[2].startswith("deepcrew: 'python3 -m pytest' could run code and the sandbox is off")


def test_without_a_sandbox_up_leaves_out_the_settings_commands_that_run_code_and_says_so(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    monkeypatch.delitem(cli.SANDBOX_TOOLS, "win32", raising=False)
    assert deepcrew("config", "preset", "builder")[0] == 0
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", "win32")
        code, out, _ = deepcrew(*UP)
        assert code == 3 and json.loads(out)["questions"][0]["id"] == "sandbox"
        code, _, err = deepcrew("config", "set", "sandbox", "off")
        assert code == 1 and "'python3' could run code" in err and providers.env_file_get("DEEPCREW_SANDBOX") == "on"
        assert deepcrew("config", "preset", "standard")[0] == 0 and deepcrew("config", "set", "sandbox", "off")[0] == 0
    assert deepcrew(*UP)[0] == 0 and read_json(team_path(project, "main") / "team.json")["sandbox"] is None
    providers.env_file_set({"DEEPCREW_ALLOW": presets.SCOPES["builder"]["values"]["DEEPCREW_ALLOW"]})
    code, out, err = deepcrew(*UP, "--team", "other")
    assert (code, err) == (0, "") and read_json(team_path(project, "other") / "team.json")["bash_allow"] == presets.SCOPES["standard"]["values"]["DEEPCREW_ALLOW"].split(",")
    assert ("commands  standard   (the sandbox is off, so these wait for approval: python3, pytest, node, npm test, npm run, make, cargo test, "
            "go test)") in out.splitlines()
    assert deepcrew(*UP, "--team", "other")[0] == 0
    code, out, err = deepcrew(*UP, "--team", "third", "--allow", "python3")
    assert (code, out) == (1, "") and "'python3' could run code and the sandbox is off" in err


@pytest.mark.parametrize("version, fix", [("9.9.9", ""), ("1.0.0", "update Claude Code"), ("2.1.133", "")])
def test_the_host_check_warns_only_below_the_oldest_claude_code_deepcrew_runs_on(project, tmp_path, monkeypatch, version, fix):
    stub = tmp_path / "claude-other"
    stub.write_text(f"#!/bin/sh\ncase \"$1\" in --version) echo '{version} (Claude Code)';; --help) echo 'Usage: claude {' '.join(cli.REQUIRED_FLAGS)}';; esac\n")
    stub.chmod(0o755)
    monkeypatch.setenv("DEEPCREW_CLAUDE", str(stub))
    r = check_of(project, "claude")
    assert r["level"] == ("warn" if fix else "ok")
    assert fix in r["fix"] and bool(r["fix"]) == bool(fix)


@pytest.mark.sandbox
def test_a_pack_can_add_allowlist_entries_and_set_the_member_tools(make_board, tmp_path):
    pack = tmp_path / "concise"
    pack.mkdir()
    (pack / "pack.json").write_text(json.dumps({"allow": ["node", "ls"], "tools": ["Read", "Edit", "TaskUpdate"]}))
    board = make_board(pack=str(pack), bash_allow=["ls", "wc"])
    assert board.cfg["bash_allow"] == ["ls", "wc", "node"] and board.cfg["tools"] == ["Read", "Edit", "TaskUpdate"]
    assert make_board(name="plain", pack=None).cfg["tools"] == list(cli.DEFAULT_TOOLS)
    (pack / "pack.json").write_text(json.dumps({"allow": "node"}))
    with pytest.raises(ValueError, match="allow"):
        make_board(name="bad", pack=str(pack))


@pytest.mark.sandbox
def test_sync_team_carries_only_the_settings_just_written(make_board):
    board = make_board(approval_timeout_s=900)
    providers.env_file_set({"DEEPCREW_HOLD_S": "120", "DEEPCREW_ALLOW": "ls,python3"})
    result = cli.sync_team(board, ["DEEPCREW_HOLD_S", "DEEPCREW_ALLOW"])
    cfg = json.loads((board.dir / "team.json").read_text(encoding="utf-8"))
    assert cfg["hold_s"] == 120 and cfg["approval_timeout_s"] == 900 and set(result["changed"]) == {"hold_s", "bash_allow"}
    assert cfg["sandbox"] and "python3" in cfg["bash_allow"] and board.cfg["hold_s"] == 120
    assert cli.sync_team(board, ["DEEPCREW_HOLD_S"])["changed"] == {}


@pytest.mark.sandbox
def test_the_advanced_settings_reach_the_team_and_keep_what_the_leader_approved(make_board, tmp_path, monkeypatch):
    board = make_board()
    rid = board.request("perms", "alpha", {"tool": "Bash", "input": {"command": "jq ."}, "tool_use_id": None, "always": False})["id"]
    policy.answer_permission(board, rid, True, always=True)
    secret = tmp_path / "secret-dir"
    secret.mkdir()
    providers.env_file_set({"DEEPCREW_ALLOW": "ls,make", "DEEPCREW_ALLOW_OFF": "ls", "DEEPCREW_TOOLS_OFF": "WebFetch,Bash",
                            "DEEPCREW_SANDBOX_DENY_READ": f"{secret},~/does-not-exist-here", "DEEPCREW_SANDBOX_ALLOW_WRITE": str(tmp_path / "cache")})
    keys = ["DEEPCREW_ALLOW", "DEEPCREW_ALLOW_OFF", "DEEPCREW_TOOLS_OFF", "DEEPCREW_SANDBOX_DENY_READ", "DEEPCREW_SANDBOX_ALLOW_WRITE"]
    changed = cli.sync_team(board, keys)["changed"]
    cfg = read_json(board.dir / "team.json")
    assert cfg["bash_allow"] == ["make"] and cfg["exact_allow"] == ["jq ."] and cfg["approved_allow"] == [] and set(changed) >= {"bash_allow", "tools"}
    assert "WebFetch" not in cfg["tools"] and "Bash" in cfg["tools"]
    assert cfg["sandbox"] and str(secret) in cfg["sandbox"]["filesystem"]["denyRead"] and cfg["sandbox"]["filesystem"]["allowWrite"] == [str(tmp_path / "cache")]
    assert not any("does-not-exist-here" in d for d in cfg["sandbox"]["filesystem"]["denyRead"])


def test_without_the_sandbox_only_read_only_programs_from_the_settings_and_the_pack_join_the_allowlist(make_board, tmp_path):
    pack = tmp_path / "concise"
    pack.mkdir()
    (pack / "pack.json").write_text(json.dumps({"allow": ["./run.sh", "wc -l"]}))
    providers.env_file_set({"DEEPCREW_SANDBOX": "off", "DEEPCREW_ALLOW": "ls,./build.sh,bin/rails server,jq .,make"})
    board = make_board(pack=str(pack), bash_allow=cli.allow_setting())
    assert board.cfg["sandbox"] is None and board.cfg["bash_allow"] == ["ls", "wc -l"]
    assert cli.left_out(board.cfg) == ["./build.sh", "bin/rails server", "jq .", "make", "./run.sh"]
    assert policy.decide(board, "alpha", "Bash", {"command": "./build.sh"}) is None
    providers.env_file_set({"DEEPCREW_SANDBOX": "on", "DEEPCREW_ALLOW": "ls,./build.sh,ls; rm -rf x"})
    on = make_board("on", pack=str(pack), bash_allow=cli.allow_setting())
    assert on.cfg["bash_allow"] == ["ls", "./build.sh", "./run.sh", "wc -l"] and cli.left_out(on.cfg) == ["ls; rm -rf x"]


def test_inbox_lists_a_members_question_that_comes_without_options(make_board, add_member, deepcrew):
    board = make_board("main", default_location=True, question_timeout_s=30)
    add_member(board, "alpha", ["src/"], "active")
    got = {}
    asking = threading.Thread(target=lambda: got.update(member.call(board, "alpha", "message", {"to": "leader", "text": "Ship it?", "wait": True})))
    asking.start()
    deadline = time.monotonic() + 5
    while not board.pending("questions") and time.monotonic() < deadline:
        time.sleep(0.05)
    (q,) = board.pending("questions")
    code, out, err = deepcrew("inbox")
    assert (code, err) == (0, "") and f"question {q['id']} from alpha (the member is waiting): Ship it?" in out and "options" not in out
    assert json.loads(deepcrew("inbox", "--json")[1])["questions"][0]["id"] == q["id"]
    assert deepcrew("answer", q["id"], "yes")[0] == 0
    asking.join(5)
    assert got == {"answer": "yes"}


def test_approve_answers_every_id_it_is_given_and_reports_the_ones_it_cannot(lead, deepcrew):
    def ask(cmd):
        return lead.request("perms", "alpha", {"tool": "Bash", "input": {"command": cmd}, "tool_use_id": None, "always": False})["id"]

    a, b, c, d = (ask(cmd) for cmd in ("wc -c", "du -sh", "ls docs", "ls src"))
    code, out, err = deepcrew("approve", a, "P0000dead", b, a)
    assert (code, out) == (1, f"{a} allowed\n{b} allowed\n") and err == "deepcrew: no request 'P0000dead'\n"
    assert sorted(r["id"] for r in lead.pending("perms")) == sorted([c, d])
    code, out, err = deepcrew("approve", c, d, "--deny")
    assert (code, out, err) == (0, f"{c} denied\n{d} denied\n", "") and lead.pending("perms") == []
    e, f = ask("wc -l"), ask("wc a && wc b")
    assert deepcrew("approve", e, f, "--always")[1] == (f"{e} allowed (added to the team allowlist)\n"
                                                         f"{f} allowed (nothing to widen: 'wc a && wc b' is not one simple command)\n")
    cfg = read_json(lead.dir / "team.json")
    assert cfg["exact_allow"] == ["wc -l"] and cfg["bash_allow"] == []
    with pytest.raises(SystemExit):
        deepcrew("approve")


def asked(board, tool, member="alpha", **tool_input):
    return board.request("perms", member, {"tool": tool, "input": tool_input, "tool_use_id": None, "always": False})["id"]


@pytest.mark.sandbox
def test_approve_always_says_what_it_put_in_force_and_why_it_put_nothing(lead, deepcrew, tmp_path):
    command, fetch, mcp = asked(lead, "Bash", command="npm run build"), asked(lead, "WebFetch", url="https://docs.python.org/3/"), asked(lead, "mcp__github__create_issue", title="x")
    code, out, err = deepcrew("approve", command, fetch, mcp, "--always")
    assert (code, err) == (0, "") and out.splitlines() == [f"{command} allowed (added to the team allowlist)",
                                                           f"{fetch} allowed (members reach docs.python.org without asking)",
                                                           f"{mcp} allowed (members use mcp__github__create_issue without asking)"]
    cfg = read_json(lead.dir / "team.json")
    assert cfg["exact_allow"] == ["npm run build"] and cfg["tool_allow"] == ["mcp__github__create_issue"] and lead.sites() == ["docs.python.org"]
    assert (cfg["sites"][0]["by"], read_json(lead.dir / "perms" / f"{fetch}.json")["widened"]["kind"]) == ("leader", "site")
    nothing = {asked(lead, "Edit", file_path="docs/x.md"): "Edit calls are answered one by one",
               asked(lead, "WebFetch", url="http://127.0.0.1:8000/"): "127.0.0.1 is an address, not a domain name, so it is approved call by call",
               asked(lead, "WebFetch", url="https://docs.python.org/3/library/"): "docs.python.org is among the team's sites already",
               asked(lead, "mcp__deepcrew__message", to="leader"): "'mcp__deepcrew__message' is one of deepcrew's own tools, which members use without asking already",
               asked(lead, "Bash", command="cat /etc/hosts"): "'cat /etc/hosts' names a place outside the project (/etc/hosts), so it is approved call by call"}
    for rid, why in nothing.items():
        assert deepcrew("approve", rid, "--always")[1] == f"{rid} allowed (nothing to widen: {why})\n", why
    plain, denied = asked(lead, "WebFetch", url="https://pypi.org/simple/"), asked(lead, "mcp__github__list_issues")
    assert deepcrew("approve", plain)[1] == f"{plain} allowed\n"
    assert deepcrew("approve", denied, "--deny", "--always")[1] == f"{denied} denied\n"
    after = read_json(lead.dir / "team.json")
    assert lead.sites() == ["docs.python.org"] and after["tool_allow"] == ["mcp__github__create_issue"] and after["exact_allow"] == ["npm run build"]


@pytest.mark.sandbox
def test_the_inbox_shows_what_always_would_do_and_offers_it_only_when_it_would_do_something(lead, deepcrew, tmp_path):
    command, fetch, mcp = asked(lead, "Bash", command="npm run build"), asked(lead, "WebFetch", url="https://docs.python.org/3/"), asked(lead, "mcp__github__create_issue")
    compound, edit = asked(lead, "Bash", command="wc a && wc b"), asked(lead, "Edit", file_path="docs/x.md")
    out = deepcrew("inbox")[1]
    assert f"permission {command} from alpha: Bash {{\"command\": \"npm run build\"}}\n  answer: deepcrew approve {command} [--deny] [--always: `npm run build` runs without asking from now on]\n" in out + "\n"
    assert f"  answer: deepcrew approve {fetch} [--deny] [--always: members reach docs.python.org without asking]\n" in out + "\n"
    assert f"  answer: deepcrew approve {mcp} [--deny] [--always: members use mcp__github__create_issue without asking]\n" in out + "\n"
    assert f"  answer: deepcrew approve {compound} [--deny]\n" in out + "\n" and f"  answer: deepcrew approve {edit} [--deny]\n" in out + "\n"
    folder = tmp_path / "data"
    folder.mkdir()
    linkable = lead.request("perms", "alpha", {"tool": "Read", "input": {"file_path": str(folder / "x")}, "tool_use_id": None, "always": False,
                                               "folder": str(folder), "write": False})["id"]
    out = deepcrew("inbox")[1]
    assert f"  answer: deepcrew approve {linkable} [--deny] [--link]\n  outside the project: {folder} (reading)" in out
    data = json.loads(deepcrew("inbox", "--json")[1])
    kinds = {r["id"]: (r["widening"] or {}).get("kind") for r in data["permission_requests"]}
    assert kinds == {command: "command", fetch: "site", mcp: "tool", compound: None, edit: None, linkable: None}
    assert sorted(data["new"]) == []
    first = json.loads(deepcrew("status", "--json")[1])["permission_requests"]
    assert {r["id"]: (r["widening"] or {}).get("kind") for r in first} == kinds


@pytest.mark.sandbox
def test_the_text_of_what_always_would_do_writes_home_paths_with_a_tilde(lead, deepcrew, project):
    rid = asked(lead, "Bash", command=f"npm test --prefix {project.as_posix()}")
    out = deepcrew("inbox")[1]
    assert Path.home().as_posix() in out.splitlines()[0]
    assert f"  answer: deepcrew approve {rid} [--deny] [--always: `npm test --prefix ~/proj` runs without asking from now on]" in out
    assert json.loads(deepcrew("inbox", "--json")[1])["permission_requests"][0]["widening"]["value"] == f"npm test --prefix {project.as_posix()}"


def test_the_inbox_keys_of_permission_requests_do_not_change_with_what_always_would_do(lead, deepcrew):
    rid = asked(lead, "WebFetch", url="https://docs.python.org/3/")
    assert json.loads(deepcrew("inbox", "--json")[1])["new"] == [f"perm:{rid}"]
    policy.set_site(lead, "docs.python.org")
    again = json.loads(deepcrew("inbox", "--json")[1])
    assert again["new"] == [] and "--always" not in deepcrew("inbox")[1]


def test_the_inbox_says_what_the_users_allow_put_in_force(lead, deepcrew):
    ids = {kind: asked(lead, tool, **ti) for kind, (tool, ti) in {
        "command": ("Bash", {"command": "wc -c"}), "site": ("WebFetch", {"url": "https://api.render.com/v1/x"}), "tool": ("mcp__render__list", {}),
        "none": ("Edit", {"file_path": "docs/x.md"})}.items()}
    for rid in ids.values():
        policy.answer_permission(lead, rid, True, by="user")
    out = deepcrew("inbox")[1]
    assert f"the user answered {ids['command']} from alpha on the page: allowed (added to the team allowlist): Bash" in out
    assert f"the user answered {ids['site']} from alpha on the page: allowed (members reach api.render.com without asking): WebFetch" in out
    assert f"the user answered {ids['tool']} from alpha on the page: allowed (members use mcp__render__list without asking): mcp__render__list" in out
    assert f"the user answered {ids['none']} from alpha on the page: allowed: Edit" in out
    legacy = asked(lead, "Bash", command="ls docs")
    policy.answer_permission(lead, legacy, True, by="user")
    path = lead.dir / "perms" / f"{legacy}.json"
    cli.write_json(path, {key: value for key, value in read_json(path).items() if key != "widened"})
    assert f"the user answered {legacy} from alpha on the page: allowed (added to the team allowlist): Bash" in deepcrew("inbox")[1]


def up_ready(providers_toml, monkeypatch):
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", KEY)


def test_one_claude_session_leads_one_team_until_stop_all_releases_it(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    assert deepcrew(*UP)[0] == 0
    leaders = state_home() / "leaders"
    rec = read_json(leaders / "session-1.json")
    assert {k: rec[k] for k in ("project", "team", "team_dir")} == {"project": str(project), "team": "main", "team_dir": str(team_path(project, "main"))}
    assert isinstance(rec["ts"], float)
    code, out, err = deepcrew(*UP, "--team", "other")
    assert (code, out) == (4, "") and err.count("\n") == 1 and "already leads team 'main'" in err
    assert shlex.join(["deepcrew", "stop", "--all", "--project", str(project), "--team", "main"]) in err
    assert not team_path(project, "other").exists()
    assert deepcrew(*UP)[0] == 0
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-2")
    assert deepcrew(*UP, "--team", "other")[0] == 0
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    assert deepcrew("stop", "--all", "--team", "other")[0] == 0 and (leaders / "session-1.json").exists()
    assert not (leaders / "session-2.json").exists()
    assert deepcrew(*UP, "--team", "other")[0] == 4
    assert deepcrew("stop", "--all")[0] == 0 and not (leaders / "session-1.json").exists()
    assert deepcrew(*UP, "--team", "other")[0] == 0 and read_json(leaders / "session-1.json")["team"] == "other"


def test_a_session_whose_team_is_gone_may_lead_another_and_a_terminal_has_no_lock(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    assert deepcrew(*UP)[0] == 0
    shutil.rmtree(team_path(project, "main"))
    assert deepcrew(*UP, "--team", "other")[0] == 0
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert deepcrew(*UP, "--team", "third")[0] == 0 and deepcrew(*UP, "--team", "fourth")[0] == 0 and not (state_home() / "leaders" / "None.json").exists()


def test_the_command_that_frees_the_session_quotes_each_word_for_the_shell(tmp_path, monkeypatch):
    odd = tmp_path / "it's $(here)" / "proj"
    led = tmp_path / "team"
    led.mkdir()
    (led / "team.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    cli.record_leader(odd, "main", led)
    refusal = cli.session_refusal(tmp_path / "other")
    assert shlex.split(refusal.split("finish it first: ", 1)[1]) == ["deepcrew", "stop", "--all", "--project", str(odd), "--team", "main"]


def test_a_proposed_spawn_command_runs_no_part_of_the_task_text_in_the_shell():
    role = "Fix `rm -rf ~` and $(id) in 'quotes'"
    (line,) = cli._proposal_lines([{"tasks": ["7"], "waited": 61, "name": "fix-rm", "region": ["src/a b/"], "role": role, "strength": "strong"}])
    command = line.splitlines()[-1].strip()
    assert shlex.split(command) == ["deepcrew", "spawn", "fix-rm", "--region", "src/a b/", "--task", "7", "--role", role, "--strength", "strong"]
    assert f"--role {shlex.quote(role)} " in command


def test_the_session_id_names_a_file_inside_the_leaders_folder(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "../../x/y")
    assert cli.session_id() == ".._.._x_y"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "  ")
    assert cli.session_id() is None


@pytest.mark.sandbox
def test_up_prints_the_summary_block_first_and_starts_the_page(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    started = []
    monkeypatch.setattr(cli, "start_page", lambda *args: started.append(args) or PAGE_URL)
    code, out, err = deepcrew(*UP)
    block = [f"settings  {PAGE_URL}", "team      main   (~/proj)", "members   up to 8, 0 running", "model     m, effort default, thinking on", "commands  standard",
             "sandbox   on", "key       saved in this team's private copy on this computer (mode 0600); it goes only to http://127.0.0.1:9",
             "reviewer  on: m looks at members' shell commands first and lets routine work on the project run without you",
             "budget    none set, $0.00 spent; the user can set one on the page",
             "setup     waiting for the user on the page: check the settings and press Done. Start members after the inbox says it is done"]
    assert (code, err) == (0, "") and out.splitlines()[:10] == block and out.splitlines()[10] == "" and started == [(project, "main", team_path(project, "main"))]
    assert deepcrew(*UP)[0] == 0 and len(started) == 2
    data = json.loads(deepcrew(*UP, "--json")[1])
    assert data["summary"] == block and data["page_url"] == PAGE_URL and "playbook" in data and data["setup"]["missing"] == ["confirm"]


def test_up_without_the_page_shows_the_server_that_runs_or_says_how_to_start_one(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    monkeypatch.setattr(cli, "start_page", lambda *args: pytest.fail("--no-ui starts no server"))
    assert deepcrew(*UP, "--no-ui")[1].splitlines()[0] == "settings  not running: deepcrew ui"
    monkeypatch.setattr(cli, "page_url", lambda team_dir: "http://127.0.0.1:5/?t=x")
    assert deepcrew(*UP, "--no-ui")[1].splitlines()[0] == "settings  http://127.0.0.1:5/?t=x"
    monkeypatch.setattr(cli, "start_page", lambda *args: None)
    assert deepcrew(*UP)[1].splitlines()[0] == "settings  not running: deepcrew ui"


def test_start_page_hands_the_team_to_the_ui_and_survives_its_failure(real_page, project, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ui, "start_background", lambda *args: calls.append(args) or PAGE_URL)
    assert real_page.start(project, "main", tmp_path) == PAGE_URL and calls == [(project, "main", tmp_path)]

    def boom(*args):
        raise OSError("no port")

    monkeypatch.setattr(ui, "start_background", boom)
    assert real_page.start(project, "main", tmp_path) is None
    assert "the settings page did not start (no port)" in capsys.readouterr().err


def test_page_url_is_the_address_of_the_server_that_runs(real_page, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "running", lambda team_dir: seen.append(team_dir) or {"url": PAGE_URL, "port": 1, "pid": 2, "started": 3.0})
    assert real_page.url(tmp_path) == PAGE_URL and seen == [tmp_path]
    monkeypatch.setattr(ui, "running", lambda team_dir: None)
    assert real_page.url(tmp_path) is None


@pytest.mark.sandbox
def test_summary_names_the_scope_or_lists_the_commands_and_what_a_pack_adds(make_board, add_member, tmp_path, monkeypatch):
    standard = presets.SCOPES["standard"]["values"]["DEEPCREW_ALLOW"].split(",")
    pack = tmp_path / "concise"
    pack.mkdir()
    (pack / "pack.json").write_text(json.dumps({"allow": ["node", "ls"]}))
    board = make_board(bash_allow=standard, pack=str(pack))
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"])
    assert board.cfg["pack_allow"] == ["node"]
    assert cli.summary(board, None)[0] == "settings  not running: deepcrew ui"
    assert cli.summary(board, None)[2:] == ["members   up to 8, 1 running", "model     m, effort default, thinking on", "commands  standard   (+ from pack concise: node)",
                                            "sandbox   on", "key       not saved yet; it stays on this computer and goes only to http://127.0.0.1:9",
                                            "reviewer  on: m looks at members' shell commands first and lets routine work on the project run without you",
                                            "budget    none set; the user can set one on the page"]
    assert cli.summary(make_board("a", bash_allow=["du -sh", "wc -l"]), "U")[4] == "commands  du -sh, wc -l" and cli.summary(make_board("b"), "U")[0] == "settings  U"
    assert cli.summary(make_board("c"), None)[4] == "commands  none, every shell command waits for you"
    builder = presets.SCOPES["builder"]["values"]["DEEPCREW_ALLOW"].split(",")
    assert cli.summary(make_board("d", bash_allow=builder), None)[4] == "commands  builder"
    monkeypatch.setenv("DEEPCREW_SANDBOX", "off")
    off = make_board("e", bash_allow=["ls", "python3 -m pytest", "du -sh"])
    assert cli.summary(off, None)[4:6] == ["commands  ls, du -sh   (the sandbox is off, so these wait for approval: python3 -m pytest)", "sandbox   off"]
    off.cfg["member"] = {"model": "deepseek-v4-pro[1m]", "effort": "max"}
    assert cli.summary(off, None)[3] == "model     deepseek-v4-pro[1m], effort max, thinking on"
    monkeypatch.setenv("FAKE_KEY", KEY)
    assert cli.summary(off, None)[6] == "key       saved in the environment variable FAKE_KEY; it stays on this computer and goes only to http://127.0.0.1:9"


def test_status_prints_the_summary_block_and_inbox_only_its_page_line(lead, deepcrew, monkeypatch):
    code, out, err = deepcrew("status")
    assert (code, err) == (0, "") and out.splitlines()[:9] == cli.summary(lead, None) and out.splitlines()[9:11] == ["", "tasks:"]
    assert "settings  not running: deepcrew ui" in out and "settings" not in deepcrew("inbox")[1]
    monkeypatch.setattr(cli, "page_url", lambda team_dir: PAGE_URL)
    data = json.loads(deepcrew("status", "--json")[1])
    assert data["page_url"] == PAGE_URL and data["summary"][0] == f"settings  {PAGE_URL}"
    assert deepcrew("inbox")[1] == f"nothing new\nsettings  {PAGE_URL}\n" and json.loads(deepcrew("inbox", "--json")[1])["page_url"] == PAGE_URL


def test_text_output_writes_home_paths_with_a_tilde_and_json_keeps_them(deepcrew, project, providers_toml, monkeypatch):
    up_ready(providers_toml, monkeypatch)
    (project / "CLAUDE.md").write_text("Use tabs.\n")
    out = deepcrew(*UP)[1]
    head = out.split("\n\n# ")[0]
    assert [line for line in head.splitlines() if str(Path.home()) in line] == []
    assert "team folder (notes, artifacts; members may read it): ~/proj-deepcrew/main" in head
    assert json.loads(deepcrew(*UP, "--json")[1])["shared_dir"] == str(project.parent / "proj-deepcrew" / "main")
    assert "project CLAUDE.md for members: CLAUDE.md" in deepcrew("instructions")[1]
    assert json.loads(deepcrew("instructions", "--json")[1])["project_memory"] == ["CLAUDE.md"]
    for argv in (["link"], ["check", "--offline"], ["config", "show"], ["note", "add", "plan", "--text", "x"], ["config", "set", "extensions", "none"]):
        assert str(Path.home()) not in deepcrew(*argv)[1], argv
    assert json.loads(deepcrew("link", "--json")[1])["tools"] == str(cli.tools_dir())


@pytest.mark.sandbox
def test_config_preset_and_set_change_the_team_that_runs_and_say_what_changed(lead, deepcrew):
    assert lead.cfg["max_active"] == 8 and lead.cfg["bash_allow"] == []
    code, out, err = deepcrew("config", "preset", "solo", "builder")
    cfg = read_json(lead.dir / "team.json")
    assert (code, err) == (0, "") and (cfg["max_active"], cfg["hold_s"], cfg["offer_size"]) == (1, 120, 3) and "python3" in cfg["bash_allow"]
    assert "team main: max_active 8 -> 1" in out and "team main: hold_s 600 -> 120" in out and "team main: bash_allow none -> ls, wc, du" in out
    assert "applies to teams created from now on" not in out and f"({providers.shown(providers.ENV_FILE)})" in out and "DEEPCREW_MAX_ACTIVE=1" in out
    code, out, _ = deepcrew("config", "set", "max_active", "3")
    assert (code, read_json(lead.dir / "team.json")["max_active"]) == (0, 3) and out.splitlines()[1:] == ["team main: max_active 1 -> 3"]
    assert deepcrew("config", "set", "max_active", "3")[1].splitlines()[1:] == ["team main: no change"]
    code, out, err = deepcrew("config", "set", "sandbox", "off")
    assert (code, out) == (1, "") and "'python3' could run code" in err and read_json(lead.dir / "team.json")["sandbox"]
    assert providers.env_file_get("DEEPCREW_SANDBOX") == "on"
    code, out, _ = deepcrew("config", "preset", "fast", "standard")
    cfg = read_json(lead.dir / "team.json")
    assert code == 0 and "python3" not in cfg["bash_allow"] and cfg["sandbox"] is None
    assert "team main: sandbox on, network pypi.org" in out and "-> off" in out
    assert deepcrew("config", "set", "sandbox", "off")[1].splitlines()[1:] == ["team main: no change"]
    assert deepcrew("config", "set", "sandbox", "on")[0] == 0 and read_json(lead.dir / "team.json")["sandbox"]
    assert Board(lead.dir).cfg["max_active"] == 3


def test_config_set_model_and_effort_reach_the_running_team(make_board, provider_table, providers_toml, deepcrew):
    providers_toml(FAKE_TOML.replace('models = ["m"]', 'models = ["m", "n"]').replace("efforts = []", 'efforts = ["low", "high"]'))
    board = make_board("main", default_location=True, provider={**provider_table, "models": ["m", "n"], "efforts": ["low", "high"]})
    assert deepcrew("config", "set", "provider", "fake")[0] == 0
    assert deepcrew("config", "set", "model", "n")[1].splitlines()[1:] == ["team main: member m, effort default, thinking on -> n, effort default, thinking on"]
    assert deepcrew("config", "set", "effort", "high")[1].splitlines()[1:] == ["team main: member n, effort default, thinking on -> n, effort high, thinking on"]
    assert deepcrew("config", "set", "thinking", "off")[1].splitlines()[1:] == ["team main: member n, effort high, thinking on -> n, effort high, thinking off"]
    assert read_json(board.dir / "team.json")["member"] == {"model": "n", "effort": "high", "thinking": False}


def test_config_without_a_team_says_it_applies_to_teams_created_later(deepcrew):
    for argv in (("set", "max_active", "2"), ("set", "sandbox", "off"), ("preset", "solo")):
        code, out, err = deepcrew("config", *argv)
        assert (code, err) == (0, "") and out.rstrip().endswith("applies to teams created from now on)") and "team main" not in out, argv


@pytest.mark.sandbox
def test_config_show_names_every_setting_config_set_writes(deepcrew, monkeypatch):
    monkeypatch.delenv("DEEPCREW_BROWSER")
    data = json.loads(deepcrew("config", "show", "--json")[1])
    assert {k: data[k] for k in ("thinking", "project_memory", "sandbox", "local_ports", "browser")} == {
        "thinking": "on", "project_memory": "on", "sandbox": "on", "local_ports": "off", "browser": "auto"} and data["deny_read"] == list(cli.DENY_READ)
    for argv in (("thinking", "off"), ("deny_read", "~/secrets,~/keys"), ("local_ports", "on"), ("browser", "off")):
        assert deepcrew("config", "set", *argv)[0] == 0, argv
    assert {"thinking: off", "deny_read: ~/secrets, ~/keys", "local_ports: on", "browser: off"} <= set(deepcrew("config", "show")[1].splitlines())
    code, _, err = deepcrew("config", "set", "deny_read", "secrets")
    assert code == 1 and "not an absolute path" in err and providers.env_file_get("DEEPCREW_SANDBOX_DENY_READ") == "~/secrets,~/keys"


def test_config_set_browser_says_members_take_it_rather_than_that_nothing_changed(lead, deepcrew, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPCREW_BROWSER")
    browser = tmp_path / "chrome"
    browser.write_text("a browser")
    code, out, _ = deepcrew("config", "set", "browser", str(browser))
    assert (code, out) == (0, f"browser = ~/chrome  ({providers.shown(providers.ENV_FILE)}; members take it when they next start)\n")
    assert providers.env_file_get("DEEPCREW_BROWSER") == str(browser) and "browser: ~/chrome" in deepcrew("config", "show")[1].splitlines()


def test_the_key_question_writes_nothing_and_names_the_settings_file_with_a_tilde(deepcrew):
    q = cli.key_question(providers.load("deepseek"))
    assert not providers.ENV_FILE.exists() and q["answer_with"] == (
        "tell the user: save a valid key on the team page, or run `deepcrew auth set deepseek` in your own terminal; "
        "or write DEEPSEEK_API_KEY=<key> into ~/.config/deepcrew/.env (mode 0600, outside every repository)")
    code, out, _ = deepcrew("auth", "set", "deepseek")
    assert code == 3 and json.loads(out)["questions"] == [q] and providers.env_file_has("DEEPSEEK_API_KEY")


def test_spawn_refuses_names_that_are_not_good_taken_or_used_before(lead, deepcrew, procs, post_task):
    for bad in ("Physics", "9lives", "a" * 25, "with_underscore", "x"):
        code, out, err = deepcrew("spawn", bad, "--region", "lib/")
        assert (code, out) == (1, "") and '2-24 characters of a-z, 0-9 and \'-\'' in err and '"physics-engine"' in err and procs.calls == [], bad
    code, out, err = deepcrew("spawn", "alpha", "--region", "lib/")
    assert code == 1 and "member 'alpha' exists: it wakes by itself on new work in its region or on mail" in err and 'deepcrew send alpha "..."' in err
    assert deepcrew("spawn", "physics-engine", "--region", "lib/")[0] == 0 and read_json(lead.dir / "names.json") == ["physics-engine"]
    lead.update_member("physics-engine", state="idle", pid=None)
    assert deepcrew("forget", "physics-engine")[0] == 0 and read_json(lead.dir / "names.json") == ["physics-engine"]
    code, _, err = deepcrew("spawn", "physics-engine", "--region", "lib/")
    assert code == 1 and "was used earlier in this team" in err and len(procs.calls) == 1
    _seen_by_all(lead, post_task(lead, "Physics engine", ["web/"])["id"])
    assert [g["name"] for g in route_plan(lead)] == ["physics-engine-2"]


def route_plan(board):
    from deepcrew import route
    return route.plan(board)["proposals"]


def test_spawn_stops_at_the_teams_member_limit_and_config_raises_it(make_board, add_member, deepcrew, procs, post_task):
    board = make_board("main", default_location=True, max_active=3)
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    assert deepcrew("spawn", "gamma", "--region", "lib/")[0] == 0
    code, out, err = deepcrew("spawn", "delta", "--region", "web/")
    assert (code, out) == (1, "") and "team 'main' already has 3 members, its limit" in err and "deepcrew config set max_active" in err and len(procs.calls) == 1
    assert deepcrew("config", "set", "max_active", "4")[0] == 0 and deepcrew("spawn", "delta", "--region", "web/")[0] == 0 and len(procs.calls) == 2


def test_spawn_proposals_starts_what_fits_and_says_what_is_left(make_board, add_member, deepcrew, procs, post_task):
    board = make_board("main", default_location=True, max_active=3)
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    post_task(board, "first job", ["lib/"])
    post_task(board, "second job", ["web/"])
    _seen_by_all(board, "1", "2")
    code, out, err = deepcrew("spawn", "--proposals")
    assert (code, err) == (0, "") and out == "started first-job region=['lib/'] task=1\n1 proposal(s) not started: the team is at its limit of 3 members\n"
    code, _, err = deepcrew("spawn", "--proposals")
    assert code == 1 and "already has 3 members, its limit" in err


def test_a_prompt_pack_with_a_stray_dollar_is_refused_naming_the_file_and_line(deepcrew, project, providers_toml, monkeypatch, tmp_path):
    up_ready(providers_toml, monkeypatch)
    pack = tmp_path / "bad"
    pack.mkdir()
    (pack / "member_system.md").write_text("fine: $team ${notes} costs $$5")
    (pack / "leader.md").write_text("# playbook\n\nrun $(date) to see the time\n")
    monkeypatch.setenv("DEEPCREW_PROMPT_PACK", str(pack))
    code, out, err = deepcrew(*UP)
    assert (code, out) == (1, "") and err == "deepcrew: prompt pack bad, leader.md line 3: invalid placeholder '$('; write $$ for a literal $\n"
    assert not team_path(project, "main").exists()
    (pack / "leader.md").write_text("# playbook\n\nrun $$(date) for $team")
    assert deepcrew(*UP)[0] == 0
    (pack / "task_open.md").write_text("costs $5")
    with pytest.raises(ValueError, match=r"task_open\.md line 1: invalid placeholder '\$5'"):
        prompts.check_pack(str(pack))
    (pack / "task_open.md").write_text("ends with $")
    with pytest.raises(ValueError, match=r"line 1: invalid placeholder '\$'"):
        prompts.check_pack(str(pack))
    assert prompts.check_pack(None) is None and prompts.check_pack(str(prompts.BASIC)) is None


def stub_ui(monkeypatch, done=None, start=None):
    calls = []

    def start_background(project, team, team_dir, timeout_s=28800.0):
        calls.append(("start", project, team, team_dir))
        if start:
            raise start
        return PAGE_URL

    monkeypatch.setattr(ui, "start_background", start_background)
    monkeypatch.setattr(ui, "wait_done", lambda team_dir, timeout_s: calls.append(("wait", team_dir, timeout_s)) or done)
    monkeypatch.setattr(ui, "serve_team", lambda project, team, timeout_s: calls.append(("serve", project, team, timeout_s)))
    monkeypatch.setattr(ui, "serve", lambda **kw: calls.append(("page", kw["project"], kw["team"])) or {"done": True})
    return calls


def test_ui_with_a_team_starts_its_server_prints_the_url_and_the_confirmed_settings(lead, deepcrew, project, monkeypatch):
    done = {"ts": 1.0, "kind": "done", "values": {"DEEPCREW_MODEL": "m", "DEEPCREW_MAX_ACTIVE": "3", "DEEPSEEK_API_KEY": "sk-hidden"}, "changed": {"max_active": [8, 3]}}
    calls = stub_ui(monkeypatch, done)
    code, out, err = deepcrew("ui", "--timeout", "5")
    assert (code, err) == (0, "")
    assert out.splitlines() == [PAGE_URL, "settings confirmed", "  DEEPCREW_MODEL=m", "  DEEPCREW_MAX_ACTIVE=3", "team main: max_active 8 -> 3"] and "sk-hidden" not in out
    assert calls == [("start", project, "main", lead.dir), ("wait", lead.dir, 5.0)]
    first, rest = deepcrew("ui", "--json")[1].split("\n", 1)
    assert json.loads(first) == {"url": PAGE_URL} and json.loads(rest) == {"url": PAGE_URL, "done": True, "values": {"DEEPCREW_MODEL": "m", "DEEPCREW_MAX_ACTIVE": "3"},
                                                                          "changed": {"max_active": [8, 3]}}


def test_ui_without_waiting_or_without_done(lead, deepcrew, monkeypatch):
    calls = stub_ui(monkeypatch, None)
    assert deepcrew("ui", "--no-wait") == (0, f"{PAGE_URL}\n", "") and [c[0] for c in calls] == ["start"]
    assert deepcrew("ui")[1] == f"{PAGE_URL}\ntimed out without Done\n" and calls[-1] == ("wait", lead.dir, 1800.0)
    stub_ui(monkeypatch, None, start=OSError("no port"))
    code, out, err = deepcrew("ui")
    assert (code, out) == (1, "") and err == "deepcrew: the settings server did not start: no port\n"


def test_ui_serve_runs_the_teams_server_in_this_process_and_a_page_without_a_team_ends_at_done(lead, deepcrew, project, monkeypatch):
    calls = stub_ui(monkeypatch)
    assert deepcrew("ui", "--serve", "--timeout", "60") == (0, "", "") and calls == [("serve", project, "main", 60.0)]
    assert "--serve" not in cli.parser()._subparsers._group_actions[0].choices["ui"].format_help()
    del calls[:]
    out = deepcrew("ui", "--team", "nobody")[1]
    assert out == f"saved: {providers.shown(providers.ENV_FILE)}\n" and calls == [("page", project, "nobody")]
    del calls[:]
    assert deepcrew("ui", "--serve", "--team", "nobody", "--timeout", "60")[0] == 0 and calls == [("serve", project, "nobody", 60.0)]


def test_a_prompt_pack_placeholder_deepcrew_does_not_fill_is_refused(tmp_path):
    pack = tmp_path / "typo"
    pack.mkdir()
    (pack / "leader.md").write_text("# playbook\n\nYou lead $team for $owner\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"leader\.md line 3: \$owner is not filled here \(this template gets .*\$team"):
        prompts.check_pack(str(pack))
    (pack / "leader.md").write_text("# playbook\n\nYou lead ${team} on $project\n", encoding="utf-8")
    prompts.check_pack(str(pack))


def test_the_basic_pack_uses_only_placeholders_deepcrew_fills():
    for name in prompts.NAMES:
        raw = (prompts.BASIC / f"{name}.md").read_text(encoding="utf-8")
        used = {m.group("named") or m.group("braced") for m in Template.pattern.finditer(raw)} - {None}
        assert used <= prompts.FILLED[name], name


@pytest.fixture
def allowing(make_board, tmp_path):
    pack = tmp_path / "concise"
    pack.mkdir()
    (pack / "pack.json").write_text(json.dumps({"allow": ["du -sh"]}))
    return make_board("main", default_location=True, pack=str(pack), bash_allow=["ls", "wc -l"])


def listed_commands(deepcrew):
    return {r["command"]: r["source"] for r in json.loads(deepcrew("allow", "--json")[1])["allow"]}


def test_allow_lists_each_command_with_where_it_came_from(allowing, deepcrew):
    code, out, err = deepcrew("allow")
    assert (code, err) == (0, "") and out.splitlines() == ["  ls                           settings", "  wc -l                        settings",
                                                           "  du -sh                       pack"]
    rid = allowing.request("perms", "alpha", {"tool": "Bash", "input": {"command": "wc -c"}, "tool_use_id": None, "always": False})["id"]
    policy.answer_permission(allowing, rid, True, by="user")
    assert deepcrew("allow", "cat")[0] == 0
    assert listed_commands(deepcrew) == {"ls": "settings", "wc -l": "settings", "du -sh": "pack", "cat": "approved by leader", "wc -c": "approved by user, exactly this command"}
    assert list(listed_commands(deepcrew))[-1] == "wc -c"
    assert cli.allow_sources(allowing) == [{"command": c, "source": s} for c, s in listed_commands(deepcrew).items()]
    assert [(r["command"], r["by"], r["on"], r["exact"]) for r in read_jsonl_rows(allowing)] == [("wc -c", "user", True, True), ("cat", "leader", True, False)]


def read_jsonl_rows(board):
    return [json.loads(line) for line in (board.dir / "run" / policy.ALLOW_LOG).read_text().splitlines()]


def test_allow_adds_and_removes_commands_and_the_members_calls_follow(allowing, deepcrew):
    cli.write_json(allowing.dir / "run" / "settings.json", member.settings(allowing))
    before = (allowing.dir / "run" / "settings.json").read_text()

    def allowed(cmd):
        return (policy.decide(allowing, "alpha", "Bash", {"command": cmd}) or {}).get("behavior") == "allow"

    assert not allowed("cat README.md") and not allowed("wc -c README.md")
    assert deepcrew("allow", "cat", "wc -c")[0] == 0 and allowed("cat README.md") and allowed("wc -c README.md")
    assert (allowing.dir / "run" / "settings.json").read_text() == before
    assert deepcrew("allow", "cat", "--remove")[0] == 0 and not allowed("cat README.md") and allowed("wc -c README.md")
    assert "cat" not in listed_commands(deepcrew) and "cat" not in read_json(allowing.dir / "team.json")["allow_off"]
    assert [(r["command"], r["by"], r["on"]) for r in read_jsonl_rows(allowing)] == [("cat", "leader", True), ("wc -c", "leader", True), ("cat", "leader", False)]
    assert deepcrew("allow", "cat")[0] == 0 and listed_commands(deepcrew)["cat"] == "approved by leader"


def test_allow_refuses_what_cannot_be_listed_and_says_why_with_exit_status_1(allowing, deepcrew, monkeypatch):
    code, out, err = deepcrew("allow", "ls; rm -rf x", "wc a && wc b", "echo $HOME", "  ")
    assert code == 1 and out.count("is not one simple command") == 4 and err == ""
    assert set(listed_commands(deepcrew)) == {"ls", "wc -l", "du -sh"}
    code, out, _ = deepcrew("allow", "not-there", "--remove")
    assert code == 1 and "'not-there' is not on the allowlist" in out
    data = json.loads(deepcrew("allow", "ls; x", "wc -c", "--json")[1])
    assert data["problems"] == ["'ls; x' is not one simple command"] and "wc -c" in {r["command"] for r in data["allow"]}
    cfg = read_json(allowing.dir / "team.json")
    cfg["sandbox"] = None
    cli.write_json(allowing.dir / "team.json", cfg)
    code, out, _ = deepcrew("allow", "make test")
    assert code == 1 and "'make test' could run code and the sandbox is off, so it is approved call by call" in out and "make test" not in listed_commands(deepcrew)


def test_removing_an_entry_of_the_settings_or_the_pack_keeps_it_off_when_the_settings_are_synced(allowing, deepcrew):
    providers.env_file_set({"DEEPCREW_ALLOW": "ls,wc -l"})
    assert deepcrew("allow", "ls", "du -sh", "--remove")[0] == 0 and deepcrew("allow", "cat")[0] == 0
    cfg = read_json(allowing.dir / "team.json")
    assert cfg["allow_off"] == ["ls", "du -sh"] and listed_commands(deepcrew) == {"wc -l": "settings", "cat": "approved by leader"}
    for keys in (["DEEPCREW_ALLOW"], ["DEEPCREW_SANDBOX"], ["DEEPCREW_ALLOW", "DEEPCREW_HOLD_S"]):
        cli.sync_team(allowing, keys)
        assert listed_commands(deepcrew) == {"wc -l": "settings", "cat": "approved by leader"}, keys
    assert read_json(allowing.dir / "team.json")["allow_off"] == ["ls", "du -sh"]
    assert deepcrew("allow", "ls")[0] == 0 and "ls" in listed_commands(deepcrew) and "ls" not in read_json(allowing.dir / "team.json")["allow_off"]
    providers.env_file_set({"DEEPCREW_ALLOW_OFF": "wc -l"})
    cli.sync_team(allowing, ["DEEPCREW_ALLOW_OFF"])
    assert "wc -l" not in listed_commands(deepcrew)
    providers.env_file_set({"DEEPCREW_ALLOW_OFF": ""})
    cli.sync_team(allowing, ["DEEPCREW_ALLOW_OFF"])
    assert "wc -l" in listed_commands(deepcrew)


def test_allow_lists_adds_and_removes_the_mcp_tools_members_use_without_asking(allowing, deepcrew):
    github = "mcp__github__create_issue"
    code, out, err = deepcrew("allow", "--tool", github, "--tool", "mcp__github__list_issues")
    assert (code, err) == (0, "") and out.splitlines() == [f"  {'ls':28} settings", f"  {'wc -l':28} settings", f"  {'du -sh':28} pack",
                                                           f"  {github:28} approved by leader", f"  {'mcp__github__list_issues':28} approved by leader"]
    rid = allowing.request("perms", "alpha", {"tool": "mcp__render__list", "input": {}, "tool_use_id": None, "always": False})["id"]
    policy.answer_permission(allowing, rid, True, by="user")
    data = json.loads(deepcrew("allow", "--json")[1])
    assert data["tools"] == [{"tool": github, "source": "approved by leader"}, {"tool": "mcp__github__list_issues", "source": "approved by leader"},
                             {"tool": "mcp__render__list", "source": "approved by user"}] and data["problems"] == []
    assert [r["command"] for r in data["allow"]] == ["ls", "wc -l", "du -sh"]
    assert (policy.decide(allowing, "alpha", github, {}) or {}).get("behavior") == "allow"
    code, out, _ = deepcrew("allow", "--tool", github, "--remove")
    assert code == 0 and github not in out and policy.decide(allowing, "alpha", github, {}) is None
    assert [(r.get("kind"), r.get("tool"), r["by"], r["on"]) for r in read_jsonl_rows(allowing) if r.get("kind")] == [
        ("tool", github, "leader", True), ("tool", "mcp__github__list_issues", "leader", True), ("tool", "mcp__render__list", "user", True), ("tool", github, "leader", False)]


def test_allow_refuses_a_tool_that_is_none_and_still_does_the_rest(allowing, deepcrew):
    code, out, _ = deepcrew("allow", "cat", "--tool", "Bash", "--tool", "mcp__deepcrew__message", "--tool", "mcp__a__b")
    assert code == 1 and "'Bash' is not the name of an MCP tool, such as mcp__github__create_issue" in out
    assert "'mcp__deepcrew__message' is one of deepcrew's own tools, which members use without asking already" in out
    assert listed_commands(deepcrew)["cat"] == "approved by leader" and [t["tool"] for t in json.loads(deepcrew("allow", "--json")[1])["tools"]] == ["mcp__a__b"]
    code, out, _ = deepcrew("allow", "--tool", "mcp__not__there", "--remove")
    assert code == 1 and "'mcp__not__there' is not on the list of tools members use without asking" in out


def test_allow_says_empty_only_when_there_are_neither_commands_nor_tools(make_board, deepcrew):
    make_board("main", default_location=True, bash_allow=[])
    assert deepcrew("allow")[1] == "  (empty)\n"
    assert deepcrew("allow", "--tool", "mcp__a__b")[1] == f"  {'mcp__a__b':28} approved by leader\n"


def test_the_inbox_asks_for_more_work_once_per_set_of_idle_members_and_not_when_there_is_none_to_give(lead, deepcrew, post_task, finish, add_member):
    add_member(lead, "gamma", ["lib/"])
    t1, t2 = post_task(lead, "one", ["src/"]), post_task(lead, "two", ["docs/"])
    assert json.loads(deepcrew("inbox", "--json")[1])["capacity"] is None
    lead.claim("alpha", t1["id"])
    lead.claim("beta", t2["id"])
    data = json.loads(deepcrew("inbox", "--json")[1])
    assert data["capacity"] == {"idle": ["gamma"], "free": 5, "ready": 0, "room": 6, "done": 0, "running": [
        {"task": t1["id"], "title": "one", "owner": "alpha", "helpers": []}, {"task": t2["id"], "title": "two", "owner": "beta", "helpers": []}]}
    assert data["new"] == ["capacity:gamma"]
    text = deepcrew("inbox")[1]
    assert "capacity: 1 idle member(s), room for 5 more, 0 ready task(s): post up to 6 more subtasks" in text and "deepcrew assist TASK NAME [PATH ...]" in text
    assert f"running: {t1['id']} one (alpha); {t2['id']} two (beta)" in text and "(shown before)" in text
    finish(lead, "alpha", t1["id"])
    data = json.loads(deepcrew("inbox", "--json")[1])
    assert data["new"] == ["capacity:alpha,gamma"] and data["capacity"]["done"] == 1 and data["capacity"]["idle"] == ["alpha", "gamma"]
    lead.update_member("alpha", state="active", pid=4242)
    lead.update_member("alpha", state="idle", pid=None)
    assert json.loads(deepcrew("inbox", "--json")[1])["new"] == []
    for n in range(7):
        post_task(lead, f"ready {n}", [f"w{n}/"])
    data = json.loads(deepcrew("inbox", "--json")[1])
    assert data["capacity"] is None and data["new"] == [] and "capacity:" not in deepcrew("inbox")[1]


def test_a_full_crew_hears_of_no_proposals_while_status_still_lists_them(make_board, add_member, deepcrew, post_task):
    board = make_board("main", default_location=True, max_active=1, propose_after_s=0)
    add_member(board, "alpha", ["src/"])
    _seen_by_all(board, post_task(board, "new area", ["web/"])["id"])
    assert json.loads(deepcrew("inbox", "--json")[1])["proposals"] == []
    assert [g["name"] for g in json.loads(deepcrew("status", "--json")[1])["proposals"]] == ["new-area"]


def test_assist_puts_an_idle_member_on_a_task_and_says_so(lead, deepcrew, post_task, procs, add_member):
    add_member(lead, "gamma", ["lib/"])
    t = post_task(lead, "big job", ["src/"])
    lead.claim("alpha", t["id"])
    lead.update_member("alpha", state="active", pid=4242)
    code, out, err = deepcrew("assist", t["id"], "beta", "src/pkg/")
    assert (code, err) == (0, "") and out == f"beta now helps with task {t['id']}; both were told by mail\n"
    assert lead.helping(lead.member("beta"))["paths"] == ["src/pkg/"] and len(lead.peek_mail("alpha")) == 1 and len(procs.calls) == 1
    assert procs.calls[0]["member"]["name"] == "beta"
    code, out, err = deepcrew("assist", t["id"], "beta")
    assert code == 1 and out == "" and err == f"deepcrew: beta is busy with task {t['id']}\n"
    assert deepcrew("assist", t["id"], "ghost")[2] == "deepcrew: 'ghost' is not a working member of this team\n"
    assert deepcrew("assist", "99", "beta")[2] == "deepcrew: there is no task 99\n"
    code, _, err = deepcrew("assist", t["id"], "gamma", "/etc")
    assert code == 1 and "project-relative" in err and lead.helping(lead.member("gamma")) is None


def test_status_shows_the_capacity_the_helpers_and_the_latest_summary(lead, deepcrew, post_task, add_member, procs):
    add_member(lead, "gamma", ["lib/"])
    t = post_task(lead, "big job", ["src/"])
    lead.claim("alpha", t["id"])
    lead.update_member("alpha", state="active", pid=4242)
    out = deepcrew("status")[1]
    assert "capacity: 2 idle member(s), room for 5 more, 0 ready task(s)" in out and "helps=" not in out and "summary   " not in out
    assert deepcrew("assist", t["id"], "beta")[0] == 0 and deepcrew("summary", "Parser half done.")[0] == 0
    out = deepcrew("status")[1]
    beta = next(line for line in out.splitlines() if line.startswith("  beta"))
    assert f"helps={t['id']}" in beta and "helps=" not in next(line for line in out.splitlines() if line.startswith("  alpha"))
    assert "capacity: 1 idle member(s)" in out and f"{t['id']} big job (alpha + beta)" in out and "summary   Parser half done." in out
    data = json.loads(deepcrew("status", "--json")[1])
    assert data["helpers"] == {"beta": lead.member("beta")["helps"]} and data["capacity"]["running"][0]["helpers"] == ["beta"]
    assert data["leader_summary"]["text"] == "Parser half done."


def test_summary_records_what_the_leader_says_and_status_shows_the_latest_line(lead, deepcrew):
    assert deepcrew("summary") == (0, "no summary yet\n", "") and json.loads(deepcrew("summary", "--json")[1]) == {"summaries": []}
    assert json.loads(deepcrew("status", "--json")[1])["leader_summary"] is None
    code, out, err = deepcrew("summary", "Parser done.\nDocs next.")
    assert (code, err) == (0, "") and re.fullmatch(r"\d\d:\d\d  Parser done\.\nDocs next\.\n", out)
    assert deepcrew("summary", "second")[0] == 0
    shown = deepcrew("summary")[1].splitlines()
    assert [line[7:] for line in shown if re.match(r"\d\d:\d\d  ", line)] == ["second", "Parser done."]
    assert deepcrew("summary", "x" * 4001)[0] == 1 and deepcrew("summary", "   ")[0] == 1 and len(lead.summaries()) == 2
    status = deepcrew("status")[1]
    assert "summary   second" in status and "Parser" not in status
    deepcrew("summary", "L1 " + "y" * 300 + "\nsecond line")
    long = [line for line in deepcrew("status")[1].splitlines() if line.startswith("summary   ")]
    assert long == ["summary   " + ("L1 " + "y" * 300)[:160]]
    data = json.loads(deepcrew("status", "--json")[1])
    assert data["leader_summary"]["text"].endswith("second line") and set(data) >= {"capacity", "helpers", "leader_summary"}
    assert [r["text"][:2] for r in json.loads(deepcrew("summary", "--json")[1])["summaries"]] == ["L1", "se", "Pa"]


def tools_flag(board) -> list[str]:
    argv = member.argv(board, {"name": "alpha", "session": "s"}, resume=False)
    return argv[argv.index("--tools") + 1].split(",")


def test_websearch_is_a_default_tool_the_team_allows_and_deepcrew_tools_off_switches_it_off(deepcrew, make_board, monkeypatch, project, providers_toml):
    assert "WebSearch" in cli.DEFAULT_TOOLS and "WebSearch" in cli.OPTIONAL_TOOLS and "WebSearch" in cli.tools_setting()
    board = make_board()
    assert "WebSearch" in board.cfg["tools"] and "WebSearch" in member.settings(board)["permissions"]["allow"]
    assert "WebSearch" in tools_flag(board)
    monkeypatch.setenv("DEEPCREW_TOOLS_OFF", "WebSearch")
    assert "WebSearch" not in cli.tools_setting() and "WebFetch" in cli.tools_setting()
    off = make_board("off")
    assert "WebSearch" not in off.cfg["tools"] and "WebSearch" not in member.settings(off)["permissions"]["allow"]
    assert "WebSearch" not in tools_flag(off)
    monkeypatch.delenv("DEEPCREW_TOOLS_OFF")
    cli.sync_team(board, ["DEEPCREW_TOOLS_OFF"])
    assert "WebSearch" in board.cfg["tools"]
    providers.env_file_set({"DEEPCREW_TOOLS_OFF": "WebSearch,Agent"})
    result = cli.sync_team(board, ["DEEPCREW_TOOLS_OFF"])
    assert "WebSearch" not in board.cfg["tools"] and "Agent" not in board.cfg["tools"] and "tools" in result["changed"]
    assert "WebSearch" not in member.settings(board)["permissions"]["allow"] and "no optional tools" not in cli.show_value("tools", board.cfg["tools"])
    up_ready(providers_toml, monkeypatch)
    monkeypatch.delenv("DEEPCREW_TOOLS_OFF", raising=False)
    providers.env_file_set({"DEEPCREW_TOOLS_OFF": ""})
    assert deepcrew(*UP)[0] == 0 and "WebSearch" in read_json(team_path(project, "main") / "team.json")["tools"]


def test_a_team_never_has_more_members_than_the_limit_the_command_line_the_settings_and_the_presets_agree_on(deepcrew, providers_toml, monkeypatch, project):
    assert cli.MAX_MEMBERS == ui.MAX_MEMBERS == 32 and int(presets.SIZES["large"]["values"]["DEEPCREW_MAX_ACTIVE"]) == 16
    assert cli.positive("max_active", 32) == 32 and cli.positive("DEEPCREW_MAX_ACTIVE", "16") == 16 and cli.positive("hold_s", 5000) == 5000
    for raw in (33, "33", 1000):
        with pytest.raises(ValueError, match="max_active is at most 32"):
            cli.positive("max_active", raw)
    with pytest.raises(ValueError, match="DEEPCREW_MAX_ACTIVE is at most 32"):
        cli.positive("DEEPCREW_MAX_ACTIVE", 33)
    for raw in (0, -1, "x", None, "1.5"):
        with pytest.raises(ValueError, match="must be a positive whole number"):
            cli.positive("max_active", raw)
    code, _, err = deepcrew("config", "set", "max_active", "33")
    assert code == 1 and "max_active is at most 32" in err and providers.env_file_get("DEEPCREW_MAX_ACTIVE") is None
    assert deepcrew("config", "set", "max_active", "32")[0] == 0 and cli.user_settings()["max_active"] == 32
    providers.env_file_set({"DEEPCREW_MAX_ACTIVE": "33"})
    with pytest.raises(ValueError, match="at most 32"):
        cli.user_settings()
    providers.env_file_set({"DEEPCREW_MAX_ACTIVE": "8"})
    assert deepcrew("config", "preset", "large")[0] == 0 and cli.user_settings()["max_active"] == 16 and cli.user_settings()["offer_size"] == 8
    up_ready(providers_toml, monkeypatch)
    code, out, err = deepcrew(*UP, "--max-active", "33")
    assert code == 1 and "max_active is at most 32" in err and not team_path(project, "main").exists()
    assert deepcrew(*UP, "--max-active", "0")[0] == 1 and not team_path(project, "main").exists()
    assert deepcrew(*UP, "--max-active", "32")[0] == 0 and read_json(team_path(project, "main") / "team.json")["max_active"] == 32


def test_check_warns_when_the_member_limit_would_use_half_the_hosts_memory(project, monkeypatch):
    providers.env_file_set({"DEEPCREW_MAX_ACTIVE": "16"})

    def memory(checks):
        return next((c for c in checks if c["id"] == "memory"), None)

    monkeypatch.setattr(doctor, "host_memory_mb", lambda: 16 * cli.MEMBER_MB * 2 - 1)
    warn = memory(cli.host_checks(project))
    assert warn["level"] == "warn" and f"16 members take about {16 * cli.MEMBER_MB} MB" in warn["detail"] and "config preset squad" in warn["fix"]
    monkeypatch.setattr(doctor, "host_memory_mb", lambda: 16 * cli.MEMBER_MB * 2)
    assert memory(cli.host_checks(project)) is None
    monkeypatch.setattr(doctor, "host_memory_mb", lambda: None)
    assert memory(cli.host_checks(project)) is None
    assert cli.host_memory_mb() is None or cli.host_memory_mb() > 0


def test_audit_accepts_a_helpers_edits_in_the_task_it_helped_with_and_flags_the_same_edits_made_without_help(lead, deepcrew, project, post_task, finish, add_member, procs):
    add_member(lead, "gamma", ["lib/"])
    t = post_task(lead, "big job", ["src/"])
    lead.claim("alpha", t["id"])
    lead.update_member("alpha", state="active", pid=4242)
    assert deepcrew("assist", t["id"], "beta", "src/pkg/")[0] == 0
    for rel, text in {"src/a.py": "A = 2\n", "src/pkg/b.py": "B = 2\n"}.items():
        (project / rel).write_text(text)
    lead.log_edit("alpha", "Edit", "src/a.py")
    lead.log_edit("alpha", "Edit", "src/pkg/b.py")
    lead.log_edit("beta", "Edit", "src/pkg/b.py")

    def flags():
        return {f["path"]: (f["owner"], f["edited_by"], f["flags"]) for f in json.loads(deepcrew("audit", "--json")[1])["files"]}

    assert flags() == {"src/a.py": ("alpha", ["alpha"], []), "src/pkg/b.py": ("alpha", ["alpha", "beta"], [])}
    finish(lead, "alpha", t["id"])
    lead.log_edit("beta", "Edit", "src/a.py")
    lead.log_edit("gamma", "Edit", "src/pkg/b.py")
    assert flags() == {"src/a.py": ("alpha", ["alpha", "beta"], ["edited_by_non_owner"]), "src/pkg/b.py": ("alpha", ["alpha", "beta", "gamma"], ["edited_by_non_owner"])}
    assert [(r["member"], r.get("for_owner")) for r in map(json.loads, (lead.dir / "edits.jsonl").read_text().splitlines())] == [
        ("alpha", None), ("alpha", None), ("beta", "alpha"), ("beta", None), ("gamma", None)]


def test_stop_all_also_closes_the_teams_page_and_says_so(lead, deepcrew, monkeypatch):
    closed = []
    monkeypatch.setattr(ui, "stop_page", lambda team_dir: closed.append(Path(team_dir)) or True)
    code, out, err = deepcrew("stop", "--all")
    assert (code, err) == (0, "") and out.splitlines()[-1] == "page: closed" and closed == [lead.dir]
    assert deepcrew("stop", "alpha")[0] == 0 and closed == [lead.dir]
    monkeypatch.setattr(ui, "stop_page", lambda team_dir: False)
    assert "page" not in deepcrew("stop", "--all")[1]


def test_a_key_is_asked_for_in_a_terminal_only(monkeypatch):
    asked = []
    monkeypatch.setattr(providers, "ask_tty", lambda p: asked.append(p["name"]) or "the .env file")
    assert cli.obtain_key(PROVIDER) is None and asked == []
    monkeypatch.setattr(providers, "can_prompt", lambda: True)
    assert cli.obtain_key(PROVIDER) == "the .env file" and asked == ["fake"]


def test_the_inbox_asks_for_a_new_summary_once_three_tasks_closed_since_the_last_one(lead, deepcrew, post_task, finish):
    tasks = [post_task(lead, f"t{i}", [f"src/{i}/"]) for i in range(4)]
    for t in tasks[:2]:
        lead.claim("alpha", t["id"])
        finish(lead, "alpha", t["id"])
    assert json.loads(deepcrew("inbox", "--json")[1])["summary"] is None
    lead.claim("beta", tasks[2]["id"])
    finish(lead, "beta", tasks[2]["id"])
    data = json.loads(deepcrew("inbox", "--json")[1])
    assert data["summary"] == {"since": 3, "at": None} and "summary:0" in data["new"]
    assert 'summary: 3 tasks done and the page has no summary for the user yet: deepcrew summary "done, running, next, blockers"' in deepcrew("inbox")[1]
    row = lead.add_summary("three done")
    assert row["done"] == 3 and json.loads(deepcrew("inbox", "--json")[1])["summary"] is None
    assert cli.QUIET[1] == "summary:"


def test_a_provider_without_a_balance_endpoint_is_not_called_checked(monkeypatch):
    p = {**cli.providers.load("deepseek"), "balance_url": None}
    monkeypatch.setattr(cli.providers, "resolve_key", lambda p: ("env:DEEPSEEK_API_KEY", "sk-" + "a" * 32))
    monkeypatch.setattr(cli.providers, "balance", lambda p, key: pytest.fail("no endpoint to ask"))
    (api,) = [c for c in cli.provider_checks(p, offline=False) if c["id"] == "api"]
    assert api["level"] == "skip" and "not checked" in api["detail"]


def test_site_lets_members_fetch_at_once_and_its_help_and_lines_say_so(lead, deepcrew):
    help_text = " ".join(cli.parser().format_help().split())
    assert "let members reach DOMAIN: fetches and sandboxed shell commands (downloads) go through at once" in help_text
    url = {"url": "https://docs.python.org/3/library/"}
    assert policy.decide(lead, "alpha", "WebFetch", url) is None
    code, out, err = deepcrew("site", "docs.python.org")
    assert (code, err) == (0, "") and out == "added docs.python.org: members fetch from it and connect to it without asking, from now on\n"
    assert policy.decide(lead, "alpha", "WebFetch", url)["behavior"] == "allow"
    assert deepcrew("site")[1].splitlines()[-1] == "added: docs.python.org" and json.loads(deepcrew("site", "--json")[1]) == {"sites": ["docs.python.org"]}
    code, out, _ = deepcrew("site", "docs.python.org", "--remove")
    assert code == 0 and out == ("removed docs.python.org: members ask before they fetch from it or connect to it (a member process "
                                 "started before keeps its sandbox's sites until it ends)\n")
    assert policy.decide(lead, "alpha", "WebFetch", url) is None


def test_the_site_list_names_the_added_sites_also_when_the_sandbox_is_off(lead, deepcrew):
    deepcrew("site", "*.github.com")
    lead.update_cfg(lambda cfg: cfg.__setitem__("sandbox", None))
    assert deepcrew("site")[1].splitlines() == ["the sandbox is off: members' commands are not limited to sites", "added: *.github.com"]


def test_region_shows_a_members_region_and_who_changed_it_last(lead, deepcrew, procs):
    assert deepcrew("region", "alpha") == (0, "alpha: src/\n", "")
    assert json.loads(deepcrew("region", "alpha", "--json")[1]) == {"member": "alpha", "region": ["src/"], "was": ["src/"], "changed": False, "last_change": None, "routing": None}
    deepcrew("region", "alpha", "lib/")
    out = deepcrew("region", "alpha")[1].splitlines()
    assert out[0] == "alpha: lib/" and re.fullmatch(r"changed by leader at \d\d:\d\d, it was src/", out[1])
    deepcrew("region", "alpha", "--drop", "lib/")
    assert deepcrew("region", "alpha")[1].splitlines()[0] == "alpha: no files (it reads, and changes nothing)"
    assert deepcrew("region", "alpha")[1].splitlines()[1].endswith("it was lib/")


def test_region_sets_the_region_tells_the_member_and_routes(lead, deepcrew, procs):
    code, out, err = deepcrew("region", "alpha", "src/pkg/", "src//a.py", "lib/")
    assert (code, err) == (0, "") and out.splitlines() == ["alpha's region is now lib/, src/a.py, src/pkg/ (it was src/); alpha was told by mail", "  resumed alpha"]
    assert lead.member("alpha")["region"] == ["lib/", "src/a.py", "src/pkg/"] and lead.member("beta")["region"] == ["docs/"]
    (call,) = procs.calls
    assert call["member"]["name"] == "alpha" and "Your region was changed by the leader. It is now: lib/, src/a.py, src/pkg/." in call["prompt"]
    assert "You may now also change: lib/." in call["prompt"] and "You no longer own: src/." in call["prompt"]
    code, out, _ = deepcrew("region", "alpha", "src/pkg/", "lib/", "src/a.py")
    assert (code, out) == (0, "alpha's region is unchanged: lib/, src/a.py, src/pkg/\n") and len(procs.calls) == 1 and lead.peek_mail("alpha") == []
    data = json.loads(deepcrew("region", "alpha", "lib/", "--json")[1])
    assert (data["member"], data["region"], data["was"], data["changed"]) == ("alpha", ["lib/"], ["lib/", "src/a.py", "src/pkg/"], True)
    assert data["last_change"]["by"] == "leader" and data["last_change"]["was"] == ["lib/", "src/a.py", "src/pkg/"] and "woken" in data["routing"]


def test_region_moves_a_boundary_when_two_members_need_the_same_file(lead, deepcrew, procs, post_task):
    t = post_task(lead, "fix the guide", ["docs/guide.md"])
    code, out, err = deepcrew("region", "alpha", "--add", "docs/guide.md")
    assert code == 1 and out == "" and err == "deepcrew: docs/guide.md overlaps the region of beta: ['docs/']. Take it off beta first\n"
    code, out, err = deepcrew("region", "beta", "--drop", "docs/", "--add", "docs/api/")
    assert (code, err) == (0, "") and out.splitlines()[0] == "beta's region is now docs/api/ (it was docs/); beta was told by mail"
    code, out, err = deepcrew("region", "alpha", "--add", "docs/guide.md")
    assert (code, err) == (0, "") and out.splitlines()[0] == "alpha's region is now docs/guide.md, src/ (it was src/); alpha was told by mail"
    assert [x["id"] for x in lead.eligible(lead.member("alpha"))] == [t["id"]] and lead.eligible(lead.member("beta")) == []
    assert any(c["member"]["name"] == "alpha" and "fix the guide" in c["prompt"] for c in procs.calls)


def test_region_refuses_what_cannot_be_done_and_says_why(lead, deepcrew, post_task):
    def refused(*argv):
        code, out, err = deepcrew("region", *argv)
        assert (code, out) == (1, ""), argv
        return err.removeprefix("deepcrew: ").rstrip("\n")

    assert refused("ghost", "x/") == "unknown member 'ghost'; members: alpha, beta"
    assert refused("alpha", "docs/x.md") == "docs/x.md overlaps the region of beta: ['docs/']. Take it off beta first"
    assert refused("alpha", "src/", "--add", "lib/") == "give the whole region as PATH ..., or change it with --add and --drop, not both"
    assert refused("alpha", "--drop", "src/a.py") == ("alpha's region (src/) has no entry src/a.py. A region lists whole folders and files, "
                                                       "so to cut a part out give the parts to keep: deepcrew region alpha PATH ...")
    assert refused("alpha", "--drop", "src/", "--drop", "lib/") == ("alpha's region (src/) has no entry lib/. A region lists whole folders and files, "
                                                                    "so to cut a part out give the parts to keep: deepcrew region alpha PATH ...")
    assert "project-relative" in refused("alpha", "/etc") and "must stay inside" in refused("alpha", "--add", "../x")
    t = post_task(lead, "work", ["src/a.py", "src/b.py"])
    lead.claim("alpha", t["id"])
    assert refused("alpha", "src/a.py") == (f"alpha holds task {t['id']}, which changes ['src/b.py']: outside the new region ['src/a.py']. "
                                            "Keep those paths in the region, or end the task first")
    assert lead.member("alpha")["region"] == ["src/"] and lead.member("beta")["region"] == ["docs/"] and lead.peek_mail("alpha") == []


def test_region_add_skips_what_the_region_covers_already_and_drop_takes_whole_entries(lead, deepcrew, procs):
    assert deepcrew("region", "alpha", "--add", "src/pkg/", "--add", "src/a.py")[1] == "alpha's region is unchanged: src/\n"
    deepcrew("region", "alpha", "src/pkg/", "src/a.py", "README.md")
    assert deepcrew("region", "alpha", "--drop", "src/a.py", "--drop", "README.md", "--add", "src/b.py")[1].splitlines()[0] == (
        "alpha's region is now src/b.py, src/pkg/ (it was README.md, src/a.py, src/pkg/); alpha was told by mail")


def test_audit_does_not_blame_a_member_for_edits_in_a_region_it_has_since_given_up(lead, deepcrew, project, clock, procs):
    (project / "src" / "a.py").write_text("A = 2\n")
    lead.log_edit("alpha", "Edit", "src/a.py")
    deepcrew("region", "alpha", "src/pkg/")
    deepcrew("region", "beta", "docs/", "src/a.py")
    files = {f["path"]: f for f in json.loads(deepcrew("audit", "--json")[1])["files"]}
    assert files["src/a.py"]["owner"] == "beta" and files["src/a.py"]["edited_by"] == ["alpha"] and files["src/a.py"]["flags"] == []
    lead.log_edit("alpha", "Edit", "src/a.py")
    assert json.loads(deepcrew("audit", "--json")[1])["files"][0]["flags"] == ["edited_by_non_owner"]
    (project / "docs" / "guide.md").write_text("changed\n")
    lead.log_edit("beta", "Edit", "docs/guide.md")
    flags = {f["path"]: f["flags"] for f in json.loads(deepcrew("audit", "--json")[1])["files"]}
    assert flags["docs/guide.md"] == [] and flags["src/a.py"] == ["edited_by_non_owner"]


def test_version_prints_the_package_version(capsys):
    from deepcrew import __version__
    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0 and capsys.readouterr().out.strip().endswith(__version__)


def test_a_summary_carries_the_round_report_the_page_shows(lead, deepcrew):
    code, out, _ = deepcrew("summary", "Round 2 done.", "--finished", "streaks table", "--finished", "GET /streaks", "--running", "weekly chart",
                            "--risks", "time zones", "--decide", "bars or a line?", "--json")
    (row,) = json.loads(out)["summaries"]
    assert code == 0 and row["text"] == "Round 2 done." and row["finished"] == ["streaks table", "GET /streaks"] and row["decide"] == ["bars or a line?"]
    assert (row["running"], row["risks"]) == (["weekly chart"], ["time zones"])
    assert deepcrew("summary", "--decide", "ship it?")[0] == 0 and lead.summaries(1)[0] == {**lead.summaries(1)[0], "text": "", "decide": ["ship it?"]}


def test_audit_accepts_an_edit_its_task_lease_allowed_and_names_the_lease(lead, deepcrew, project, post_task):
    t = post_task(lead, "readme", ["README.md", "docs/guide.md"])
    assert lead.claim("beta", t["id"])[0] is None and deepcrew("assign", t["id"], "alpha")[0] == 0
    for rel in ("README.md", "docs/guide.md"):
        (project / rel).write_text("changed\n")
        lead.log_edit("alpha", "Edit", rel)
    files = {f["path"]: f for f in json.loads(deepcrew("audit", "--json")[1])["files"]}
    assert (files["README.md"]["flags"], files["README.md"]["leased_to"]) == ([], ["alpha"])
    assert (files["docs/guide.md"]["owner"], files["docs/guide.md"]["flags"], files["docs/guide.md"]["leased_to"]) == ("beta", [], ["alpha"])
    assert "lease=alpha" in deepcrew("audit")[1]
