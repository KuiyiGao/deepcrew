from __future__ import annotations

import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from deepcrew import extensions, member, policy, providers
from deepcrew.board import read_json
from page_helpers import TeamPages, ask_perm, page_script

ASKS = policy.SANDBOX_NETWORK
REAL_ASK = ('{"type":"control_request","request_id":"6f8fd74c-7b1b-4aed-a89e-5762b066ea72","request":{"subtype":"can_use_tool","tool_name":"SandboxNetworkAccess",'
            '"display_name":"SandboxNetworkAccess","input":{"host":"example.invalid"},"permission_suggestions":[{"type":"addRules","rules":[{"toolName":"WebFetch",'
            '"ruleContent":"domain:example.invalid"}],"behavior":"allow","destination":"localSettings"}],"tool_use_id":"8d5754e0-f3a2-488b-bb27-428b63bf12a6",'
            '"description":"Allow network connection to example.invalid?"}}')
SITE_TEXT = "members reach {host} without asking"


def ask(rid="r1", **body):
    return {"type": "control_request", "request_id": rid, "request": body}


def sandbox_ask(host, rid="r1", call="u1"):
    return ask(rid, subtype="can_use_tool", tool_name=ASKS, input={"host": host}, tool_use_id=call)


def reply(rid, **response):
    return {"subtype": "success", "request_id": rid, "response": response}


