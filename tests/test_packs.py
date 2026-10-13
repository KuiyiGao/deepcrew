from __future__ import annotations

import json
import re
from pathlib import Path

from deepcrew import cli, doctor, member, prompts, providers
from deepcrew.board import read_json, team_path

KEY = "sk-" + "a" * 32
UP = ("up", "--provider", "fake", "--model", "m", "--effort", "default", "--offline")


def make_pack(folder: Path, files: dict[str, str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def test_config_set_pack_saves_a_pack_only_once_it_is_checked_and_basic_goes_back_to_the_shipped_one(deepcrew):
    mine = make_pack(providers.CONFIG_DIR / "packs" / "mine", {"leader.md": "# mine for $team\n", "pack.json": '{"allow": ["node"]}', "NOTES.txt": "x"})
    env = providers.shown(providers.ENV_FILE)
    code, out, err = deepcrew("config", "set", "pack", "mine")
    assert (code, err) == (0, "") and out.splitlines() == [f"pack = mine  ({env}; applies to teams created from now on)",
                                                           f"  {providers.shown(mine)}: leader.md, pack.json (the other texts come from basic)"]
    assert providers.env_file_get("DEEPCREW_PROMPT_PACK") == "mine" and "pack: mine" in deepcrew("config", "show")[1].splitlines()
    assert json.loads(deepcrew("config", "set", "pack", "mine", "--json")[1]) == {"pack": "mine", "folder": str(mine), "files": ["leader.md", "pack.json"]}
    packs = providers.CONFIG_DIR / "packs"
    make_pack(packs / "empty", {"README.md": "a pack folder without a pack\n"})
    make_pack(packs / "dollar", {"member_wake.md": "You are back, $name.\nIt costs $5.\n"})
    make_pack(packs / "typo", {"leader.md": "# playbook for $owner\n"})
    make_pack(packs / "badjson", {"pack.json": "{nope"})
    make_pack(packs / "badkey", {"pack.json": '{"model": ["x"]}'})
    for value, why in (("nope", f"no prompt pack at {providers.shown(packs / 'nope')}: start one with a copy of the basic pack, "),
                       ("empty", f"{providers.shown(packs / 'empty')} holds none of a pack's files (member_system.md, "),
                       ("dollar", "prompt pack dollar, member_wake.md line 2: invalid placeholder '$5'; write $$ for a literal $"),
                       ("typo", "prompt pack typo, leader.md line 1: $owner is not filled here"),
                       ("badjson", "pack.json is not valid JSON"),
                       ("badkey", "pack.json: 'model' must be one of allow, tools")):
        code, out, err = deepcrew("config", "set", "pack", value)
        assert (code, out) == (1, "") and err.startswith("deepcrew: ") and why in err, value
    assert providers.env_file_get("DEEPCREW_PROMPT_PACK") == "mine"
    code, out, _ = deepcrew("config", "set", "pack", "basic")
    assert (code, out) == (0, f"pack = basic  ({env}; applies to teams created from now on)\n")
    assert providers.setting("DEEPCREW_PROMPT_PACK") is None and json.loads(deepcrew("config", "show", "--json")[1])["pack"] == "basic"


def test_a_pack_given_by_a_path_is_saved_whole_and_the_next_team_takes_it_while_a_team_keeps_its_own(deepcrew, project, providers_toml,
                                                                                                    monkeypatch, tmp_path):
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", KEY)
    assert deepcrew(*UP)[0] == 0
    make_pack(tmp_path / "prompts" / "review", {"leader.md": "# review playbook for $team\n", "member_system.md": "# review protocol for $team\n"})
    monkeypatch.chdir(tmp_path / "prompts")
    code, out, _ = deepcrew("config", "set", "pack", "./review")
    review = str(Path.cwd() / "review")
    assert code == 0 and providers.env_file_get("DEEPCREW_PROMPT_PACK") == review
    assert out.splitlines()[1:] == [f"  {providers.shown(review)}: member_system.md, leader.md (the other texts come from basic)",
                                    "team main keeps pack basic, the one it was created with"]
    assert read_json(team_path(project, "main") / "team.json")["pack"] is None
    code, out, _ = deepcrew(*UP, "--team", "review")
    assert code == 0 and read_json(team_path(project, "review") / "team.json")["pack"] == review and "# review playbook for review" in out
    code, out, _ = deepcrew("config", "set", "pack", "basic", "--team", "review")
    assert code == 0 and out.splitlines()[1:] == [f"team review keeps pack {providers.shown(review)}, the one it was created with"]
    assert deepcrew("config", "set", "pack", "basic")[1].count("\n") == 1


def test_check_deep_tries_the_pack_new_teams_get_and_a_pack_that_would_fail_is_its_result(monkeypatch):
    mine = make_pack(providers.CONFIG_DIR / "packs" / "mine", {"member_system.md": "# mine\n\nYou are on team $team.\n"})
    providers.env_file_set({"DEEPCREW_PROMPT_PACK": "mine"})
    seen = []

    def start(board, name, region, task=None, role=None, strength=None):
        seen.append(dict(board.cfg))
        with board.locked():
            board.new_member(name, region, f"session-{name}")
        return board.update_member(name, state="active", pid=4242)

    monkeypatch.setattr(member, "start", start)
    monkeypatch.setattr(member, "stop", lambda board, name: True)
    monkeypatch.setattr(member, "POLL_S", 0.05)
    monkeypatch.setattr(doctor, "CONFORMANCE_TIMEOUT_S", 0.2)
    assert cli.conformance("/usr/bin/true")["level"] == "fail" and seen[0]["pack"] == "mine" and seen[0]["tools"] == list(cli.DEFAULT_TOOLS)
    (mine / "task_open.md").write_text("Finish task $id by $deadline.\n", encoding="utf-8")
    assert cli.conformance("/usr/bin/true") == {
        "id": "conformance", "level": "fail", "detail": "prompt pack mine, task_open.md line 1: $deadline is not filled here (this template gets $id, $name)",
        "fix": "fix the pack, or go back to the shipped one: deepcrew config set pack basic"}
    assert len(seen) == 1
