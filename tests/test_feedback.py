from __future__ import annotations

import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from conftest import PROVIDER
from deepcrew import cli, member, policy, providers, route
from deepcrew.board import read_json

DEEPSEEK = {**providers.BUILTIN["deepseek"], "name": "deepseek"}


@pytest.mark.parametrize("command, words", [
    ("grep -E '^(ERROR|FAIL|ok):' log.txt", [["grep", "-E", "^(ERROR|FAIL|ok):", "log.txt"]]),
    ('python3 -c "import json\nprint(json.load(open(\'a.json\'))[\'k\'])"', [["python3", "-c", "import json\nprint(json.load(open('a.json'))['k'])"]]),
    ("cd src\nwc -l a.py", [["cd", "src"], ["wc", "-l", "a.py"]]),
    ("python3 - <<'EOF'\nimport os\nprint(os.getcwd())\nEOF\nwc -l a.py", [["python3", "-"], ["wc", "-l", "a.py"]]),
    ("cat <<-\"END\"\n\t$(rm -rf x)\n\tEND", [["cat"]]),
    ("# look first\nls src  # then count\nwc -l a.py 2>&1 | head -3", [["ls", "src"], ["wc", "-l", "a.py"], ["head", "-3"]]),
    ("ls \\\n  src", [["ls", "src"]]),
    ('echo "a \\"quoted\\" word" a#b', [["echo", 'a "quoted" word', "a#b"]]),
])
def test_quoted_text_line_breaks_heredocs_and_comments_are_read_as_the_shell_reads_them(command, words):
    assert policy.parts(command) == words


@pytest.mark.parametrize("command", [
    'echo "$HOME"', "echo `id`", "ls *.py", "ls src &", "cat > out.txt <<'E'\nx\nE", "python3 - <<EOF\n$(id)\nEOF",
    "python3 - <<'EOF'\nprint(1)\n", "ls (src)", "echo 'open", "ls;", "ls && \n", "a\r\nb", "ls >out.txt",
])
def test_expansions_redirections_to_files_and_unfinished_syntax_still_go_to_the_leader(command):
    assert policy.parts(command) is None


def test_an_allowlisted_command_runs_however_its_arguments_are_quoted_and_time_only_times_it():
    allow = ["python3", "grep", "wc", "cd"]
    assert policy.allowlisted('python3 -c "import sys\nprint(sys.argv)"', allow, sandboxed=True)
    assert policy.allowlisted("time python3 -B check.py", allow, sandboxed=True)
    assert policy.allowlisted("cd src\ngrep -n 'def (x)' a.py | wc -l", allow, sandboxed=True)
    assert not policy.allowlisted("cd src\nrm -rf build", allow, sandboxed=True)
    assert not policy.allowlisted('python3 -c "print(1)"', allow, sandboxed=False)
    assert policy.allow_refusal('python3 -c "print(1)"', sandboxed=True)


def test_post_to_gives_the_task_to_that_member_and_offers_it_to_nobody_else(make_board, add_member):
    board = make_board()
    add_member(board, "alpha", ["src/"], "active")
    add_member(board, "beta", ["docs/"], "active")
    t = board.post("read the guide", "", [], [], "leader", to="alpha")
    assert board.task(t["id"])["owner"] == "alpha" and board.task(t["id"])["state"] == "claimed"
    assert board.member("alpha")["assigned"] == t["id"] and [x["id"] for x in member.offer(board, board.member("alpha"))] == [t["id"]]
    assert member.offer(board, board.member("beta")) == []
    with pytest.raises(ValueError, match="cannot give the task to alpha: it holds task 1"):
        board.post("another", "", [], [], "leader", to="alpha")
    with pytest.raises(ValueError, match="cannot give the task to beta: the scope .'src/a.py'. is outside its region"):
        board.post("code", "", ["src/a.py"], [], "leader", to="beta")
    assert len(board.tasks()) == 1
    t2 = board.post("read the notes", "", [], [], "leader")
    assert [x["id"] for x in member.offer(board, board.member("beta"))] == [t2["id"]]


