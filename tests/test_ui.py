from __future__ import annotations

import http.client
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepcrew import board as board_module
from deepcrew import cli, extensions, member, policy, presets, providers, settings, ui
from page_helpers import TeamPages, ask_perm, ask_question, caller, filed_at, running, standin_widening

KEY = "sk-" + "a" * 32
REAL_UI_LABEL = cli.ui_label


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "SECURITY", str(tmp_path / "no-security"))


@pytest.fixture(autouse=True)
def _widening(monkeypatch):
    standin_widening(monkeypatch)


@pytest.fixture
def page():
    url, thread, result = running(extensions=[])
    page = caller(url)
    page.thread, page.result = thread, result
    yield page
    if thread.is_alive():
        page.call("POST", "/api/done", {})
    thread.join(5)


def env_text():
    return providers.ENV_FILE.read_text()


def test_the_page_opens_only_with_the_token(page):
    status, reply, body = page.call("GET", f"/?t={page.token}")
    assert status == 200 and b"<title>deepcrew</title>" in body and b"tablist" not in body
    assert reply.getheader("Cache-Control") == "no-store"
    assert reply.getheader("Content-Security-Policy") == "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:"
    assert page.call("GET", "/")[0] == 403
    assert page.call("GET", "/?t=wrong")[0] == 403


def test_every_api_call_needs_the_token_and_the_host(page):
    assert page.call("GET", "/api/state", auth=None)[0] == 403
    assert page.call("GET", "/api/state", auth="wrong")[0] == 403
    assert page.call("POST", "/api/save", {"values": {}}, auth="wrong")[0] == 403
    assert page.call("GET", "/api/state")[0] == 200
    assert page.call("GET", "/api/state", host="evil.example")[0] == 403
    assert page.call("GET", f"/?t={page.token}", host=f"localhost:{page.port}")[0] == 403
    assert not providers.ENV_FILE.exists()


def test_bodies_must_be_small_json_objects(page):
    assert page.call("POST", "/api/save", raw=b"x" * (ui.MAX_BODY + 1))[0] == 413
    assert page.call("POST", "/api/save", raw=b"{nope")[0] == 400
    assert page.call("POST", "/api/save", raw=b"[]")[0] == 400
    assert page.call("POST", "/api/save", raw=b"{}", headers={"Content-Type": "text/plain"})[0] == 415
    assert page.call("GET", "/nothing")[0] == 404
    assert page.call("GET", "/favicon.ico", auth=None)[0] == 204


def test_state_lists_choices_and_never_the_key(page):
    status, _, state = page.call("GET", "/api/state")
    assert status == 200 and state["provider"] == "deepseek" and state["key"] == {"found": False, "source": None, "where": "",
                                                                                         "endpoint": "https://api.deepseek.com/anthropic"}
    assert state["sizes"] == presets.SIZES and state["scopes"] == presets.SCOPES and "default" in state["efforts"]
    assert state["env_file"].startswith("~") and Path(state["env_file"].replace("~", str(Path.home()), 1)) == providers.ENV_FILE
    assert state["strengths"]["strongest"]["values"] == {"DEEPCREW_MODEL": "deepseek-v4-pro[1m]", "DEEPCREW_EFFORT": "max"}
    status, _, state = page.call("POST", "/api/key", {"key": KEY})
    assert status == 200 and state["key"] == {"found": True, "source": "dotenv:DEEPSEEK_API_KEY",
                                                 "where": f"{providers.shown(providers.ENV_FILE)} on this computer (mode 0600)", "endpoint": "https://api.deepseek.com/anthropic"}
    assert KEY not in json.dumps(state) and KEY not in json.dumps(page.call("GET", "/api/state")[2])
    assert providers.ENV_FILE.read_text().count(KEY) == 1
    assert stat.S_IMODE(providers.ENV_FILE.stat().st_mode) == 0o600


def test_the_page_offers_every_ready_provider_and_adds_one_once_its_key_is_saved_or_it_is_chosen(page):
    toml = providers.CONFIG_DIR / "providers.toml"
    state = page.call("GET", "/api/state")[2]
    assert state["providers"] == ["deepseek", *sorted(providers.TEMPLATES)] and state["labels"]["zai"] == "Z.ai (GLM)"
    status, _, zai = page.call("GET", "/api/state?provider=zai")
    assert (status, zai["provider"], zai["key"]["found"], zai["key"]["endpoint"]) == (200, "zai", False, "https://api.z.ai/api/anthropic")
    assert zai["models"] == ["glm-5.3-flash[1m]", "glm-5.3[1m]"] and not toml.exists() and providers.known() == ["deepseek"]
    status, _, zai = page.call("POST", "/api/key", {"key": "zai-" + "k" * 20, "provider": "zai"})
    assert (status, zai["key"]["found"], zai["key"]["source"]) == (200, True, "dotenv:ZAI_API_KEY") and "zai" in providers.known()
    assert "zai-" + "k" * 20 not in json.dumps(zai)
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_PROVIDER": "minimax"}})[0] == 200
    assert "minimax" in providers.known() and providers.setting("DEEPCREW_PROVIDER") == "minimax"
    assert page.call("GET", "/api/state?provider=nobody")[0] == 400


def test_the_state_says_what_a_setting_that_is_not_set_stands_for(page):
    limits = {f"DEEPCREW_{k.upper()}": str(v) for k, v in cli.DEFAULTS.items()}
    assert page.call("GET", "/api/state")[2]["defaults"] == {**limits, "DEEPCREW_ALLOW": presets.SCOPES["standard"]["values"]["DEEPCREW_ALLOW"],
                                                             "DEEPCREW_SANDBOX": "off"}
    assert page.call("POST", "/api/preset", {"names": ["locked"]})[0] == 200
    assert page.call("GET", "/api/state")[2]["defaults"] == limits


def test_a_providers_file_that_is_not_toml_is_named_with_a_tilde(page, providers_toml):
    providers_toml("[fake\nbase_url = 1\n")
    status, _, body = page.call("GET", "/api/state")
    assert status == 400 and body["error"].startswith("~/.config/deepcrew/providers.toml is not valid TOML") and str(Path.home()) not in body["error"]


def test_a_bad_key_is_refused_and_not_echoed(page):
    status, _, body = page.call("POST", "/api/key", {"key": "nope"})
    assert status == 400 and "nothing was stored" in body["error"] and "nope" not in body["error"]
    assert not providers.ENV_FILE.exists()
    assert page.call("POST", "/api/key", {"key": 5})[0] == 400


def test_the_keychain_is_used_where_there_is_one(page, monkeypatch):
    monkeypatch.setattr(ui, "use_keychain", lambda p=None: True)
    stored = []
    monkeypatch.setattr(providers, "store_keychain", lambda p, key: stored.append(key) or "keychain:deepcrew-deepseek")
    status, _, state = page.call("POST", "/api/key", {"key": KEY})
    assert status == 200 and stored == [KEY] and KEY not in json.dumps(state)
    assert not providers.ENV_FILE.exists()


def test_save_writes_the_env_file_privately(page):
    values = {"DEEPCREW_MODEL": "deepseek-v4-pro[1m]", "DEEPCREW_EFFORT": "high", "DEEPCREW_MAX_ACTIVE": "3", "DEEPCREW_SANDBOX": "on",
              "DEEPCREW_SANDBOX_DOMAINS": "PyPI.org, example.com", "DEEPCREW_ALLOW": "ls,  npm test"}
    status, _, state = page.call("POST", "/api/save", {"values": values})
    assert status == 200 and stat.S_IMODE(providers.ENV_FILE.stat().st_mode) == 0o600
    assert "DEEPCREW_MODEL=deepseek-v4-pro[1m]" in env_text() and "DEEPCREW_SANDBOX_DOMAINS=pypi.org,example.com" in env_text()
    assert state["values"]["DEEPCREW_ALLOW"] == "ls,npm test" and state["values"]["DEEPCREW_MAX_ACTIVE"] == "3"


