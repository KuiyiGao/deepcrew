from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import PROVIDER
from deepcrew import cli, member, policy, route
from deepcrew.board import read_json, tools_dir

TWO = {**PROVIDER, "models": ["small", "big"], "efforts": ["high", "max"]}


@pytest.fixture
def team(make_board, add_member):
    board = make_board("main", provider=TWO, default_location=True)
    board.cfg["member"] = {"model": "small", "effort": "high"}
    cfg = read_json(board.dir / "team.json")
    cfg["member"] = board.cfg["member"]
    (board.dir / "team.json").write_text(json.dumps(cfg))
    add_member(board, "alpha", ["src/"])
    add_member(board, "boss", ["lib/"], model="big", effort="max")
    return board


def test_a_task_that_needs_the_strongest_model_goes_only_to_a_member_on_it(team, post_task):
    hard = team.post("parser core", "", [], [], "leader", strength="strongest")
    easy = post_task(team, "readme")
    assert hard["strength"] == "strongest" and team.needs(hard) == "big" and team.needs(easy) is None
    alpha, boss = team.member("alpha"), team.member("boss")
    assert not team.fits(alpha, hard) and team.fits(boss, hard) and team.fits(alpha, easy)
    assert [t["id"] for t in team.eligible(alpha)] == [easy["id"]] and {t["id"] for t in team.eligible(boss)} == {hard["id"], easy["id"]}
    with team.locked():
        assert team.why_not("alpha", hard["id"]) == f"task {hard['id']} needs a member on big (strength strongest); alpha runs small"
        assert team.why_not("boss", hard["id"]) is None
    with pytest.raises(ValueError, match="strength is one of"):
        team.post("x", "", [], [], "leader", strength="huge")


def test_the_leader_hears_when_no_member_runs_the_model_a_task_needs(team, post_task):
    team.forget("boss")
    hard = team.post("review the design", "", [], [], "leader", strength="strongest")
    (b,) = route.plan(team)["blocked"]
    assert b["task"] == hard["id"] and f"spawn NAME --strength strongest --task {hard['id']}" in b["reason"]
    inside = team.post("tricky part", "", ["src/core/"], [], "leader", strength="strongest")
    reasons = {x["task"]: x["reason"] for x in route.plan(team)["blocked"]}
    assert "its region belongs to alpha on small" in reasons[inside["id"]] and "--strength strongest" in reasons[inside["id"]]


def test_post_and_spawn_take_a_strength(team, deepcrew, procs):
    code, out, _ = deepcrew("post", "hard one", "--strength", "strongest", "--no-route")
    assert code == 0 and "strength=strongest (big)" in out
    code, out, err = deepcrew("spawn", "brain", "--region", "web/", "--strength", "strongest")
    assert (code, err) == (0, "") and out == "started brain region=['web/'] task=None model=big\n"
    assert team.model_of(team.member("brain")) == "big" and member.env(team, team.member("brain"))["ANTHROPIC_MODEL"] == "big"


def test_spawn_needs_a_key_first(team, deepcrew, monkeypatch):
    monkeypatch.delenv("FAKE_KEY", raising=False)
    code, out, err = deepcrew("spawn", "newbie", "--region", "web/")
    assert (code, out) == (1, "") and "no fake key yet: the user saves it on the team's page" in err and "stays on this computer" in err


@pytest.fixture
def ending(monkeypatch):
    gone = []
    monkeypatch.setattr(member, "alive", lambda m: bool(m and m.get("pid")) and m["name"] not in gone)
    monkeypatch.setattr(member, "_end", lambda m, force: gone.append(m["name"]) or False)
    return gone


def test_rest_ends_the_members_and_nothing_starts_until_resume(team, add_member, post_task, procs, ending, deepcrew, monkeypatch):
    team.update_member("alpha", state="active", pid=4242, runs=1)
    post_task(team, "work", ["src/a.py"])
    assert route.rest(team, by="user") == ["alpha"] and team.paused()["by"] == "user" and ending == ["alpha"]
    assert team.member("alpha")["state"] == "idle" and route.plan(team)["wake"] == []
    assert member.wake(team, "alpha") is None
    with pytest.raises(member.LaunchError, match="the crew is resting"):
        member.start(team, "late", ["web/"])
    assert "crew: the user rested the crew on the page (Rest crew) at " in deepcrew("inbox")[1]
    assert "resting since" in deepcrew("status")[1].splitlines()[2]
    woken = []
    monkeypatch.setattr(member, "wake", lambda b, name: woken.append(name) or b.member(name))
    code, out, _ = deepcrew("resume")
    assert code == 0 and "the crew may work again" in out and team.paused() is None and woken == ["alpha"]


