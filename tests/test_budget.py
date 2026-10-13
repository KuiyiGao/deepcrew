from __future__ import annotations

import io
import json
import queue
import subprocess
import threading
from types import SimpleNamespace

import pytest

from conftest import PROVIDER
from deepcrew import cli, member, route, ui
from page_helpers import TeamPages

PRICED = {**PROVIDER, "prices": {"m": {"cache_hit": 1.0, "cache_miss": 10.0, "output": 100.0}}}


@pytest.fixture
def priced(make_board, add_member):
    board = make_board("main", provider=PRICED, default_location=True)
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    return board


@pytest.fixture
def link(priced):
    return ui.TeamLink("main", priced.dir)


def turn(board, name, tokens, *, done=True, mid="m1"):
    log = board.dir / "logs" / f"{name}.ndjson"
    log.parent.mkdir(exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "message": {"id": mid, "usage": {"input_tokens": tokens}}}) + "\n")
        if done:
            f.write(json.dumps({"type": "result", "usage": {"input_tokens": tokens}}) + "\n")


def test_the_spend_counts_every_member_and_the_removed_ones_at_list_prices(priced, make_board, add_member, deepcrew):
    turn(priced, "alpha", 100_000)
    turn(priced, "beta", 50_000)
    assert member.spent(priced) == pytest.approx(1.5) and member.budget_state(priced) == {"usd": None, "spent": pytest.approx(1.5), "reached": False}
    assert deepcrew("forget", "alpha")[0] == 0 and priced.member("alpha") is None
    assert member.spent(priced) == pytest.approx(1.5) and priced.live_cfg()["spent_removed"] == pytest.approx(1.0)
    plain = make_board("plain")
    add_member(plain, "gamma", ["src/"])
    turn(plain, "gamma", 100_000)
    assert member.spent(plain) is None and member.budget_state(plain)["reached"] is False


def test_reaching_the_budget_rests_the_crew_and_only_the_user_can_go_on(priced, link, deepcrew, monkeypatch):
    view = ui.set_budget({"usd": 1}, link)
    assert view["budget"] == {"usd": 1.0, "spent": 0.0, "reached": False} and priced.paused() is None
    assert priced.budget()["by"] == "user" and "budget: the user set the team's budget to $1.00, $0.00 spent so far" in deepcrew("inbox")[1]
    turn(priced, "alpha", 100_000)
    assert member.check_budget(priced) is True
    paused = priced.paused()
    assert (paused["by"], paused["usd"], paused["spent"]) == ("budget", 1.0, pytest.approx(1.0))
    assert member.check_budget(priced) is True and priced.paused() == paused
    assert route.plan(priced)["wake"] == [] and member.wake(priced, "beta") is None
    with pytest.raises(member.LaunchError, match=r"spent \$1.00 of the \$1.00 budget the user set"):
        member.start(priced, "late", ["web/"])
    with pytest.raises(ValueError, match="only the user can raise it"):
        route.resume(priced, by="leader")
    code, _, err = deepcrew("resume")
    assert code == 1 and "only the user can raise it, on the team's page" in err and priced.paused() == paused
    out = deepcrew("inbox")[1]
    assert "budget: the team has spent $1.00 of the $1.00 the user set; each member stops after its current turn" in out
    assert "budget    $1.00 of $1.00: reached, the crew rests" in deepcrew("status")[1]
    with pytest.raises(ui.Refused, match="only the user can raise it"):
        ui.crew_action({"action": "resume"}, link)


