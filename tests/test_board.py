from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from deepcrew import board as board_module
from deepcrew import policy
from deepcrew.board import MAIL_KINDS, SUBDIRS, file_lock, read_json, task_list_id, view, write_json


def raw_task(board, tid) -> dict:
    return read_json(board.tasks_dir / f"{tid}.json")


def test_the_team_directory_is_private_and_laid_out(board):
    assert all((board.dir / sub).is_dir() for sub in SUBDIRS) and stat.S_IMODE(board.dir.stat().st_mode) == 0o700
    assert board.version().startswith("0:") and board.root == board.root.resolve()
    assert board.cfg["task_list"].startswith("deepcrew-") and board.tasks_dir == board.dir / "claude" / "tasks" / board.cfg["task_list"]
    assert board.tasks_dir.is_dir() and (board.tasks_dir / ".lock").is_file()
    assert board.shared_dir == board.root.parent / f"{board.root.name}-deepcrew" / "t"
    assert board.notes_dir.is_dir() and board.artifacts_dir.is_dir() and board.notes_dir.parent == board.artifacts_dir.parent == board.shared_dir


def test_every_team_has_its_own_task_list(tmp_path):
    assert task_list_id(tmp_path / "a") == task_list_id(tmp_path / "a") != task_list_id(tmp_path / "b")


def test_the_team_folder_leaves_the_project_untouched(board, project):
    board.note_write("leader", "topic", "text")
    (board.artifacts_dir / "alpha").mkdir()
    (board.artifacts_dir / "alpha" / "report.txt").write_text("r")
    status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"], capture_output=True, text=True, check=True)
    assert status.stdout == "" and not (project / ".git" / "info" / "exclude").read_text().count(".deepcrew")


def test_the_leader_may_name_the_team_folder(make_board, tmp_path):
    board = make_board(shared_dir=str(tmp_path / "elsewhere"))
    assert board.shared_dir == tmp_path / "elsewhere" and board.notes_dir.is_dir()


def test_post_writes_claude_codes_task_file_and_validates_its_arguments(board):
    t = board.post(" Fix it ", "details", ["src//a/", "docs/", "src/a/"], [], "leader")
    assert (t["id"], t["title"], t["body"], t["scope"], t["state"], t["owner"], t["posted_by"]) == ("1", "Fix it", "details", ["docs/", "src/a/"], "open", None, "leader")
    raw = raw_task(board, "1")
    assert time.time() - raw["metadata"].pop("posted") < 5
    assert raw == {"id": "1", "subject": "Fix it", "description": "details", "status": "pending", "blocks": [], "blockedBy": [],
                   "metadata": {"scope": ["docs/", "src/a/"], "by": "leader"}}
    version = board.version()
    assert board.post("two", "", [], ["1", "1"], "alpha")["deps"] == ["1"] and board.version() != version
    bad = [("", "", [], []), (5, "", [], []), ("x" * 201, "", [], []), ("t", None, [], []), ("t", "x" * 20001, [], []), ("t", "", "src/", []),
           ("t", "", "", []), ("t", "", [5], []), ("t", "", ["/abs"], []), ("t", "", [], "1"), ("t", "", [], [7]), ("t", "", [], ["99"])]
    for title, body, scope, deps in bad:
        with pytest.raises(ValueError):
            board.post(title, body, scope, deps, "leader")
    assert [x["id"] for x in board.tasks()] == ["1", "2"] and board.post("ok", "", [], [], "leader")["id"] == "3"
    assert not Path(f"{board.tasks_dir}/.lock.lock").exists()


def test_ids_continue_from_the_highest_ever_handed_out_and_stay_in_numeric_order(board):
    (board.tasks_dir / ".highwatermark").write_text("8")
    assert [board.post(t, "", [], [], "leader")["id"] for t in ("older", "newer")] == ["9", "10"] == [t["id"] for t in board.tasks()]
    assert board.task("10")["title"] == "newer"
    (board.tasks_dir / ".highwatermark").write_text("20")
    assert board.post("after deletions", "", [], [], "leader")["id"] == "21"
    (board.tasks_dir / ".highwatermark").write_text("3")
    assert board.post("stale mark", "", [], [], "leader")["id"] == "22"
    (board.tasks_dir / ".highwatermark").write_text("")
    assert board.post("empty mark", "", [], [], "leader")["id"] == "23"


def test_a_posted_task_is_listed_in_the_blocks_of_each_dependency(board, post_task):
    a, b = post_task(board, "a"), post_task(board, "b")
    path = board.tasks_dir / "1.json"
    path.write_text(json.dumps({**raw_task(board, "1"), "activeForm": "Doing a"}))
    c = post_task(board, "c", [], [b["id"], a["id"]])
    assert c["deps"] == ["1", "2"] and raw_task(board, "1")["blocks"] == raw_task(board, "2")["blocks"] == ["3"]
    (board.tasks_dir / ".highwatermark").write_text("9")
    assert post_task(board, "d", [], [a["id"]])["id"] == "10" and raw_task(board, "1")["blocks"] == ["3", "10"]
    assert raw_task(board, "1")["activeForm"] == "Doing a"


def test_a_half_written_task_file_is_skipped_not_quarantined(board, post_task):
    post_task(board, "a")
    half = board.tasks_dir / "2.json"
    half.write_text('{"id": "2", "subj')
    (board.tasks_dir / "notes.json").write_text("{}")
    assert [t["id"] for t in board.tasks()] == ["1"] and board.task("2") is None
    assert half.exists() and not (board.tasks_dir / "bad").exists() and not (board.dir / "errors.jsonl").exists()
    assert post_task(board, "c")["id"] == "3"
    half.write_text(json.dumps({"id": "2", "subject": "b", "status": "pending", "metadata": {"scope": []}}))
    assert [t["title"] for t in board.tasks()] == ["a", "b", "c"]


def test_two_posters_never_get_the_same_id(board):
    errors = []

    def poster(k):
        try:
            for i in range(10):
                board.post(f"t{k}-{i}", "", [], [], "leader")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=poster, args=(k,)) for k in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == [] and [t["id"] for t in board.tasks()] == [str(i) for i in range(1, 31)]


def test_file_lock_is_a_lock_directory_beside_the_file_and_is_released_even_when_the_work_fails(tmp_path):
    path = tmp_path / "1.json"
    with file_lock(path):
        assert Path(f"{path}.lock").is_dir()
    assert not Path(f"{path}.lock").exists()
    with pytest.raises(RuntimeError), file_lock(path):
        raise RuntimeError("boom")
    assert not Path(f"{path}.lock").exists()