def test_rest_from_the_command_line_says_who_was_running(team, ending, deepcrew):
    team.update_member("boss", state="active", pid=4242, runs=1)
    assert deepcrew("rest") == (0, "the crew rests (boss was running); `deepcrew resume` starts it again\n", "")
    assert team.paused()["by"] == "leader" and "crew:" not in deepcrew("inbox")[1]
    assert deepcrew("rest")[1] == "the crew rests (nobody was running); `deepcrew resume` starts it again\n"
    deepcrew("resume")
    ending.clear()
    team.update_member("alpha", state="active", pid=4243, runs=1)
    team.update_member("boss", state="active", pid=4242, runs=2)
    assert deepcrew("rest")[1] == "the crew rests (alpha, boss were running); `deepcrew resume` starts it again\n"


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "render-app"
    d.mkdir()
    (d / "render.yaml").write_text("services: []\n")
    return d


def test_a_folder_must_exist_lie_outside_the_project_and_hold_no_secrets(team, project, tmp_path, outside):
    assert policy.check_folder(team, str(outside)) == str(outside.resolve())
    (tmp_path / ".ssh" / "keys").mkdir(parents=True)
    for bad, why in ((tmp_path / "nope", "is not a folder"), (project / "src", "inside the project"), (Path("/"), "too wide"),
                     (Path.home(), "too wide"), (tmp_path / ".ssh" / "keys", "which members may not use"), (team.dir, "which members may not use")):
        with pytest.raises(ValueError, match=why):
            policy.check_folder(team, str(bad))


@pytest.mark.sandbox
def test_a_linked_folder_is_read_by_members_at_once_and_written_only_when_linked_for_writing(team, outside, tmp_path, deepcrew):
    f, yaml, elsewhere = str(outside.resolve()), str(outside.resolve() / "render.yaml"), tmp_path / "elsewhere.txt"
    elsewhere.write_text("x\n")
    assert policy.decide(team, "alpha", "Read", {"file_path": yaml}) is None
    code, out, _ = deepcrew("link", str(outside))
    assert code == 0 and f"linked {f}  (read, linked by leader)" in out and f"/add-dir {f}" in out
    assert policy.decide(team, "alpha", "Read", {"file_path": yaml})["behavior"] == "allow"
    assert policy.decide(team, "alpha", "Edit", {"file_path": yaml})["message"].endswith("is in a folder linked for reading only")
    assert policy.outside(team, f"cat {shlex.quote(yaml)}") == [] and policy.outside(team, f"cat {shlex.quote(str(elsewhere))}") == [str(elsewhere)]
    assert deepcrew("link", str(outside), "--write")[0] == 0 and policy.decide(team, "alpha", "Edit", {"file_path": yaml})["behavior"] == "allow"
    box = member.settings(team)["sandbox"]["filesystem"]["allowWrite"]
    assert f in box and str(tools_dir()) in box
    args = member.argv(team, team.member("alpha"), resume=False)
    dirs = [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]
    assert dirs == [str(team.shared_dir), str(tools_dir()), f] and (tools_dir() / "bin").is_dir()
    assert deepcrew("link", str(outside), "--remove")[0] == 0 and team.folders() == []
    code, _, err = deepcrew("link", str(outside), "--remove")
    assert code == 1 and "is not linked" in err


def test_the_tools_folder_is_writable_by_every_member_and_on_their_path(team):
    tool = str(tools_dir() / "bin" / "render")
    assert policy.decide(team, "alpha", "Write", {"file_path": tool})["behavior"] == "allow"
    assert member.env(team, team.member("alpha"))["PATH"].split(os.pathsep)[-1] == str(tools_dir() / "bin")
    assert str(tools_dir()) in member.system_prompt(team)


