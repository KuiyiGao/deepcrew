from __future__ import annotations

import calendar
import json
import re
import subprocess
import tomllib
from types import SimpleNamespace

import pytest

from deepcrew import doctor, member, policy, providers, route, settings, ui
from deepcrew.board import Board, read_json

from conftest import FAKE_TOML
from page_helpers import page_script

KEY = "sk-" + "f" * 32
NOKEY = {"name": "nokey", "base_url": "http://127.0.0.1:9", "models": ["n1"], "efforts": [], "fast_model": "n1", "auto_compact_window": 100000,
         "timeout_ms": 1000, "key_sources": ["env:NOKEY_KEY"], "key_pattern": ".+"}
BACKUP = {"name": "backup", "base_url": "http://127.0.0.1:9", "models": ["small", "big[1m]"], "efforts": ["low", "high", "max"], "fast_model": "small",
          "auto_compact_window": 100000, "timeout_ms": 1000, "key_sources": ["env:BACKUP_KEY"], "key_pattern": ".+",
          "prices": {"small": {"cache_hit": 0.2, "cache_miss": 2.0, "output": 20.0}, "big": {"cache_hit": 0.5, "cache_miss": 5.0, "output": 50.0}}}
SPARE = {**BACKUP, "name": "spare", "key_sources": ["env:SPARE_KEY"]}
NO_BALANCE = {"type": "result", "is_error": True, "api_error_status": 402, "result": "API Error: 402 Insufficient Balance"}
NO_NOKEY_KEY = "nokey has no key (`deepcrew auth set nokey` in your own terminal stores one)"


def entry(t: dict) -> str:
    lines = [f"[{t['name']}]", *(f"{k} = {json.dumps(v)}" for k, v in t.items() if k not in ("name", "prices"))]
    for model, price in (t.get("prices") or {}).items():
        lines += [f'[{t["name"]}.prices."{model}"]', *(f"{k} = {v}" for k, v in price.items())]
    return "\n".join(lines) + "\n"


def team_json(board) -> dict:
    return read_json(board.dir / "team.json")


def leader_notices(board) -> list[str]:
    return [msg["text"] for _, msg in board.peek_mail("leader") if msg["kind"] == "notice"]


@pytest.fixture
def backed(make_board, add_member, providers_toml, monkeypatch):
    providers_toml(FAKE_TOML + entry(NOKEY) + entry(BACKUP) + entry(SPARE))
    monkeypatch.setenv("FAKE_KEY", KEY)
    monkeypatch.setenv("BACKUP_KEY", KEY)
    providers.env_file_set({"DEEPCREW_FALLBACK": "nokey,backup"})
    board = make_board("main", default_location=True)
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    return board