def test_file_lock_takes_over_a_lock_whose_holder_died_and_waits_for_a_live_one(tmp_path, monkeypatch):
    path, lock = tmp_path / "1.json", Path(f"{tmp_path / '1.json'}.lock")
    lock.mkdir()
    old = time.time() - board_module.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    with file_lock(path):
        assert lock.is_dir() and lock.stat().st_mtime > old
    assert not lock.exists()
    lock.mkdir()
    monkeypatch.setattr(board_module, "LOCK_WAIT_S", 0.05)
    with pytest.raises(TimeoutError, match="is held"), file_lock(path):
        pytest.fail("entered a held lock")
    assert lock.is_dir()
    monkeypatch.setattr(board_module, "LOCK_WAIT_S", 5)
    threading.Timer(0.1, lock.rmdir).start()
    with file_lock(path):
        assert lock.is_dir()


def test_deepcrew_writes_task_files_only_under_claude_codes_lock(board, add_member, post_task, monkeypatch):
    add_member(board, "alpha", ["src/"])
    t = post_task(board, "t", ["src/a.py"])
    monkeypatch.setattr(board_module, "LOCK_WAIT_S", 0.05)
    for held, act in ((f"{board.tasks_dir}/.lock", lambda: board.post("u", "", [], [], "leader")),
                      (f"{board.tasks_dir}/{t['id']}.json", lambda: board.claim("alpha", t["id"]))):
        Path(f"{held}.lock").mkdir()
        with pytest.raises(TimeoutError):
            act()
        Path(f"{held}.lock").rmdir()
        assert [x["id"] for x in board.tasks()] == [t["id"]] and board.task(t["id"])["state"] == "open"
    assert board.claim("alpha", t["id"])[1] == "claimed"


def test_view_maps_a_task_file_to_deepcrews_fields():
    md = {"scope": ["src//a/", "docs/"], "by": "alpha", "summary": "s", "pass:beta": "no", "release:gamma": "tired", "skip:delta": "silent"}
    md.update({"volunteer:eps": "12.5", "posted": "40.5"})
    raw = {"id": "7", "subject": "S", "description": "D", "status": "pending", "blocks": ["9"], "blockedBy": ["3", "4"], "metadata": md}
    assert view(raw) == {"id": "7", "title": "S", "body": "D", "scope": ["docs/", "src/a/"], "deps": ["3", "4"], "state": "open", "owner": None, "summary": "s", "gist": None,
                         "posted_by": "alpha", "passed_by": ["beta"], "released_by": ["gamma"], "ignored_by": ["delta"], "strength": None,
                         "volunteers": {"eps": 12.5}, "pitches": {}, "posted": 40.5,
                         "reasons": {"pass:beta": "no", "release:gamma": "tired", "skip:delta": "silent"}}
    bare = view({"id": "1"})
    assert (bare["title"], bare["body"], bare["scope"], bare["deps"], bare["state"], bare["posted_by"], bare["reasons"]) == ("", "", [], [], "open", None, {})
    assert view({"id": "1", "metadata": None})["scope"] == []


@pytest.mark.parametrize("fields, state", [
    ({}, "open"), ({"owner": "alpha", "status": "in_progress"}, "claimed"), ({"owner": "", "status": "pending"}, "open"),
    ({"owner": "alpha", "status": "completed"}, "done"), ({"owner": "alpha", "status": "completed", "metadata": {"outcome": "cancelled"}}, "cancelled"),
    ({"status": "completed", "metadata": {"outcome": "other"}}, "done")])
def test_view_derives_the_state_from_status_owner_and_outcome(fields, state):
    assert view({"id": "1", **fields})["state"] == state


@pytest.mark.parametrize("scope", ["src/", ["/abs"], [5], ["../x"], [["src/"]]])
def test_view_marks_an_unusable_scope_as_none_so_no_region_contains_it(scope):
    assert view({"id": "1", "metadata": {"scope": scope}})["scope"] is None


def test_eligible_needs_open_done_dependencies_the_region_and_no_refusal_by_the_member(board, add_member, post_task, finish, task_edit):
    alpha = add_member(board, "alpha", ["src/"])
    add_member(board, "gamma", ["docs/"])
    a = post_task(board, "a", ["src/a.py"])
    post_task(board, "elsewhere", ["docs/x.md"])
    b = post_task(board, "needs a", ["src/a.py"], [a["id"]])
    free, c = post_task(board, "no files"), post_task(board, "c", ["src/a.py"])
    broken = post_task(board, "broken scope", ["src/a.py"])
    task_edit(board, broken["id"], scope="src/a.py")
    assert [t["title"] for t in board.eligible(alpha)] == ["a", "no files", "c"]
    board.claim("alpha", a["id"])
    assert [t["title"] for t in board.eligible(alpha)] == ["no files", "c"]
    finish(board, "alpha", a["id"])
    assert [t["title"] for t in board.eligible(alpha)] == ["needs a", "no files", "c"]
    board.mark("alpha", [b["id"]], "pass", "not now")
    board.claim("alpha", free["id"])
    board.release("alpha", free["id"], "not mine")
    board.mark("alpha", [c["id"]], "skip", "offered; neither taken nor declined")
    assert board.eligible(alpha) == []
    other = {"name": "beta", "region": ["src/"]}
    assert [t["title"] for t in board.eligible(other)] == ["needs a", "no files", "c"]


def test_a_cancelled_dependency_never_becomes_done(board, add_member, post_task):
    alpha = add_member(board, "alpha", ["src/"])
    gone = post_task(board, "gone", ["src/a.py"])
    post_task(board, "after", ["src/a.py"], [gone["id"]])
    board.cancel(gone["id"], "leader", "no longer needed")
    assert board.eligible(alpha) == [] and board.claim("alpha", "2") == (None, "task 2 waits for 1")


def test_claim_explains_every_refusal_and_holds_one_task_at_a_time(board, add_member, post_task, finish, task_edit):
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    a = post_task(board, "a", ["src/a.py"])
    dep = post_task(board, "dep", ["src/pkg/"], [a["id"]])
    docs = post_task(board, "docs", ["docs/x.md"])
    broken = post_task(board, "broken", ["src/a.py"])
    task_edit(board, broken["id"], scope="src/a.py")
    version = board.version()
    assert board.claim("ghost", a["id"]) == (None, "'ghost' is not a member of this team")
    assert board.claim("alpha", "99") == (None, "there is no task 99") and board.claim("alpha", "../1") == (None, "there is no task ../1")
    assert board.claim("alpha", dep["id"]) == (None, "task 2 waits for 1")
    assert board.claim("alpha", docs["id"]) == (None, "task 3 changes ['docs/x.md'], outside alpha's region ['src/']")
    assert board.claim("alpha", broken["id"]) == (None, "task 4 has no usable scope: its metadata.scope is not a list of project paths")
    assert board.version() == version
    t, why = board.claim("alpha", a["id"])
    assert (t["id"], t["state"], t["owner"], why) == ("1", "claimed", "alpha", "claimed") and board.member("alpha")["task"] == "1"
    assert (raw_task(board, "1")["owner"], raw_task(board, "1")["status"]) == ("alpha", "in_progress")
    assert board.claim("alpha", a["id"])[1] == "claimed"
    assert board.claim("alpha", docs["id"]) == (None, "alpha holds task 1 and takes another only once that one is done or given back")
    assert board.claim("beta", a["id"]) == (None, "task 1 is claimed by alpha")
    finish(board, "alpha", a["id"])
    assert board.member("alpha")["task"] is None and board.claim("beta", a["id"]) == (None, "task 1 is done by alpha")
    assert board.claim("alpha", dep["id"])[1] == "claimed"
    board.cancel(dep["id"], "leader", "not needed")
    assert board.claim("beta", dep["id"]) == (None, "task 2 is cancelled")


