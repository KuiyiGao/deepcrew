from __future__ import annotations

import base64
import functools
import http.client
import json
import os
import sys
import textwrap
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from deepcrew import browser, member

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the stand-in browser is a script started through its #! line, and a test makes a FIFO")

PNG = b"\x89PNG\r\n\x1a\n" + b"stand-in"
STAND_IN = textwrap.dedent('''\
    import http.client, json, os, sys, urllib.parse
    args = sys.argv[1:]
    url = urllib.parse.urlsplit(args[-1])
    opt = dict(a[2:].split("=", 1) for a in args[:-1] if "=" in a)
    with open(os.environ["STAND_IN_ARGS"], "w") as f:
        json.dump({"args": args, "env_tmp": os.environ.get("MAC_CHROMIUM_TMPDIR")}, f)
    for path in (url.path, "/app.js", "/missing.js", "/link/secret.txt"):
        c = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        print(f'[1:2:0101/000000.000:INFO:CONSOLE:3] "GET {path} {r.status}", source: http://127.0.0.1:{url.port}/index.html (3)', file=sys.stderr)
        r.read()
    print('[1:2:0101/000000.000:INFO:CONSOLE:0] "GL Driver Message (OpenGL, Performance): GPU stall due to ReadPixels", source: x (0)', file=sys.stderr)
    print(f'[1:2:0101/000000.000:INFO:CONSOLE:9] "Uncaught Error: boom", source: http://127.0.0.1:{url.port}/app.js (9)', file=sys.stderr)
    open(opt["screenshot"], "wb").write(PNG)
    ''').replace("PNG)", f"{PNG!r})")