def test_raising_the_budget_resumes_a_crew_that_rested_for_it_and_not_one_the_user_stopped(priced, link, deepcrew, monkeypatch, post_task):
    woken = queue.Queue()
    monkeypatch.setattr(member, "wake", lambda b, name: woken.put(name))
    post_task(priced, "fix the parser", scope=["src/a.py"])
    turn(priced, "alpha", 100_000)
    ui.set_budget({"usd": 1}, link)
    assert priced.paused()["by"] == "budget"
    view = ui.set_budget({"usd": 2.5}, link)
    assert view["paused"] is None and view["budget"]["reached"] is False
    assert woken.get(timeout=5) == "alpha"
    resumed = priced.live_cfg()["resumed"]
    assert (resumed["by"], resumed["budget"]) == ("user", 2.5)
    assert "crew: the user raised the budget to $2.50 on the page" in deepcrew("inbox")[1]
    route.rest(priced, by="user")
    ui.set_budget({"usd": 0.5}, link)
    assert priced.paused()["by"] == "user"
    ui.set_budget({"usd": 9}, link)
    assert priced.paused()["by"] == "user" and woken.empty()
    route.resume(priced, by="user")
    assert woken.get_nowait() == "alpha"
    ui.set_budget({"usd": 0.5}, link)
    assert priced.paused()["by"] == "budget"
    view = ui.set_budget({"usd": None}, link)
    assert view["paused"] is None and view["budget"]["usd"] is None and priced.budget() is None
    assert woken.get(timeout=5) == "alpha" and "budget: the user removed the team's budget" in deepcrew("inbox")[1]
    for thread in threading.enumerate():
        if thread.name == "resume":
            thread.join(5)


def test_the_pages_total_is_the_budgets_spend_and_unknown_while_a_member_has_no_prices(priced, link, add_member):
    turn(priced, "alpha", 100_000)
    view = ui.team_view(link)
    assert view["total"]["cost_usd"] == pytest.approx(1.0) == view["budget"]["spent"]
    add_member(priced, "gamma", ["web/"], model="unpriced")
    turn(priced, "gamma", 100_000)
    view = ui.team_view(link)
    assert next(m["cost_usd"] for m in view["members"] if m["name"] == "alpha") == pytest.approx(1.0)
    assert view["total"]["cost_usd"] is None and view["budget"]["spent"] is None


def test_the_page_takes_an_amount_and_refuses_anything_else(team_page):
    page = team_page()
    status, _, view = page.call("POST", "/api/budget", {"usd": 5})
    assert status == 200 and view["budget"] == {"usd": 5.0, "spent": None, "reached": False}
    for bad in (0, -1, 0.001, "5", True, 1e9, float("inf")):
        status, _, reply = page.call("POST", "/api/budget", {"usd": bad}) if bad != float("inf") else page.call("POST", "/api/budget", raw='{"usd": 1e999}')
        assert status == 400 and "a budget is an amount in USD" in reply["error"], bad
    assert page.call("GET", "/api/team")[2]["budget"]["usd"] == 5.0


@pytest.fixture
def team_page(lead):
    pages = TeamPages(lead)
    yield pages
    pages.close()


