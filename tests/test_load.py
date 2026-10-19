from __future__ import annotations

import pytest

from deepcrew import load, member, route


@pytest.fixture
def host(monkeypatch):
    now = {"cpu": 0.2, "mem": 0.6, "gpu": 0.1, "looks": 0}

    def gpu():
        now["looks"] += 1
        return now["gpu"]
    monkeypatch.setenv("DEEPCREW_LOAD_GUARD", "on")
    monkeypatch.setattr(load, "_cpu", lambda: now["cpu"])
    monkeypatch.setattr(load, "_mem", lambda: now["mem"])
    monkeypatch.setattr(load, "_gpu", gpu)
    return now


@pytest.mark.parametrize(("look", "before", "level"), [
    ({"cpu": 0.5, "mem": 0.5, "gpu": 0.5}, "ok", "ok"),
    ({"cpu": 0.95, "mem": 0.5, "gpu": None}, "ok", "busy"),
    ({"cpu": None, "mem": 0.15, "gpu": None}, "ok", "busy"),
    ({"cpu": 0.2, "mem": 0.5, "gpu": 0.93}, "ok", "busy"),
    ({"cpu": 1.6, "mem": 0.5, "gpu": 0.1}, "ok", "full"),
    ({"cpu": 0.2, "mem": 0.08, "gpu": 0.1}, "busy", "full"),
    ({"cpu": 0.85, "mem": 0.5, "gpu": 0.1}, "busy", "busy"),
    ({"cpu": 0.80, "mem": 0.5, "gpu": 0.1}, "busy", "ok"),
    ({"cpu": 0.2, "mem": 0.105, "gpu": 0.1}, "full", "full"),
    ({"cpu": 0.2, "mem": 0.12, "gpu": 0.1}, "full", "busy"),
    ({"cpu": None, "mem": None, "gpu": None}, "full", "ok"),
])
def test_the_levels_and_the_margin_on_the_way_back(look, before, level):
    assert load.level(look, before) == level


def test_one_look_is_shared_for_a_while_and_the_gpu_is_averaged_with_the_last(host):
    first = load.sample(1000.0)
    assert (first["level"], host["looks"]) == ("ok", 1)
    host["cpu"], host["gpu"] = 2.0, 0.5
    assert load.sample(1000.0 + load.SAMPLE_S - 1)["level"] == "ok" and host["looks"] == 1
    later = load.sample(1000.0 + load.SAMPLE_S)
    assert (later["level"], later["gpu"], host["looks"]) == ("full", 0.3, 2)


def test_how_many_may_run_by_level():
    assert load.limit(None, 8, 3) == 8 and load.limit({"level": "ok"}, 8, 3) == 8
    assert load.limit({"level": "busy"}, 8, 3) == 3 and load.limit({"level": "busy"}, 8, 0) == 1
    assert load.limit({"level": "full"}, 8, 5) == 1 and load.limit({"level": "busy"}, 2, 5) == 2


def test_a_busy_host_holds_new_starts_and_off_means_off(make_board, add_member, post_task, host, monkeypatch):
    board = make_board(max_active=4)
    add_member(board, "run", ["run/"], "active")
    for name in ("one", "two"):
        add_member(board, name, [f"{name}/"])
        post_task(board, name, [f"{name}/x.py"])
    p = route.plan(board)
    assert (p["wake"], p["waiting"], p["load"]["level"]) == (["one", "two"], [], "ok")
    monkeypatch.setattr(load, "sample", lambda now=None: {"cpu": 0.2, "mem": 0.15, "gpu": 0.1, "level": "busy", "ts": 0})
    p = route.plan(board)
    assert (p["wake"], p["waiting"]) == ([], ["one", "two"])
    monkeypatch.setenv("DEEPCREW_LOAD_GUARD", "off")
    assert route.plan(board)["wake"] == ["one", "two"] and route.plan(board)["load"] is None


def test_a_full_host_ends_the_members_that_wait_between_turns_without_a_task(make_board, add_member, monkeypatch):
    board = make_board()
    add_member(board, "waits", ["a/"], "active")
    add_member(board, "works", ["b/"], "active", turn={"since": 1})
    add_member(board, "holds", ["c/"], "active", task="7")
    add_member(board, "rests", ["d/"])
    ended = []
    monkeypatch.setattr(member, "_end", lambda m, force: ended.append((m["name"], force)) or True)
    assert route.ease(board, {"level": "busy"}) == [] and route.ease(board, None) == []
    assert route.ease(board, {"level": "full"}) == ["waits"] and ended == [("waits", False)]