def test_a_request_outside_the_project_names_its_folder_and_the_answer_can_link_it(team, outside, deepcrew):
    yaml = str(outside.resolve() / "render.yaml")
    assert policy.folder_wanted(team, "Read", {"file_path": yaml}) == {"folder": str(outside.resolve()), "write": False}
    assert policy.folder_wanted(team, "Bash", {"command": f"ls {shlex.quote(str(outside))}"}) == {"folder": str(outside.resolve()), "write": False}
    assert policy.folder_wanted(team, "Read", {"file_path": "src/a.py"}) == {}
    r = team.request("perms", "alpha", {"tool": "Edit", "input": {"file_path": yaml}, "tool_use_id": None, "always": False,
                                        **policy.folder_wanted(team, "Edit", {"file_path": yaml})})
    assert r["write"] is True
    code, out, _ = deepcrew("approve", r["id"], "--link")
    assert code == 0 and f"linked {outside.resolve()} for writing" in out and team.folders()[0]["write"] is True
    plain = team.request("perms", "alpha", {"tool": "Bash", "input": {"command": "date"}, "tool_use_id": None, "always": False})
    code, _, err = deepcrew("approve", plain["id"], "--link")
    assert code == 1 and "only an allowed request that reaches into a folder" in err


@pytest.mark.sandbox
def test_sites_join_the_members_sandbox_and_bad_names_are_refused(team, deepcrew):
    assert deepcrew("site", "api.render.com")[0] == 0 and deepcrew("site", "*.github.com")[0] == 0
    assert team.sites() == ["api.render.com", "*.github.com"]
    domains = member.settings(team)["sandbox"]["network"]["allowedDomains"]
    assert "api.render.com" in domains and "*.github.com" in domains and "pypi.org" in domains
    for bad in ("not a host", "http://x.com", "localhost", "a..b"):
        code, _, err = deepcrew("site", bad)
        assert code == 1 and "is not a host name" in err, bad
    assert deepcrew("site", "api.render.com", "--remove")[0] == 0 and team.sites() == ["*.github.com"]
    assert "added: *.github.com" in deepcrew("site")[1]


def test_the_leader_gets_the_add_dir_line_as_soon_as_a_folder_is_wanted_or_linked(team, outside, tmp_path, deepcrew):
    folder, notes = str(outside.resolve()), str(outside.resolve() / "notes.txt")
    r = team.request("perms", "alpha", {"tool": "Read", "input": {"file_path": notes}, "tool_use_id": None, "always": False,
                                        **policy.folder_wanted(team, "Read", {"file_path": notes})})
    out = deepcrew("inbox")[1]
    assert f"outside the project: {folder} (reading)" in out and f"\n  /add-dir {folder}\n" in out
    code, out, _ = deepcrew("approve", r["id"], "--link")
    assert code == 0 and out.splitlines()[-1] == f"/add-dir {folder}"
    assert cli.summary(team, None)[-1] == f"/add-dir {folder}" and "for this chat:" in cli.summary(team, None)[-2]
    second = tmp_path / "second"
    second.mkdir()
    code, out, _ = deepcrew("link", str(second), "--json")
    assert code == 0 and json.loads(out)["add_dir"] == f"/add-dir {second.resolve()}"
    assert deepcrew("link", str(second))[1].splitlines()[-1] == f"/add-dir {second.resolve()}"
    third = tmp_path / "third"
    third.mkdir()
    policy.set_folder(team, str(third), by="user")
    out = deepcrew("inbox")[1]
    assert f"link: the user linked {third.resolve()} for reading" in out and f"\n  /add-dir {third.resolve()}" in out


def test_a_resume_while_the_page_rest_still_waits_for_members_stops_nobody_it_started(team, ending, monkeypatch):
    threads, stopped = [], []
    monkeypatch.setattr(route.threading, "Thread", lambda target, name, daemon: SimpleNamespace(start=lambda: threads.append(target)))
    monkeypatch.setattr(member, "stop", lambda board, name: stopped.append(name))
    team.update_member("alpha", state="active", pid=4242, runs=1)
    team.update_member("boss", state="active", pid=4343, runs=1)
    assert route.rest(team, by="user", wait=False) == ["alpha", "boss"] and ending == ["alpha", "boss"]
    monkeypatch.setattr(member, "wake", lambda b, name: None)
    route.resume(team, by="user")
    threads[0]()
    assert stopped == []
    ending.clear()
    team.update_member("alpha", state="active", pid=4242, runs=2)
    team.update_member("boss", state="active", pid=4343, runs=2)
    route.rest(team, by="user", wait=False)
    threads[-1]()
    assert stopped == ["alpha", "boss"]