@pytest.mark.parametrize("values, message", [
    ({"DEEPCREW_NOPE": "1"}, "unknown setting"),
    ({"DEEPCREW_MODEL": "gpt-9"}, "DEEPCREW_MODEL must be one of"),
    ({"DEEPCREW_EFFORT": "extreme"}, "DEEPCREW_EFFORT must be one of"),
    ({"DEEPCREW_PROVIDER": "nobody"}, "DEEPCREW_PROVIDER must be one of"),
    ({"DEEPCREW_MAX_ACTIVE": "0"}, "positive whole number"),
    ({"DEEPCREW_HOLD_S": "-5"}, "positive whole number"),
    ({"DEEPCREW_OFFER_SIZE": "many"}, "positive whole number"),
    ({"DEEPCREW_SANDBOX": "maybe"}, "on or off"),
    ({"DEEPCREW_SANDBOX_DOMAINS": "a.com; b.com"}, "DEEPCREW_SANDBOX_DOMAINS"),
    ({"DEEPCREW_ALLOW": "ls; rm -rf ~"}, "shell syntax"),
    ({"DEEPCREW_ALLOW": "ls $(whoami)"}, "shell syntax"),
    ({"DEEPCREW_SANDBOX": "off", "DEEPCREW_ALLOW": "ls,python3 -m pytest"}, "'python3 -m pytest' could run code and the sandbox is off"),
    ({"DEEPCREW_SANDBOX": "off", "DEEPCREW_ALLOW": "ls,jq ."}, "'jq .' is not one of the read-only programs"),
])
def test_save_refuses_bad_values_and_writes_nothing(page, values, message):
    status, _, body = page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_TURNS": "7", **values}})
    assert status == 400 and message in body["error"]
    assert not providers.ENV_FILE.exists()


@pytest.mark.sandbox
def test_turning_the_sandbox_off_checks_the_saved_allowlist(page):
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_ALLOW": "ls,make"}})[0] == 200
    status, _, body = page.call("POST", "/api/save", {"values": {"DEEPCREW_SANDBOX": "off"}})
    assert status == 400 and "'make' could run code" in body["error"]
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_SANDBOX": "off", "DEEPCREW_ALLOW": "ls"}})[0] == 200
    assert "DEEPCREW_SANDBOX=off" in env_text()


def test_an_unset_sandbox_is_off_for_the_saved_allowlist_too(page):
    assert providers.setting("DEEPCREW_SANDBOX") is None
    status, _, body = page.call("POST", "/api/save", {"values": {"DEEPCREW_ALLOW": "ls,make"}})
    assert status == 400 and "'make' could run code" in body["error"]
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_ALLOW": "ls,du"}})[0] == 200


def test_presets_combine_a_strength_a_size_and_a_scope(page):
    status, _, state = page.call("POST", "/api/preset", {"names": ["strongest", "crowd", "builder"]})
    assert status == 200
    expected = presets.values(["strongest", "crowd", "builder"], providers.load("deepseek"))
    assert {k: state["values"].get(k) for k in expected} == {k: v or None for k, v in expected.items()}
    assert "DEEPCREW_MAX_ACTIVE=8" in env_text() and "DEEPCREW_MODEL=deepseek-v4-pro[1m]" in env_text()
    for bad in ({"names": ["nope"]}, {"names": ["solo", "pair"]}, {"names": []}, {"names": "solo"}, {"setup": "huge"}):
        assert page.call("POST", "/api/preset", bad)[0] == 400, bad


def test_a_setup_on_the_page_sets_all_three_and_the_page_has_its_row(page):
    status, _, state = page.call("POST", "/api/preset", {"setup": "strong"})
    expected = presets.values(["strongest", "squad", "builder"], providers.load("deepseek"))
    assert status == 200 and {k: state["values"].get(k) for k in expected} == {k: v or None for k, v in expected.items()}
    assert state["setups"]["strong"]["presets"] == ["strongest", "squad", "builder"] and state["setups"]["strong"]["values"] == expected
    html = (Path(ui.__file__).parent / "ui.html").read_text(encoding="utf-8")
    assert 'id="setups"' in html and '{setup: name}' in html


def test_a_provider_from_providers_toml_is_validated_against_its_own_models(page, providers_toml):
    providers_toml()
    assert "fake" in page.call("GET", "/api/state")[2]["providers"]
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_PROVIDER": "fake", "DEEPCREW_MODEL": "m", "DEEPCREW_EFFORT": "default"}})[0] == 200
    status, _, state = page.call("GET", "/api/state?provider=fake")
    assert status == 200 and state["models"] == ["m"] and state["efforts"] == ["default"]
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_MODEL": "deepseek-flash[1m]"}})[0] == 400


def test_check_needs_a_key_and_only_then_asks_the_provider(page, monkeypatch):
    assert page.call("POST", "/api/check", {})[0] == 400
    page.call("POST", "/api/key", {"key": KEY})
    seen = []
    monkeypatch.setattr(providers, "balance", lambda p, key: seen.append(key) or {"ok": True, "status": 200, "balances": {"CNY": "1.00"}})
    status, _, body = page.call("POST", "/api/check", {})
    assert status == 200 and body["balances"] == {"CNY": "1.00"} and seen == [KEY] and KEY not in json.dumps(body)


def test_a_provider_without_a_balance_endpoint_is_not_asked_and_the_key_is_not_called_checked(page, providers_toml, monkeypatch):
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", "sk-" + "b" * 32)
    monkeypatch.setattr(providers, "balance", lambda p, key: pytest.fail("there is nothing to ask"))
    status, _, body = page.call("POST", "/api/check", {"provider": "fake"})
    assert status == 200 and body == {"checked": False, "detail": "the provider has no balance endpoint to ask"}


def test_done_stops_the_server_and_returns_the_settings(page):
    page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_ACTIVE": "2"}})
    status, _, body = page.call("POST", "/api/done", {})
    assert (status, body) == (200, {"ok": True})
    page.thread.join(5)
    assert not page.thread.is_alive()
    assert page.result["done"] is True and page.result["values"]["DEEPCREW_MAX_ACTIVE"] == "2"
    assert "key" in page.result and KEY not in json.dumps(page.result)
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", page.port, timeout=2).request("GET", "/")


def test_serve_gives_up_after_the_timeout():
    result = ui.serve(timeout_s=0.05, extensions=[])
    assert result["done"] is False and result["provider"] == "deepseek"


class Demo(ui.Extension):
    name = "demo"
    html = b"<p id=demo>demo page</p>"

    def __init__(self, log):
        super().__init__()
        self.log = log
        self.routes = {("GET", "/api/demo/hello"): lambda body, query: {"hello": query.get("who", ["world"])[0]},
                       ("POST", "/api/demo/echo"): lambda body, query: {"echo": body}}
        self.streams = {"/api/demo/events": self.events}

    def start(self):
        self.log.append("start")

    def stop(self):
        self.log.append("stop")

    def events(self, handler):
        handler.sse()
        handler.wfile.write(b"event: hi\ndata: {}\n\n")
        handler.wfile.flush()
        handler.server.closing.wait(5)