def test_the_command_line_posts_to_a_member(lead, deepcrew):
    code, out, _ = deepcrew("post", "review", "--to", "beta", "--no-route")
    assert code == 0 and "given to beta" in out and lead.task("1")["owner"] == "beta"


def test_a_task_given_with_to_starts_its_member_even_without_routing(make_board, add_member, deepcrew, monkeypatch):
    board = make_board("main", default_location=True)
    add_member(board, "qa", ["tests/"])
    started = []
    monkeypatch.setattr(route, "run", lambda b: started.append(1) or {})
    assert deepcrew("post", "check it", "--scope", "tests/", "--to", "qa", "--no-route")[0] == 0
    assert started == [1]


def test_completing_a_task_again_says_it_is_done_and_where_the_news_goes(crew, task_edit, post_task):
    t = post_task(crew, "polish", ["src/"])
    crew.claim("alpha", t["id"])
    task_edit(crew, t["id"], status="completed", summary="first pass")
    out = member.task_rules(crew, "alpha", "TaskUpdate", {"taskId": t["id"], "status": "completed", "metadata": {"summary": "second pass"}})
    assert "already completed: send what changed since to the leader with message" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_route_says_a_ready_task_waits_for_the_turn_of_the_member_that_can_take_it(make_board, add_member, post_task):
    board = make_board()
    add_member(board, "alpha", ["src/"], "active", turn={"since": time.time() - 600, "at": time.time(), "doing": "thinking", "chars": 40000})
    t = post_task(board, "fix", ["src/a.py"])
    assert route.plan(board)["notes"] == [f"task {t['id']} goes to alpha when its current turn ends"]
    board.update_member("alpha", turn=None)
    assert route.plan(board)["notes"] == []
    line = cli.turn_text({"state": "active", "turn": {"since": time.time() - 610, "doing": "thinking", "chars": 40000}})
    assert line == "  turn=10m thinking, about 10k tokens so far"


def test_the_driver_records_a_long_turn_without_logging_its_chunks(make_board, add_member):
    board = make_board()
    add_member(board, "alpha", ["src/"], "active")
    turn = member._turn_started(board, "alpha")
    for ev in ({"type": "content_block_start", "content_block": {"type": "thinking"}},
               {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "x" * 800}}):
        member._progress(board, "alpha", turn, json.dumps({"type": "stream_event", "event": ev}))
    turn["noted"] = 0.0
    member._progress(board, "alpha", turn, json.dumps({"type": "stream_event", "event": {"type": "ping"}}))
    assert board.member("alpha")["turn"]["doing"] == "thinking" and board.member("alpha")["turn"]["chars"] == 800
    assert "--include-partial-messages" in member.argv(board, board.member("alpha"), resume=False)


def test_turn_notes_do_not_make_the_inbox_return(make_board, monkeypatch):
    board = make_board("main", default_location=True)
    monkeypatch.setattr(cli, "inbox_items", lambda b: {"note:task 3 goes to alpha when its current turn ends": ("note", "task 3 goes to alpha")})
    t0 = time.monotonic()
    assert cli.main(["inbox", "--wait", "1.2", "--project", str(board.root)]) == 0
    assert time.monotonic() - t0 >= 1.0


def test_the_budget_line_shows_the_spend_now_not_the_spend_when_the_user_set_it(make_board, deepcrew, monkeypatch):
    board = make_board("main", default_location=True)
    budget = {"usd": 2.0, "by": "user", "ts": time.time(), "spent": 0.25}
    board.update_cfg(lambda cfg: cfg.__setitem__("budget", budget))
    monkeypatch.setattr(member, "spent", lambda b: 1.5)
    assert "budget: the user set the team's budget to $2.00, $1.50 spent so far" in deepcrew("inbox")[1]