@pytest.fixture
def fake_browser(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "fake-browser"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!{sys.executable}\n{STAND_IN}")
    exe.chmod(0o755)
    monkeypatch.setenv("DEEPCREW_BROWSER", str(exe))
    monkeypatch.setenv("STAND_IN_ARGS", str(tmp_path / "args.json"))
    return tmp_path / "args.json"


@pytest.fixture
def site(project, tmp_path):
    (project / "index.html").write_text("<script src=app.js></script>")
    (project / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("sk-not-for-pages")
    (project / "link").mkdir()
    (project / "link" / "secret.txt").symlink_to(tmp_path / "secret.txt")
    return project


def test_find_honours_the_setting_and_off(fake_browser, monkeypatch):
    assert browser.find() == os.environ["DEEPCREW_BROWSER"]
    monkeypatch.setenv("DEEPCREW_BROWSER", "off")
    assert browser.find() is None
    monkeypatch.setenv("DEEPCREW_BROWSER", "/no/such/browser")
    assert browser.find() is None


def test_a_shot_serves_the_project_only_and_reports_console_errors_and_missing_files(site, fake_browser):
    shot = browser.shoot(site, "index.html", "?t=2.5", (640, 360), 1500)
    assert shot["png"] == PNG and shot["url"] == "/index.html?t=2.5"
    assert shot["console"][:2] == ["GET /index.html 200  (/index.html:3)", "GET /app.js 200  (/index.html:3)"]
    assert "GET /missing.js 404  (/index.html:3)" in shot["console"] and "GET /link/secret.txt 404  (/index.html:3)" in shot["console"]
    assert shot["errors"] == ["Uncaught Error: boom  (/app.js:9)"] and not any("GPU stall" in c for c in shot["console"])
    assert shot["missing"] == ["/missing.js (404)", "/link/secret.txt (404)"]
    seen = json.loads(fake_browser.read_text())
    args = seen["args"]
    assert "--headless=new" in args and "--window-size=640,360" in args and "--virtual-time-budget=1500" in args
    port = args[-1].split(":")[2].split("/")[0]
    assert "--proxy-server=http://127.0.0.1:9" in args and f"--proxy-bypass-list=<-loopback>;127.0.0.1:{port}" in args
    assert not any(a.startswith("--no-sandbox") for a in args) and seen["env_tmp"] and not os.path.exists(seen["env_tmp"])


def test_a_failure_before_the_browser_starts_leaves_nothing_listening(site, fake_browser, monkeypatch):
    servers = []

    class Kept(browser.ThreadingHTTPServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            servers.append(self)

    def full(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(browser, "ThreadingHTTPServer", Kept)
    monkeypatch.setattr(browser.tempfile, "TemporaryDirectory", full)
    with pytest.raises(OSError, match="No space left"):
        browser.shoot(site)
    (server,) = servers
    assert server.socket.fileno() == -1


def test_closed_places_inside_the_project_are_refused(site, fake_browser):
    shot = browser.shoot(site, closed=(str(site / "app.js"),))
    assert "/app.js (404)" in shot["missing"]


@pytest.mark.parametrize(("path", "query", "words"), [("../x.html", "", "not a file of the project"), ("nope.html", "", "not a file"),
                                                      ("index.html", "t=1", r"starts with \? or #")])
def test_a_page_outside_the_project_or_a_bad_query_is_refused(site, fake_browser, path, query, words):
    with pytest.raises(ValueError, match=words):
        browser.shoot(site, path, query)


def test_without_a_browser_the_member_is_told_what_the_user_can_do(site, monkeypatch):
    monkeypatch.setenv("DEEPCREW_BROWSER", "off")
    with pytest.raises(ValueError, match="config set browser"):
        browser.shoot(site)


def vision(board, on=True):
    board.update_cfg(lambda cfg: cfg["provider"].__setitem__("vision", ["m"] if on else []))
    board.cfg = board.live_cfg()


def test_a_member_on_a_model_with_vision_gets_the_picture_and_may_save_it_in_its_region(crew, site, fake_browser):
    vision(crew)
    out = member.call(crew, "alpha", "screenshot", {"query": "?t=1", "width": 5000, "save": "src/shot.png"})
    assert base64.b64decode(out["image_png"]) == PNG and out["saved"] == "src/shot.png" and (site / "src" / "shot.png").read_bytes() == PNG
    assert "--window-size=1920,540" in json.loads(fake_browser.read_text())["args"]
    edits = [json.loads(x) for x in (crew.dir / "edits.jsonl").read_text().splitlines()]
    assert any(e["path"] == "src/shot.png" and e["member"] == "alpha" and e["tool"] == "screenshot" for e in edits)


def test_a_picture_is_never_written_outside_the_members_region(crew, site, fake_browser):
    vision(crew)
    for save in ("docs/shot.png", "src/shot.txt", "../shot.png", str(crew.artifacts_dir / "beta" / "x.png")):
        with pytest.raises(ValueError, match="save is a .png path inside your region"):
            member.call(crew, "alpha", "screenshot", {"save": save})
    assert not (site / "docs" / "shot.png").exists()
    with pytest.raises(ValueError, match="whole numbers"):
        member.call(crew, "alpha", "screenshot", {"width": "wide"})


def test_without_vision_the_picture_is_kept_in_the_members_artifacts(crew, site, fake_browser):
    vision(crew, on=False)
    out = member.call(crew, "alpha", "screenshot", {})
    assert "image_png" not in out and "does not take images" in out["note"]
    assert out["saved"].startswith(str(crew.artifacts_dir / "alpha" / "screenshot-")) and Path(out["saved"]).read_bytes() == PNG


def test_the_tool_is_listed_only_with_a_browser(crew, fake_browser, monkeypatch):
    assert [t["name"] for t in member.tools_for(crew)] == ["message", "approve", "screenshot"]
    monkeypatch.setenv("DEEPCREW_BROWSER", "off")
    assert member.tools_for(crew) == member.TOOLS


def test_the_mcp_server_returns_the_picture_as_an_image_block(crew, site, fake_browser, monkeypatch, capsys):
    vision(crew)
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "screenshot", "arguments": {}}}
    monkeypatch.setattr(sys, "stdin", iter([json.dumps(request) + "\n"]))
    member.serve(crew, "alpha")
    reply = json.loads(capsys.readouterr().out)["result"]
    assert reply["isError"] is False and reply["content"][0] == {"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}
    assert "image_png" not in json.loads(reply["content"][1]["text"])


def test_a_save_replaces_the_name_so_no_link_carries_the_picture_elsewhere(crew, site, fake_browser, tmp_path, monkeypatch):
    vision(crew)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    os.link(site / "docs" / "guide.md", site / "src" / "alias.png")
    member.call(crew, "alpha", "screenshot", {"save": "src/alias.png"})
    assert (site / "src" / "alias.png").read_bytes() == PNG and (site / "docs" / "guide.md").read_text() == "guide\n"
    (site / "src" / "link.png").symlink_to(victim)
    with pytest.raises(ValueError, match="inside your region"):
        member.call(crew, "alpha", "screenshot", {"save": "src/link.png"})
    real = browser.shoot
    def swapping(*a, **kw):
        (site / "src" / "race.png").symlink_to(victim)
        return real(*a, **kw)
    monkeypatch.setattr(browser, "shoot", swapping)
    with pytest.raises(ValueError, match="inside your region"):
        member.call(crew, "alpha", "screenshot", {"save": "src/race.png"})
    assert victim.read_text() == "keep me"
    monkeypatch.setattr(browser, "shoot", real)
    browser.write_inside(os.path.realpath(site), ["src", "race.png"], PNG)
    assert (site / "src" / "race.png").read_bytes() == PNG and victim.read_text() == "keep me"
    (site / "src" / "frames").symlink_to(tmp_path)
    with pytest.raises(OSError):
        browser.write_inside(os.path.realpath(site), ["src", "frames", "a.png"], PNG)
    assert not (tmp_path / "a.png").exists()
    out = member.call(crew, "alpha", "screenshot", {"save": str(crew.artifacts_dir / "alpha" / "frames" / "t1.png")})
    assert Path(out["saved"]).read_bytes() == PNG


def test_a_pipe_in_the_project_does_not_hold_the_server(site, fake_browser):
    os.mkfifo(site / "app.js.fifo")
    (site / "index.html").write_text("<script src=app.js.fifo></script>")
    handler_missing = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(browser._Files, root=os.path.realpath(site), closed=(), missing=handler_missing))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        c.request("GET", "/app.js.fifo")
        assert c.getresponse().status == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_mcp_server_survives_any_error_in_one_call(crew, monkeypatch, capsys):
    def boom(board, me, name, a):
        raise RuntimeError("an extension failed")
    monkeypatch.setattr(member, "call", boom)
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "arguments": {}}}) + "\n",
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"]
    monkeypatch.setattr(sys, "stdin", iter(lines))
    member.serve(crew, "alpha")
    first, second = (json.loads(x) for x in capsys.readouterr().out.splitlines())
    assert first["result"]["isError"] is True and "RuntimeError: an extension failed" in first["result"]["content"][0]["text"]
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}