def test_an_extension_adds_its_page_its_routes_and_its_stream():
    log = []
    url, thread, result = running(extensions=[Demo(log)])
    page = caller(url)
    status, _, body = page.call("GET", f"/?t={page.token}")
    assert status == 200 and body.count(b"</body>") == 1 and body.index(b"<p id=demo>") < body.index(b"</body>")
    assert page.call("GET", "/api/demo/hello?who=you")[2] == {"hello": "you"}
    assert page.call("POST", "/api/demo/echo", {"a": 1})[2] == {"echo": {"a": 1}}
    assert page.call("GET", "/api/demo/hello", auth=None)[0] == 403 and page.call("GET", "/api/demo/hello", host="evil.example")[0] == 403
    assert page.call("POST", "/api/demo/echo", raw=b"x" * (ui.MAX_BODY + 1))[0] == 413
    assert page.call("GET", "/api/demo/nothing")[0] == 404
    conn = http.client.HTTPConnection("127.0.0.1", page.port, timeout=3)
    conn.request("GET", "/api/demo/events", headers={"Host": f"127.0.0.1:{page.port}", "X-Deepcrew-Token": page.token})
    reply = conn.getresponse()
    assert reply.status == 200 and reply.getheader("Content-Type") == "text/event-stream" and "default-src 'self'" in reply.getheader("Content-Security-Policy")
    assert reply.readline() == b"event: hi\n"
    assert log == ["start"]
    assert page.call("POST", "/api/done", {})[0] == 200
    thread.join(5)
    assert log == ["start", "stop"] and result["done"] is True
    assert reply.read() == b"data: {}\n\n"
    conn.close()


def test_the_streams_stay_behind_the_token_and_the_host():
    url, thread, _ = running(extensions=[Demo([])])
    page = caller(url)
    for auth, host in (("wrong", f"127.0.0.1:{page.port}"), (page.token, "evil.example")):
        conn = http.client.HTTPConnection("127.0.0.1", page.port, timeout=3)
        conn.request("GET", "/api/demo/events", headers={"Host": host, "X-Deepcrew-Token": auth})
        assert conn.getresponse().status == 403
        conn.close()
    page.call("POST", "/api/done", {})
    thread.join(5)


def test_an_extension_may_only_use_routes_under_its_own_name(capsys):
    class Greedy(Demo):
        def __init__(self, log):
            super().__init__(log)
            self.routes = {("GET", "/api/state"): lambda body, query: {"taken": True}}

    class Wild(Demo):
        name = "wild"

    log = []
    url, thread, _ = running(extensions=[Greedy(log), Wild(log), Demo(log)])
    page = caller(url)
    assert page.call("GET", "/api/state")[2]["provider"] == "deepseek"
    assert page.call("GET", "/api/demo/hello")[0] == 200 and log == ["start"]
    page.call("POST", "/api/done", {})
    thread.join(5)
    assert capsys.readouterr().err.count("skipped") == 2


def test_an_extension_whose_name_is_taken_or_bad_or_whose_paths_clash_is_skipped_and_says_why(capsys):
    class Twin(Demo):
        def __init__(self, log):
            super().__init__(log)
            self.routes, self.streams = {("GET", "/api/demo/other"): lambda body, query: {"twin": True}}, {}

    class Bad(Demo):
        name = "Bad Name"

    class Tangled(Demo):
        name = "tangled"

        def __init__(self, log):
            super().__init__(log)
            self.routes, self.streams = {("GET", "/api/tangled/x"): lambda body, query: {}}, {"/api/tangled/x": self.events}

    kept, routes, streams = ui._mount([Demo([]), Twin([]), Bad([]), Tangled([])])
    assert [e.name for e in kept] == ["demo"] and ("GET", "/api/demo/other") not in routes and set(streams) == {"/api/demo/events"}
    assert capsys.readouterr().err.splitlines() == [
        "deepcrew ui: extension 'demo' skipped (another extension has this name)",
        "deepcrew ui: extension 'Bad Name' skipped (a name is lowercase letters, digits and dashes, starting with a letter)",
        "deepcrew ui: extension 'tangled' skipped (routes must be GET or POST under /api/tangled/, and a stream's path is not also a route's)"]


def declare(monkeypatch, *pages):
    monkeypatch.setattr(extensions, "loaded", lambda project=None: [(spec, SimpleNamespace(page=factory)) for spec, factory in pages])


def test_pages_come_from_the_declared_extensions_and_a_page_hook_that_raises_is_skipped(monkeypatch, capsys, tmp_path):
    made = []

    def good(project, team):
        made.append((project, team))
        return Demo([])

    def broken(project, team):
        raise RuntimeError("no luck")

    declare(monkeypatch, ("pkg.broken", broken), ("pkg.none", lambda p, t: None), ("pkg.good", good))
    assert [e.name for e in ui.load_extensions(tmp_path, "main")] == ["demo"] and made == [(tmp_path, "main")]
    assert capsys.readouterr().err == "deepcrew ui: extension pkg.broken skipped (RuntimeError: no luck)\n"
    url, thread, _ = running(project=tmp_path, team="crew")
    page = caller(url)
    assert page.call("GET", "/api/demo/hello")[0] == 200 and made[-1] == (tmp_path, "crew")
    page.call("POST", "/api/done", {})
    thread.join(5)


def test_the_page_is_named_after_the_extensions_that_add_a_tab(monkeypatch):
    def tab(project, team):
        return Demo([])

    def section(project, team):
        return Demo([])

    section.tab = False

    def stand_in(project, team):
        return Demo([])

    stand_in.replaces = "live"

    points = [("pkg.zeta", tab), ("pkg.mod:alpha", tab), ("pkg.section", section), ("pkg.own", stand_in)]
    monkeypatch.setattr(cli, "ui_label", REAL_UI_LABEL)
    assert ui.tab_names() == [] and cli.ui_label() == "team call"
    declare(monkeypatch, *points[2:])
    assert ui.tab_names() == [] and cli.ui_label() == "team call"
    declare(monkeypatch, *points)
    assert ui.tab_names() == ["alpha", "zeta"] and cli.ui_label() == "team call, alpha, zeta"


def test_the_page_has_the_live_view_and_the_tools_of_its_own_and_an_extension_of_the_same_name_stands_in_for_one(tmp_path):
    own = ui.builtin_pages(tmp_path, "t")
    assert [e.name for e in own] == ["live", "tools"] and own[0].routes and own[0].streams and b"</script>" in own[0].html
    assert own[1].html == b"" and set(own[1].routes) == {("GET", "/api/tools/list"), ("POST", "/api/tools/mcp"), ("POST", "/api/tools/skill")}

    class StandIn(ui.Extension):
        name = "live"

    other, stand_in = Demo([]), StandIn()
    assert ui.with_builtin(own, [other]) == [*own, other]
    assert ui.with_builtin(own, [stand_in, other]) == [own[1], stand_in, other]


def test_serve_ends_the_extensions_on_the_timeout_too():
    log = []
    result = ui.serve(timeout_s=0.05, extensions=[Demo(log)])
    assert result["done"] is False and log == ["start", "stop"]


def test_the_team_name_in_the_pages_title_is_text():
    url, thread, _ = running(extensions=[], team="x</title><script>alert(1)</script>")
    page = caller(url)
    body = page.call("GET", f"/?t={page.token}")[2]
    assert b"<title>deepcrew x&lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;</title>" in body and body.count(b"<script>") == 1
    page.call("POST", "/api/done", {})
    thread.join(5)