@pytest.fixture
def claude_proc(monkeypatch):
    fake = SimpleNamespace(lines=[], status=0)

    def popen(args, **kw):
        out = iter(json.dumps(x) + "\n" if isinstance(x, dict) else x for x in fake.lines)
        stdin = SimpleNamespace(write=lambda text: None, flush=lambda: None, close=lambda: None)
        return SimpleNamespace(pid=1, stdin=stdin, stdout=out, wait=lambda timeout=None: fake.status, kill=lambda: None)

    monkeypatch.setattr(member, "subprocess", SimpleNamespace(Popen=popen, PIPE=-1, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(member, "_wait_for_work", lambda board, me: (None, []))
    return fake


@pytest.fixture
def routed(monkeypatch):
    calls = []
    monkeypatch.setattr(route, "run", lambda board: calls.append(board.cfg["provider"]["name"]) or {})
    return calls


def test_the_ready_entries_load_once_added_and_deepseek_stays_the_one_built_in_default():
    assert list(providers.BUILTIN) == ["deepseek"] and providers.known() == ["deepseek"] and settings.default_provider() == "deepseek"
    assert sorted(providers.TEMPLATES) == ["minimax", "minimax-cn", "zai", "zhipu"]
    with pytest.raises(providers.ProviderError, match=r"^unknown provider 'zai'; known: deepseek; ready entries: minimax, minimax-cn, zai, zhipu \(add one in "):
        providers.load("zai")
    providers.CONFIG_DIR.mkdir(parents=True)
    (providers.CONFIG_DIR / "providers.toml").write_text("# mine\n[deepseek.tariff]\nholidays = [\"2026-10-01\"]\n")
    for name in providers.TEMPLATES:
        assert providers.add_template(name) is True and providers.add_template(name) is False, name
    text = (providers.CONFIG_DIR / "providers.toml").read_text()
    assert text.startswith("# mine\n[deepseek.tariff]") and text.count("read 2026-10-11") == 4 and text.count("[zai]\n") == 1
    assert providers.load("deepseek")["tariff"]["holidays"] == ["2026-10-01"] and settings.default_provider() == "deepseek"
    loaded = {name: providers.load(name) for name in providers.TEMPLATES}
    assert providers.known() == ["deepseek", "minimax", "minimax-cn", "zai", "zhipu"]
    assert {n: p["base_url"] for n, p in loaded.items()} == {"zai": "https://api.z.ai/api/anthropic", "zhipu": "https://open.bigmodel.cn/api/anthropic",
                                                             "minimax": "https://api.minimax.io/anthropic", "minimax-cn": "https://api.minimax.cn/anthropic"}
    for name, p in loaded.items():
        bare = {m.split("[")[0] for m in p["models"]}
        assert (bare <= set(p.get("prices") or {})) == bool(p.get("prices")) == (name in ("zai", "minimax")), name
        assert p["fast_model"].split("[")[0] in bare and set(p["vision"]) <= bare and p["documents"] is False and p["key_sources"][1] == f"keychain:deepcrew-{name}"
        assert providers.valid_key(p, "k" * 40) and not providers.valid_key(p, "short") and not providers.valid_key(p, "with a space in it ok")
        assert "CNY" in providers.TEMPLATES[name] or name in ("zai", "minimax")


def test_a_users_own_entry_of_that_name_stays_and_a_providers_toml_that_is_not_toml_is_left_alone(providers_toml):
    providers_toml(entry({**BACKUP, "name": "zai"}))
    before = (providers.CONFIG_DIR / "providers.toml").read_text()
    assert providers.add_template("zai") is False and (providers.CONFIG_DIR / "providers.toml").read_text() == before
    assert providers.load("zai")["models"] == BACKUP["models"]
    providers_toml("[zai\n")
    with pytest.raises(providers.ProviderError, match="is not valid TOML"):
        providers.add_template("minimax")
    providers_toml("")
    with pytest.raises(providers.ProviderError, match="unknown provider 'nope'"):
        providers.add_template("nope")
    assert (providers.CONFIG_DIR / "providers.toml").read_text() == ""


def test_an_error_means_what_the_providers_rules_say_with_deepseeks_statuses_as_the_default():
    zai, zhipu = (tomllib.loads(providers.TEMPLATES[n])[n] for n in ("zai", "zhipu"))
    kind = providers.error_kind
    assert [kind({}, s, "x") for s in (402, 429, 503, 529, 400, 401, 500)] == ["balance", "rate_limit", "overload", "overload", None, None, None]
    said = "API Error: Request rejected (429) - "
    assert kind(zai, 429, said + "Insufficient balance or no resource package. Please recharge.") == "balance"
    assert kind(zai, 429, said + "Your GLM Coding Plan package has expired and is temporarily unavailable.") == "balance"
    assert kind(zai, 429, said + "The service may be temporarily overloaded, please try again later") == "overload"
    assert kind(zai, 429, said + "Rate limit reached for requests") == "rate_limit" and kind(zai, 402, "x") == "balance"
    assert kind(zhipu, 429, said + "\u60a8\u7684\u8d26\u6237\u5df2\u6b20\u8d39\uff0c\u8bf7\u5145\u503c\u540e\u91cd\u8bd5") == "balance"
    assert kind(zhipu, 429, said + "\u8be5\u6a21\u578b\u5f53\u524d\u8bbf\u95ee\u91cf\u8fc7\u5927\uff0c\u8bf7\u60a8\u7a0d\u540e\u518d\u8bd5") == "overload"
    assert kind({"errors": {"rate_limit": ["402"]}}, 402, "x") == "rate_limit"
    assert kind({"errors": {"balance": ["429:QUOTA is insufficient"]}}, 429, "Token quota is insufficient") == "balance"


@pytest.mark.parametrize("errors", ['{ balance = ["4x9"] }', '{ other = ["429"] }', '{ balance = "429" }', '{ balance = [429] }', '["429"]',
                                    '{ balance = ["429:"] }'])
def test_an_error_rule_that_is_not_a_status_and_words_is_refused(providers_toml, errors):
    providers_toml(FAKE_TOML + f"errors = {errors}\n")
    with pytest.raises(providers.ProviderError, match="errors maps balance, overload, rate_limit to lists of"):
        providers.load("fake")


@pytest.mark.parametrize("errors, status, said, advice", [
    ({}, 402, "API Error: 402 Insufficient Balance", "the provider account has no balance: top it up, then resume the crew"),
    ({}, 529, "API Error: 529 Upstream model overloaded; retryable.", "the provider is overloaded: resume the crew later"),
    ({}, 401, "API Error: 401 invalid key", "the provider rejected the key: save a valid key on the team page, or run `deepcrew auth set fake` in your own "
                                            "terminal, then resume the crew"),
    ({"balance": ["429:insufficient balance"]}, 429, "API Error: Request rejected (429) - Insufficient balance or no resource package. Please recharge.",
     "the provider account has no balance: top it up, then resume the crew"),
    ({"balance": ["429:insufficient balance"]}, 429, "API Error: Request rejected (429) - Rate limit reached for requests",
     "the provider's rate or concurrency limit: run fewer members at once, or resume the crew later")])
def test_without_backups_an_api_error_stops_the_member_with_the_advice_its_providers_rules_give(make_board, add_member, provider_table, claude_proc,
                                                                                                 errors, status, said, advice):
    board = make_board(provider={**provider_table, "errors": errors})
    add_member(board, "alpha", ["src/"])
    claude_proc.lines, claude_proc.status = [{"type": "result", "is_error": True, "api_error_status": status, "result": said}], 1
    assert member.drive(board, "alpha", False, "go") == "failed"
    assert leader_notices(board) == [f"alpha stopped: API error {status}: {advice}. {said}"]


@pytest.mark.parametrize("status, said, moves", [
    (402, "API Error: 402 Insufficient Balance", True), (429, "API Error: Request rejected (429) - rate limit", True),
    (503, "API Error: 503 Service Unavailable", True), (529, "API Error: 529 Upstream model overloaded; retryable.", True),
    (401, "API Error: 401 invalid key", False), (400, "API Error: 400 a document it cannot read", False), (500, "API Error: 500 internal", False)])
def test_an_empty_balance_a_rate_limit_or_an_overload_moves_the_team_and_other_errors_stop_the_member(backed, claude_proc, routed, status, said, moves):
    claude_proc.lines, claude_proc.status = [{"type": "result", "is_error": True, "api_error_status": status, "result": said}], 1
    state, cfg = member.drive(backed, "alpha", False, "go"), team_json(backed)
    assert (state, cfg["provider"]["name"], routed) == (("idle", "backup", ["backup"]) if moves else ("failed", "fake", []))
    if moves:
        record = cfg["provider_switches"][-1]
        assert {k: record[k] for k in ("from", "to", "by", "kind", "status", "said", "member", "skipped")} == {
            "from": "fake", "to": "backup", "by": "fallback", "kind": providers.error_kind({}, status, said), "status": status, "said": said,
            "member": "alpha", "skipped": [NO_NOKEY_KEY]}
        alpha = Board(backed.dir).member("alpha")
        assert (alpha["state"], alpha["failure"], alpha["provider"], leader_notices(backed)) == ("idle", None, "fake", [])
        assert cfg["provider"]["key_sources"] == [f"file:{backed.dir / 'secret' / 'backup.key'}", "env:BACKUP_KEY"] and cfg["member"]["model"] == "small"


def test_the_team_moves_once_and_a_member_whose_request_failed_on_the_provider_it_left_goes_on_with_it(backed, claude_proc, routed):
    stale = Board(backed.dir)
    claude_proc.lines, claude_proc.status = [NO_BALANCE], 1
    assert member.drive(backed, "alpha", False, "go") == "idle" and member.drive(stale, "beta", False, "go") == "idle"
    assert [s["to"] for s in team_json(backed)["provider_switches"]] == ["backup"] and routed == ["backup", "backup"] and leader_notices(backed) == []


def test_a_member_still_on_the_provider_the_team_left_ends_after_its_turn_and_is_resumed_on_the_new_one_with_its_task(backed, claude_proc, routed, post_task):
    stale = Board(backed.dir)
    t = post_task(backed, "x", ["src/"])
    assert backed.claim("alpha", t["id"])[0]
    providers.env_file_set({"DEEPCREW_PROVIDER": "backup"})
    assert settings.sync_team(Board(backed.dir), ["DEEPCREW_PROVIDER"])["changed"]["provider"] == ["fake", "backup"]
    claude_proc.lines = [{"type": "result", "subtype": "success", "is_error": False, "usage": {}}]
    assert member.drive(stale, "alpha", False, "go") == "idle" and routed == ["backup"]
    b = Board(backed.dir)
    assert b.task(t["id"])["state"] == "open" and [x["id"] for x in member.offer(b, b.member("alpha"))] == [t["id"]] and leader_notices(b) == []


def test_with_backups_listed_and_none_able_to_take_over_the_member_fails_as_before_and_says_why(make_board, add_member, claude_proc, providers_toml):
    providers_toml(FAKE_TOML + entry(NOKEY))
    providers.env_file_set({"DEEPCREW_FALLBACK": "nokey"})
    board = make_board()
    add_member(board, "alpha", ["src/"])
    claude_proc.lines, claude_proc.status = [NO_BALANCE], 1
    assert member.drive(board, "alpha", False, "go") == "failed" and team_json(board)["provider"]["name"] == "fake"
    assert leader_notices(board) == ["alpha stopped: API error 402: the provider account has no balance: top it up, then resume the crew. API Error: 402 "
                                     f"Insufficient Balance. No backup provider could take over: {NO_NOKEY_KEY}."]


def test_backups_are_tried_in_the_order_listed_and_the_team_never_moves_back_by_itself(backed, monkeypatch):
    monkeypatch.setenv("SPARE_KEY", KEY)
    providers.env_file_set({"DEEPCREW_FALLBACK": "backup,spare"})
    settings.sync_team(backed, ["DEEPCREW_FALLBACK"])

    def fall(ran_on, kind):
        return settings.fall_back(Board(backed.dir), "alpha", ran_on, 429, kind, "API Error: 429")
    assert fall("fake", "balance") == ("backup", "") and fall("fake", "balance") == ("backup", "")
    assert fall("backup", "overload") == ("spare", "")
    assert fall("spare", "rate_limit") == (None, "No backup provider could take over: backup failed; spare failed.")
    assert [s["to"] for s in team_json(backed)["provider_switches"]] == ["backup", "spare"]
    providers.env_file_set({"DEEPCREW_PROVIDER": "fake"})
    settings.sync_team(Board(backed.dir), ["DEEPCREW_PROVIDER"])
    assert settings.fallback_now(team_json(backed)) is None and fall("fake", "balance") == ("backup", "")


def test_while_the_team_has_a_budget_a_backup_without_prices_is_passed_over(backed):
    backed.update_cfg(lambda cfg: cfg.update(fallback=[{**BACKUP, "name": "free", "prices": {}}, BACKUP], budget={"usd": 5.0, "by": "user", "ts": 1}))
    assert settings.fall_back(backed, "alpha", "fake", 402, "balance", "API Error: 402") == ("backup", "")
    assert settings.fallback_now(team_json(backed))["skipped"] == ["free has no prices, so the budget could not be counted there"]


def test_a_move_keeps_each_members_strength_on_the_new_providers_models_and_efforts(make_board, add_member, provider_table):
    on, two = settings.on_provider, {"models": ["a", "b[1m]"], "efforts": ["low", "medium", "high", "max"]}
    assert on("a", "medium", two, BACKUP) == ("small", "high") and on("a", "low", two, BACKUP) == ("small", "low")
    assert on("b[1m]", "max", two, BACKUP) == ("big[1m]", "max") and on("b[1m]", None, two, BACKUP) == ("big[1m]", None)
    assert on("a", "high", two, {"models": ["one[1m]"], "efforts": []}) == ("one[1m]", None)
    assert on("x", "xhigh", two, {"models": ["p", "q"], "efforts": ["low", "high"]}) == ("p", "high")
    board = make_board(provider={**provider_table, "models": ["m", "n"], "efforts": ["high", "max"]})
    add_member(board, "boss", ["src/"], model="n", effort="max")
    board.update_cfg(lambda cfg: settings.switch_to(board, cfg, BACKUP, {"ts": 1, "by": "settings"}))
    assert (board.member("boss")["model"], board.member("boss")["effort"], team_json(board)["member"]["model"]) == ("big[1m]", "max", "small")


def test_config_set_provider_adds_a_ready_entry_and_moves_the_chosen_model_and_the_projects_team_to_it(deepcrew, make_board, providers_toml, monkeypatch):
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", KEY)
    board = make_board("main", default_location=True)
    providers.env_file_set({"DEEPCREW_PROVIDER": "fake", "DEEPCREW_MODEL": "m", "DEEPCREW_EFFORT": "default"})
    code, out, err = deepcrew("config", "set", "provider", "zai")
    cfg, lines = team_json(board), out.splitlines()
    assert (code, err) == (0, "") and providers.choices() == {"provider": "zai", "model": "glm-5.3-flash[1m]", "effort": "default", "thinking": None}
    assert (cfg["provider"]["name"], cfg["member"]["model"]) == ("zai", "glm-5.3-flash[1m]") and cfg["provider_switches"][-1]["by"] == "settings"
    assert lines[0].startswith("provider = zai  (") and "team main: provider fake -> zai" in lines
    assert "team main: member m, effort default, thinking on -> glm-5.3-flash[1m], effort default, thinking on" in lines
    assert f"added the ready zai entry to {providers.shown(providers.CONFIG_DIR / 'providers.toml')}" in lines
    assert "zai: no key yet: save a valid key on the team page, or run `deepcrew auth set zai` in your own terminal" in lines
    out = deepcrew("config", "set", "provider", "zhipu")[1]
    assert "zhipu: costs are not tracked, as its entry has no prices in USD; the page shows no cost and a budget cannot be counted" in out.splitlines()
    assert deepcrew("config", "set", "provider", "fake")[0] == 0 and team_json(board)["provider"]["name"] == "fake"
    code, _, err = deepcrew("config", "set", "provider", "nope")
    assert code == 1 and "unknown provider 'nope'; known: deepseek, fake, zai, zhipu; ready entries: minimax, minimax-cn" in err


def test_config_set_fallback_checks_each_name_adds_ready_entries_and_the_team_follows(deepcrew, make_board, providers_toml, monkeypatch):
    providers_toml(FAKE_TOML + entry(BACKUP))
    monkeypatch.setenv("BACKUP_KEY", KEY)
    board = make_board("main", default_location=True)
    for value, refusal in (("zai,nope", "unknown provider 'nope'"), ("zai,zai", "fallback names each provider once")):
        code, _, err = deepcrew("config", "set", "fallback", value)
        assert code == 1 and refusal in err and "[zai]" not in (providers.CONFIG_DIR / "providers.toml").read_text()
    code, out, err = deepcrew("config", "set", "fallback", "backup,zai")
    lines = out.splitlines()
    assert (code, err) == (0, "") and providers.env_file_get("DEEPCREW_FALLBACK") == "backup,zai"
    assert [t["name"] for t in team_json(board)["fallback"]] == ["backup", "zai"] and "team main: fallback none -> backup, zai" in lines
    assert lines[0].startswith("fallback = backup, zai  (") and "backup: key found in env:BACKUP_KEY" in lines
    assert "zai: no key yet: save a valid key on the team page, or run `deepcrew auth set zai` in your own terminal" in lines
    assert (board.dir / "secret" / "backup.key").read_text() == KEY
    assert json.loads(deepcrew("config", "show", "--json")[1])["fallback"] == ["backup", "zai"] and "fallback: backup, zai" in deepcrew("config", "show")[1]
    code, out, _ = deepcrew("config", "set", "fallback", "none")
    assert (code, team_json(board)["fallback"]) == (0, []) and "team main: fallback backup, zai -> none" in out and not (board.dir / "secret" / "backup.key").exists()


def test_up_with_the_name_of_a_ready_entry_adds_it_and_starts_the_team_on_its_fast_model():
    member_cfg, p, chosen = settings.member_config(SimpleNamespace(provider="minimax", model=None, effort=None))
    assert (p["name"], member_cfg, chosen) == ("minimax", {"model": "MiniMax-M3[1m]", "effort": None, "thinking": True}, False)
    assert "[minimax]" in (providers.CONFIG_DIR / "providers.toml").read_text()


def test_auth_set_with_the_name_of_a_ready_entry_adds_it_and_asks_for_that_providers_key(deepcrew):
    code, out, err = deepcrew("auth", "set", "zai")
    (question,) = json.loads(out)["questions"]
    assert code == 3 and question["prompt"].startswith("deepcrew needs a zai API key") and "ZAI_API_KEY=<key>" in question["answer_with"]
    assert "added the ready zai entry" in err and "[zai]" in (providers.CONFIG_DIR / "providers.toml").read_text() and providers.env_file_has("ZAI_API_KEY")
    code, _, err = deepcrew("auth", "status", "minimax")
    assert code == 1 and "unknown provider 'minimax'" in err


def test_done_on_the_page_never_moves_the_team_and_saving_another_provider_there_does(backed):
    settings.fall_back(backed, "alpha", "fake", 402, "balance", "API Error: 402 Insufficient Balance")
    providers.env_file_set({"DEEPCREW_PROVIDER": "fake", "DEEPCREW_MODEL": "m"})
    link = ui.TeamLink("main", backed.dir)
    ui.confirm(link)
    assert team_json(backed)["provider"]["name"] == "backup" and settings.fallback_now(team_json(backed))
    assert ui.save({"DEEPCREW_PROVIDER": "fake"}, link)["changed"]["provider"] == ["backup", "fake"]
    assert team_json(backed)["provider"]["name"] == "fake" and settings.fallback_now(team_json(backed)) is None


def test_a_key_only_in_the_leaders_environment_gets_a_copy_for_each_backup_and_stop_all_removes_every_copy(backed, deepcrew, monkeypatch):
    copies = backed.dir / "secret"
    assert sorted(p.name for p in copies.glob("*.key")) == ["backup.key", "fake.key"]
    monkeypatch.delenv("BACKUP_KEY")
    assert settings.refresh_key_copy(Board(backed.dir)) is True and sorted(p.name for p in copies.glob("*.key")) == ["fake.key"]
    monkeypatch.setenv("BACKUP_KEY", KEY)
    settings.refresh_key_copy(Board(backed.dir))
    monkeypatch.delenv("BACKUP_KEY")
    assert providers.resolve_key(settings.with_key_copy(backed.dir, BACKUP)) == (f"file:{copies / 'backup.key'}", KEY)
    assert deepcrew("stop", "--all")[0] == 0 and not list(copies.glob("*.key"))


def test_the_keys_of_the_backups_are_closed_to_members_as_the_providers_own_are(backed, tmp_path):
    key_file = tmp_path / "keys" / "spare.key"
    backed.update_cfg(lambda cfg: cfg.__setitem__("fallback", [{**SPARE, "key_sources": [f"file:{key_file}", "keychain:deepcrew-spare"]}]))
    places = policy.protected(Board(backed.dir))
    assert str(key_file) in places and "deepcrew-spare" in places and "deepcrew-fake" not in places
    key_file.parent.mkdir()
    with pytest.raises(ValueError, match="which members may not use"):
        policy.check_folder(Board(backed.dir), str(key_file.parent))


@pytest.mark.sandbox
def test_with_the_sandbox_on_the_backups_key_files_are_closed_to_reading(make_board, providers_toml, tmp_path):
    key_file = tmp_path / "spare.key"
    providers_toml(FAKE_TOML + entry({**SPARE, "key_sources": [f"file:{key_file}"]}))
    board = make_board()
    assert str(key_file) not in team_json(board)["sandbox"]["filesystem"]["denyRead"]
    providers.env_file_set({"DEEPCREW_FALLBACK": "spare"})
    settings.sync_team(board, ["DEEPCREW_FALLBACK"])
    assert str(key_file) in team_json(board)["sandbox"]["filesystem"]["denyRead"]


def test_status_and_the_inbox_say_once_which_provider_failed_why_and_what_the_team_runs_on_now(backed, deepcrew):
    assert "provider  fake; backups: nokey, backup" in deepcrew("status")[1].splitlines()
    settings.fall_back(backed, "alpha", "fake", 402, "balance", "API Error: 402 Insufficient Balance")
    data = json.loads(deepcrew("status", "--json")[1])
    assert (data["provider"]["name"], data["provider"]["backups"], data["provider"]["fallback"]["from"]) == ("backup", ["nokey"], "fake")
    (line,) = [x for x in data["summary"] if x.startswith("provider")]
    assert re.fullmatch(r"provider  backup since \d\d:\d\d: fake had no balance; once it works again, deepcrew config set provider fake moves the team back; "
                        r"backups: nokey", line)
    first, again = deepcrew("inbox")[1].splitlines(), deepcrew("inbox")[1].splitlines()
    said = ("provider: fake had no balance (API error 402 for alpha: API Error: 402 Insufficient Balance), so the team switched to backup; members go "
            f"on there with the same sessions and files ({NO_NOKEY_KEY}). Tell the user; once fake works again, deepcrew config set provider fake "
            "moves the team back")
    assert said in first and f"{said}   (shown before)" in again and json.loads(deepcrew("inbox", "--json")[1])["provider"]["to"] == "backup"


def test_prices_and_the_self_check_say_plainly_that_costs_are_not_tracked_for_an_entry_without_prices(deepcrew, monkeypatch):
    assert deepcrew("config", "set", "provider", "zhipu")[0] == 0
    lines = deepcrew("prices")[1].splitlines()
    assert lines == ["prices    costs are not tracked for zhipu: its entry has no prices in USD, so no cost is counted and a budget cannot be"]
    monkeypatch.setenv("ZHIPU_API_KEY", "k" * 40)
    (prices,) = [r for r in doctor.provider_checks(providers.load("zhipu"), offline=True) if r["id"] == "prices"]
    assert prices["level"] == "warn" and prices["detail"].startswith("costs are not tracked for zhipu: its entry has no prices in USD")


def test_the_self_check_says_which_backups_have_a_key(monkeypatch):
    monkeypatch.setenv("BACKUP_KEY", KEY)
    assert doctor.fallback_checks([NOKEY, BACKUP, {**BACKUP, "name": "fake"}], "fake") == [doctor.check(
        "fallback", "warn", "backups in order: nokey without a key, costs not tracked; backup ready", "deepcrew auth set nokey in your own terminal")]
    assert doctor.fallback_checks([BACKUP], "fake")[0]["level"] == "ok" and doctor.fallback_checks([], "fake") == []


def test_the_team_panel_shows_the_provider_a_move_to_a_backup_and_costs_that_are_not_tracked(backed):
    link = ui.TeamLink("main", backed.dir)
    plain = ui.team_view(link)
    assert plain["provider"] == {"name": "fake", "priced": False, "backups": ["nokey", "backup"], "fallback": None}
    settings.fall_back(backed, "alpha", "fake", 402, "balance", "API Error: 402 Insufficient Balance")
    moved = ui.team_view(link)["provider"]
    out = page_script([f"showProvider({json.dumps(moved)}); const el = $('#team-provider'); return [el.textContent, el.title, el.classList.contains('moved')];",
                       f"showTeam({json.dumps(plain)}); return [$('#team-cost').textContent, $('#team-cost').title, $('#team-provider').textContent];"],
                      ui.state())
    (text, title, warned), (cost, cost_title, shown) = out["results"]
    assert re.fullmatch(r"provider backup, since \d\d:\d\d: fake had no balance", text) and warned is True
    assert title == ("The team switched to backup because fake had no balance (API Error: 402 Insufficient Balance). To go back once it works again, "
                     "choose fake under Model, Custom, and save. Backups: nokey.")
    assert (cost, cost_title, shown) == ("costs not tracked", "Costs are not tracked for fake: its entry has no prices in USD.", "provider fake")


def test_each_turn_is_priced_at_the_prices_of_the_provider_it_ran_on(make_board, add_member, provider_table):
    cheap, peak_hours = {"m": {"cache_hit": 0.1, "cache_miss": 1.0, "output": 10.0}}, {"peak_utc": ["06:00-10:00"], "off_peak": 0.5}
    board = make_board(provider={**provider_table, "prices": cheap, "tariff": peak_hours})
    add_member(board, "alpha", ["src/"])

    def mark(name, prices, model, tariff=None):
        return {"type": member.PROCESS_MARK, "ts": 1, "provider": name, "model": model, "prices": prices, "tariff": tariff}
    turn = {"type": "result", "usage": {"input_tokens": 1_000_000}}
    off_peak = {"type": member.TIME_MARK, "ts": calendar.timegm((2026, 10, 8, 5, 48, 0))}
    log = board.dir / "logs" / "alpha.ndjson"
    log.write_text("\n".join(json.dumps(e) for e in (mark("fake", cheap, "m", peak_hours), off_peak, turn)) + "\n")
    board.update_cfg(lambda cfg: settings.switch_to(board, cfg, BACKUP, {"ts": 1, "by": "settings"}))
    assert member.usage(board, "alpha")["cost_usd"] == 0.5
    with open(log, "a") as f:
        f.write("\n".join(json.dumps(e) for e in (mark("backup", BACKUP["prices"], "small"), off_peak, turn)) + "\n")
    assert {k: member.usage(board, "alpha")[k] for k in ("cost_usd", "list_usd")} == {"cost_usd": 2.5, "list_usd": 3.0}
    with open(log, "a") as f:
        f.write("\n".join(json.dumps(e) for e in (mark("zhipu", {}, "glm-5.3-flash[1m]"), turn)) + "\n")
    assert "cost_usd" not in member.usage(board, "alpha")


def test_every_model_tier_claude_code_names_runs_the_members_model_the_fable_tier_too(make_board, add_member):
    board = make_board()
    add_member(board, "alpha", ["src/"])
    e = member.env(board, board.member("alpha"))
    assert e["ANTHROPIC_DEFAULT_FABLE_MODEL"] == e["ANTHROPIC_DEFAULT_OPUS_MODEL"] == e["ANTHROPIC_DEFAULT_SONNET_MODEL"] == e["ANTHROPIC_MODEL"] == "m"
    assert e["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == board.cfg["provider"]["fast_model"]
