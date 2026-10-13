from __future__ import annotations

import pytest

from conftest import PROVIDER
from deepcrew import cli, member, memory, providers, ui
from deepcrew.board import Board, read_json


def test_thinking_off_reaches_claude_code_and_on_sends_nothing(make_board, add_member):
    board = make_board()
    m = add_member(board, "alpha", ["src/"])
    assert "CLAUDE_CODE_DISABLE_THINKING" not in member.env(board, m)
    board.update_cfg(lambda cfg: cfg["member"].__setitem__("thinking", False))
    assert member.env(board, m)["CLAUDE_CODE_DISABLE_THINKING"] == "1"


def test_deepseek_asks_for_a_larger_output_budget_than_claude_codes_default(make_board, add_member):
    board = make_board(provider={**providers.BUILTIN["deepseek"], "name": "deepseek"})
    m = add_member(board, "alpha", ["src/"])
    assert member.env(board, m)["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "128000"
    plain = make_board("plain")
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in member.env(plain, add_member(plain, "beta", ["docs/"]))


def test_members_read_the_team_instructions_then_the_projects_claude_md(make_board, add_member, project):
    board = make_board()
    assert member.memory(board) == "" and member.instructions(board) == ("", "none")
    (project / "CLAUDE.md").write_text("Use tabs.\n", encoding="utf-8")
    (project / ".claude").mkdir(exist_ok=True)
    (project / ".claude" / "CLAUDE.md").write_text("Run make test.\n", encoding="utf-8")
    providers.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (providers.CONFIG_DIR / member.INSTRUCTIONS).write_text("Answer in English.\n", encoding="utf-8")
    text = member.memory(board)
    assert text.index("default instructions") < text.index("Answer in English.") < text.index("Use tabs.") < text.index("Run make test.")
    (board.dir / member.INSTRUCTIONS).write_text("Keep functions short.\n", encoding="utf-8")
    assert member.instructions(board) == ("Keep functions short.", "team") and "Answer in English." not in member.memory(board)
    a, b = add_member(board, "alpha", ["src/"]), add_member(board, "beta", ["docs/"])
    member.argv(board, a, resume=False)
    first = (board.dir / "claude" / "CLAUDE.md").read_text(encoding="utf-8")
    member.argv(board, b, resume=False)
    assert (board.dir / "claude" / "CLAUDE.md").read_text(encoding="utf-8") == first == member.memory(board)
    board.update_cfg(lambda cfg: cfg.__setitem__("project_memory", False))
    assert "Use tabs." not in member.memory(board) and "Keep functions short." in member.memory(board)


def test_the_projects_claude_md_is_cut_and_nothing_left_removes_the_file(make_board, add_member, project, monkeypatch):
    board = make_board()
    monkeypatch.setattr(memory, "PROJECT_MEMORY_CHARS", 10)
    (project / "CLAUDE.md").write_text("0123456789abcdef\n", encoding="utf-8")
    assert "0123456789\n[... cut at 10 characters]" in member.memory(board) and "abcdef" not in member.memory(board)
    member.argv(board, add_member(board, "alpha", ["src/"]), resume=False)
    assert (board.dir / "claude" / "CLAUDE.md").exists()
    (project / "CLAUDE.md").unlink()
    member.write_memory(board)
    assert not (board.dir / "claude" / "CLAUDE.md").exists()


def test_the_instructions_command_sets_shows_and_clears_them(make_board, deepcrew, project, tmp_path):
    board = make_board("main", default_location=True)
    code, out, _ = deepcrew("instructions")
    assert code == 0 and "instructions for team main: none" in out and "project CLAUDE.md for members: none" in out
    code, out, _ = deepcrew("instructions", "Write docstrings.")
    assert code == 0 and "team, 17 chars; members take them when they next start or wake" in out
    assert member.instructions(board) == ("Write docstrings.", "team")
    src = tmp_path / "rules.md"
    src.write_text("Prefer the standard library.\n", encoding="utf-8")
    assert deepcrew("instructions", "--default", "--file", src)[0] == 0
    assert (providers.CONFIG_DIR / member.INSTRUCTIONS).read_text(encoding="utf-8") == "Prefer the standard library.\n"
    assert deepcrew("instructions", "--clear")[0] == 0 and member.instructions(board) == ("Prefer the standard library.", "default")
    code, _, err = deepcrew("instructions", "x" * (member.INSTRUCTIONS_MAX_CHARS + 1))
    assert code != 0 and "at most 20,000 characters" in err and member.instructions(board)[1] == "default"


def test_config_set_thinking_and_project_memory_reach_the_running_team(make_board, deepcrew, project):
    board = make_board("main", default_location=True)
    (project / "CLAUDE.md").write_text("Use tabs.\n", encoding="utf-8")
    assert "memory    members read the project's CLAUDE.md" in cli.summary(board, None)
    code, out, _ = deepcrew("config", "set", "thinking", "off")
    assert code == 0 and read_json(board.dir / "team.json")["member"]["thinking"] is False
    assert any(line.endswith("thinking off") for line in cli.summary(Board(board.dir), None))
    assert deepcrew("config", "set", "project_memory", "off")[0] == 0 and read_json(board.dir / "team.json")["project_memory"] is False
    assert cli.memory_line(Board(board.dir)) is None
    code, _, err = deepcrew("config", "set", "thinking", "maybe")
    assert code != 0 and "thinking is on or off" in err


def test_the_page_takes_on_or_off_and_shows_each_members_effort(make_board, add_member):
    with pytest.raises(ui.Refused, match="DEEPCREW_THINKING must be on or off"):
        ui.checked({"DEEPCREW_THINKING": "sometimes"})
    assert ui.checked({"DEEPCREW_THINKING": "OFF", "DEEPCREW_PROJECT_MEMORY": "on"}) == {"DEEPCREW_THINKING": "off", "DEEPCREW_PROJECT_MEMORY": "on"}
    board = make_board("main", provider={**PROVIDER, "efforts": ["high", "max"]}, default_location=True)
    board.update_cfg(lambda cfg: cfg["member"].update(effort="high", thinking=False))
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"], effort="max", model="m")
    rows = {r["name"]: r for r in ui.team_view(ui.TeamLink("main", board.dir))["members"]}
    assert (rows["alpha"]["effort"], rows["beta"]["effort"], rows["alpha"]["thinking"]) == ("high", "max", False)