def wait_for(pred, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if got := pred():
            return got
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def answer_all(board, allow=True, **how):
    return [policy.answer_permission(board, r["id"], allow, **how) for r in board.pending("perms")]


def perms(board) -> list[dict]:
    return sorted((read_json(p) for p in (board.dir / "perms").glob("P*.json")), key=lambda r: r["ts"])


@pytest.fixture
def patient(crew):
    crew.update_cfg(lambda cfg: cfg.__setitem__("approval_timeout_s", 30))
    return crew


class Pipe:
    def __init__(self, delay=0.0005):
        self.lines, self.inside, self.most, self.closed, self.delay, self.guard = [], 0, 0, False, delay, threading.Lock()

    def write(self, text):
        if self.closed:
            raise ValueError("I/O operation on closed file")
        with self.guard:
            self.inside += 1
            self.most = max(self.most, self.inside)
        time.sleep(self.delay)
        self.lines.append(text)
        with self.guard:
            self.inside -= 1

    def flush(self):
        pass

    def close(self):
        self.closed = True


def hammer(write, writers=8, each=20):
    start = threading.Barrier(writers)

    def run(n):
        start.wait()
        for i in range(each):
            write(f"{n}:{i}\n")
    threads = [threading.Thread(target=run, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_the_driver_and_the_threads_that_answer_write_to_claude_one_line_at_a_time():
    raw = Pipe()
    hammer(raw.write)
    assert raw.most > 1
    pipe = Pipe()
    stdin = member.Input(pipe)
    hammer(stdin.send)
    assert pipe.most == 1 and len(pipe.lines) == 160 and len(set(pipe.lines)) == 160


def test_nothing_is_written_to_claude_once_its_input_is_closed_or_claude_has_gone():
    pipe = Pipe()
    stdin = member.Input(pipe)
    assert stdin.send("one\n") is True
    stdin.close()
    assert stdin.send("two\n") is False and pipe.closed and pipe.lines == ["one\n"]
    stdin.close()

    class Gone:
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

        def close(self):
            raise BrokenPipeError(32, "Broken pipe")
    gone = member.Input(Gone())
    assert gone.send("x\n") is False
    gone.close()


def test_the_real_line_a_sandboxed_command_makes_claude_print_is_a_question_for_the_permission_rules(crew, clock):
    request = json.loads(REAL_ASK)
    clock.on_sleep = lambda: answer_all(crew)
    out = member.control_reply(crew, "alpha", request)
    assert out == reply("6f8fd74c-7b1b-4aed-a89e-5762b066ea72", behavior="allow", updatedInput={"host": "example.invalid"})
    (filed,) = perms(crew)
    assert (filed["member"], filed["tool"], filed["input"], filed["tool_use_id"]) == ("alpha", ASKS, {"host": "example.invalid"}, "8d5754e0-f3a2-488b-bb27-428b63bf12a6")


def test_a_question_about_a_site_of_the_team_is_allowed_at_once_and_nothing_is_filed(crew):
    policy.set_site(crew, "api.example.com")
    assert member.control_reply(crew, "alpha", sandbox_ask("api.example.com")) == reply("r1", behavior="allow", updatedInput={"host": "api.example.com"})
    assert perms(crew) == []


@pytest.mark.parametrize("by, allow, message", [("leader", False, "the leader denied this call"), ("user", False, "the user denied this call")])
def test_a_question_nobody_decides_goes_to_the_leader_or_the_user_and_a_no_is_a_deny_with_its_reason(crew, clock, by, allow, message):
    clock.on_sleep = lambda: answer_all(crew, allow, by=by)
    assert member.control_reply(crew, "alpha", sandbox_ask("example.org")) == reply("r1", behavior="deny", message=message)


def test_a_question_nobody_answers_is_denied_after_approval_timeout_s(crew, clock):
    assert crew.live_cfg()["approval_timeout_s"] == 0.3
    out = member.control_reply(crew, "alpha", sandbox_ask("example.org"))
    assert out == reply("r1", behavior="deny", message="no answer from the leader within 0.3 s; denied") and perms(crew)[0]["state"] == "expired"


def test_the_answer_of_a_user_who_allows_also_adds_the_site_so_the_next_question_is_allowed_at_once(crew, clock):
    clock.on_sleep = lambda: answer_all(crew, by="user")
    member.control_reply(crew, "alpha", sandbox_ask("api.example.com"))
    assert crew.sites() == ["api.example.com"] and perms(crew)[0]["widened"]["kind"] == "site"
    clock.on_sleep = lambda: pytest.fail("a site of the team is no question")
    assert member.control_reply(crew, "alpha", sandbox_ask("api.example.com", "r2", "u2")) == reply("r2", behavior="allow", updatedInput={"host": "api.example.com"})


def test_an_mcp_server_that_asks_for_input_is_declined_at_once(crew):
    out = member.control_reply(crew, "alpha", ask("e1", subtype="elicitation", mcp_server_name="asking", message="Which colour?", mode="form",
                                                  requested_schema={"type": "object", "properties": {"colour": {"type": "string"}}}))
    assert out == reply("e1", action="decline") and perms(crew) == []


@pytest.mark.parametrize("subtype", ["hook_callback", "mcp_message", "initialize", "interrupt", "rewind_files", None])
def test_any_other_request_gets_an_error_so_nothing_waits_for_an_answer_that_never_comes(crew, subtype):
    request = ask("x1", **({"subtype": subtype} if subtype else {}))
    assert member.control_reply(crew, "alpha", request) == {"subtype": "error", "request_id": "x1", "error": f"{providers.NAME} does not answer {subtype} requests"}
    assert member.control_reply(crew, "alpha", {"type": "control_request", "request_id": "x2", "request": "not a dict"})["subtype"] == "error"
    assert perms(crew) == []


@pytest.mark.parametrize("body", [{"input": {"host": "example.org"}}, {"tool_name": ASKS}, {"tool_name": ASKS, "input": "example.org"}, {"tool_name": 5, "input": {}}])
def test_a_permission_question_without_a_tool_and_an_input_is_an_error(crew, body):
    out = member.control_reply(crew, "alpha", ask("x1", subtype="can_use_tool", **body))
    assert out == {"subtype": "error", "request_id": "x1", "error": "a can_use_tool request names a tool and its input"} and perms(crew) == []


def test_a_permission_question_about_another_tool_is_decided_as_the_approve_tool_would_and_an_odd_tool_use_id_is_none(crew, clock):
    allowed = {"command": "wc -l README.md"}
    assert member.control_reply(crew, "alpha", ask("c1", subtype="can_use_tool", tool_name="Bash", input=allowed, tool_use_id=7)) == reply(
        "c1", behavior="allow", updatedInput=allowed)
    clock.on_sleep = lambda: answer_all(crew)
    member.control_reply(crew, "alpha", ask("c2", subtype="can_use_tool", tool_name="Bash", input={"command": "git status"}, tool_use_id=7))
    assert perms(crew)[0]["tool_use_id"] is None


def test_each_request_is_answered_in_a_thread_of_its_own_and_an_answer_may_come_later_than_the_next_request(patient):
    crew = patient
    pipe = Pipe(delay=0)
    controls = member.Controls(crew, "alpha", member.Input(pipe))
    first, second = controls.start(sandbox_ask("one.example.org", "r1", "u1")), controls.start(sandbox_ask("two.example.org", "r2", "u2"))
    wait_for(lambda: len(crew.pending("perms")) == 2, "both questions filed")
    by_host = {r["input"]["host"]: r for r in crew.pending("perms")}
    policy.answer_permission(crew, by_host["two.example.org"]["id"], False)
    second.join(10)
    assert [json.loads(line)["response"]["request_id"] for line in pipe.lines] == ["r2"] and first.is_alive()
    policy.answer_permission(crew, by_host["one.example.org"]["id"], True)
    first.join(10)
    lines = [json.loads(line) for line in pipe.lines]
    assert [x["response"]["request_id"] for x in lines] == ["r2", "r1"] and all(x["type"] == "control_response" for x in lines)
    assert lines[0]["response"]["response"]["behavior"] == "deny" and lines[1]["response"]["response"]["behavior"] == "allow"
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in pipe.lines)


def test_an_answer_that_comes_after_claudes_input_is_closed_writes_nothing(patient):
    crew = patient
    pipe = Pipe(delay=0)
    stdin = member.Input(pipe)
    thread = member.Controls(crew, "alpha", stdin).start(sandbox_ask("example.org"))
    wait_for(lambda: crew.pending("perms"), "the question filed")
    stdin.close()
    answer_all(crew)
    thread.join(10)
    assert not thread.is_alive() and pipe.lines == [] and pipe.closed


def test_a_request_that_cannot_be_answered_is_left_alone_and_one_for_which_no_thread_starts_gets_an_error(crew, monkeypatch):
    pipe = Pipe(delay=0)
    controls = member.Controls(crew, "alpha", member.Input(pipe))
    assert controls.start({"type": "control_request", "request": {"subtype": "elicitation"}}) is None and pipe.lines == []

    def no_thread(self):
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(threading.Thread, "start", no_thread)
    assert controls.start(sandbox_ask("example.org", "r9")) is None
    assert json.loads(pipe.lines[0]) == {"type": "control_response", "response": {"subtype": "error", "request_id": "r9", "error": f"{providers.NAME} cannot answer just now"}}
    assert crew.pending("perms") == []


def test_an_answer_that_fails_is_an_error_answer_never_a_request_left_waiting(crew, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("team.json is gone")
    monkeypatch.setattr(policy, "approve", boom)
    pipe = Pipe(delay=0)
    controls = member.Controls(crew, "alpha", member.Input(pipe))
    controls.start(sandbox_ask("example.org")).join(10)
    assert json.loads(pipe.lines[0])["response"] == {"subtype": "error", "request_id": "r1", "error": f"{providers.NAME} could not answer: RuntimeError: team.json is gone"}


def test_when_claude_has_ended_the_requests_it_left_unanswered_are_expired_and_no_one_elses(patient, add_member):
    crew = patient
    add_member(crew, "gamma", ["lib/"], "active")
    pipe = Pipe(delay=0)
    controls = member.Controls(crew, "alpha", member.Input(pipe))
    thread = controls.start(sandbox_ask("example.org", "r1", "u1"))
    other = crew.request("perms", "alpha", {"tool": "Bash", "input": {"command": "make"}, "tool_use_id": "from-the-mcp-server", "always": False})
    question = crew.request("questions", "alpha", {"question": "which file?", "options": []})
    theirs = crew.request("perms", "gamma", {"tool": ASKS, "input": {"host": "example.org"}, "tool_use_id": "u1", "always": False})
    wait_for(lambda: len(crew.pending("perms")) == 3, "the questions filed")
    controls.expire()
    assert {r["id"] for r in crew.pending("perms")} == {theirs["id"]} and crew.pending("questions") == []
    assert {read_json(crew.dir / k / f"{r['id']}.json")["state"] for k, r in (("perms", other), ("questions", question))} == {"expired"}
    thread.join(10)
    assert [r["state"] for r in perms(crew) if r["member"] == "alpha" and r["tool"] == ASKS] == ["expired"]


def line(obj) -> str:
    return json.dumps(obj) + "\n"


@pytest.fixture
def claude(monkeypatch):
    fake = SimpleNamespace(script=None, sent=[], closed=False, status=0)

    class Stdin:
        def write(self, text):
            fake.sent.append(text)

        def flush(self):
            pass

        def close(self):
            fake.closed = True

    def popen(args, **kw):
        return SimpleNamespace(pid=1, stdin=Stdin(), stdout=iter(fake.script()), wait=lambda timeout=None: fake.status, kill=lambda: None)

    monkeypatch.setattr(member, "subprocess", SimpleNamespace(Popen=popen, PIPE=-1, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(member, "_wait_for_work", lambda board, me: (None, []))
    return fake


RESULT = {"type": "result", "subtype": "success", "is_error": False, "usage": {}}


def responses(fake) -> list[dict]:
    return [json.loads(s)["response"] for s in fake.sent if '"control_response"' in s]


def test_the_driver_answers_a_question_while_it_reads_on_and_the_command_goes_on_when_it_is_answered(patient, claude):
    crew = patient
    read_on = {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 1}}}

    def script():
        yield REAL_ASK + "\n"
        yield line(read_on)
        wait_for(lambda: crew.pending("perms"), "the question filed")
        assert str(read_on["message"]["id"]) in (crew.dir / "logs" / "alpha.ndjson").read_text()
        assert responses(claude) == []
        answer_all(crew, by="user")
        wait_for(lambda: responses(claude), "the answer written to claude")
        yield line(RESULT)

    claude.script = script
    assert member.drive(crew, "alpha", False, "go") == "idle"
    (answer,) = responses(claude)
    assert answer == reply("6f8fd74c-7b1b-4aed-a89e-5762b066ea72", behavior="allow", updatedInput={"host": "example.invalid"})
    assert json.loads(claude.sent[0])["type"] == "user" and claude.closed
    assert REAL_ASK in (crew.dir / "logs" / "alpha.ndjson").read_text()
    assert crew.sites() == ["example.invalid"]


def test_the_driver_declines_an_elicitation_and_refuses_what_it_does_not_answer(crew, claude):
    def script():
        yield line(ask("e1", subtype="elicitation", mcp_server_name="asking", message="Which colour?"))
        yield line(ask("h1", subtype="hook_callback", callback_id="cb1", input={}))
        wait_for(lambda: len(responses(claude)) == 2, "both answers written")
        yield line(RESULT)

    claude.script = script
    assert member.drive(crew, "alpha", False, "go") == "idle"
    assert sorted(responses(claude), key=lambda x: x["request_id"]) == [reply("e1", action="decline"),
                                                                       {"subtype": "error", "request_id": "h1", "error": f"{providers.NAME} does not answer hook_callback requests"}]


def test_a_question_still_waiting_when_the_member_goes_idle_is_expired_and_its_late_answer_writes_nothing(patient, claude):
    crew = patient
    def script():
        yield line(sandbox_ask("example.org"))
        wait_for(lambda: crew.pending("perms"), "the question filed")
        yield line(RESULT)

    claude.script = script
    assert member.drive(crew, "alpha", False, "go") == "idle"
    (filed,) = perms(crew)
    assert filed["state"] == "expired" and crew.pending("perms") == []
    time.sleep(0.5)
    assert responses(claude) == [] and claude.closed
    with pytest.raises(ValueError, match="already expired"):
        policy.answer_permission(crew, filed["id"], True)


def test_a_claude_that_is_gone_before_it_reads_its_first_message_fails_the_member_with_the_usual_notice(patient, claude, monkeypatch):
    def gone_stdin(args, **kw):
        class Gone:
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                pass

            def close(self):
                raise BrokenPipeError(32, "Broken pipe")
        return SimpleNamespace(pid=1, stdin=Gone(), stdout=iter([]), wait=lambda timeout=None: 1, kill=lambda: None)

    monkeypatch.setattr(member, "subprocess", SimpleNamespace(Popen=gone_stdin, PIPE=-1, TimeoutExpired=subprocess.TimeoutExpired))
    assert member.drive(patient, "alpha", False, "go") == "failed"
    (notice,) = [m["text"] for _, m in patient.peek_mail("leader") if m["kind"] == "notice"]
    assert notice.startswith("alpha stopped: Claude Code ended before its turn's result (exit status 1)")


def test_a_site_added_while_a_member_runs_lets_its_next_connection_through_and_a_name_that_is_no_host_never_matches(crew):
    def ask_for(host):
        return policy.decide(crew, "alpha", ASKS, {"host": host})
    assert ask_for("api.example.com") is None
    policy.set_site(crew, "api.example.com")
    assert ask_for("api.example.com") == {"behavior": "allow", "updatedInput": {"host": "api.example.com"}}
    assert ask_for("API.Example.com.") == {"behavior": "allow", "updatedInput": {"host": "API.Example.com."}}
    assert ask_for("sub.api.example.com") is None and ask_for("example.com") is None and ask_for("evilapi.example.com") is None
    policy.set_site(crew, "*.example.org")
    assert ask_for("a.example.org") and ask_for("a.b.example.org") and ask_for("example.org") is None
    assert ask_for("*.example.org") is None
    policy.set_site(crew, "api.example.com", on=False)
    assert ask_for("api.example.com") is None
    for odd in ("localhost", "::1", "127.0.0.1", "", None, 5, ["api.example.com"], {"a": 1}):
        assert ask_for(odd) is None, odd
    assert policy.decide(crew, "alpha", ASKS, {}) is None and policy.decide(crew, "alpha", "Bash", {"host": "a.example.org"}) is None


def question(host) -> dict:
    return {"member": "alpha", "tool": ASKS, "input": {"host": host}}


def test_always_for_a_sandbox_question_adds_the_host_as_a_site_with_the_text_of_a_fetch_and_refuses_what_a_fetch_refuses(crew):
    fetch = {"member": "alpha", "tool": "WebFetch", "input": {"url": "https://api.example.com/v1/items", "prompt": "list"}}
    want = {"kind": "site", "value": "api.example.com", "text": SITE_TEXT.format(host="api.example.com")}
    assert policy.widening(crew, question("api.example.com")) == want == policy.widening(crew, fetch)
    assert policy.widening(crew, question("API.Example.COM.")) == want
    assert policy.widening_refusal(crew, question("api.example.com")) == ""
    refused = {"10.0.0.5": "10.0.0.5 is an address, not a domain name, so it is approved call by call",
               "localhost": "'localhost' is not a host name such as api.example.com, so it is approved call by call",
               "::1": "'::1' is not a host name such as api.example.com, so it is approved call by call",
               "*.example.com": "'*.example.com' is not a host name such as api.example.com, so it is approved call by call",
               "my_host.example.com": "'my_host.example.com' is not a host name such as api.example.com, so it is approved call by call",
               None: "None is not a host name such as api.example.com, so it is approved call by call"}
    for host, why in refused.items():
        assert policy.widening(crew, question(host)) is None and policy.widening_refusal(crew, question(host)) == why, host
    assert policy.widening(crew, {"member": "alpha", "tool": ASKS, "input": "host"}) is None
    assert policy.widening(crew, {"member": "alpha", "tool": ASKS}) is None
    policy.set_site(crew, "api.example.com")
    assert policy.widening(crew, question("api.example.com")) is None
    assert policy.widening_refusal(crew, question("api.example.com")) == "api.example.com is among the team's sites already"


def test_widen_puts_the_site_of_a_sandbox_question_in_force_and_the_users_allow_does_it_by_itself(crew):
    w = policy.widening(crew, question("api.example.com"))
    assert policy.widen(crew, w, "leader") is True and crew.sites() == ["api.example.com"]
    assert policy.decide(crew, "alpha", ASKS, {"host": "api.example.com"})["behavior"] == "allow"
    ids = {host: crew.request("perms", "alpha", {**question(host), "tool_use_id": f"u-{host}", "always": False})["id"]
           for host in ("deny.example.com", "leader.example.com", "always.example.com", "user.example.com")}
    assert policy.answer_permission(crew, ids["deny.example.com"], False, by="user")["widened"] is None
    assert policy.answer_permission(crew, ids["leader.example.com"], True)["widened"] is None
    r = policy.answer_permission(crew, ids["always.example.com"], True, always=True)
    assert r["always"] is True and r["widened"]["value"] == "always.example.com"
    r = policy.answer_permission(crew, ids["user.example.com"], True, by="user")
    assert r["always"] is True and r["widened"]["value"] == "user.example.com"
    assert crew.sites() == ["api.example.com", "always.example.com", "user.example.com"]


def test_the_inbox_line_of_a_sandbox_question_prints_the_tool_and_the_input_and_offers_always(lead, deepcrew):
    rid = ask_perm(lead, tool=ASKS, tool_input={"host": "api.example.com"})
    code, out, _ = deepcrew("inbox")
    assert code == 0
    assert (f'permission {rid} from alpha: {ASKS} {{"host": "api.example.com"}}\n  answer: deepcrew approve {rid} [--deny] '
            f"[--always: {SITE_TEXT.format(host='api.example.com')}]") in out
    rid = ask_perm(lead, tool=ASKS, tool_input={"host": "localhost"})
    assert f'permission {rid} from alpha: {ASKS} {{"host": "localhost"}}\n  answer: deepcrew approve {rid} [--deny]\n' in deepcrew("inbox")[1] + "\n"


@pytest.fixture
def team_page(lead):
    pages = TeamPages(lead)
    yield pages
    pages.close()


def first_look(page):
    state, team, requests = (page.call("GET", path)[2] for path in ("/api/state", "/api/team", "/api/requests"))
    return state, {"GET /api/team": team, "GET /api/requests": requests}


def test_the_page_shows_a_sandbox_question_as_connect_to_the_host_with_what_allow_adds(team_page, lead):
    ask_perm(lead, tool=ASKS, tool_input={"host": "api.example.com"})
    ask_perm(lead, tool=ASKS, tool_input={"host": "localhost"}, member="beta")
    page = team_page()
    state, replies = first_look(page)
    assert [(r["tool"], r["input"], r["widening"] and r["widening"]["value"]) for r in replies["GET /api/requests"]["perms"]] == [
        (ASKS, {"host": "api.example.com"}, "api.example.com"), (ASKS, {"host": "localhost"}, None)]
    rows = ('return [...$("#waiting-list").children].map((row) => [row.querySelector(".rq-who").children.map((kid) => kid.textContent),'
            ' row.querySelector(".rq-what").textContent, row.querySelector(".rq-what").getAttribute("title"), row.querySelector(".rq-also").textContent]);')
    out = page_script([rows], state, replies)
    assert out["results"] == [[
        [["alpha", ASKS], "connect to api.example.com", "connect to api.example.com", "Allow also: " + SITE_TEXT.format(host="api.example.com") + "."],
        [["beta", ASKS], "connect to localhost", "connect to localhost", ""]]]


def test_the_users_allow_on_the_page_answers_a_sandbox_question_and_adds_the_site_but_localhost_is_asked_every_time(team_page, lead):
    page = team_page()
    named, local = ask_perm(lead, tool=ASKS, tool_input={"host": "api.example.com"}), ask_perm(lead, tool=ASKS, tool_input={"host": "localhost"})
    status, _, reply = page.call("POST", "/api/answer", {"id": named, "allow": True})
    assert status == 200 and (reply["answered"]["state"], reply["answered"]["by"], reply["answered"]["always"]) == ("allowed", "user", True)
    assert reply["answered"]["widened"]["kind"] == "site" and lead.sites() == ["api.example.com"]
    assert [r["input"]["host"] for r in reply["requests"]["perms"]] == ["localhost"]
    reply = page.call("POST", "/api/answer", {"id": local, "allow": True})[2]
    assert (reply["answered"]["state"], reply["answered"]["always"], reply["answered"]["widened"]) == ("allowed", False, None) and lead.sites() == ["api.example.com"]


def test_the_page_reads_what_a_request_wants_by_the_tool_and_leaves_every_other_line_as_it_was(team_page, lead):
    state, replies = first_look(team_page())
    steps = [f"return wants({json.dumps(tool)}, {json.dumps(tool_input)});" for tool, tool_input in [
        (ASKS, {"host": "api.example.com"}), (ASKS, {"host": 5}), (ASKS, {}), (ASKS, None), ("Bash", {"command": "ls -l", "host": "x.example.com"}),
        ("Edit", {"file_path": "docs/a.md"}), ("NotebookEdit", {"notebook_path": "n.ipynb"}), ("WebFetch", {"url": "https://x.example.com/", "host": "ignored.example.com"}),
        ("mcp__x__y", {"host": "not.a.sandbox.question"})]]
    assert page_script(steps, state, replies)["results"] == [
        "connect to api.example.com", '{"host":5}', "{}", "{}", "ls -l", "docs/a.md", "n.ipynb", '{"url":"https://x.example.com/","host":"ignored.example.com"}',
        '{"host":"not.a.sandbox.question"}']


def test_asks_about_one_tool_call_that_come_at_once_file_one_request_and_share_its_answer(crew, monkeypatch):
    slow = crew.request
    monkeypatch.setattr(type(crew), "request", lambda self, kind, who, payload: (time.sleep(0.3), slow(kind, who, payload))[1])
    crew.update_cfg(lambda cfg: cfg.__setitem__("approval_timeout_s", 10))
    go, got = threading.Barrier(2), []

    def ask_twice(call):
        go.wait()
        got.append(policy.approve(crew, "alpha", "Bash", {"command": "make test"}, call))

    threads = [threading.Thread(target=ask_twice, args=("toolu_1",)) for _ in range(2)]
    for t in threads:
        t.start()
    wait_for(lambda: crew.pending("perms"), "the request filed")
    time.sleep(0.5)
    assert len(perms(crew)) == 1
    policy.answer_permission(crew, crew.pending("perms")[0]["id"], False)
    for t in threads:
        t.join(10)
    assert len(perms(crew)) == 1 and got == [{"behavior": "deny", "message": "the leader denied this call"}] * 2
    assert policy._ASKS == {}


def test_asks_about_different_tool_calls_do_not_wait_for_each_other(crew):
    crew.update_cfg(lambda cfg: cfg.__setitem__("approval_timeout_s", 10))
    got = {}
    threads = [threading.Thread(target=lambda c=c: got.__setitem__(c, policy.approve(crew, "alpha", "Bash", {"command": f"make {c}"}, c))) for c in ("toolu_1", "toolu_2")]
    for t in threads:
        t.start()
    wait_for(lambda: len(crew.pending("perms")) == 2, "a request for each call")
    answer_all(crew)
    for t in threads:
        t.join(10)
    assert sorted(got) == ["toolu_1", "toolu_2"] and all(v["behavior"] == "allow" for v in got.values()) and policy._ASKS == {}


def test_two_first_loads_of_a_project_file_at_once_run_it_once(project, tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, "_loaded", {})
    monkeypatch.setattr(extensions, "_failures", {})
    monkeypatch.setattr(extensions, "_said", set())
    (project / "ctl_tools").mkdir()
    ran = tmp_path / "ran.txt"
    (project / "ctl_tools" / "slow_ext.py").write_text(f"import time\n\nwith open({str(ran)!r}, 'a') as f:\n    f.write('x')\ntime.sleep(0.4)\n\n\n"
                                                   "def playbook(board):\n    return 'slow'\n", encoding="utf-8")
    extensions.declare(project, "ctl_tools.slow_ext")
    ran.unlink()
    go, got = threading.Barrier(2), []

    def load():
        go.wait()
        got.append(extensions.loaded(project))

    threads = [threading.Thread(target=load) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert ran.read_text() == "x"
    assert len(got) == 2 and got[0] is got[1] and [spec for spec, _ in got[0]] == ["ctl_tools.slow_ext"]
