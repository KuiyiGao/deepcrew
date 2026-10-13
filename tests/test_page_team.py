from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from deepcrew import cli, member, policy, presets, providers, ui
from page_helpers import TeamPages, ask_perm, ask_question, caller, page_script, running, standin_widening

TOTAL_KEYS = {"requests", "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "cost_usd"}
MEMBER_KEYS = {"name", "state", "task", "model", "region"} | TOTAL_KEYS


@pytest.fixture(autouse=True)
def _widening(monkeypatch):
    standin_widening(monkeypatch)


@pytest.fixture
def team_page(lead):
    pages = TeamPages(lead)
    yield pages
    pages.close()


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "notes.txt").write_text("notes\n")
    return folder


def folder_request(board, folder, tool="Read", write=False):
    return board.request("perms", "alpha", {"tool": tool, "input": {"file_path": str(folder / "notes.txt")}, "tool_use_id": None, "always": False,
                                            "folder": str(folder), "write": write})["id"]


def test_the_page_has_the_team_panel_the_crew_buttons_and_the_key_line(team_page):
    page = team_page()
    status, _, body = page.call("GET", f"/?t={page.token}")
    text = body.decode()
    assert status == 200
    for ident in ("team-panel", "crew-stop", "crew-resume", "key-local", "team-saved", "team-cmp"):
        assert f'id="{ident}"' in text, ident
    assert '<section id="team-panel" hidden>' in text
    assert text.index("<main>") < text.index('id="team-panel"') < text.index('id="keyform"') < text.index('id="key-local"')
    assert "Link folder" in text and "/api/team" in text and "/api/crew" in text and "Waiting for you:" in text
    assert ('title="Rests the crew: every member process ends. The team, this page and the leader stay. Resume crew lets them work again.">Rest crew</button>'
            in text)
    assert 'id="crew-resume">Resume crew</button>' in text


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to read the page's script")
def test_the_pages_script_parses(tmp_path):
    text = Path(ui.__file__).with_name("ui.html").read_text(encoding="utf-8")
    script = tmp_path / "ui.js"
    script.write_text(re.search(r"<script>(.*?)</script>", text, re.S).group(1), encoding="utf-8")
    done = subprocess.run([shutil.which("node"), "--check", str(script)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_the_team_route_gives_the_panel_the_members_and_the_totals(team_page, lead):
    page = team_page()
    status, _, view = page.call("GET", "/api/team")
    assert status == 200 and view["exists"] is True
    assert set(view) >= {"members", "total", "paused", "folders", "sites", "tools", "tasks", "setup", "model"}
    assert sorted(m["name"] for m in view["members"]) == ["alpha", "beta"] and all(set(m) >= MEMBER_KEYS and m["state"] == "idle" for m in view["members"])
    assert set(view["total"]) >= TOTAL_KEYS and view["total"]["cost_usd"] is None and view["total"]["requests"] == 0
    assert view["paused"] is None and view["folders"] == [] and view["sites"] == [] and isinstance(view["tools"], str) and view["tools"]
    assert view["tasks"] == {"open": 0, "claimed": 0, "done": 0, "cancelled": 0}
    log = lead.dir / "logs" / "alpha.ndjson"
    log.parent.mkdir(exist_ok=True)
    log.write_text(json.dumps({"type": "assistant", "message": {"id": "m1", "usage": {}}}) + "\n"
                   + json.dumps({"type": "result", "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000}}) + "\n")
    view = page.call("GET", "/api/team")[2]
    alpha = next(m for m in view["members"] if m["name"] == "alpha")
    assert (alpha["requests"], alpha["input_tokens"], alpha["output_tokens"], alpha["cache_read_input_tokens"]) == (1, 1000, 200, 5000)
    assert (view["total"]["requests"], view["total"]["input_tokens"], view["total"]["cache_read_input_tokens"]) == (1, 1000, 5000)
    saving = view["saving"]
    assert saving["compare"] == "Sonnet 5.5" and set(saving["claude"]) == {"Haiku 5.5", "Sonnet 5.5", "Opus 5.5", "Fable 5.1"}
    assert saving["claude"]["Sonnet 5.5"] == pytest.approx((1000 * 2.5 + 5000 * 0.1 + 200 * 10.0) / 1e6)


def test_the_same_tokens_are_priced_at_claudes_list_prices():
    tokens = {"input": 1_000_000, "cache_write": 500_000, "cache_read": 2_000_000, "output": 100_000}
    assert member.claude_equivalent(tokens, "Sonnet 5.5") == pytest.approx(3.75 + 0.2 + 1.0)
    assert member.claude_equivalent(tokens, "Opus 5.5") == pytest.approx(7.5 + 0.4 + 2.0) and member.claude_equivalent({}, "Haiku 5.5") == 0
    assert member.claude_equivalent(tokens, "Fable 5.1") == pytest.approx(18.75 + 0.5 + 5.0)
    assert member.claude_equivalent(tokens, "Haiku 5.5") == pytest.approx(0.1875 + 0.02 + 0.05)
    long = {**tokens, "long": {"input": 1_000_000, "cache_write": 0, "cache_read": 2_000_000, "output": 50_000}}
    assert member.claude_equivalent(long, "Haiku 5.5") == pytest.approx(0.5 * 0.125 + 0.05 * 0.5 + 0.625 + 0.1 + 0.05 * 2.5)
    assert member.claude_equivalent(long, "Opus 5.5") == member.claude_equivalent(tokens, "Opus 5.5")


def test_usage_keeps_apart_the_tokens_of_requests_whose_prompt_is_over_the_long_prompt_line(lead):
    log = lead.dir / "logs" / "alpha.ndjson"
    log.parent.mkdir(exist_ok=True)

    def reply(i, read):
        return json.dumps({"type": "assistant", "message": {"id": i, "usage": {"input_tokens": 1000, "cache_read_input_tokens": read}}})
    log.write_text("\n".join([reply("m1", 50_000), reply("m2", 150_000),
                              json.dumps({"type": "result", "usage": {"input_tokens": 2000, "cache_read_input_tokens": 200_000, "output_tokens": 800}}),
                              reply("m3", 120_000)]) + "\n")
    u = member.usage(lead, "alpha")
    assert u["long"] == {"input_tokens": 2000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 270_000, "output_tokens": 400}
    tokens = member.claude_tokens([u])
    assert tokens["long"]["cache_read"] == 270_000 and tokens["cache_read"] == 320_000


def test_rest_crew_rests_the_crew_and_resume_ends_the_rest(team_page, lead, procs):
    page = team_page()
    status, _, view = page.call("POST", "/api/crew", {"action": "rest"})
    assert status == 200 and view["exists"] is True and view["paused"]["by"] == "user" and abs(view["paused"]["ts"] - time.time()) < 30
    assert lead.live_cfg()["paused"]["by"] == "user"
    assert page.call("GET", "/api/team")[2]["paused"]["by"] == "user"
    status, _, view = page.call("POST", "/api/crew", {"action": "resume"})
    assert status == 200 and view["paused"] is None and not lead.live_cfg().get("paused")
    assert page.call("GET", "/api/team")[2]["paused"] is None
    for body in ({"action": "explode"}, {"action": None}, {}):
        status, _, reply = page.call("POST", "/api/crew", body)
        assert status == 400 and reply["error"], body
    assert not lead.live_cfg().get("paused")


def test_without_a_team_there_is_no_panel_to_fill(lead):
    later = ui.TeamLink("later", lead.dir.parent / "later")
    assert ui.team_view(later) == {"exists": False}
    url, thread, _ = running(extensions=[])
    page = caller(url)
    status, _, body = page.call("GET", "/api/team")
    assert status == 400 and "no team" in body["error"]
    status, _, body = page.call("POST", "/api/crew", {"action": "rest"})
    assert status == 400 and "no team" in body["error"]
    page.call("POST", "/api/done", {})
    thread.join(5)


def test_a_request_that_reaches_outside_the_project_names_its_folder_and_link_folder_allows_it_and_links_the_folder(team_page, lead, outside):
    page = team_page()
    reach, plain = folder_request(lead, outside), ask_perm(lead, "wc -c")
    perms = {r["id"]: r for r in page.call("GET", "/api/requests")[2]["perms"]}
    assert (perms[reach]["folder"], perms[reach]["write"]) == (providers.shown(outside), False)
    assert "folder" not in perms[plain] and "write" not in perms[plain]
    status, _, reply = page.call("POST", "/api/answer", {"id": reach, "allow": True, "link": True})
    assert status == 200 and (reply["answered"]["state"], reply["answered"]["by"]) == ("allowed", "user")
    assert [r["id"] for r in reply["requests"]["perms"]] == [plain]
    (linked,) = lead.folders()
    assert os.path.realpath(linked["path"]) == os.path.realpath(outside) and linked["write"] is False and linked["by"] == "user"
    assert page.call("GET", "/api/team")[2]["folders"] == providers.shown(lead.folders())


def test_a_call_that_writes_links_the_folder_for_writing_and_only_an_allow_can_link(team_page, lead, outside):
    page = team_page()
    edit = folder_request(lead, outside, tool="Edit", write=True)
    assert page.call("GET", "/api/requests")[2]["perms"][0]["write"] is True
    plain = ask_perm(lead, "wc -c")
    for body in ({"id": edit, "allow": False, "link": True}, {"id": edit, "allow": True, "link": "yes"}, {"id": plain, "allow": True, "link": True}):
        assert page.call("POST", "/api/answer", body)[0] == 400, body
    assert lead.folders() == [] and {r["id"] for r in page.call("GET", "/api/requests")[2]["perms"]} == {edit, plain}
    assert page.call("POST", "/api/answer", {"id": edit, "allow": True, "link": True})[0] == 200
    (linked,) = lead.folders()
    assert linked["write"] is True and os.path.realpath(linked["path"]) == os.path.realpath(outside)


def test_the_colours_are_kept_in_the_settings_file_and_the_page_comes_in_them(team_page):
    page = team_page()
    assert page.call("GET", "/api/state")[2]["theme"] == "auto" and ui.theme() == "auto"
    assert '<html lang="en" data-theme="auto">' in page.call("GET", f"/?t={page.token}")[2].decode()
    status, _, reply = page.call("POST", "/api/theme", {"theme": "dark"})
    assert status == 200 and reply == {"theme": "dark"} and providers.env_file_get("DEEPCREW_THEME") == "dark"
    assert '<html lang="en" data-theme="dark">' in page.call("GET", f"/?t={page.token}")[2].decode()
    assert page.call("GET", "/api/state")[2]["theme"] == "dark"
    for bad in ("purple", "", None, 1):
        status, _, reply = page.call("POST", "/api/theme", {"theme": bad})
        assert status == 400 and "theme must be one of: auto, light, dark" in reply["error"], bad
    providers.env_file_set({"DEEPCREW_THEME": "Neon"})
    assert ui.theme() == "auto"


def looks(page):
    state, team, requests = (page.call("GET", path)[2] for path in ("/api/state", "/api/team", "/api/requests"))
    return state, {"GET /api/team": team, "GET /api/requests": requests}


def field(key):
    return f'fields.find((f) => f.dataset.key === "{key}")'


def test_save_sends_only_the_settings_the_user_changed(team_page):
    page = team_page()
    state, replies = looks(page)
    assert len(cli.allow_setting()) == len(presets.SCOPES["standard"]["values"]["DEEPCREW_ALLOW"].split(","))
    out = page_script(['await $("#save").onclick();',
                       f'{field("DEEPCREW_ALLOW")}.value += ",make"; await $("#save").onclick();',
                       f'{field("DEEPCREW_MAX_TURNS")}.value = "150"; await $("#save").onclick();'], state, {**replies, "POST /api/save": state})
    untouched, allow, turns = (r["body"]["values"] for r in out["requests"] if r["path"] == "/api/save")
    assert page.call("POST", "/api/save", {"values": untouched})[0] == 200
    assert len(cli.allow_setting()) == 7 and not providers.env_file_has("DEEPCREW_ALLOW")
    assert untouched == {} and allow == {"DEEPCREW_ALLOW": "ls,wc,du,head,tail,cat,diff,make"} and turns == {"DEEPCREW_MAX_TURNS": "150"}


def test_a_setting_that_is_not_set_shows_what_the_team_uses(team_page):
    page = team_page()
    steps = ['return [...$("#strengths").children, ...$("#sizes").children, ...$("#scopes").children].filter((b) => b.getAttribute("aria-pressed") === "true")'
             '.map((b) => b.dataset.name);',
             f'return [{field("DEEPCREW_ALLOW")}.value, {field("DEEPCREW_ALLOW")}.placeholder, {field("DEEPCREW_MAX_ACTIVE")}.value, '
             f'{field("DEEPCREW_MAX_ACTIVE")}.placeholder, {field("DEEPCREW_NOTE_MAX_CHARS")}.placeholder];']
    out = page_script(steps, *looks(page))
    assert out["results"] == [["crowd", "standard"], ["ls,wc,du,head,tail,cat,diff", "none", "", "8", str(cli.DEFAULTS["note_max_chars"])]]
    assert page.call("POST", "/api/preset", {"names": ["locked"]})[0] == 200
    assert page_script(steps, *looks(page))["results"] == [["crowd", "locked"], ["", "none", "", "8", str(cli.DEFAULTS["note_max_chars"])]]


def test_an_option_is_sent_by_its_number_and_the_focus_goes_on_to_the_next_request(team_page, lead):
    choice, perm = ask_question(lead, "which folder?", options=[f"{Path.home()}/data", "none"]), ask_perm(lead, "wc -c")
    page = team_page()
    state, replies = looks(page)
    left = {"answered": {}, "requests": {**replies["GET /api/requests"], "questions": []}}
    out = page_script(['const [row] = $("#waiting-list").children, button = row.querySelector("button"); button.focus(); await button.onclick();'
                       ' return [button.textContent, document.activeElement.textContent, $("#waiting-list").children.length];'],
                      state, {**replies, "POST /api/answer": left})
    assert [r["body"] for r in out["requests"] if r["path"] == "/api/answer"] == [{"id": choice, "option": 0}]
    assert out["results"] == [["~/data", "Allow", 1]]
    assert {r["id"] for r in replies["GET /api/requests"]["perms"]} == {perm}


def test_check_says_a_key_was_not_checked_when_there_is_no_balance_endpoint(team_page, providers_toml, monkeypatch):
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", "sk-" + "b" * 32)
    page = team_page()
    state, replies = looks(page)
    checked = page.call("POST", "/api/check", {"provider": "fake"})[2]
    out = page_script(['await $("#check").onclick(); return [$("#m-key").textContent, $("#m-key").classList.contains("bad")];'],
                      state, {**replies, "POST /api/check": checked})
    assert out["results"] == [["Not checked: the provider has no balance endpoint to ask.", False]]


def test_the_line_after_a_save_names_what_the_team_took(team_page):
    page = team_page()
    state, replies = looks(page)
    memory = page.call("POST", "/api/save", {"values": {"DEEPCREW_PROJECT_MEMORY": "off"}})[2]
    thinking = page.call("POST", "/api/save", {"values": {"DEEPCREW_THINKING": "off"}})[2]
    out = page_script([f"return teamLine({json.dumps(memory)});", f"return teamLine({json.dumps(thinking)});",
                       'return teamLine({sync_error: "ValueError: ~/x/team.json is not readable"});'], state, replies)
    assert out["results"] == [" Team updated: project CLAUDE.md off.", " Team updated: thinking off.",
                              " The team was not updated: ValueError: ~/x/team.json is not readable"]


def test_a_time_that_is_not_today_shows_its_day(team_page):
    page = team_page()
    state, replies = looks(page)
    stopped = page.call("POST", "/api/crew", {"action": "rest"})[2]
    stopped["paused"]["ts"] -= 3 * 86400
    out = page_script(['return clock(Date.now() / 1000);', f'showTeam({json.dumps(stopped)}); return $("#team-status").textContent;'], state, replies)
    today, status = out["results"]
    assert re.fullmatch(r"\d\d:\d\d", today) and re.fullmatch(r"Crew resting since \S.* \d\d:\d\d, by the user", status), status


def test_a_refused_setting_is_named_by_its_fields_label_and_its_field_is_marked(team_page):
    page = team_page()
    state, replies = looks(page)
    status, _, refused = page.call("POST", "/api/save", {"values": {"DEEPCREW_SANDBOX": "off", "DEEPCREW_ALLOW": "ls,make"}})
    assert status == 400 and refused["error"].startswith("DEEPCREW_ALLOW: 'make' could run code")
    out = page_script([f'{field("DEEPCREW_ALLOW")}.value = "ls,make"; await $("#save").onclick();'
                       ' return [$("#m-save").textContent, fields.filter((f) => f.getAttribute("aria-invalid") === "true").map((f) => f.dataset.key)];'],
                      state, {**replies, "POST /api/save": {"__status__": 400, **refused}})
    assert out["results"] == [["Allowed commands" + refused["error"][len("DEEPCREW_ALLOW"):], ["DEEPCREW_ALLOW"]]]


def test_the_saved_figures_tooltip_says_what_it_compares(team_page):
    out = page_script(['showSaved({claude: {"Sonnet 5.5": 3}, compare: "Sonnet 5.5"}, 1, 2); return $("#team-saved").title;'], *looks(team_page()))
    assert out["results"] == ["What the same tokens would cost at Claude Sonnet 5.5's list prices: at least $3.0000 (3.0x cheaper here)."]


NOTES = 'return [...$("#waiting-list").children].map((row) => { const note = row.querySelector(".rq-also"); return [note.hidden, note.textContent]; });'


def test_each_request_shows_under_its_buttons_what_an_allow_adds_as_the_server_words_it(team_page, lead):
    ask_perm(lead, "npm run build")
    ask_perm(lead, tool="WebFetch", tool_input={"url": "https://docs.example.com/guide", "prompt": "summarise"}, member="beta")
    ask_perm(lead, tool="mcp__github__create_issue", tool_input={"title": "Fix the build"})
    ask_perm(lead, "ls && pwd")
    ask_perm(lead, tool="Edit", tool_input={"file_path": "docs/a.md"}, member="beta")
    page = team_page()
    state, replies = looks(page)
    sent = replies["GET /api/requests"]["perms"]
    assert [w and w["kind"] for w in (r["widening"] for r in sent)] == ["command", "site", "tool", None, None]
    out = page_script([NOTES], state, replies)
    assert out["results"] == [[
        [False, "Allow also: `npm run build` runs without asking from now on."],
        [False, "Allow also: members reach docs.example.com without asking."],
        [False, "Allow also: members use mcp__github__create_issue without asking."],
        [True, ""],
        [True, ""]]]
    assert "Allow also: " + sent[0]["widening"]["text"] + "." == out["results"][0][0][1]


def test_the_page_guesses_nothing_a_simple_command_the_server_gives_no_widening_has_no_note_and_no_tooltip(team_page, lead):
    ask_perm(lead, "wc -c")
    page = team_page()
    state, replies = looks(page)
    replies["GET /api/requests"]["perms"][0]["widening"] = None
    out = page_script([NOTES, 'const [allow] = $("#waiting-list").children[0].querySelectorAll("button"); return [allow.textContent, allow.title || ""];'], state, replies)
    assert out["results"] == [[[True, ""]], ["Allow", ""]]
    assert "SHELL_META" not in Path(ui.__file__).with_name("ui.html").read_text(encoding="utf-8")


def test_the_note_is_text_never_markup(team_page, lead, monkeypatch):
    markup = '<img src=x onerror="alert(1)"> & <b>bold</b>'
    monkeypatch.setattr(policy, "widening", lambda board, r: {"kind": "command", "value": "wc -c", "text": markup})
    ask_perm(lead, "wc -c")
    state, replies = looks(team_page())
    assert page_script([NOTES], state, replies)["results"] == [[[False, "Allow also: " + markup + "."]]]


def test_the_page_builds_its_rows_from_text_and_elements_only():
    page = Path(ui.__file__).with_name("ui.html").read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", page)


def test_a_text_that_ends_with_a_full_stop_gets_one_not_two(team_page, lead, monkeypatch):
    monkeypatch.setattr(policy, "widening", lambda board, r: {"kind": "tool", "value": "mcp__x__y", "text": "members use mcp__x__y without asking."})
    ask_perm(lead, tool="mcp__x__y", tool_input={})
    state, replies = looks(team_page())
    assert page_script([NOTES], state, replies)["results"] == [[[False, "Allow also: members use mcp__x__y without asking."]]]


def test_the_note_sits_under_the_buttons_and_the_buttons_that_allow_point_to_it(team_page, lead, outside):
    folder_request(lead, outside)
    page = team_page()
    state, replies = looks(page)
    steps = ['const [row] = $("#waiting-list").children; return [...row.children].map((el) => String(el.classList));',
             'const [row] = $("#waiting-list").children, note = row.querySelector(".rq-also");'
             ' return [row.querySelectorAll("button").map((b) => [b.textContent, b.getAttribute("aria-describedby")]), note.getAttribute("id")];']
    out = page_script(steps, state, replies)
    assert out["results"][0] == ["rq-who", "rq-what", "rq-act", "rq-also", "rq-folder", "msg bad"]
    buttons, note_id = out["results"][1]
    assert note_id.startswith("also-") and buttons == [["Allow", note_id], ["Deny", None], ["Link folder", note_id]]


def test_the_note_follows_the_servers_widening_at_each_look_and_the_row_stays(team_page, lead):
    ask_perm(lead, "npm test")
    state, replies = looks(team_page())
    gone = json.loads(json.dumps(replies["GET /api/requests"]))
    gone["perms"][0]["widening"] = None
    back = json.loads(json.dumps(replies["GET /api/requests"]))
    back["perms"][0]["widening"]["text"] = "`npm test` and its arguments run without asking from now on"
    out = page_script(['const row = $("#waiting-list").children[0], note = row.querySelector(".rq-also"), first = [note.hidden, note.textContent];'
                       f' showRequests({json.dumps(gone)}); const second = [note.hidden, note.textContent];'
                       f' showRequests({json.dumps(back)}); return [first, second, [note.hidden, note.textContent], $("#waiting-list").children[0] === row];'],
                      state, replies)
    assert out["results"] == [[[False, "Allow also: `npm test` runs without asking from now on."], [True, ""],
                               [False, "Allow also: `npm test` and its arguments run without asking from now on."], True]]


def test_the_crew_rests_in_the_pages_words():
    page = Path(ui.__file__).with_name("ui.html").read_text(encoding="utf-8")
    source = Path(ui.__file__).read_text(encoding="utf-8")
    for retired in ("Stop crew", "Stopping", "Crew stopped"):
        assert retired not in page and retired not in source, retired
    assert "Rest crew" in page and "Resting\u2026" in page and '"Crew resting"' in page and '" since "' in page and '", by the "' in page


def test_rest_crew_says_resting_while_the_crew_rests_and_the_status_says_since_when_and_by_whom(team_page):
    page = team_page()
    state, replies = looks(page)
    team = replies["GET /api/team"]
    resting = {**team, "paused": {"by": "user", "ts": time.time()}}
    steps = ['const b = $("#crew-stop"); b.textContent = "Rest crew"; const run = crewAction("rest", b); const during = b.textContent; await run;'
             ' return [during, b.textContent, $("#team-status").textContent];',
             'const b = $("#crew-resume"); b.textContent = "Resume crew"; const run = crewAction("resume", b); const during = b.textContent; await run;'
             ' return [during, b.textContent];']
    out = page_script(steps, state, {**replies, "POST /api/crew": resting})
    (rest_during, rest_after, status), resume = out["results"]
    assert (rest_during, rest_after) == ("Resting\u2026", "Rest crew") and resume == ["Resuming\u2026", "Resume crew"]
    assert re.fullmatch(r"Crew resting since \d\d:\d\d, by the user", status), status
    assert [r["body"] for r in out["requests"] if r["path"] == "/api/crew"] == [{"action": "rest"}, {"action": "resume"}]