def test_the_audit_leaves_out_untracked_files_that_were_there_and_unchanged(make_board, add_member, project):
    (project / "notes.txt").write_text("mine\n", encoding="utf-8")
    (project / "scratch.txt").write_text("old\n", encoding="utf-8")
    board = make_board()
    add_member(board, "alpha", ["src/"])
    (project / "scratch.txt").write_text("changed\n", encoding="utf-8")
    os.utime(project / "scratch.txt", ns=(1, 1))
    (project / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    r = cli.audit(board)
    assert [f["path"] for f in r["files"]] == ["scratch.txt", "src/new.py"] and r["untracked_unchanged"] == 1


def test_the_audit_names_the_shell_command_that_most_likely_wrote_a_file(crew, project):
    member.hook_post(crew, "alpha", {"tool_name": "Bash", "tool_input": {"command": "python3 tools/gen.py > src/gen.js"}})
    row = json.loads((crew.dir / "edits.jsonl").read_text().splitlines()[-1])
    assert (row["member"], row["tool"], row["path"], row["command"]) == ("alpha", "Bash", "", "python3 tools/gen.py > src/gen.js")
    t = row["ts"]
    with open(crew.dir / "edits.jsonl", "a") as f:
        for ts, command in ((t + 100, "echo x > /dev/null"), (t + 200, "node build.mjs")):
            f.write(json.dumps({"ts": ts, "member": "beta", "task": None, "tool": "Bash", "path": "", "command": command}) + "\n")
    for rel, mtime in (("src/gen.js", t - 5), ("src/late.js", t + 150), ("docs/old.md", t - 2 * cli.SHELL_WINDOW_S)):
        (project / rel).write_text("x\n")
        os.utime(project / rel, (mtime, mtime))
    files = {f["path"]: f for f in cli.audit(crew)["files"]}
    assert files["src/gen.js"]["flags"] == ["shell_edit"] and files["src/gen.js"]["shell"] == {"member": "alpha", "command": "python3 tools/gen.py > src/gen.js", "others": ["beta"]}
    assert files["src/late.js"]["shell"]["command"] == "node build.mjs" and files["src/late.js"]["flags"] == ["shell_edit", "edited_by_non_owner"]
    with open(crew.dir / "edits.jsonl", "a") as f:
        f.write(json.dumps({"ts": t + 300, "member": "alpha", "task": None, "tool": "Write", "path": "src/late.js"}) + "\n")
    assert "shell" not in {f["path"]: f for f in cli.audit(crew)["files"]}["src/late.js"]
    assert files["docs/old.md"]["flags"] == ["no_logged_edit"] and "shell" not in files["docs/old.md"]


@pytest.mark.sandbox
def test_the_places_members_may_not_read_never_include_the_project_and_can_be_set(make_board, deepcrew, tmp_path, monkeypatch):
    home = tmp_path / "vol"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("DEEPCREW_SANDBOX_DENY_READ", f"{home},{tmp_path / 'secret'}")
    (tmp_path / "secret").mkdir()
    deny = cli.sandbox_settings(PROVIDER, home / "proj")["filesystem"]["denyRead"]
    assert str(tmp_path / "secret") in deny and str(home) not in deny
    monkeypatch.delenv("DEEPCREW_SANDBOX_DENY_READ")
    assert "~/.claude" in cli.DENY_READ
    assert deepcrew("config", "set", "deny_read", "~/.ssh,~/private")[0] == 0
    assert providers.setting("DEEPCREW_SANDBOX_DENY_READ") == "~/.ssh,~/private"


@pytest.mark.sandbox
def test_members_may_listen_on_local_ports_only_when_the_user_turns_it_on(monkeypatch):
    assert "allowLocalBinding" not in cli.sandbox_settings(PROVIDER)["network"]
    monkeypatch.setenv("DEEPCREW_SANDBOX_LOCAL_PORTS", "on")
    assert cli.sandbox_settings(PROVIDER)["network"]["allowLocalBinding"] is True


def test_an_empty_account_is_reported_with_what_to_do(monkeypatch):
    monkeypatch.setattr(providers, "resolve_key", lambda p: ("env:X", "sk-test"))
    monkeypatch.setattr(providers, "balance", lambda p, k: {"ok": False, "status": 200, "balances": {}, "detail": "the balance is not enough for API calls"})
    api = cli.provider_checks(DEEPSEEK, offline=False)[-1]
    assert api["level"] == "fail" and "top it up" in api["fix"]


def test_a_model_without_prices_is_flagged(monkeypatch):
    monkeypatch.setattr(providers, "resolve_key", lambda p: ("env:X", "sk-test"))
    monkeypatch.setenv("DEEPCREW_MODEL", "deepseek-flsh")
    assert any(c["id"] == "prices" and c["level"] == "warn" for c in cli.provider_checks(DEEPSEEK, offline=True))


def test_tokens_of_subagents_searches_and_summaries_are_counted(make_board, add_member):
    board = make_board()
    add_member(board, "alpha", ["src/"])
    def result(main, extra):
        return {"type": "result", "usage": {"input_tokens": main[2], "output_tokens": 0},
                "modelUsage": {"m[1m]": {"inputTokens": main[0], "outputTokens": 0}, "m": {"inputTokens": extra, "outputTokens": 0}}}
    lines = [{"type": member.PROCESS_MARK}, result((100, 0, 100), 30), result((250, 0, 150), 30),
             {"type": member.PROCESS_MARK}, result((80, 0, 80), 5)]
    (board.dir / "logs" / "alpha.ndjson").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    assert member.usage(board, "alpha")["input_tokens"] == 100 + 30 + 150 + 80 + 5


def test_thinking_off_reaches_a_provider_that_thinks_by_default(make_board, add_member):
    board = make_board(provider=DEEPSEEK)
    m = add_member(board, "alpha", ["src/"])
    board.update_cfg(lambda cfg: cfg["member"].__setitem__("thinking", False))
    assert json.loads(member.env(board, m)["CLAUDE_CODE_EXTRA_BODY"]) == {"thinking": {"type": "disabled"}}
    plain = make_board("plain")
    plain.update_cfg(lambda cfg: cfg["member"].__setitem__("thinking", False))
    assert "CLAUDE_CODE_EXTRA_BODY" not in member.env(plain, add_member(plain, "beta", ["docs/"]))


def test_a_pdf_is_not_read_by_a_member_of_a_provider_without_documents(make_board, add_member, project):
    board = make_board(provider=DEEPSEEK)
    add_member(board, "alpha", ["src/"])
    assert "Read" in member.settings(board)["hooks"]["PreToolUse"][0]["matcher"]
    pdf = member.hook_pre(board, "alpha", {"tool_name": "Read", "tool_input": {"file_path": str(project / "docs" / "paper.PDF")}})
    assert pdf["hookSpecificOutput"]["permissionDecision"] == "deny" and "pdftotext" in pdf["hookSpecificOutput"]["permissionDecisionReason"]
    assert member.hook_pre(board, "alpha", {"tool_name": "Read", "tool_input": {"file_path": str(project / "docs" / "guide.md")}}) is None
    assert "Read" not in member.settings(make_board("plain"))["hooks"]["PreToolUse"][0]["matcher"]


def test_an_api_error_tells_the_leader_what_to_do(make_board, add_member, monkeypatch):
    board = make_board()
    add_member(board, "alpha", ["src/"], "active")
    out = json.dumps({"type": "result", "api_error_status": 402, "result": "Insufficient Balance"}) + "\n"
    fake = SimpleNamespace(stdin=SimpleNamespace(write=lambda s: None, flush=lambda: None, close=lambda: None),
                           stdout=iter([out]), wait=lambda timeout=None: 0, kill=lambda: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake)
    assert member.drive(board, "alpha", False, "go") == "failed"
    note = [msg for _, msg in board.peek_mail("leader")][-1]["text"]
    assert note.startswith("alpha stopped: API error 402: the provider account has no balance: top it up")
    assert read_json(board.dir / "members" / "alpha.json")["turn"] is None


def test_webfetch_skips_anthropics_hostname_check_off_anthropic(make_board):
    assert member.settings(make_board(provider=DEEPSEEK))["skipWebFetchPreflight"] is True
    anthropic = make_board("a", provider={**PROVIDER, "base_url": "https://api.anthropic.com"})
    assert "skipWebFetchPreflight" not in member.settings(anthropic)
