from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from conftest import PROVIDER
from deepcrew import cli, member, memory, policy, providers


def write(root, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def headings(board) -> list[str]:
    return [s.heading for s in memory.sections(board)]


def part(board, name: str) -> str:
    (found,) = [s.text for s in memory.sections(board) if s.path is not None and s.path.relative_to(board.root).as_posix() == name]
    return found


def test_a_path_in_text_is_an_import_and_goes_in_backticks():
    assert memory.sealed("Read @docs/a.md first.\n") == ("Read `@docs/a.md` first.\n", ["docs/a.md"])
    assert memory.sealed("@docs/a.md\n- @docs/b.md\n> @docs/c.md\n# Heading @docs/d.md\n") == (
        "`@docs/a.md`\n- `@docs/b.md`\n> `@docs/c.md`\n# Heading `@docs/d.md`\n", ["docs/a.md", "docs/b.md", "docs/c.md", "docs/d.md"])


@pytest.mark.parametrize("text", [
    "```\n@a.md\n```\n",
    "~~~\n@a.md\n~~~\n",
    "````\n```\n@a.md\n```\n````\n",
    "   ```\n   @a.md\n   ```\n",
    "para\n\n    @a.md\n",
    "para\n\n\t@a.md\n",
    "use `@a.md` here\n",
    "use ``a ` @a.md x``\n",
    "use `a\n@a.md b` here\n",
    "<!-- @a.md -->\n",
    "<!--\n@a.md\n-->\n",
    "text <!-- @a.md --> more\n",
    "[link](@a.md)\n",
    "<div>\n@a.md\n</div>\n",
    "\\@a.md\n",
    "mail me at me@a.md\n",
    "(@a.md)\n",
])
def test_a_path_in_code_a_comment_or_a_link_target_is_no_import_and_stays_as_it_is(text):
    assert memory.sealed(text) == (text, [])


@pytest.mark.parametrize("text", ["@a.md", "text @a.md", "text\n@a.md", "*@a.md*", "**@a.md**", "[@a.md](x)", "<b>@a.md</b>", "<!-- c -->@a.md", "`x`@a.md",
                                  "- item\n  - @a.md", "1. @a.md", "> - @a.md"])
def test_a_path_claude_code_reads_is_wrapped_wherever_it_stands(text):
    out, _ = memory.sealed(text)
    assert "`@a.md" in out and not any(m.read for m in memory.mentions(out)[0])


def test_a_fence_inside_an_html_comment_opens_no_code_block():
    text = "<!--\n```\n-->\n@a.md\n```\n@b.md\n"
    assert memory.sealed(text) == ("<!--\n```\n-->\n`@a.md`\n```\n@b.md\n```", ["a.md"])


def test_a_path_that_comments_in_an_html_block_cut_in_pieces_is_wrapped_whole():
    text = "<!-- a -->@a.md<!-- b -->@b.md\n"
    assert memory.sealed(text) == ("<!-- a -->`@a.md<!-- b -->@b.md`\n", ["a.md@b.md"])


def test_a_path_in_a_list_item_is_found_even_inside_a_code_span_as_claude_code_does():
    assert memory.sealed("- run `npm i @a.md x` now\n") == ("- run `npm i ``@a.md`` x` now\n", ["a.md"])
    assert memory.sealed("run `npm i @a.md x` now\n")[1] == []


def test_emphasis_makes_texts_of_its_own_as_it_does_for_claude_code():
    assert memory.sealed("**@a.md** and *see @b.md* and _x @c.md_ and __@d.md__\n") == (
        "**`@a.md`** and *see `@b.md`* and _x `@c.md`_ and __`@d.md`__\n", ["a.md", "b.md", "c.md", "d.md"])
    assert memory.sealed("a**@a.md** and a_@b.md_ and **@c.md and *@d.md\n")[1] == []


def test_an_emphasis_that_ends_inside_a_code_span_makes_what_is_in_it_text():
    text = "_x `` @a.md y_ z`` and **x `` @b.md y** z`` and _x ` @c.md y_ z`\n"
    assert memory.sealed(text) == ("_x `` ```@a.md``` y_ z`` and **x `` ```@b.md``` y** z`` and _x ` @c.md y_ z`\n", ["a.md", "b.md"])
    assert memory.sealed("_x <!-- a > b_ @a.md --> y_\n")[1] == ["a.md"]


def test_fragments_escaped_spaces_and_the_forms_a_path_may_take(tmp_path):
    base = tmp_path / "docs"
    assert memory.import_path("rules.md#install", base) == base / "rules.md"
    assert memory.import_path("my\\ notes.md", base) == base / "my notes.md"
    assert memory.import_path("./a/../b.md", base) == base / "b.md"
    assert memory.import_path("~/notes.md", base) == Path.home() / "notes.md"
    assert memory.import_path((tmp_path / "abs" / "x.md").as_posix(), base) == tmp_path / "abs" / "x.md"
    assert memory.import_path("../x.md", base) == tmp_path / "x.md"
    for token in ("/", "@x", "#heading", "%x", "(x)", "~", "~user/x", "*x", "&x"):
        assert memory.import_path(token, base) is None, token
    text = "see @rules.md#install and @a\\ b.md and @/ and @@x and @#h\n"
    assert memory.sealed(text) == ("see `@rules.md#install` and `@a\\ b.md` and @/ and @@x and @#h\n", ["rules.md#install", "a\\ b.md"])


def test_a_path_that_ends_in_a_slash_names_a_folder_unless_it_is_relative(tmp_path):
    base = tmp_path / "docs"
    assert memory.import_path("~/notes.md/", base) is None and memory.import_path((tmp_path / "x.md").as_posix() + "/", base) is None
    assert memory.import_path("rules.md/", base) == base / "rules.md" and memory.import_path("./rules.md/.", base) == base / "rules.md"
    assert memory.import_path("~/notes.md/.", base) == Path.home() / "notes.md"


def test_a_quotation_that_holds_only_a_link_definition_is_read_and_goes_on_with_the_lines_after_it():
    assert memory.mentions(">[x]: u\nlazy @a.md\n")[0] == [memory.Mention(13, 18, "a.md", True)]


def test_the_backticks_never_join_those_of_the_text_and_pair_with_none_of_them():
    assert memory.sealed("`x`@a.md y\n")[0] == "`x` ``@a.md`` y\n"
    assert memory.sealed("see [@a.md](u) and `c`\n")[0] == "see [``@a.md``](u) and `c`\n"
    assert memory.sealed("`a` `b` @c.md\n")[0] == "`a` `b` ``@c.md``\n"
    assert memory.sealed("@a.md `\n")[0] == "``@a.md`` `\n"
    assert memory.sealed("@a.md ``\n")[0] == "`@a.md` ``\n" and memory.sealed("@a.md ` ``` @b.md\n")[0] == "``@a.md`` ` ``` ``@b.md``\n"


def test_punctuation_or_emphasis_that_closes_a_path_stays_outside_the_backticks():
    assert memory.sealed("(see @a.md.)\n")[0] == "(see `@a.md`.)\n"
    assert memory.sealed("**@a.md**\n")[0] == "**`@a.md`**\n"
    assert memory.sealed("see @a.md, then\n")[0] == "see `@a.md`, then\n"


def test_line_ends_are_the_ones_claude_codes_lexer_reads():
    assert memory.sealed("see\r\n@a.md\r\n```\r\n@b.md\r\n```\r\n") == ("see\n`@a.md`\n```\n@b.md\n```\n", ["a.md"])


def test_a_code_fence_or_html_block_the_text_leaves_open_is_closed():
    assert memory.sealed("```\ncode @a.md\n") == ("```\ncode @a.md\n```", [])
    assert memory.sealed("~~~~\ncode") == ("~~~~\ncode\n~~~~", [])
    assert memory.sealed("<!-- note @a.md") == ("<!-- note @a.md\n-->", [])
    assert memory.sealed("<script>\nx")[0] == "<script>\nx\n</script>"
    assert memory.sealed("```\nclosed\n```\n")[0] == "```\nclosed\n```\n"


def test_wrapping_a_text_leaves_nothing_for_claude_code_to_read():
    text = "\n".join(["@a.md", "- @b.md `x @c.md y`", "> `a` @d.md", "1. item", "   ```", "   @e.md", "   ```", "[x](y) @f.md", "<b>@g.md</b>",
                      "text `a", "@h.md `", "\\\\@i.md", "```", "@j.md"])
    out, imports = memory.sealed(text)
    assert imports == ["a.md", "b.md", "c.md", "d.md", "f.md", "g.md", "i.md"]
    assert not any(m.read for m in memory.mentions(out)[0])


def test_a_text_the_lexer_fails_on_or_cannot_finish_in_time_gets_a_backslash_before_every_path(monkeypatch):
    text = "see @a.md and `@b.md` and \\@c.md and me@d.md\n"
    assert memory._blunt(text) == "see \\@a.md and `\\@b.md` and \\@c.md and me@d.md\n"
    assert memory.sealed(text, time.monotonic() - 1) == (memory._blunt(text), [])
    monkeypatch.setattr(memory, "mentions", lambda *a, **k: 1 / 0)
    assert memory.sealed(text) == (memory._blunt(text), [])
    monkeypatch.undo()
    deep = "> " * 5000 + "@a.md\n"
    assert memory.sealed(deep) == (memory._blunt(deep), [])


def test_a_text_that_would_keep_an_import_after_wrapping_is_escaped_instead(monkeypatch):
    real = memory.mentions

    def liar(text, deadline=None):
        found, closing = real(text, deadline)
        return (found, closing) if "`" not in text else ([memory.Mention(0, 2, "a.md", True)], closing)
    monkeypatch.setattr(memory, "mentions", liar)
    assert memory.sealed("@a.md\n") == ("\\@a.md\n", [])


def test_members_get_the_instructions_then_claude_md_then_dot_claude_md_then_the_rules_by_path(make_board, project):
    write(project, {"CLAUDE.md": "root\n", ".claude/CLAUDE.md": "dot\n", ".claude/rules/b.md": "b rule\n", ".claude/rules/a/z.md": "z rule\n",
                    ".claude/rules/a.md": "a rule\n", ".claude/rules/notes.txt": "not a rule\n", "CLAUDE.local.md": "mine\n"})
    board = make_board()
    (board.dir / member.INSTRUCTIONS).write_text("Be brief.\n", encoding="utf-8")
    assert headings(board) == ["Instructions for team t (team instructions)", "The project's CLAUDE.md", "The project's .claude/CLAUDE.md",
                               "The project's .claude/rules/a.md", "The project's .claude/rules/a/z.md", "The project's .claude/rules/b.md"]
    assert member.memory(board) == ("# Instructions for team t (team instructions)\n\nBe brief.\n\n# The project's CLAUDE.md\n\nroot\n\n"
                                    "# The project's .claude/CLAUDE.md\n\ndot\n\n# The project's .claude/rules/a.md\n\na rule\n\n"
                                    "# The project's .claude/rules/a/z.md\n\nz rule\n\n# The project's .claude/rules/b.md\n\nb rule\n")
    assert "mine" not in member.memory(board) and "not a rule" not in member.memory(board)


def test_the_instructions_heading_says_whose_instructions_they_are(make_board):
    board = make_board()
    providers.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (providers.CONFIG_DIR / member.INSTRUCTIONS).write_text("Default.\n", encoding="utf-8")
    assert headings(board) == ["Instructions for team t (default instructions)"]
    (board.dir / member.INSTRUCTIONS).write_text("Own.\n", encoding="utf-8")
    assert headings(board) == ["Instructions for team t (team instructions)"] and "set by the user" not in member.memory(board)


def test_a_relative_import_is_read_from_the_folder_of_the_file_that_holds_it(make_board, project):
    write(project, {"CLAUDE.md": "Rules: @docs/rules.md\n", "docs/rules.md": "Rule text. More: @./more.md and @more2.md\n", "docs/more.md": "More text.\n",
                    "docs/more2.md": "More two.\n", "more.md": "not this one\n"})
    board = make_board()
    assert headings(board) == ["The project's CLAUDE.md", "The project's docs/rules.md (imported by CLAUDE.md)",
                               "The project's docs/more.md (imported by docs/rules.md)", "The project's docs/more2.md (imported by docs/rules.md)"]
    assert member.memory(board) == ("# The project's CLAUDE.md\n\nRules: `@docs/rules.md`\n\n"
                                    "# The project's docs/rules.md (imported by CLAUDE.md)\n\nRule text. More: `@./more.md` and `@more2.md`\n\n"
                                    "# The project's docs/more.md (imported by docs/rules.md)\n\nMore text.\n\n"
                                    "# The project's docs/more2.md (imported by docs/rules.md)\n\nMore two.\n")


def test_an_imported_file_comes_right_after_its_importer_depth_first(make_board, project):
    write(project, {"CLAUDE.md": "@a.md\n@b.md\n", "a.md": "a\n@c.md\n", "b.md": "b\n", "c.md": "c\n", ".claude/CLAUDE.md": "dot\n@b.md\n"})
    board = make_board()
    assert headings(board) == ["The project's CLAUDE.md", "The project's a.md (imported by CLAUDE.md)", "The project's c.md (imported by a.md)",
                               "The project's b.md (imported by CLAUDE.md)", "The project's .claude/CLAUDE.md"]


def test_imports_go_five_files_deep_and_no_file_comes_twice(make_board, project):
    write(project, {"CLAUDE.md": "@l1.md\n", **{f"l{k}.md": f"level {k}\n@l{k + 1}.md\n" for k in range(1, 8)}})
    assert headings(make_board())[-1] == "The project's l4.md (imported by l3.md)" and len(headings(make_board())) == 5
    write(project, {"CLAUDE.md": "@a.md @a.md @./a.md\n", "a.md": "a @b.md\n", "b.md": "b @a.md @CLAUDE.md\n"})
    assert headings(make_board()) == ["The project's CLAUDE.md", "The project's a.md (imported by CLAUDE.md)", "The project's b.md (imported by a.md)"]


def test_a_missing_file_or_one_that_is_no_text_is_left_out_without_a_word(make_board, project):
    write(project, {"CLAUDE.md": "@missing.md @pic.png @bin.dat @notes @UPPER.MD @dir.md\n", "pic.png": "x", "bin.dat": "y", "notes": "no extension\n", "UPPER.MD": "upper\n",
                    "dir.md/inner.md": "inside\n"})
    assert headings(make_board()) == ["The project's CLAUDE.md", "The project's notes (imported by CLAUDE.md)", "The project's UPPER.MD (imported by CLAUDE.md)"]


def test_a_file_that_is_no_utf8_text_is_left_out(make_board, project):
    write(project, {"CLAUDE.md": "@bad.md @empty.md @blank.md\n", "empty.md": "", "blank.md": "  \n"})
    (project / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    assert headings(make_board()) == ["The project's CLAUDE.md"]


def test_an_import_outside_the_project_is_left_out_but_its_path_is_still_in_backticks(make_board, project, tmp_path):
    write(tmp_path, {"outside.md": "OUTSIDE TEXT\n", "home-note.md": "HOME TEXT\n"})
    far = (tmp_path / "outside.md").as_posix()
    write(project, {"CLAUDE.md": f"@../outside.md @~/home-note.md @{far} @docs/in.md\n", "docs/in.md": "inside\n"})
    board = make_board()
    text = member.memory(board)
    assert "OUTSIDE TEXT" not in text and "HOME TEXT" not in text and "inside" in text
    assert f"`@../outside.md` `@~/home-note.md` `@{far}` `@docs/in.md`" in text
    assert headings(board) == ["The project's CLAUDE.md", "The project's docs/in.md (imported by CLAUDE.md)"]


@pytest.mark.posix("symlinks")
def test_a_link_inside_the_project_that_leads_out_is_left_out_and_one_that_stays_in_is_read(make_board, project, tmp_path):
    write(tmp_path, {"elsewhere/secret.md": "SECRET\n", "elsewhere/note.md": "ELSEWHERE\n"})
    (project / "docs").mkdir(exist_ok=True)
    (project / "docs" / "leak.md").symlink_to(tmp_path / "elsewhere" / "secret.md")
    (project / "out").symlink_to(tmp_path / "elsewhere")
    (project / "docs" / "alias.md").symlink_to(project / "docs" / "guide.md")
    write(project, {"CLAUDE.md": "@docs/leak.md @out/note.md @docs/alias.md\n"})
    board = make_board()
    assert "SECRET" not in member.memory(board) and "ELSEWHERE" not in member.memory(board)
    assert headings(board) == ["The project's CLAUDE.md", "The project's docs/alias.md (imported by CLAUDE.md)"] and part(board, "docs/alias.md") == "guide"


@pytest.mark.posix("symlinks")
def test_an_import_in_a_linked_memory_file_is_read_from_the_folder_the_link_leads_to(make_board, project):
    write(project, {"shared/CLAUDE.md": "shared @inner.md\n", "shared/inner.md": "inner\n", "inner.md": "wrong one\n"})
    (project / "CLAUDE.md").symlink_to(project / "shared" / "CLAUDE.md")
    board = make_board()
    assert headings(board) == ["The project's CLAUDE.md", "The project's shared/inner.md (imported by CLAUDE.md)"] and "wrong one" not in member.memory(board)


def test_nothing_comes_from_team_state_the_settings_or_a_closed_place(make_board, project, tmp_path, monkeypatch):
    write(project, {"cfg/key.md": "SETTINGS\n", "closed/key.md": "CLOSED\n", "state/team.md": "STATE\n", "ok.md": "fine\n",
                    "CLAUDE.md": "@cfg/key.md @closed/key.md @state/team.md @ok.md\n"})
    monkeypatch.setattr(providers, "CONFIG_DIR", project / "cfg")
    monkeypatch.setattr(policy, "CLOSED_TO_LINKS", (*policy.CLOSED_TO_LINKS, str(project / "closed")))
    board = cli.create_team(project, "t", PROVIDER, {"model": "m", "effort": None}, claude="/usr/bin/true", team_dir=project / "state")
    text = member.memory(board)
    assert "SETTINGS" not in text and "CLOSED" not in text and "STATE" not in text and "fine" in text
    assert headings(board) == ["The project's CLAUDE.md", "The project's ok.md (imported by CLAUDE.md)"]


def test_a_closed_place_that_holds_the_project_does_not_close_it(make_board, project, monkeypatch):
    write(project, {"CLAUDE.md": "@a.md\n", "a.md": "a\n"})
    monkeypatch.setattr(providers, "CONFIG_DIR", project.parent)
    assert headings(make_board()) == ["The project's CLAUDE.md", "The project's a.md (imported by CLAUDE.md)"]


def test_each_file_is_cut_and_an_import_in_the_part_that_is_cut_is_not_read(make_board, project, monkeypatch):
    monkeypatch.setattr(memory, "PROJECT_MEMORY_CHARS", 30)
    write(project, {"CLAUDE.md": "0123456789 @a.md 0123456789 abcdef @b.md\n", "a.md": "A" * 40 + "\n", "b.md": "b\n"})
    board = make_board()
    assert headings(board) == ["The project's CLAUDE.md", "The project's a.md (imported by CLAUDE.md)"]
    assert part(board, "CLAUDE.md") == "0123456789 `@a.md` 0123456789 ab\n[... cut at 30 characters]" and "abcdef" not in member.memory(board)
    assert part(board, "a.md") == "A" * 30 + "\n[... cut at 30 characters]"


def test_a_cut_inside_a_code_fence_closes_it_before_the_notice(make_board, project, monkeypatch):
    monkeypatch.setattr(memory, "PROJECT_MEMORY_CHARS", 20)
    write(project, {"CLAUDE.md": "```\ncode code code code\n```\nafter\n"})
    assert part(make_board(), "CLAUDE.md") == "```\ncode code code c\n```\n[... cut at 20 characters]"


def test_the_rules_keep_their_text_without_the_front_matter_and_those_with_paths_are_left_to_claude_code(make_board, project):
    write(project, {".claude/rules/plain.md": "---\ndescription: x\n---\nPlain rule.\n", ".claude/rules/csv.md": "---\npaths: src/**/*.ts, tests/**\n---\nCsv rule.\n",
                    ".claude/rules/list.md": "---\npaths:\n  - \"src/a/*.py\"\n  - lib/**  # c\n---\nList rule.\n",
                    ".claude/rules/flow.md": "---\npaths: [\"a/{b,c}/*.md\", 'd.md']\n---\nFlow rule.\n", ".claude/rules/all.md": "---\npaths: \"**\"\n---\nAll rule.\n",
                    ".claude/rules/empty.md": "---\npaths:\n---\nEmpty paths rule.\n", "CLAUDE.md": "---\nkey: v\n---\nRoot text.\n"})
    board = make_board()
    assert headings(board) == ["The project's CLAUDE.md", "The project's .claude/rules/all.md", "The project's .claude/rules/empty.md",
                               "The project's .claude/rules/plain.md"]
    assert [part(board, f".claude/rules/{n}.md") for n in ("plain", "all", "empty")] == ["Plain rule.", "All rule.", "Empty paths rule."]
    assert part(board, "CLAUDE.md") == "Root text." and "Csv rule." not in memory.memory(board)


def test_an_imported_file_loses_its_front_matter_and_is_never_taken_for_a_rule_with_paths(make_board, project):
    write(project, {"CLAUDE.md": "@doc.md\n", "doc.md": "---\npaths: src/**\n---\nDoc text.\n"})
    board = make_board()
    assert headings(board) == ["The project's CLAUDE.md", "The project's doc.md (imported by CLAUDE.md)"] and part(board, "doc.md") == "Doc text."


def test_a_rule_that_is_imported_comes_where_it_was_first_met(make_board, project):
    write(project, {"CLAUDE.md": "@.claude/rules/b.md\n", ".claude/rules/a.md": "a\n", ".claude/rules/b.md": "b\n"})
    assert headings(make_board()) == ["The project's CLAUDE.md", "The project's .claude/rules/b.md (imported by CLAUDE.md)", "The project's .claude/rules/a.md"]


def test_the_instructions_are_a_file_at_the_projects_root_and_their_paths_are_wrapped_too(make_board, project, tmp_path):
    write(project, {"docs/style.md": "Style text.\n", "CLAUDE.md": "root\n"})
    write(tmp_path, {"outside.md": "OUTSIDE\n"})
    board = make_board()
    (board.dir / member.INSTRUCTIONS).write_text("Follow @docs/style.md and not @../outside.md.\n", encoding="utf-8")
    assert headings(board) == ["Instructions for team t (team instructions)", "The project's docs/style.md (imported by the team instructions)", "The project's CLAUDE.md"]
    assert member.memory(board).startswith("# Instructions for team t (team instructions)\n\nFollow `@docs/style.md` and not `@../outside.md`.\n\n")
    assert "OUTSIDE" not in member.memory(board)
    board.update_cfg(lambda cfg: cfg.__setitem__("project_memory", False))
    assert headings(board) == ["Instructions for team t (team instructions)", "The project's docs/style.md (imported by the team instructions)"]


def test_the_default_instructions_name_themselves_in_the_headings_of_what_they_import(make_board, project):
    write(project, {"a.md": "a\n"})
    providers.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (providers.CONFIG_DIR / member.INSTRUCTIONS).write_text("See @a.md\n", encoding="utf-8")
    assert headings(make_board()) == ["Instructions for team t (default instructions)", "The project's a.md (imported by the default instructions)"]


def test_a_heading_cannot_carry_an_import_either(make_board, project):
    write(project, {"CLAUDE.md": "@docs/b\\ @~/x.md\n", "docs/b @~/x.md": "text\n"})
    board = make_board()
    assert headings(board) == ["The project's CLAUDE.md", "The project's docs/b `@~/x.md` (imported by CLAUDE.md)"]
    assert not any(m.read for m in memory.mentions(member.memory(board))[0])


def test_project_memory_pairs_are_files_under_the_project_with_their_texts(make_board, project):
    write(project, {"CLAUDE.md": "@docs/a.md\n", "docs/a.md": "a\n"})
    board = make_board()
    assert member.project_memory(board) == [(board.root / "CLAUDE.md", "`@docs/a.md`"), (board.root / "docs" / "a.md", "a")]
    assert cli.memory_line(board) == "memory    members read the project's CLAUDE.md and the project's docs/a.md"


def test_the_members_claude_md_is_one_text_with_the_imports_in_it_and_removed_when_there_is_nothing(make_board, project, add_member):
    write(project, {"CLAUDE.md": "See @docs/a.md\n", "docs/a.md": "imported\n"})
    board = make_board()
    first, second = add_member(board, "alpha", ["src/"]), add_member(board, "beta", ["docs/"])
    member.argv(board, first, resume=False)
    text = (board.dir / "claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "See `@docs/a.md`" in text and "imported" in text
    member.argv(board, second, resume=False)
    assert (board.dir / "claude" / "CLAUDE.md").read_text(encoding="utf-8") == text == member.memory(board)
    for name in ("CLAUDE.md", "docs/a.md"):
        (project / name).unlink()
    member.write_memory(board)
    assert not (board.dir / "claude" / "CLAUDE.md").exists()


def test_a_text_that_would_keep_the_lexer_busy_for_a_minute_is_given_up_on_at_the_deadline(make_board, project, monkeypatch):
    write(project, {"CLAUDE.md": "*a " * 13000 + "\n@a.md\n", "a.md": "a\n"})
    monkeypatch.setattr(memory, "WORK_S", 0.3)
    started = time.monotonic()
    board = make_board()
    assert headings(board) == ["The project's CLAUDE.md"] and part(board, "CLAUDE.md").endswith("\n\\@a.md") and time.monotonic() - started < 5


def test_a_memory_file_that_takes_the_lexer_too_long_is_wrapped_without_being_read(make_board, project, monkeypatch):
    write(project, {"CLAUDE.md": "See @a.md\n[a](" * 3000, "a.md": "a\n"})
    monkeypatch.setattr(memory, "WORK_S", -1)
    board = make_board()
    assert headings(board) == ["The project's CLAUDE.md"] and "\\@a.md" in part(board, "CLAUDE.md")


def test_the_read_of_a_file_is_bounded_by_the_cut(make_board, project, monkeypatch):
    monkeypatch.setattr(memory, "PROJECT_MEMORY_CHARS", 10)
    monkeypatch.setattr(memory, "READ_SLACK", 8)
    write(project, {"CLAUDE.md": "é" * 1000 + "\n"})
    assert part(make_board(), "CLAUDE.md") == "é" * 10 + "\n[... cut at 10 characters]"


@pytest.mark.posix("a FIFO")
def test_a_file_that_is_no_regular_file_is_never_opened(make_board, project):
    os.mkfifo(project / "pipe.md")
    write(project, {"CLAUDE.md": "@pipe.md\n"})
    assert headings(make_board()) == ["The project's CLAUDE.md"]