def test_a_member_stops_after_the_turn_that_spent_the_budget(priced, link, monkeypatch):
    ui.set_budget({"usd": 1}, link)
    lines = [json.dumps({"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 100_000}}}) + "\n",
             json.dumps({"type": "result", "usage": {"input_tokens": 100_000}}) + "\n"]

    class Proc:
        pid, stdin, stdout = 1, io.StringIO(), iter(lines)

        def wait(self, timeout=None):
            return 0

    Proc.stdin.close = lambda: None
    monkeypatch.setattr(member, "subprocess", SimpleNamespace(Popen=lambda args, **kw: Proc(), PIPE=-1, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(member, "argv", lambda *a, **kw: ["claude"])
    monkeypatch.setattr(member, "_wait_for_work", lambda b, me: pytest.fail("no next turn after the budget is spent"))
    assert member.drive(priced, "alpha", False, "go") == "idle"
    assert Proc.stdin.getvalue().count('"type": "user"') == 1 and priced.paused()["by"] == "budget"
    assert priced.member("alpha")["state"] == "idle"


def test_a_member_holding_for_work_ends_when_the_crew_rests(priced):
    priced.update_cfg(lambda cfg: cfg.__setitem__("paused", {"by": "budget", "ts": 1}))
    assert member._wait_for_work(priced, "alpha") == (None, [])


def test_the_log_is_read_once_then_only_what_was_appended(priced, monkeypatch):
    turn(priced, "alpha", 100_000)
    assert member.usage(priced, "alpha")["cost_usd"] == pytest.approx(1.0)
    log = priced.dir / "logs" / "alpha.ndjson"
    reads = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda f, mode="r", *a, **kw: reads.append((str(f), mode)) or real_open(f, mode, *a, **kw))
    assert member.usage(priced, "alpha")["cost_usd"] == pytest.approx(1.0) and reads == []
    turn(priced, "alpha", 50_000, mid="m2", done=False)
    u = member.usage(priced, "alpha")
    assert u["cost_usd"] == pytest.approx(1.5) and u["running"] is True and u["requests"] == 2
    with real_open(log, "a", encoding="utf-8") as f:
        f.write('{"type": "result", "usage": {"input_tok')
    assert member.usage(priced, "alpha")["cost_usd"] == pytest.approx(1.5)
    with real_open(log, "a", encoding="utf-8") as f:
        f.write('ens": 50000}}\n')
    u = member.usage(priced, "alpha")
    assert u["cost_usd"] == pytest.approx(1.5) and "running" not in u and u["input_tokens"] == 150_000
    log.write_text(json.dumps({"type": "result", "usage": {"input_tokens": 300_000}}) + "\n" * 200)
    assert member.usage(priced, "alpha")["cost_usd"] == pytest.approx(3.0)
    log.write_text("")
    assert member.usage(priced, "alpha")["cost_usd"] == 0


def test_the_summary_says_the_budget_in_one_line():
    assert cli.budget_line({"usd": None, "spent": 1.234, "reached": False}) == "budget    none set, $1.23 spent; the user can set one on the page"
    assert cli.budget_line({"usd": None, "spent": None, "reached": False}) == "budget    none set; the user can set one on the page"
    assert cli.budget_line({"usd": 5, "spent": 1.2, "reached": False}) == "budget    $1.20 of $5.00"
    assert cli.budget_line({"usd": 5, "spent": 5.01, "reached": True}) == "budget    $5.01 of $5.00: reached, the crew rests"
    assert cli.budget_line({"usd": 5, "spent": None, "reached": False}) == "budget    $5.00, not counted: the model has no prices"


def test_the_leader_hears_once_when_most_of_the_budget_is_spent(priced, link, deepcrew):
    ui.set_budget({"usd": 1}, link)
    turn(priced, "alpha", 70_000)
    assert member.check_budget(priced) is False and "near" not in priced.budget()
    turn(priced, "beta", 15_000)
    assert member.check_budget(priced) is False and priced.budget()["near"]["spent"] == pytest.approx(0.85)
    first = priced.budget()["near"]
    assert member.check_budget(priced) is False and priced.budget()["near"] == first
    out = deepcrew("inbox")[1]
    assert "budget: the team has spent $0.85 of the $1.00 the user set (85%); at the budget each member stops after its turn" in out
    assert "(85%); at the budget each member stops after its turn. Tell the user now, in case they want to raise it on the page   (shown before)" in \
        deepcrew("inbox")[1]
    ui.set_budget({"usd": 2}, link)
    assert "near" not in priced.budget() and member.check_budget(priced) is False
    turn(priced, "alpha", 80_000)
    assert member.check_budget(priced) is False and priced.budget()["near"]["spent"] == pytest.approx(1.65)
    turn(priced, "beta", 50_000)
    assert member.check_budget(priced) is True
    out = deepcrew("inbox")[1]
    assert "budget: the team has spent $2.15 of the $2.00 the user set; each member stops" in out and "(82%)" not in out