def test_the_state_follows_the_env_file_between_two_loads(page):
    assert page.call("GET", "/api/state")[2]["values"] == {}
    providers.env_file_set({"DEEPCREW_MAX_ACTIVE": "3"})
    assert page.call("GET", "/api/state")[2]["values"]["DEEPCREW_MAX_ACTIVE"] == "3"


def test_the_ui_command_prints_the_url_and_ends_at_the_timeout(deepcrew, monkeypatch):
    code, out, _ = deepcrew("ui", "--timeout", 0.05, "--json")
    assert code == 0 and out.startswith('{"url": "http://127.0.0.1:') and '"done": false' in out
    code, out, _ = deepcrew("ui", "--timeout", 0.05)
    assert code == 0 and out.splitlines()[0].startswith("http://127.0.0.1:") and out.splitlines()[-1].startswith("timed out without Done")


@pytest.fixture
def served(lead):
    pages = TeamPages(lead)
    yield pages
    pages.close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_team_has_one_page_and_its_address_is_in_run_ui_json(served, lead):
    first = served()
    info = first.info
    path = lead.dir / "run" / "ui.json"
    assert json.loads(path.read_text()) == info and set(info) == {"url", "port", "pid", "started", "version"} and info["pid"] == os.getpid()
    assert info["version"] == ui.__version__
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert first.call("GET", "/api/ping")[2]["ok"] is True and first.call("GET", "/api/ping")[2]["team"] == "main"
    assert first.call("GET", "/api/ping", auth="wrong")[0] == 403 and first.call("GET", "/api/ping", host="evil.example")[0] == 403
    assert first.call("GET", "/api/state")[2]["team"] == "main"
    began = time.monotonic()
    ui.serve_team(lead.root, "main", 30)
    assert time.monotonic() - began < 2
    assert json.loads(path.read_text()) == info and first.call("GET", "/api/ping")[0] == 200
    assert not (lead.dir / "run" / "ui.lock").exists()


def test_a_foreground_page_belongs_to_no_team(page):
    assert page.call("GET", "/api/state")[2]["team"] is None and page.call("GET", "/api/ping")[2]["team"] is None
    assert "changed" not in page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_TURNS": "7"}})[2]


def test_a_ui_json_nobody_answers_for_is_ignored_and_removed(lead):
    path = lead.dir / "run" / "ui.json"
    path.parent.mkdir(exist_ok=True)
    assert ui.running(lead.dir) is None
    for text in ("not json", "[]", json.dumps({"url": "http://127.0.0.1:1/?t=x"}), json.dumps({"url": f"http://127.0.0.1:{free_port()}/?t=x", "port": free_port(), "pid": 1, "started": 0})):
        path.write_text(text)
        assert ui.running(lead.dir) is None
        assert text == "not json" or not path.exists()
    path.write_text("not json")
    assert ui.running(lead.dir) is None and path.exists()


def test_a_stale_file_does_not_stop_a_new_server_from_starting(served, lead):
    path = lead.dir / "run" / "ui.json"
    path.parent.mkdir(exist_ok=True)
    stale = {"url": f"http://127.0.0.1:{free_port()}/?t=old", "port": free_port(), "pid": 1, "started": 0}
    path.write_text(json.dumps(stale))
    page = served()
    assert page.info["pid"] == os.getpid() and page.info["started"] > 0 and not page.info["url"].endswith("?t=old")
    assert json.loads(path.read_text()) == page.info and page.call("GET", "/api/ping")[0] == 200


def test_a_file_nobody_answers_for_is_removed_only_under_the_lock_and_while_it_still_names_that_server(lead, monkeypatch):
    path = lead.dir / "run" / "ui.json"
    path.parent.mkdir(exist_ok=True)
    stale = {"url": f"http://127.0.0.1:{free_port()}/?t=old", "port": free_port(), "pid": 1, "started": 0}
    newer = {**stale, "pid": 2, "started": 1}

    def replaced(info):
        path.write_text(json.dumps(newer))
        return False

    path.write_text(json.dumps(stale))
    monkeypatch.setattr(ui, "_answers", replaced)
    assert ui.running(lead.dir) is None and json.loads(path.read_text()) == newer
    monkeypatch.setattr(ui, "_answers", lambda info: False)
    monkeypatch.setattr(board_module, "LOCK_WAIT_S", 0.2)
    with board_module.file_lock(lead.dir / "run" / "ui"):
        assert ui.running(lead.dir) is None and path.exists()
    assert ui.running(lead.dir) is None and not path.exists()


def test_a_restarted_page_takes_its_old_address_so_an_open_tab_finds_it_again(served, lead):
    first = served(timeout_s=0.5)
    first.thread.join(5)
    assert not first.thread.is_alive() and ui.running(lead.dir) is None
    again = served()
    assert again.info["url"] == first.info["url"] and again.call("GET", "/api/ping")[0] == 200
    assert stat.S_IMODE((lead.dir / "run" / "ui-address.json").stat().st_mode) == 0o600


def test_a_page_whose_port_is_taken_keeps_its_token_on_a_new_port(served, lead):
    (lead.dir / "run").mkdir(exist_ok=True)
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        (lead.dir / "run" / "ui-address.json").write_text(json.dumps({"port": held.getsockname()[1], "token": "k" * 22}))
        page = served()
        assert page.info["port"] != held.getsockname()[1] and page.info["url"].endswith("/?t=" + "k" * 22)


@pytest.mark.parametrize("text", ["not json", json.dumps({"port": 70000, "token": "k" * 22}), json.dumps({"port": 1, "token": "x y"})])
def test_an_address_file_that_cannot_be_used_means_a_new_address(lead, text):
    (lead.dir / "run").mkdir(exist_ok=True)
    (lead.dir / "run" / "ui-address.json").write_text(text)
    assert ui._last_address(lead.dir) == (0, None)


def test_the_page_outlasts_its_timeout_while_members_work(lead, monkeypatch):
    link = ui.TeamLink("main", lead.dir)
    assert ui._crew_works(link) is False
    lead.update_member("beta", state="active", pid=4242)
    assert ui._crew_works(link) is True
    real, asked = ui._crew_works, []

    def works(page_link):
        if page_link.dir != lead.dir:
            return real(page_link)
        asked.append(ui.running(lead.dir) is not None)
        return len(asked) < 3

    monkeypatch.setattr(ui, "_crew_works", works)
    ui.serve_team(lead.root, "main", 0.05)
    assert asked == [True, True, True] and ui.running(lead.dir) is None


class FakeServer:
    def __init__(self, outcome, calls):
        self.outcome, self.calls = outcome, calls

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.outcome == "serves":
            argv = list(argv)
            project, team, timeout = Path(argv[argv.index("--project") + 1]), argv[argv.index("--team") + 1], float(argv[argv.index("--timeout") + 1])
            threading.Thread(target=ui.serve_team, args=(project, team, timeout), daemon=True).start()
        return SimpleNamespace(poll=lambda: None if self.outcome == "serves" else 1)


