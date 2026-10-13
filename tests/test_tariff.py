from __future__ import annotations

import calendar
import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from deepcrew import cli, member, providers, ui

TARIFF = providers.BUILTIN["deepseek"]["tariff"]
PRICES = {"m": {"cache_miss": 1.0, "cache_hit": 0.1, "output": 10.0}}


def utc(*when) -> float:
    return float(calendar.timegm((*when, *(0,) * (6 - len(when)))))


THU_0548 = utc(2026, 10, 8, 5, 48)
THU_0552 = utc(2026, 10, 8, 5, 52)


@pytest.fixture
def tariffed(provider_table):
    return {**provider_table, "prices": PRICES, "tariff": dict(TARIFF)}


@pytest.mark.parametrize(("when", "share"), [
    ((2026, 10, 8, 0, 59), 0.5), ((2026, 10, 8, 1, 0), 1.0), ((2026, 10, 8, 3, 59), 1.0), ((2026, 10, 8, 4, 0), 0.5),
    ((2026, 10, 8, 6, 0), 1.0), ((2026, 10, 8, 9, 59), 1.0), ((2026, 10, 8, 10, 0), 0.5), ((2026, 10, 8, 23, 0), 0.5),
    ((2026, 10, 10, 7, 0), 0.5), ((2026, 10, 11, 2, 0), 0.5),
])
def test_the_share_of_the_list_prices_follows_deepseeks_hours(tariffed, when, share):
    assert providers.price_share(tariffed, utc(*when)) == share


def test_a_listed_holiday_is_off_peak_all_day_and_no_tariff_means_list_prices(tariffed, provider_table):
    holiday = {**tariffed, "tariff": {**TARIFF, "holidays": ["2026-10-08"]}}
    assert providers.price_share(holiday, utc(2026, 10, 8, 7)) == 0.5 and providers.price_share(tariffed, utc(2026, 10, 8, 7)) == 1.0
    assert providers.price_share(provider_table, utc(2026, 10, 10, 7)) == 1.0 and providers.tariff_now(provider_table) is None


def test_the_next_change_is_found_across_days_and_the_weekend(tariffed):
    assert providers.tariff_now(tariffed, THU_0548) == {"share": 0.5, "peak": False, "until": utc(2026, 10, 8, 6), "next": 1.0}
    assert providers.tariff_now(tariffed, utc(2026, 10, 8, 7))["until"] == utc(2026, 10, 8, 10)
    friday_evening = providers.tariff_now(tariffed, utc(2026, 10, 9, 10))
    assert friday_evening["until"] == utc(2026, 10, 12, 1) and friday_evening["next"] == 1.0
    assert providers.tariff_now({**tariffed, "tariff": {**TARIFF, "off_peak": 1}}, THU_0548) is None


@pytest.mark.parametrize(("change", "words"), [
    ({"peak_utc": ["10:00-06:00"]}, "HH:MM-HH:MM"), ({"peak_utc": ["6-10"]}, "HH:MM-HH:MM"), ({"peak_days": ["monday"]}, "peak_days"),
    ({"off_peak": 2}, "from 0 to 1"), ({"off_peak": True}, "from 0 to 1"), ({"holidays": ["8 Oct"]}, "YYYY-MM-DD"),
    ({"peak_days": [{"mon": 1}]}, "peak_days"), ({"peak_days": "mon"}, "peak_days"), ({"peak_utc": ["22:00-24:59"]}, "HH:MM-HH:MM"),
])
def test_a_tariff_that_cannot_be_used_is_refused(change, words):
    with pytest.raises(providers.ProviderError, match=words):
        providers.check_tariff({**TARIFF, **change})


def test_providers_toml_adds_holidays_and_keeps_the_built_in_hours():
    providers.CONFIG_DIR.mkdir(parents=True)
    (providers.CONFIG_DIR / "providers.toml").write_text('[deepseek.tariff]\nholidays = ["2026-10-08"]\n\n[deepseek.prices.deepseek-flash]\noutput = 1.5\n')
    p = providers.load("deepseek")
    assert p["tariff"]["peak_utc"] == TARIFF["peak_utc"] and p["tariff"]["holidays"] == ["2026-10-08"]
    assert p["prices"]["deepseek-v4-pro"] == providers.BUILTIN["deepseek"]["prices"]["deepseek-v4-pro"]
    assert p["prices"]["deepseek-flash"] == {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.5}
    (providers.CONFIG_DIR / "providers.toml").write_text('[deepseek.tariff]\noff_peak = 3\n')
    with pytest.raises(providers.ProviderError, match="provider 'deepseek': tariff: off_peak"):
        providers.load("deepseek")


def stream(board, name, *events):
    (board.dir / "logs" / f"{name}.ndjson").write_text("\n".join(json.dumps(e) for e in events) + "\n")


def result(tokens):
    return {"type": "result", "usage": {"input_tokens": tokens}}


def test_each_turn_costs_the_price_of_the_hour_it_ended(make_board, add_member, tariffed, monkeypatch):
    board = make_board(provider=tariffed)
    add_member(board, "alpha", ["src/"])
    monkeypatch.setattr(member.time, "time", lambda: utc(2026, 10, 8, 7))
    stream(board, "alpha", {"type": member.TIME_MARK, "ts": THU_0548}, result(1_000_000),
           {"type": member.TIME_MARK, "ts": utc(2026, 10, 8, 6, 30)}, result(1_000_000),
           result(1_000_000),
           {"type": "assistant", "message": {"id": "now", "usage": {"input_tokens": 1_000_000}}})
    u = member.usage(board, "alpha")
    assert u["cost_usd"] == pytest.approx(3.5) and u["list_usd"] == pytest.approx(4.0) and u["running"]
    monkeypatch.setattr(member.time, "time", lambda: utc(2026, 10, 8, 11))
    assert member.usage(board, "alpha")["cost_usd"] == pytest.approx(3.0)
    assert member.spent(board) == pytest.approx(3.0)