def test_a_failed_member_is_given_no_task(board, add_member, post_task):
    add_member(board, "alpha", ["src/"], "failed")
    t = post_task(board, "t", ["src/a.py"])
    why = "'alpha' is not a working member of this team (it failed; resume the crew once the cause is fixed)"
    assert board.claim("alpha", t["id"]) == (None, why)
    with board.locked():
        assert board._assign("alpha", t["id"]) == (None, why)
    assert board.task(t["id"])["state"] == "open" and board.member("alpha")["task"] is None


def test_a_claim_that_cannot_rewrite_the_task_file_claims_nothing(board, add_member, post_task, monkeypatch):
    add_member(board, "alpha", ["src/"])
    t = post_task(board, "t", ["src/a.py"])
    version = board.version()
    monkeypatch.setattr(board, "_update", lambda tid, change: None)
    assert board.claim("alpha", t["id"]) == (None, f"task {t['id']} could not be read just now; try again")
    assert board.member("alpha")["task"] is None and board.version() == version


def test_claim_refuses_a_task_another_member_is_taking(board, add_member, post_task):
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["src2/"])
    t = post_task(board, "t", ["src/a.py"])
    board.update_member("beta", task=t["id"])
    assert board.claim("alpha", t["id"]) == (None, "task 1 is being taken by beta")


def test_release_gives_a_held_task_back_and_remembers_who_did_it_by_choice(board, add_member, post_task, finish):
    alpha = add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    a, b = post_task(board, "a", ["src/a.py"]), post_task(board, "b", ["src/a.py"])
    board.claim("alpha", a["id"])
    assert board.release("beta", a["id"], "mine now")["state"] == "claimed" and board.member("alpha")["task"] == a["id"]
    t = board.release("alpha", a["id"], "needs the docs owner")
    assert (t["state"], t["owner"], t["released_by"], t["reasons"]) == ("open", None, ["alpha"], {"release:alpha": "needs the docs owner"})
    assert board.member("alpha")["task"] is None and "owner" not in raw_task(board, "1") and raw_task(board, "1")["status"] == "pending"
    assert [x["id"] for x in board.eligible(alpha)] == [b["id"]]
    board.claim("alpha", b["id"])
    t = board.release("alpha", b["id"], by_choice=False)
    assert t["released_by"] == [] and t["reasons"] == {} and [x["id"] for x in board.eligible(alpha)] == [b["id"]]
    board.claim("alpha", b["id"])
    finish(board, "alpha", b["id"])
    assert board.release("alpha", b["id"], "too late")["state"] == "done" and board.release("alpha", "99", "x") is None


def test_mark_records_a_refusal_on_open_tasks_only_and_keeps_the_first_reason(board, add_member, post_task):
    alpha = add_member(board, "alpha", ["src/"])
    a, b, c = (post_task(board, name, ["src/a.py"]) for name in "abc")
    board.claim("alpha", b["id"])
    version = board.version()
    board.mark("alpha", [a["id"], b["id"], "99"], "pass", "no")
    assert board.version() != version
    assert (board.task(a["id"])["passed_by"], board.task(a["id"])["reasons"]) == (["alpha"], {"pass:alpha": "no"})
    assert board.task(b["id"])["reasons"] == {} and board.task("99") is None
    board.mark("alpha", [a["id"]], "pass", "changed my mind")
    assert board.task(a["id"])["reasons"] == {"pass:alpha": "no"}
    board.mark("alpha", [c["id"]], "skip", "offered; neither taken nor declined")
    assert (board.task(c["id"])["ignored_by"], board.task(c["id"])["passed_by"]) == (["alpha"], []) and board.eligible(alpha) == []
    with pytest.raises(ValueError):
        board.mark("alpha", [c["id"]], "bogus", "x")


def test_sync_member_forgets_a_task_that_claude_code_took_from_it(board, add_member, post_task, task_edit):
    add_member(board, "alpha", ["src/"])
    a, b = post_task(board, "a", ["src/a.py"]), post_task(board, "b", ["src/a.py"])
    board.claim("alpha", a["id"])
    board.sync_member("alpha")
    assert board.member("alpha")["task"] == a["id"]
    task_edit(board, a["id"], status="completed", summary="done")
    board.sync_member("alpha")
    assert board.member("alpha")["task"] is None
    board.claim("alpha", b["id"])
    raw = raw_task(board, b["id"])
    board_module.write_json(board.tasks_dir / "2.json", {**raw, "status": "pending", "owner": ""})
    board.sync_member("alpha")
    assert board.member("alpha")["task"] is None and board.task(b["id"])["state"] == "open"
    board.sync_member("ghost")


def test_cancel_frees_the_holder_and_refuses_closed_tasks(board, add_member, post_task, finish):
    add_member(board, "alpha", ["src/"])
    a, b = post_task(board, "a", ["src/a.py"]), post_task(board, "b", ["src/a.py"])
    board.claim("alpha", a["id"])
    t, why = board.cancel(a["id"], "leader", "no longer needed")
    assert (why, t["state"], t["summary"]) == ("cancelled", "cancelled", "cancelled by leader: no longer needed")
    assert (raw_task(board, "1")["status"], raw_task(board, "1")["metadata"]["outcome"]) == ("completed", "cancelled") and board.member("alpha")["task"] is None
    assert board.cancel(a["id"], "leader", "") == (None, "task 1 is cancelled") and board.cancel("99", "leader", "") == (None, "there is no task 99")
    board.claim("alpha", b["id"])
    finish(board, "alpha", b["id"])
    assert board.cancel(b["id"], "leader", "") == (None, "task 2 is done")


def test_new_member_rejects_bad_names_and_overlapping_regions(board):
    with board.locked():
        m = board.new_member("alpha", ["src//pkg/", "README.md"], "s1", role="owns src")
        assert (m["region"], m["state"], m["runs"], m["role"]) == (["README.md", "src/pkg/"], "starting", 0, "owns src")
        for name in ("leader", "all", "Alpha", "1a", "", "a", "a" * 25, "a_b", None, 5):
            with pytest.raises(ValueError, match="member names are 2-24 characters"):
                board.new_member(name, ["x/"], "s")
        for region in (["src/"], ["src/pkg/b.py"], ["README.md"], ["lib/", "src/pkg/"]):
            with pytest.raises(ValueError, match="overlaps the region of alpha"):
                board.new_member("beta", region, "s")
        with pytest.raises(ValueError, match="exists"):
            board.new_member("alpha", ["z/"], "s")
        board.new_member("beta", ["src2/"], "s")
        board.new_member("reader", [], "s")
        board.new_member("reader2", [], "s")
        board.new_member("qa", ["qa/"], "s")
        board.new_member("q" * 24, ["long/"], "s")
    assert [x["name"] for x in board.members()] == ["alpha", "beta", "qa", "q" * 24, "reader", "reader2"]
    assert board.member("qa")["name"] == "qa" and board.member("a") is None and board.member("../team") is None