def test_start_background_starts_one_detached_server_and_then_hands_out_its_url(lead, monkeypatch):
    calls = []
    monkeypatch.setattr(ui.subprocess, "Popen", FakeServer("serves", calls))
    url = ui.start_background(lead.root, "main", lead.dir, 123.0)
    (argv, kw), = calls
    assert argv == [sys.executable, "-P", "-m", "deepcrew", "ui", "--serve", "--project", str(lead.root), "--team", "main", "--timeout", "123.0"]
    assert {k: kw[k] for k in member.detach_options()} == member.detach_options() and kw["stdin"] == subprocess.DEVNULL
    assert url == ui.running(lead.dir)["url"] and caller(url).call("GET", "/api/ping")[0] == 200
    assert ui.start_background(lead.root, "main", lead.dir) == url and len(calls) == 1
    assert caller(url).call("GET", f"/?t={caller(url).token}")[0] == 200


def test_start_background_replaces_a_page_that_an_older_deepcrew_started(lead, monkeypatch):
    calls = []
    monkeypatch.setattr(ui.subprocess, "Popen", FakeServer("serves", calls))
    url = ui.start_background(lead.root, "main", lead.dir, 123.0)
    path = lead.dir / "run" / ui.UI_FILE
    old = {k: v for k, v in json.loads(path.read_text()).items() if k != "version"}
    path.write_text(json.dumps(old))
    again = ui.start_background(lead.root, "main", lead.dir, 123.0)
    assert len(calls) == 2 and ui.running(lead.dir)["version"] == ui.__version__
    assert again == url and caller(again).call("GET", "/api/ping")[0] == 200


def test_start_background_names_the_log_when_the_server_does_not_come_up(lead, monkeypatch):
    calls = []
    monkeypatch.setattr(ui.subprocess, "Popen", FakeServer("dies", calls))
    with pytest.raises(ValueError) as refused:
        ui.start_background(lead.root, "main", lead.dir)
    assert str(refused.value) == "see " + providers.shown(lead.dir / "run" / "ui.log")
    assert str(refused.value).startswith("see ~") and str(Path.home()) not in str(refused.value)
    monkeypatch.setattr(ui, "START_WAIT_S", 0.3)
    monkeypatch.setattr(ui.subprocess, "Popen", lambda argv, **kw: SimpleNamespace(poll=lambda: 0))
    with pytest.raises(ValueError, match="ui.log"):
        ui.start_background(lead.root, "main", lead.dir)


def test_the_page_server_does_not_import_what_a_member_wrote_in_the_project(lead, monkeypatch):
    calls = []
    with monkeypatch.context() as m:
        m.setattr(ui.subprocess, "Popen", FakeServer("dies", calls))
        with pytest.raises(ValueError):
            ui.start_background(lead.root, "main", lead.dir)
    (argv, _), = calls
    python = argv[:argv.index("-m")]
    (lead.root / "json.py").write_text("open('planted', 'w').close()\n")
    subprocess.run([*python, "-c", "import json"], cwd=lead.root, check=True)
    assert python == [sys.executable, "-P"] and not (lead.root / "planted").exists()


@pytest.mark.sandbox
def test_a_save_on_a_teams_page_reaches_the_team_at_once(served, lead):
    page = served()
    status, _, reply = page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_TURNS": "150", "DEEPCREW_SANDBOX": "off", "DEEPCREW_ALLOW": "ls,wc"}})
    assert status == 200 and reply["values"]["DEEPCREW_MAX_TURNS"] == "150" and "sync_error" not in reply
    assert reply["changed"]["max_turns"] == [200, 150] and reply["changed"]["sandbox"][1] is None and reply["changed"]["bash_allow"][1] == ["ls", "wc"]
    cfg = board_module.read_json(lead.dir / "team.json")
    assert (cfg["max_turns"], cfg["sandbox"], cfg["bash_allow"]) == (150, None, ["ls", "wc"])
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_TURNS": "150"}})[2]["changed"] == {}
    status, _, reply = page.call("POST", "/api/preset", {"names": ["pair"]})
    assert status == 200 and reply["changed"]["max_active"] == [lead.cfg["max_active"], 2] and board_module.read_json(lead.dir / "team.json")["max_active"] == 2


def test_only_the_keys_just_written_are_carried_into_the_team(served, lead, monkeypatch):
    seen = []
    monkeypatch.setattr(settings, "sync_team", lambda board, keys: seen.append((board.dir, set(keys))) or {"team": "main", "changed": {"max_turns": [1, 2]}})
    page = served()
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_TURNS": "9", "DEEPCREW_HOLD_S": "5"}})[2]["changed"] == {"max_turns": [1, 2]}
    assert page.call("POST", "/api/save", {"values": {}})[2]["changed"] == {}
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_TURNS": "x"}})[0] == 400
    page.call("POST", "/api/preset", {"names": ["solo"]})
    assert seen == [(lead.dir, {"DEEPCREW_MAX_TURNS", "DEEPCREW_HOLD_S"}), (lead.dir, set(presets.values(["solo"], providers.load("deepseek"))))]


def test_a_team_that_cannot_follow_says_so_and_the_settings_stay_saved(served, lead, monkeypatch):
    def broken(board, keys):
        raise ValueError("team.json is not readable")
    monkeypatch.setattr(settings, "sync_team", broken)
    page = served()
    status, _, reply = page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_TURNS": "9"}})
    assert status == 200 and reply["changed"] == {} and "team.json is not readable" in reply["sync_error"]
    assert "DEEPCREW_MAX_TURNS=9" in env_text()


def test_a_team_that_does_not_exist_yet_has_nothing_to_follow(lead, monkeypatch):
    link = ui.TeamLink("later", lead.dir.parent / "later")
    monkeypatch.setattr(settings, "sync_team", lambda *a: pytest.fail("there is no team to change"))
    assert link.sync(["DEEPCREW_MAX_TURNS"]) == {"team": "later", "changed": {}}
    assert ui.save({"DEEPCREW_MAX_TURNS": "4"}, link)["changed"] == {}


@pytest.fixture
def waiting(monkeypatch):
    looked = threading.Event()

    def sleep(seconds):
        looked.set()
        time.sleep(seconds)

    monkeypatch.setattr(ui, "time", SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=sleep))

    def start(team_dir, timeout_s=5):
        looked.clear()
        got = []
        thread = threading.Thread(target=lambda: got.append(ui.wait_done(team_dir, timeout_s)), daemon=True)
        thread.start()
        assert looked.wait(5), "wait_done did not take its first look"
        return thread, got

    return start


def test_done_confirms_the_settings_for_whoever_waits_and_the_page_stays(served, lead, waiting):
    page = served()
    page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_TURNS": "42"}})
    waiter, got = waiting(lead.dir)
    status, _, reply = page.call("POST", "/api/done", {})
    waiter.join(5)
    assert (status, reply["ok"]) == (200, True) and not waiter.is_alive()
    (event,) = got
    assert event["kind"] == "done" and event["values"]["DEEPCREW_MAX_TURNS"] == "42" and event["changed"] == {} and abs(event["ts"] - time.time()) < 10
    assert [json.loads(line) for line in (lead.dir / "run" / "ui-events.jsonl").read_text().splitlines()] == [event]
    assert page.thread.is_alive() and page.call("GET", "/api/state")[0] == 200
    page.call("POST", "/api/done", {})
    assert len((lead.dir / "run" / "ui-events.jsonl").read_text().splitlines()) == 2


def test_done_carries_what_the_team_did_not_yet_have(served, lead):
    providers.env_file_set({"DEEPCREW_HOLD_S": "77"})
    page = served()
    reply = page.call("POST", "/api/done", {})[2]
    assert reply["changed"] == {"hold_s": [lead.cfg["hold_s"], 77]} and board_module.read_json(lead.dir / "team.json")["hold_s"] == 77