def test_without_a_tariff_the_list_prices_hold(make_board, provider_table):
    board = make_board(provider={**provider_table, "prices": PRICES})
    stream(board, "alpha", {"type": member.TIME_MARK, "ts": THU_0548}, result(1_000_000))
    assert member.usage(board, "alpha")["cost_usd"] == pytest.approx(1.0) and "list_usd" not in member.usage(board, "alpha")


def test_the_driver_writes_the_time_before_each_result(make_board, add_member, tariffed, monkeypatch):
    board = make_board(provider=tariffed)
    add_member(board, "alpha", ["src/"], state="active")
    lines = [json.dumps({"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 5}}}) + "\n", json.dumps(result(5)) + "\n"]

    class Proc:
        pid, stdin, stdout = 1, io.StringIO(), iter(lines)

        def wait(self, timeout=None):
            return 0

    Proc.stdin.close = lambda: None
    monkeypatch.setattr(member, "subprocess", SimpleNamespace(Popen=lambda args, **kw: Proc(), PIPE=-1, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(member, "argv", lambda *a, **kw: ["claude"])
    monkeypatch.setattr(member, "_wait_for_work", lambda b, me: (None, []))
    member.drive(board, "alpha", False, "go")
    kinds = [json.loads(x)["type"] for x in (board.dir / "logs" / "alpha.ndjson").read_text().splitlines()]
    assert kinds == [member.PROCESS_MARK, "assistant", member.TIME_MARK, "result"]


def test_the_prices_line_and_command_say_what_tokens_cost_now_and_until_when(make_board, tariffed, monkeypatch, capsys):
    assert cli.prices_line(tariffed, THU_0548) == "prices    off-peak, half price, until 06:00 UTC (in 12 min); then list prices"
    assert cli.prices_line(tariffed, utc(2026, 10, 9, 10)) == ("prices    off-peak, half price, until Mon 01:00 UTC (in 2 d 15 h); "
                                                              "then list prices")
    board = make_board(provider=tariffed)
    monkeypatch.setattr(cli.time, "time", lambda: THU_0548)
    assert any(line.startswith("prices    off-peak") for line in cli.summary(board, None))
    assert cli.main(["prices", "--project", str(board.root), "--team", "t"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "peak      01:00-04:00 and 06:00-10:00 UTC, mon-fri; off-peak half price"
    assert out[-4:] == ["next      06:00 UTC      list prices", "          10:00 UTC      half price",
                        "          Fri 01:00 UTC  list prices", "          Fri 04:00 UTC  half price"]


def test_prices_without_a_team_use_the_provider_in_the_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["prices", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["provider"] == "deepseek" and obj["prices"]["deepseek-flash"]["output"] == 1.2 and len(obj["changes"]) == 4


def test_the_leader_hears_once_before_peak_prices_while_members_work(make_board, add_member, tariffed):
    board = make_board(provider=tariffed)
    assert cli.price_notice(board, THU_0552) is None
    add_member(board, "alpha", ["src/"], state="active")
    notice = cli.price_notice(board, THU_0552)
    assert notice["at"] == "06:00 UTC" and notice["back"] == "10:00 UTC"
    assert cli._item_line("prices", notice) == ("prices: list prices from 06:00 UTC (in 8 min), half price until then; half price again from "
                                                "10:00 UTC. Tell the user; to wait for the lower price, deepcrew rest now and deepcrew resume then")
    assert cli.price_notice(board, THU_0548) is None
    assert cli.price_notice(board, utc(2026, 10, 8, 9, 55)) is None


def test_the_notice_is_an_inbox_item_keyed_by_the_change(make_board, add_member, tariffed, monkeypatch):
    board = make_board(provider=tariffed)
    add_member(board, "alpha", ["src/"], state="active")
    monkeypatch.setattr(cli.time, "time", lambda: THU_0552)
    items = cli.inbox_items(board)
    assert items[f"prices:{utc(2026, 10, 8, 6):.0f}"][0] == "prices"


def test_the_page_shows_the_tariff(tariffed, provider_table):
    view = ui.prices_view(tariffed, THU_0548)
    assert view["peak"] is False and view["text"] == "half price until 06:00 UTC, in 12 min, then list prices"
    assert ui.prices_view(provider_table, THU_0548) is None


def test_midnight_ends_a_window_and_an_empty_day_list_means_no_peak(tariffed):
    late = {**tariffed, "tariff": {**TARIFF, "peak_utc": ["22:00-24:00"]}}
    providers.check_tariff(late["tariff"])
    assert providers.price_share(late, utc(2026, 10, 8, 23, 59)) == 1.0 and providers.price_share(late, utc(2026, 10, 9, 0, 0)) == 0.5
    never = {**tariffed, "tariff": {**TARIFF, "peak_days": []}}
    assert providers.price_share(never, utc(2026, 10, 8, 7)) == 0.5 and providers.tariff_now(never, THU_0548) is None