def test_set_region_changes_the_region_tells_the_member_and_records_who_did_it(board, add_member):
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    version, before = board.version(), time.time()
    m = board.set_region("alpha", ["src//pkg/", "README.md", "src/pkg/"], by="leader")
    assert m["region"] == ["README.md", "src/pkg/"] and board.member("alpha")["region"] == ["README.md", "src/pkg/"] and board.write_area("alpha") == m["region"]
    assert board.version() != version and m["updated"] >= before and board.member("beta")["region"] == ["docs/"]
    (change,) = m["region_changes"]
    assert (change["by"], change["was"]) == ("leader", ["src/"]) and change["ts"] >= before
    ((_, mail),) = board.peek_mail("alpha")
    assert (mail["from"], mail["kind"], mail["task"]) == ("leader", "notice", None)
    assert mail["text"] == ("Your region was changed by the leader. It is now: README.md, src/pkg/. You may now also change: README.md. "
                            "You no longer own: src/. Change files only inside your region.")
    assert board.peek_mail("beta") == []
    board.set_region("alpha", ["src/"], by="user")
    assert [(c["by"], c["was"]) for c in board.member("alpha")["region_changes"]] == [("leader", ["src/"]), ("user", ["README.md", "src/pkg/"])]
    assert board.peek_mail("alpha")[1][1]["from"] == "user"


def test_set_region_with_the_region_the_member_has_changes_and_sends_nothing(board, add_member):
    add_member(board, "alpha", ["src/", "README.md"], "active")
    version = board.version()
    m = board.set_region("alpha", ["README.md", "src//"])
    assert m["region"] == ["README.md", "src/"] and "region_changes" not in board.member("alpha") and board.peek_mail("alpha") == [] and board.version() == version


def test_set_region_refuses_an_overlap_an_unknown_member_and_paths_that_are_no_region(board, add_member):
    add_member(board, "alpha", ["src/"])
    add_member(board, "beta", ["docs/"])
    for region, clash in ((["docs/", "src/pkg/"], "src/pkg/"), (["src/a.py", "docs/x.md"], "src/a.py"), (["src/"], "src/")):
        with pytest.raises(ValueError, match=rf"^{clash} overlaps the region of alpha: \['src/'\]\. Take it off alpha first$"):
            board.set_region("beta", region)
    with pytest.raises(ValueError, match="'ghost' is not a member of this team"):
        board.set_region("ghost", ["x/"])
    for bad, why in (("src/", "region must be a list of strings"), (["/etc"], "project-relative"), ([".."], "must stay inside"), ([5], "region must be a list of strings")):
        with pytest.raises(ValueError, match=why):
            board.set_region("beta", bad)
    assert board.member("beta")["region"] == ["docs/"] and "region_changes" not in board.member("beta") and board.peek_mail("beta") == []


def test_set_region_may_leave_a_member_with_no_files_and_says_so(board, add_member):
    add_member(board, "alpha", ["src/"], "active")
    board.set_region("alpha", [])
    assert board.member("alpha")["region"] == [] and board.write_area("alpha") == []
    assert board.peek_mail("alpha")[0][1]["text"] == ("Your region was changed by the leader. It is now: no files. You no longer own: src/. "
                                                     "You can read everything and change no files.")
    board.set_region("alpha", ["src/"])
    assert board.member("alpha")["region"] == ["src/"]


def test_a_region_notice_too_long_for_a_message_is_cut_and_the_change_stands(board, add_member):
    add_member(board, "alpha", ["src/"])
    paths = [f"generated/part-{i:04d}/{'x' * 30}.txt" for i in range(1000)]
    board.set_region("alpha", paths)
    assert len(board.member("alpha")["region"]) == 1000
    ((_, mail),) = board.peek_mail("alpha")
    assert len(mail["text"]) == board_module.NOTICE_MAX_CHARS and mail["text"].startswith("Your region was changed by the leader. It is now: generated/part-0000/")


def test_set_region_never_cuts_a_path_of_the_task_the_member_holds(board, add_member, post_task, finish):
    add_member(board, "alpha", ["src/"], "active")
    t = post_task(board, "work", ["src/a.py", "src/pkg/b.py"])
    board.claim("alpha", t["id"])
    with pytest.raises(ValueError, match=rf"alpha holds task {t['id']}, which changes \['src/pkg/b.py'\]: outside the new region \['src/a.py'\]\. Keep those paths in the region"):
        board.set_region("alpha", ["src/a.py"])
    with pytest.raises(ValueError, match="which changes"):
        board.set_region("alpha", [])
    assert board.member("alpha")["region"] == ["src/"] and board.peek_mail("alpha") == []
    board.set_region("alpha", ["src/a.py", "src/pkg/"])
    finish(board, "alpha", t["id"])
    board.set_region("alpha", ["src/a.py"])
    survey = post_task(board, "survey")
    board.claim("alpha", survey["id"])
    board.set_region("alpha", [])
    assert board.member("alpha")["region"] == []


def test_set_region_is_seen_at_once_by_a_board_opened_before_and_by_what_a_member_may_take(board, add_member, post_task):
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    t = post_task(board, "shared", ["src/a.py", "docs/x.md"])
    later = board_module.Board(board.dir)
    assert [x["id"] for x in later.eligible(later.member("alpha"))] == [t["id"]] == [x["id"] for x in later.eligible(later.member("beta"))]
    board.set_region("alpha", ["src/pkg/"])
    board.set_region("beta", ["docs/", "src/a.py"])
    assert later.member("beta")["region"] == ["docs/", "src/a.py"] and later.write_area("beta") == ["docs/", "src/a.py"] and later.write_area("alpha") == ["src/pkg/"]
    assert [x["id"] for x in later.eligible(later.member("beta"))] == [t["id"]] and later.eligible(later.member("alpha")) == []
    with later.locked():
        assert later.why_not("alpha", t["id"]) == f"task {t['id']} changes {t['scope']}, outside alpha's region ['src/pkg/']"
        assert later.why_not("beta", t["id"]) is None