def test_wait_done_ignores_what_was_there_before_and_gives_up_at_its_timeout(lead, waiting):
    events = lead.dir / "run" / "ui-events.jsonl"
    events.parent.mkdir(exist_ok=True)
    events.write_text(json.dumps({"ts": 1.0, "kind": "done", "values": {}, "changed": {}}) + "\n")
    began = time.monotonic()
    assert ui.wait_done(lead.dir, 0.3) is None and 0.25 < time.monotonic() - began < 2
    waiter, got = waiting(lead.dir)
    with open(events, "a") as f:
        f.write(json.dumps({"ts": 2.0, "kind": "other"}) + "\n" + json.dumps({"ts": 3.0, "kind": "done", "values": {"a": "b"}, "changed": {}}) + "\n")
    waiter.join(5)
    assert got == [{"ts": 3.0, "kind": "done", "values": {"a": "b"}, "changed": {}}]
    assert ui.wait_done(lead.dir / "nowhere", 0.1) is None


def test_a_teams_page_ends_with_its_timeout_and_takes_its_file_with_it(lead):
    thread = threading.Thread(target=ui.serve_team, args=(lead.root, "main", 0.5), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ui.running(lead.dir):
        time.sleep(0.02)
    info = ui.running(lead.dir)
    assert info
    thread.join(5)
    assert not thread.is_alive() and not (lead.dir / "run" / "ui.json").exists()
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", info["port"], timeout=2).request("GET", "/")
    assert ui.running(lead.dir) is None


def test_a_server_never_removes_the_file_of_the_one_that_replaced_it(lead):
    thread = threading.Thread(target=ui.serve_team, args=(lead.root, "main", 0.6), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ui.running(lead.dir):
        time.sleep(0.02)
    path = lead.dir / "run" / "ui.json"
    successor = {"url": "http://127.0.0.1:9/?t=x", "port": 9, "pid": 1, "started": 0}
    path.write_text(json.dumps(successor))
    thread.join(5)
    assert json.loads(path.read_text()) == successor


ABS = os.path.abspath(os.sep + "secrets")


@pytest.mark.parametrize("key, good, stored", [
    ("DEEPCREW_ALLOW_OFF", " ls ,  wc   -l ", "ls,wc -l"),
    ("DEEPCREW_SANDBOX_DENY_READ", f"~/.ssh, {ABS}", f"~/.ssh,{ABS}"),
    ("DEEPCREW_SANDBOX_DENY_READ", "NONE", "none"),
    ("DEEPCREW_SANDBOX_DENY_READ", "", ""),
    ("DEEPCREW_SANDBOX_ALLOW_WRITE", f"{ABS}, ~/out", f"{ABS},~/out"),
    ("DEEPCREW_SANDBOX_ALLOW_WRITE", "", ""),
    ("DEEPCREW_TOOLS_OFF", "Agent, WebFetch", "Agent,WebFetch"),
    ("DEEPCREW_TOOLS_OFF", "", ""),
    ("DEEPCREW_PROPOSE_AFTER_S", "90", "90"),
    ("DEEPCREW_MAX_ACTIVE", " 32 ", "32"),
    ("DEEPCREW_MAX_ACTIVE", "016", "16"),
])
def test_checked_accepts_the_values_the_advanced_keys_take(key, good, stored):
    assert ui.checked({key: good}) == {key: stored}


@pytest.mark.parametrize("key, bad, message", [
    ("DEEPCREW_ALLOW_OFF", "ls; rm -rf ~", "DEEPCREW_ALLOW_OFF: 'ls; rm -rf ~' contains shell syntax"),
    ("DEEPCREW_ALLOW_OFF", "echo 'a", "unbalanced quotes"),
    ("DEEPCREW_MAX_ACTIVE", "33", "DEEPCREW_MAX_ACTIVE is at most 32"),
    ("DEEPCREW_SANDBOX_DENY_READ", "relative/path", "not an absolute path"),
    ("DEEPCREW_SANDBOX_DENY_READ", f"{ABS},none", "'none' is not an absolute path"),
    ("DEEPCREW_SANDBOX_DENY_READ", f"{ABS}\nsecond", "not an absolute path"),
    ("DEEPCREW_SANDBOX_ALLOW_WRITE", "none", "DEEPCREW_SANDBOX_ALLOW_WRITE: 'none' is not an absolute path"),
    ("DEEPCREW_SANDBOX_ALLOW_WRITE", "build", "not an absolute path"),
    ("DEEPCREW_TOOLS_OFF", "Bash", "DEEPCREW_TOOLS_OFF: Bash cannot be switched off"),
    ("DEEPCREW_TOOLS_OFF", "agent", "cannot be switched off"),
    ("DEEPCREW_PROPOSE_AFTER_S", "0", "DEEPCREW_PROPOSE_AFTER_S must be a positive whole number"),
    ("DEEPCREW_PROPOSE_AFTER_S", "soon", "positive whole number"),
])
def test_checked_refuses_what_the_advanced_keys_must_not_take(key, bad, message):
    with pytest.raises(ui.Refused, match=re.escape(message)):
        ui.checked({key: bad})


def test_the_advanced_keys_are_saved_to_the_env_file_and_come_back_in_the_state(page):
    values = {"DEEPCREW_ALLOW_OFF": "wc", "DEEPCREW_SANDBOX_DENY_READ": "none",
              "DEEPCREW_SANDBOX_ALLOW_WRITE": f"{ABS}", "DEEPCREW_TOOLS_OFF": "Agent,TaskStop", "DEEPCREW_PROPOSE_AFTER_S": "45"}
    status, _, state = page.call("POST", "/api/save", {"values": values})
    assert status == 200 and state["values"] == values and "DEEPCREW_SANDBOX_DENY_READ=none" in env_text()
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_PROPOSE_AFTER_S": "0", "DEEPCREW_TOOLS_OFF": "Agent"}})[0] == 400
    assert page.call("GET", "/api/state")[2]["values"] == values


def test_the_allowed_commands_take_exactly_what_the_teams_allowlist_takes(page):
    status, _, body = page.call("POST", "/api/save", {"values": {"DEEPCREW_SANDBOX": "off", "DEEPCREW_ALLOW": "ls,make"}})
    assert status == 400 and not providers.ENV_FILE.exists()
    assert body["error"] == ("DEEPCREW_ALLOW: 'make' could run code and the sandbox is off, so it is approved call by call; "
                             "turn the sandbox on or remove it from the list")
    entries = ["ls", "wc -l", "grep -r x src", "jq .", "make", "./build.sh", "python3 -m pytest"]
    for sandbox, cfg_sandbox in (("off", None), ("on", {"enabled": True})):
        taken = [e for e in entries if page.call("POST", "/api/save", {"values": {"DEEPCREW_SANDBOX": sandbox, "DEEPCREW_ALLOW": e}})[0] == 200]
        assert taken == cli.team_allow({"allow_scope": entries, "sandbox": cfg_sandbox}), sandbox
    assert taken == entries
    assert page.call("POST", "/api/save", {"values": {"DEEPCREW_ALLOW": "ls,make"}})[0] == 200
    status, _, body = page.call("POST", "/api/save", {"values": {"DEEPCREW_SANDBOX": "off"}})
    assert status == 400 and "'make' could run code" in body["error"]


def test_the_requests_route_lists_what_members_wait_on(served, lead):
    page = served()
    assert page.call("GET", "/api/requests")[2] == {"perms": [], "questions": []}
    perm, other = ask_perm(lead, "wc -c"), ask_perm(lead, tool="Edit", tool_input={"file_path": "docs/a.md"}, member="beta")
    question, choice = ask_question(lead), ask_question(lead, "tabs or spaces?", options=["tabs", "spaces"])
    status, _, got = page.call("GET", "/api/requests")
    assert status == 200 and set(got) == {"perms", "questions"}
    first, second = got["perms"]
    assert (first["id"], first["member"], first["tool"], first["input"]) == (perm, "alpha", "Bash", {"command": "wc -c"})
    assert (second["id"], second["tool"], second["input"]) == (other, "Edit", {"file_path": "docs/a.md"}) and abs(first["ts"] - time.time()) < 30
    plain, options = got["questions"]
    assert (plain["id"], plain["member"], plain["question"], plain["task"], plain["options"]) == (question, "beta", "which database?", "t1", [])
    assert options["id"] == choice and options["options"] == ["tabs", "spaces"]
    lead.answer("perms", perm, "denied")
    got = page.call("GET", "/api/requests")[2]
    assert [r["id"] for r in got["perms"]] == [other]


def test_each_permission_request_carries_what_an_allow_adds(served, lead):
    page = served()
    build = ask_perm(lead, "npm run build")
    fetch = ask_perm(lead, tool="WebFetch", tool_input={"url": "https://docs.example.com/guide", "prompt": "summarise"}, member="beta")
    issue = ask_perm(lead, tool="mcp__github__create_issue", tool_input={"title": "Fix the build"})
    compound, edit = ask_perm(lead, "ls && pwd"), ask_perm(lead, tool="Edit", tool_input={"file_path": "docs/a.md"}, member="beta")
    got = {r["id"]: r for r in page.call("GET", "/api/requests")[2]["perms"]}
    assert all("widening" in r for r in got.values())
    assert got[build]["widening"] == {"kind": "command", "value": "npm run build", "text": "`npm run build` runs without asking from now on"}
    assert got[fetch]["widening"] == {"kind": "site", "value": "docs.example.com", "text": "members reach docs.example.com without asking"}
    assert got[issue]["widening"] == {"kind": "tool", "value": "mcp__github__create_issue", "text": "members use mcp__github__create_issue without asking"}
    assert got[compound]["widening"] is None and got[edit]["widening"] is None
    reply = page.call("POST", "/api/answer", {"id": build, "allow": False})[2]
    assert {r["id"]: r["widening"] for r in reply["requests"]["perms"]} == {k: v["widening"] for k, v in got.items() if k != build}


def test_the_widening_is_worked_out_from_the_request_as_filed_and_shown_with_a_tilde(served, lead, monkeypatch):
    home, seen = str(Path.home()), []

    def spy(board, r):
        seen.append((board.dir, r["id"], r["input"]))
        return {"kind": "command", "value": r["input"]["command"], "text": f"`{r['input']['command']}` runs without asking from now on"}

    monkeypatch.setattr(policy, "widening", spy)
    perm = ask_perm(lead, f"ls {home}/data")
    got = served().call("GET", "/api/requests")[2]["perms"][0]
    assert seen == [(lead.dir, perm, {"command": f"ls {home}/data"})]
    assert got["widening"] == {"kind": "command", "value": "ls ~/data", "text": "`ls ~/data` runs without asking from now on"}
    assert home not in json.dumps(got)


def test_the_page_writes_paths_in_the_home_directory_with_a_tilde(served, lead, monkeypatch):
    home = str(Path.home())
    (Path(home) / "data").mkdir()
    policy.set_folder(lead, f"{home}/data", by="user")
    edit = ask_perm(lead, tool="Edit", tool_input={"file_path": f"{home}/elsewhere/a.md"})
    lead.request("perms", "beta", {"tool": "Read", "input": {"file_path": f"{home}/data/x.csv"}, "tool_use_id": None, "always": False,
                                   "folder": f"{home}/data", "write": False, "ts": filed_at()})
    question = ask_question(lead, f"may I read {home}/data/x.csv?", options=[f"{home}/data", "no"])
    page = served()
    team, requests, state = (page.call("GET", path)[2] for path in ("/api/team", "/api/requests", "/api/state"))
    assert home not in json.dumps([team, requests, state])
    assert team["tools"].startswith("~/") and team["folders"][0]["path"] == "~/data"
    assert requests["perms"][0]["input"] == {"file_path": "~/elsewhere/a.md"} and requests["perms"][1]["folder"] == "~/data"
    assert requests["questions"][0]["question"] == "may I read ~/data/x.csv?" and requests["questions"][0]["options"] == ["~/data", "no"]
    assert not lead.live_cfg()["folders"][0]["path"].startswith("~")
    answers = [page.call("POST", "/api/answer", body)[2] for body in ({"id": edit, "allow": False}, {"id": question, "text": f"use {home}/data"})]
    assert home not in json.dumps(answers)
    assert answers[0]["answered"]["input"] == {"file_path": "~/elsewhere/a.md"} and answers[1]["answered"]["answer"] == "use ~/data"
    assert board_module.read_json(lead.dir / "questions" / f"{question}.json")["answer"] == f"use {home}/data"

    def broken(board, keys):
        raise ValueError(f"{home}/elsewhere/team.json is not readable")

    monkeypatch.setattr(settings, "sync_team", broken)
    reply = page.call("POST", "/api/save", {"values": {"DEEPCREW_MAX_TURNS": "9"}})[2]
    assert reply["sync_error"] == "ValueError: ~/elsewhere/team.json is not readable"
    providers.ENV_FILE.unlink()
    providers.ENV_FILE.mkdir()
    status, _, reply = page.call("POST", "/api/theme", {"theme": "dark"})
    assert status == 400 and "~/.config/" in reply["error"] and home not in reply["error"]


def test_the_user_allows_a_permission_request_on_the_page(served, lead):
    page = served()
    perm = ask_perm(lead, "wc -c")
    status, _, reply = page.call("POST", "/api/answer", {"id": perm, "allow": True, "always": False})
    assert status == 200 and reply["answered"]["state"] == "allowed" and reply["answered"]["by"] == "user"
    assert reply["answered"]["always"] is True
    assert reply["requests"]["perms"] == [] and reply["requests"]["questions"] == []
    record = board_module.read_json(lead.dir / "perms" / f"{perm}.json")
    cfg = board_module.read_json(lead.dir / "team.json")
    assert (record["state"], record["by"]) == ("allowed", "user") and cfg["exact_allow"] == ["wc -c"]
    assert "wc -c" not in cfg["bash_allow"] and "wc -c" not in cfg["approved_allow"]
    status, _, body = page.call("POST", "/api/answer", {"id": perm, "allow": False})
    assert status == 400 and "already allowed" in body["error"]
    assert board_module.read_json(lead.dir / "perms" / f"{perm}.json")["state"] == "allowed"
    denied = ask_perm(lead, "rm -rf build")
    assert page.call("POST", "/api/answer", {"id": denied, "allow": False})[2]["answered"]["state"] == "denied" and "rm -rf build" not in [*board_module.read_json(lead.dir / "team.json")["bash_allow"], *board_module.read_json(lead.dir / "team.json")["exact_allow"]]
    log = [json.loads(line) for line in (lead.dir / "run" / "ui-events.jsonl").read_text().splitlines()]
    assert [(e["kind"], e["id"], e["state"]) for e in log] == [("answer", perm, "allowed"), ("answer", denied, "denied")] and all("ts" in e for e in log)


def test_always_adds_one_simple_command_to_the_teams_allowlist_as_approve_does(served, lead):
    page = served()
    simple, compound = ask_perm(lead, "wc -c"), ask_perm(lead, "wc -c && ls")
    reply = page.call("POST", "/api/answer", {"id": simple, "allow": True, "always": True})[2]
    assert reply["answered"]["always"] is True and reply["answered"]["by"] == "user"
    cfg = board_module.read_json(lead.dir / "team.json")
    assert cfg["exact_allow"] == ["wc -c"] and "wc -c" not in cfg["bash_allow"] and "wc -c" not in cfg["approved_allow"]
    assert page.call("POST", "/api/answer", {"id": compound, "allow": True, "always": True})[2]["answered"]["always"] is False
    again = board_module.read_json(lead.dir / "team.json")
    assert (again["exact_allow"], again["bash_allow"]) == (cfg["exact_allow"], cfg["bash_allow"])
    assert page.call("POST", "/api/answer", {"id": ask_perm(lead, "wc -c"), "allow": False, "always": True})[2]["answered"]["always"] is False


def test_the_user_answers_a_question_on_the_page(served, lead):
    page = served()
    free, choice = ask_question(lead), ask_question(lead, "tabs or spaces?", options=["tabs", "spaces"])
    status, _, reply = page.call("POST", "/api/answer", {"id": free, "text": "  postgres "})
    assert status == 200 and (reply["answered"]["state"], reply["answered"]["by"], reply["answered"]["answer"]) == ("answered", "user", "postgres")
    assert [q["id"] for q in reply["requests"]["questions"]] == [choice]
    assert board_module.read_json(lead.dir / "questions" / f"{free}.json")["answer"] == "postgres"
    for text in ("", "   ", None, 5):
        status, _, body = page.call("POST", "/api/answer", {"id": choice, "text": text})
        assert status == 400 and "must not be empty" in body["error"], text
    assert board_module.read_json(lead.dir / "questions" / f"{choice}.json")["state"] == "pending"
    assert page.call("POST", "/api/answer", {"id": choice, "text": "tabs"})[2]["requests"]["questions"] == []
    status, _, body = page.call("POST", "/api/answer", {"id": choice, "text": "spaces"})
    assert status == 400 and "already answered" in body["error"]


def test_an_option_is_answered_by_its_number_and_the_member_gets_it_as_written(served, lead):
    home = str(Path.home())
    choice = ask_question(lead, "which folder?", options=[f"{home}/data", "none"])
    page = served()
    assert page.call("GET", "/api/requests")[2]["questions"][0]["options"] == ["~/data", "none"]
    for bad in ({"id": choice, "option": 2}, {"id": choice, "option": -1}, {"id": choice, "option": True}, {"id": choice, "option": "0"},
                {"id": choice, "option": None}, {"id": "Q12345678", "option": 0}, {"id": ask_question(lead), "option": 0}):
        assert page.call("POST", "/api/answer", bad)[0] == 400, bad
    status, _, reply = page.call("POST", "/api/answer", {"id": choice, "option": 0})
    assert status == 200 and reply["answered"]["answer"] == "~/data" and reply["answered"]["by"] == "user"
    assert board_module.read_json(lead.dir / "questions" / f"{choice}.json")["answer"] == f"{home}/data"


def test_an_answer_names_a_request_that_exists_and_says_yes_or_no(served, lead):
    page = served()
    perm = ask_perm(lead)
    for body in ({}, {"id": 5}, {"id": "../team"}, {"id": "P12345678", "allow": True}, {"id": "Q12345678", "text": "x"}):
        assert page.call("POST", "/api/answer", body)[0] == 400, body
    for body in ({"id": perm}, {"id": perm, "allow": "yes"}, {"id": perm, "allow": True, "always": "yes"}):
        assert page.call("POST", "/api/answer", body)[0] == 400, body
    assert board_module.read_json(lead.dir / "perms" / f"{perm}.json")["state"] == "pending"
    assert page.call("POST", "/api/answer", {"id": perm, "text": "ok"})[0] == 400


def test_without_a_team_the_requests_routes_say_so(page, lead):
    assert page.call("GET", "/api/requests")[0] == 400 and "no team" in page.call("GET", "/api/requests")[2]["error"]
    perm = ask_perm(lead)
    status, _, body = page.call("POST", "/api/answer", {"id": perm, "allow": True})
    assert status == 400 and "no team" in body["error"]
    assert board_module.read_json(lead.dir / "perms" / f"{perm}.json")["state"] == "pending"
    assert ui.requests_view(ui.TeamLink("later", lead.dir.parent / "later")) == {"perms": [], "questions": []}
    with pytest.raises(ui.Refused, match="no team"):
        ui.answer_request({"id": perm, "allow": True}, ui.TeamLink("later", lead.dir.parent / "later"))


def test_the_page_has_the_requests_strip_above_the_settings(page):
    text = page.call("GET", f"/?t={page.token}")[2].decode()
    for ident in ("waiting", "waiting-n", "waiting-list", "scopes", "m-scope", "sizes", "strengths", "save", "done"):
        assert f'id="{ident}"' in text, ident
    assert text.index('id="waiting"') < text.index("<main>") and text.index('class="bar"') < text.index("<script>")
    assert 'data-key="DEEPCREW_MAX_ACTIVE">' in text and "<details id=" not in text
    assert 'data-key="DEEPCREW_PROPOSE_AFTER_S"' not in text
    assert "Waiting for you" in text and "/api/requests" in text and "deepcrew:changed" in text and "<nav" not in text
    assert '<span class="tag" id="waiting-n" aria-live="polite">' in text and '<div id="waiting-list" tabindex="-1">' in text


def test_the_tools_section_lists_the_member_tools_and_adds_and_removes_servers_and_skills(project, monkeypatch):
    from deepcrew import browser
    monkeypatch.setattr(browser, "find", lambda: None)
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"files": {"command": "tool", "args": ["x"]}}}))
    (project / ".claude" / "skills" / "greet").mkdir(parents=True)
    (project / ".claude" / "skills" / "greet" / "SKILL.md").write_text("---\nname: greet\ndescription: Greets.\n---\nHello.\n")
    tools = ui.Tools(project)
    view = tools.view()
    assert [t["name"] for t in view["own"]] == ["message"] and "Skill" not in [t["name"] for t in view["native"]]
    assert [t["name"] for t in view["native"] if t["optional"]] == list(settings.OPTIONAL_TOOLS) and all(t["what"] for t in view["native"])
    assert [c["name"] for c in view["mcp_found"]] == ["files"] and [c["name"] for c in view["skills_found"]] == ["greet"]
    got = tools.routes[("POST", "/api/tools/skill")]({"name": "greet"}, {})
    assert got["done"] == "added skill greet from the project's .claude/skills/greet" and got["skills_found"][0]["given"]
    assert "Skill" in [t["name"] for t in got["native"]]
    got = tools.routes[("POST", "/api/tools/mcp")]({"name": "files"}, {})
    assert [(r["name"], r["ok"]) for r in got["mcp"]] == [("files", True)] and got["mcp_found"][0]["given"]
    with pytest.raises(ui.Refused, match="no MCP server named 'nope'"):
        tools.routes[("POST", "/api/tools/mcp")]({"name": "nope"}, {})
    got = tools.routes[("POST", "/api/tools/mcp")]({"name": "files", "remove": True}, {})
    assert (got["mcp"], got["done"], got["notes"]) == ([], "removed MCP server files", [])
    with pytest.raises(ui.Refused, match="name: the skill"):
        tools.routes[("POST", "/api/tools/skill")]({}, {})