def test_the_region_a_member_had_at_a_time_comes_from_its_changes(board, add_member, clock, monkeypatch):
    m = {"region": ["lib/"], "region_changes": [{"ts": 100.0, "by": "leader", "was": ["src/"]}, {"ts": 200.0, "by": "leader", "was": ["src/a/"]}]}
    assert [board_module.region_at(m, t) for t in (50, 99.9, 100, 150, 199.9, 200, 250)] == [["src/"], ["src/"], ["src/a/"], ["src/a/"], ["src/a/"], ["lib/"], ["lib/"]]
    assert board_module.region_at({"region": ["x/"]}, 0) == ["x/"]
    add_member(board, "alpha", ["src/"])
    created = board.member("alpha")["created"]
    board.set_region("alpha", ["src/a/"])
    board.set_region("alpha", ["lib/"])
    m = board.member("alpha")
    assert board_module.region_at(m, created) == ["src/"] and board_module.region_at(m, m["region_changes"][1]["ts"] - 0.0001) == ["src/a/"]
    assert board_module.region_at(m, board_module.time.time()) == ["lib/"]
    monkeypatch.setattr(board_module, "REGION_CHANGES_KEPT", 2)
    for region in (["a/"], ["b/"], ["c/"]):
        board.set_region("alpha", region)
    assert [c["was"] for c in board.member("alpha")["region_changes"]] == [["a/"], ["b/"]]


def test_reap_releases_a_dead_members_task_without_blame_and_forgets_members_that_never_ran(board, add_member, post_task):
    add_member(board, "dead", ["a/"], "active", runs=1)
    add_member(board, "fresh", ["b/"], "starting")
    add_member(board, "live", ["c/"], "active", runs=2)
    add_member(board, "idle", ["d/"], "idle")
    t = post_task(board, "t", ["a/x.py"])
    board.claim("dead", t["id"])
    assert board.reap(lambda ms: {m["name"] for m in ms} & {"live"}) == ["dead", "fresh"]
    dead, task = board.member("dead"), board.task(t["id"])
    assert (dead["state"], dead["pid"], dead["task"]) == ("idle", None, None) and (task["state"], task["owner"], task["released_by"]) == ("open", None, [])
    assert board.member("fresh") is None and board.member("live")["state"] == "active" and board.member("idle")["state"] == "idle"
    assert board.claim("dead", t["id"])[0]["id"] == t["id"]
    version = board.version()
    assert board.reap(lambda ms: {m["name"] for m in ms}) == [] and board.version() == version


def test_forget_frees_the_region_and_reopens_the_task_without_blame(board, add_member, post_task):
    add_member(board, "alpha", ["src/"], "active", runs=1)
    t = post_task(board, "t", ["src/a.py"])
    board.claim("alpha", t["id"])
    assert board.forget("alpha")["region"] == ["src/"] and board.member("alpha") is None and board.forget("alpha") is None
    task = board.task(t["id"])
    assert (task["state"], task["owner"], task["released_by"]) == ("open", None, [])
    with board.locked():
        board.new_member("beta", ["src/"], "s")


def test_forget_takes_back_what_the_member_asked_for(board, add_member, post_task):
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    review, survey = post_task(board, "review"), post_task(board, "survey")
    board.mark("alpha", [review["id"]], "volunteer", "1.0")
    board.mark("beta", [review["id"], survey["id"]], "volunteer", "2.0")
    board.forget("alpha")
    assert board.task(review["id"])["volunteers"] == board.task(survey["id"])["volunteers"] == {"beta": 2.0}
    assert "volunteer:alpha" not in raw_task(board, review["id"])["metadata"]


def test_post_to_a_member_holding_a_task_says_once_how_to_post_instead(board, add_member, post_task):
    add_member(board, "alpha", ["src/"])
    board.claim("alpha", post_task(board, "first", ["src/a.py"])["id"])
    with pytest.raises(ValueError) as refused:
        board.post("next", "", ["src/b.py"], [], "leader", to="alpha")
    assert str(refused.value) == ("cannot give the task to alpha: it holds task 1, and a task with a scope inside its region is offered to it alone "
                                  "once task 1 ends; post it without --to")


def test_a_name_the_team_started_stays_used_after_the_member_is_forgotten(board, add_member):
    assert board.used_names() == [] and not (board.dir / "names.json").exists()
    add_member(board, "alpha", ["src/"], "active", runs=1)
    with board.locked():
        board.use_name("alpha")
        board.use_name("alpha")
        board.use_name("beta")
    board.forget("alpha")
    assert board.used_names() == ["alpha", "beta"] and read_json(board.dir / "names.json") == ["alpha", "beta"]


def test_a_corrupt_member_or_request_file_is_filed_away_and_never_stops_the_team(board, add_member):
    add_member(board, "alpha", ["src/"])
    for sub, name, read in (("members", "x", board.members), ("questions", "Q0000dead", lambda: board.pending("questions")),
                            ("perms", "P0000dead", lambda: board.pending("perms"))):
        (board.dir / sub / f"{name}.json").write_text("{not json")
        read()
        assert (board.dir / sub / "bad" / f"{name}.json").exists() and not (board.dir / sub / f"{name}.json").exists()
    assert [m["name"] for m in board.members()] == ["alpha"] and len((board.dir / "errors.jsonl").read_text().splitlines()) == 3


def test_mail_is_delivered_oldest_first_and_once_and_validated(board, clock):
    version = board.version()
    for i in range(8):
        board.send("alpha", "beta", "notice", f"m{i}", task="1" if i == 0 else None)
    assert board.version() != version and len(board.mail_pending("beta")) == 8
    assert [m["text"] for m in board.take_mail("beta")] == [f"m{i}" for i in range(8)] and board.take_mail("beta") == [] and board.mail_pending("beta") == []
    assert len(list((board.dir / "mail" / "beta" / "read").glob("*.json"))) == 8 and board.take_mail("nobody") == []
    assert MAIL_KINDS == ("message", "answer", "notice")
    assert [board.send("a", "beta", kind, "x")["kind"] for kind in MAIL_KINDS] == list(MAIL_KINDS) and len(board.send("a", "b", "notice", "x" * 20000)["text"]) == 20000
    for kind, text in (("bogus", "x"), ("question", "x"), ("notice", ""), ("notice", "  "), ("notice", None), ("notice", ["x"]), ("notice", "x" * 20001)):
        with pytest.raises(ValueError):
            board.send("a", "gamma", kind, text)
    assert not (board.dir / "mail" / "gamma").exists()


def test_peek_leaves_mail_unread_and_can_choose_the_sender(board, clock):
    board.send("alpha", "beta", "message", "from alpha")
    board.send("leader", "beta", "notice", "from the leader")
    both = board.peek_mail("beta")
    assert [m["text"] for _, m in both] == ["from alpha", "from the leader"] and len(board.mail_pending("beta")) == 2
    assert [m["text"] for _, m in board.peek_mail("beta", only_from="leader")] == ["from the leader"]
    assert [m["text"] for m in board.ack_mail(both[:1])] == ["from alpha"] and board.ack_mail(both[:1]) == []
    assert [m["text"] for m in board.take_mail("beta", only_from="leader")] == ["from the leader"] and board.mail_pending("beta") == []


def test_unparseable_mail_goes_to_bad_and_the_rest_is_delivered(board):
    box = board.dir / "mail" / "beta"
    box.mkdir(parents=True)
    (box / "0000000000000001-a.json").write_text("{not json")
    (box / "0000000000000002-b.json").write_bytes(b"\xff\xfe")
    board.send("alpha", "beta", "notice", "fine")
    assert [m["text"] for m in board.take_mail("beta")] == ["fine"] and board.mail_pending("beta") == []
    assert sorted(p.name for p in (box / "bad").iterdir()) == ["0000000000000001-a.json", "0000000000000002-b.json"]


def test_two_takers_never_get_the_same_message(board):
    for i in range(40):
        board.send("alpha", "beta", "notice", f"m{i}")
    got = [[], []]

    def taker(k):
        while board.mail_pending("beta"):
            got[k] += [m["text"] for m in board.take_mail("beta")]

    threads = [threading.Thread(target=taker, args=(k,)) for k in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(got[0] + got[1]) == sorted(f"m{i}" for i in range(40))


def test_the_version_changes_with_deepcrews_changes_and_with_task_files_deepcrew_did_not_write(board, post_task, task_edit):
    start = board.version()
    assert board.version() == start and board.tasks() == []
    post_task(board, "a")
    posted = board.version()
    task_edit(board, "1", summary="written by Claude Code")
    edited = board.version()
    board.send("alpha", "beta", "notice", "x")
    sent = board.version()
    assert len({start, posted, edited, sent}) == 4 and edited.split(":")[0] == posted.split(":")[0] != sent.split(":")[0]


def test_own_file_accepts_only_the_members_own_notes_and_artifacts(board):
    notes, art = str(board.notes_dir), str(board.artifacts_dir)
    assert all(board.own_file("alpha", f"{notes}/{name}") for name in ("alpha-api.md", "alpha-API_v2.1.md", "alpha-" + "x" * 80 + ".md"))
    assert board.own_file("alpha", f"{art}/alpha/report.txt") and board.own_file("alpha", f"{art}/alpha/sub/data.json")
    for refused in (f"{notes}/beta-api.md", f"{notes}/alphabet-api.md", f"{notes}/alpha-api.txt", f"{notes}/alpha-.md", f"{notes}/alpha-x/y.md",
                    f"{notes}/sub/alpha-api.md", f"{notes}/alpha-" + "x" * 81 + ".md", f"{art}/beta/x.txt", f"{art}/alpha", f"{art}/alphabet/x",
                    f"{notes}/../notes2/alpha-api.md", str(board.root / "alpha-api.md")):
        assert not board.own_file("alpha", refused), refused
    assert board.in_shared(f"{art}/beta/x.txt") and not board.in_shared(str(board.root / "src"))


def test_note_write_makes_a_markdown_file_named_for_who_wrote_it_and_what_it_is_about(board):
    path = board.note_write("leader", " API contract ", "parse() returns tokens")
    assert path == board.notes_dir / "leader-API-contract.md" and path.read_text() == "parse() returns tokens"
    assert board.note_write("leader", "API contract", "v2") == path and path.read_text() == "v2"
    assert board.note_write("leader", "???", "x").name == "leader-note.md"
    assert board.note_write("leader", "leader-API-contract.md", "v3") == path and path.read_text() == "v3"
    escape = board.note_write("leader", "../../etc/x", "x")
    assert escape.parent == board.notes_dir and "/" not in escape.name


def test_note_write_refuses_bad_topics_and_text_over_the_teams_limit(board):
    assert board.note_max_chars() == 20000
    board.cfg["note_max_chars"] = 50
    assert board.note_max_chars() == 50 and board.note_write("leader", "t", "x" * 50).exists()
    for topic, text in (("t", "x" * 51), ("t", ""), ("t", "  "), ("t", None), ("", "x"), ("  ", "x"), ("x" * 81, "x"), (5, "x")):
        with pytest.raises(ValueError):
            board.note_write("leader", topic, text)
    assert [p.name for p in board.notes_dir.glob("*.md")] == ["leader-t.md"]


@pytest.mark.parametrize("kind", ["questions", "perms"])
def test_requests_are_answered_once_and_wait_marks_them_expired(board, clock, kind):
    r = board.request(kind, "alpha", {"tool": "Bash"})
    assert r["id"][0] == kind[0].upper() and board.pending(kind) == [r]
    done = board.answer(kind, r["id"], "answered", answer="yes")
    assert (done["state"], done["by"], done["answer"]) == ("answered", "leader", "yes") and board.pending(kind) == []
    with pytest.raises(ValueError, match="already answered"):
        board.answer(kind, r["id"], "denied")
    for rid in ("P12345678", "nope", None):
        with pytest.raises((ValueError, TypeError)):
            board.answer(kind, rid, "answered")
    assert board.wait(kind, r["id"], 5)["state"] == "answered"
    pending = board.request(kind, "alpha", {})
    assert board.wait(kind, pending["id"], 0.3)["state"] == "expired" and clock.now >= 0.3 and board.pending(kind) == []
    with pytest.raises(ValueError, match="already expired"):
        board.answer(kind, pending["id"], "answered")
    waiting = board.request(kind, "alpha", {})
    clock.on_sleep = lambda: board.answer(kind, waiting["id"], "answered", answer="in time")
    assert board.wait(kind, waiting["id"], 5)["answer"] == "in time"


def test_decisions_are_newest_first_and_say_what_who_and_when(board, clock, monkeypatch):
    monkeypatch.setattr(policy, "time", clock)
    p1 = board.request("perms", "alpha", {"tool": "Bash", "input": {"command": "wc -c"}, "tool_use_id": None, "always": False})
    q1 = board.request("questions", "beta", {"question": "tabs?", "task": None, "answer": None, "options": []})
    p2 = board.request("perms", "alpha", {"tool": "Edit", "input": {"file_path": "src/a.py"}, "tool_use_id": None, "always": False})
    p3 = board.request("perms", "beta", {"tool": "WebFetch", "input": {"url": "https://x.example"}, "tool_use_id": None, "always": False})
    q2 = board.request("questions", "alpha", {"question": "ship?", "task": None, "answer": None, "options": []})
    assert board.decisions() == []
    board.answer("perms", p1["id"], "allowed", by="user", always=True)
    board.answer("questions", q1["id"], "answered", by="leader", answer="tabs")
    assert policy.set_allow(board, "wc -c", "user") is None
    board.answer("perms", p2["id"], "denied")
    assert board.wait("perms", p3["id"], 0.3)["state"] == "expired"
    assert board.wait("questions", q2["id"], 0.3)["state"] == "expired"
    assert policy.set_allow(board, "wc -c", "leader", on=False) is None
    rows = board.decisions()
    assert [(r["kind"], r["state"], r["by"]) for r in rows] == [
        ("allow", "removed", "leader"), ("question", "expired", None), ("perm", "expired", None), ("perm", "denied", "leader"), ("allow", "added", "user"),
        ("question", "answered", "leader"), ("perm", "allowed", "user")]
    assert all(set(r) == {"kind", "id", "member", "state", "by", "ts", "always", "widened", "what"} for r in rows)
    assert [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True) and len({r["ts"] for r in rows}) == len(rows)
    removed, late_q, late_p, denied, added, answer, allowed = rows
    assert (removed["id"], removed["member"], removed["what"], removed["always"]) == (None, None, "wc -c", False) and added["what"] == "wc -c"
    assert (late_p["id"], late_p["member"], late_p["what"]) == (p3["id"], "beta", "WebFetch https://x.example")
    assert (denied["what"], allowed["what"], allowed["always"], denied["always"]) == ("Edit src/a.py", "Bash wc -c", True, False)
    assert (answer["id"], answer["member"], answer["what"]) == (q1["id"], "beta", "tabs") and late_q["what"] is None
    assert allowed["ts"] == read_json(board.dir / "perms" / f"{p1['id']}.json")["answered"] > p1["ts"]
    assert late_p["ts"] == read_json(board.dir / "perms" / f"{p3['id']}.json")["answered"] > p3["ts"]
    assert [r["id"] for r in board.decisions(2)] == [None, q2["id"]] and board.decisions(0) == []


def test_a_tool_put_on_the_list_or_taken_off_shows_among_the_decisions_by_its_name(board, clock, monkeypatch):
    monkeypatch.setattr(policy, "time", clock)
    assert policy.set_tool(board, "mcp__srv__search", "user") is None
    assert policy.set_tool(board, "mcp__srv__search", "leader", on=False) is None
    rows = board.decisions()
    assert [(r["kind"], r["state"], r["by"], r["what"], r["id"], r["member"]) for r in rows] == [
        ("allow", "removed", "leader", "mcp__srv__search", None, None), ("allow", "added", "user", "mcp__srv__search", None, None)]
    assert all(set(r) == {"kind", "id", "member", "state", "by", "ts", "always", "widened", "what"} for r in rows)


def test_a_request_without_an_answer_time_is_dated_by_when_it_was_asked(board):
    p = board.request("perms", "alpha", {"tool": "Bash", "input": {"command": "ls"}, "tool_use_id": None, "always": False})
    path = board.dir / "perms" / f"{p['id']}.json"
    old = read_json(path)
    write_json(path, {**old, "state": "allowed", "by": "leader"})
    assert [(r["ts"], r["state"]) for r in board.decisions()] == [(p["ts"], "allowed")]


def test_the_leaders_summaries_are_kept_newest_first_and_validated(board, clock):
    assert board.summaries() == [] and board.summaries(1) == []
    version = board.version()
    first = board.add_summary("  Parser done.  ")
    second = board.add_summary("Docs next.\n\u4e2d\u6587 second line")
    assert first["text"] == "Parser done." and second["text"] == "Docs next.\n\u4e2d\u6587 second line" and second["ts"] > first["ts"]
    assert board.summaries() == [second, first] and board.summaries(1) == [second] and board.version() != version
    assert (board.dir / "run" / "summary.jsonl").read_text(encoding="utf-8").count("\n") == 2 and "\u4e2d\u6587" in (board.dir / "run" / "summary.jsonl").read_text(encoding="utf-8")
    assert board.add_summary("x" * board_module.SUMMARY_CHARS)["text"] == "x" * 4000
    for bad in ("", "   ", "x" * (board_module.SUMMARY_CHARS + 1), None, 5, ["a"]):
        with pytest.raises(ValueError, match="summary must be 1-4000 characters"):
            board.add_summary(bad)
    assert len(board.summaries()) == 3
    with open(board.dir / "run" / "summary.jsonl", "a", encoding="utf-8") as f:
        f.write("not json\n[1, 2]\n{\"text\": \"no time\"}\n")
    assert len(board.summaries()) == 3 and board.summaries(2)[0]["text"] == "x" * 4000


def test_a_task_file_that_changed_is_read_again_and_one_that_did_not_is_not(board, post_task, task_edit, monkeypatch):
    t = post_task(board, "first", ["src/a.py"])
    parsed, real = [], board._raw
    monkeypatch.setattr(board, "_raw", lambda tid: parsed.append(tid) or real(tid))
    assert [board.task(t["id"])["title"] for _ in range(3)] == ["first"] * 3 and board.tasks()[0]["title"] == "first"
    assert parsed == [t["id"]]
    task_edit(board, t["id"], status="completed", summary="done")
    assert (board.task(t["id"])["state"], board.task(t["id"])["summary"]) == ("done", "done") and parsed == [t["id"]] * 2
    path = board.tasks_dir / f"{t['id']}.json"
    stat = path.stat()
    path.write_text(path.read_text().replace('"first"', '"other"'))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert board.task(t["id"])["title"] == "other" and len(parsed) == 3
    path.unlink()
    assert board.task(t["id"]) is None and board.tasks() == []
    path.write_text("{half written")
    assert board.task(t["id"]) is None


def test_a_view_handed_out_is_a_copy_so_changing_it_never_changes_the_next_read(board, post_task, add_member):
    add_member(board, "alpha", ["src/"], "active")
    t = post_task(board, "first", ["src/a.py"])
    board.mark("alpha", [t["id"]], "pass", "not now")
    got = board.task(t["id"])
    got.update(title="changed", state="done")
    got["scope"].append("docs/")
    got["passed_by"].append("beta")
    got["reasons"]["pass:beta"] = "x"
    again = board.task(t["id"])
    assert (again["title"], again["state"], again["scope"], again["passed_by"], again["reasons"]) == ("first", "open", ["src/a.py"], ["alpha"], {"pass:alpha": "not now"})
    assert board.tasks()[0]["scope"] == ["src/a.py"]


def test_a_members_help_counts_only_while_the_owner_holds_the_task(board, add_member, post_task, finish, task_edit):
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    t = post_task(board, "big", ["src/"])
    board.claim("alpha", t["id"])
    helps = {"task": t["id"], "owner": "alpha", "paths": ["src/pkg/"], "since": 1.5}
    board.update_member("beta", helps=helps)
    assert board.helping(board.member("beta")) == helps and board.write_area("beta") == ["docs/", "src/pkg/"] and board.write_area("alpha") == ["src/"]
    assert board.helping(board.member("alpha")) is None and board.helping(None) is None and board.write_area("ghost") == []
    board.release("alpha", t["id"], "too big")
    assert board.helping(board.member("beta")) is None and board.write_area("beta") == ["docs/"]
    task_edit(board, t["id"], status="in_progress")
    raw = raw_task(board, t["id"])
    raw["owner"] = "gamma"
    write_json(board.tasks_dir / f"{t['id']}.json", raw)
    assert board.helping(board.member("beta")) is None
    raw["owner"] = "alpha"
    write_json(board.tasks_dir / f"{t['id']}.json", raw)
    assert board.helping(board.member("beta")) == helps
    finish(board, "alpha", t["id"])
    assert board.helping(board.member("beta")) is None and board.write_area("beta") == ["docs/"]
    board.update_member("beta", helps="garbage")
    assert board.helping(board.member("beta")) is None


def test_a_member_that_helps_cannot_claim_another_task_until_the_help_is_over(board, add_member, post_task):
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    big, other = post_task(board, "big", ["src/"]), post_task(board, "other", ["docs/x.md"])
    board.claim("alpha", big["id"])
    board.update_member("beta", helps={"task": big["id"], "owner": "alpha", "paths": ["src/"], "since": 1.0})
    why = f"beta is helping alpha with task {big['id']} and takes no other task until that one is done"
    assert board.why_not("beta", other["id"]) == why and board.claim("beta", other["id"]) == (None, why)
    assert board.task(other["id"])["state"] == "open" and board.member("beta").get("task") is None
    board.release("alpha", big["id"], "too big")
    assert board.why_not("beta", other["id"]) is None and board.claim("beta", other["id"])[0]["owner"] == "beta"


def test_partners_resume_each_other_by_mail_only_a_few_times_per_help(board, add_member, post_task):
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    add_member(board, "gamma", ["lib/"], "active")
    big = post_task(board, "big", ["src/"])
    board.claim("alpha", big["id"])
    board.update_member("beta", helps={"task": big["id"], "owner": "alpha", "paths": ["src/"], "since": 1.0})
    assert board.partner_wake("beta", {"alpha"}) and board.partner_wake("alpha", {"beta"})
    assert not board.partner_wake("beta", {"gamma"}) and not board.partner_wake("gamma", {"alpha", "beta"}) and not board.partner_wake("alpha", {"alpha"})
    assert board.member("beta")["helps"].get("wakes", 0) == 0
    wakes = [("beta", {"alpha"}), ("alpha", {"beta"}), ("beta", {"alpha"})]
    assert len(wakes) == board_module.PARTNER_WAKES == 3
    for spent, (name, senders) in enumerate(wakes, 1):
        with board.locked():
            assert board.partner_wake(name, senders, spend=True)
        assert board.member("beta")["helps"]["wakes"] == spent
    with board.locked():
        assert not board.partner_wake("beta", {"alpha"}, spend=True) and not board.partner_wake("alpha", {"beta"}, spend=True)
    assert not board.partner_wake("beta", {"alpha"}) and board.member("beta")["helps"]["wakes"] == board_module.PARTNER_WAKES
    board.update_member("beta", helps={"task": big["id"], "owner": "alpha", "paths": ["src/"], "since": 2.0})
    assert board.partner_wake("beta", {"alpha"})


def test_a_request_line_names_no_home_path_even_where_it_is_cut():
    home = str(Path.home())
    lead = "x" * (400 - len("Bash  ") - len(home) + 3)
    line = board_module.request_text({"tool": "Bash", "input": {"command": f"{lead} {home}/secret/file"}})
    assert home[:-3] not in line and len(line) <= 400


def _blocked_by(board, tid, deps):
    raw = read_json(board.tasks_dir / f"{tid}.json")
    raw["blockedBy"] = list(deps)
    write_json(board.tasks_dir / f"{tid}.json", raw)


def test_depth_is_the_longest_chain_waiting_on_a_task_and_how_many_wait_in_all(board, post_task, finish, add_member):
    a, b = post_task(board, "a", ["src/a.py"]), post_task(board, "b", ["src/b.py"])
    c = post_task(board, "c", ["src/c.py"], [a["id"]])
    d = post_task(board, "d", ["src/d.py"], [c["id"]])
    e, f = (post_task(board, n, [f"src/{n}.py"], [b["id"]]) for n in ("e", "f"))
    g = post_task(board, "g", ["src/g.py"], [e["id"], f["id"]])
    depth = board.depth()
    assert [depth[t["id"]] for t in (a, b, c, d, e, f, g)] == [(3, 2), (3, 3), (2, 1), (1, 0), (2, 1), (2, 1), (1, 0)]
    add_member(board, "alpha", ["src/"])
    board.claim("alpha", a["id"])
    finish(board, "alpha", a["id"])
    board.cancel(b["id"], "leader", "not needed")
    depth = board.depth()
    assert (depth[a["id"]], depth[b["id"]], depth[c["id"]], depth[e["id"]]) == ((1, 0), (1, 0), (2, 1), (1, 0))


def test_depth_cuts_a_cycle_where_it_closes(board, post_task):
    a = post_task(board, "a", ["src/a.py"])
    b = post_task(board, "b", ["src/b.py"], [a["id"]])
    _blocked_by(board, a["id"], [b["id"], a["id"]])
    assert sorted(board.depth()[t["id"]] for t in (a, b)) == [(1, 0), (2, 1)]


def test_a_board_holding_still_reads_once_per_thread_hands_out_copies_and_the_outer_one_holds(board, add_member):
    add_member(board, "alpha", ["src/"])
    seen = []
    with board.still():
        assert [m["name"] for m in board.members()] == ["alpha"]
        board.members()[0]["region"].append("lib/")
        th = threading.Thread(target=lambda: (add_member(board, "beta", ["docs/"]), seen.append([m["name"] for m in board.members()])))
        th.start()
        th.join()
        with board.still():
            assert [(m["name"], m["region"]) for m in board.members()] == [("alpha", ["src/"])]
    assert seen == [["alpha", "beta"]]
    assert [m["name"] for m in board.members()] == ["alpha", "beta"]


def ask(board, member="alpha", now=None):
    with board.locked():
        until = board.user_window(now)
        return board.request("questions", member, {"question": "Which?", "answer": None, "options": [], **({"user_until": until} if until else {})})


def test_a_question_waits_for_the_user_only_while_the_page_is_watched_two_at_a_time_and_not_too_often(board):
    from deepcrew.board import PAGE_SEEN, PAGE_SEEN_S, USER_FIRST_S, USER_GAP_S
    assert "user_until" not in ask(board)
    seen = board.dir / "run" / PAGE_SEEN
    seen.parent.mkdir(parents=True, exist_ok=True)
    seen.touch()
    now = seen.stat().st_mtime + 1

    def at(t):
        os.utime(seen, (t - 1, t - 1))
        return ask(board, now=t)
    first = at(now)
    assert first["user_until"] == now + USER_FIRST_S and board.for_leader("questions", now) == [q for q in board.pending("questions") if "user_until" not in q]
    assert "user_until" not in at(now + 1)
    second = at(now + USER_GAP_S)
    assert second["user_until"] and "user_until" not in at(now + 2 * USER_GAP_S)
    assert first["id"] in {q["id"] for q in board.for_leader("questions", now + USER_FIRST_S + 1)}
    assert "user_until" in at(now + USER_FIRST_S + 1)
    assert not board.page_watched(now + USER_FIRST_S + 1 + PAGE_SEEN_S)
