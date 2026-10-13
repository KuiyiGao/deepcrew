from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path, PureWindowsPath

import pytest

from deepcrew import prompts, providers

KEY = "sk-" + "a" * 32


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def tilde(*parts) -> str:
    return re.escape("/".join(["~", *parts]))


def table(name, **overrides) -> str:
    values = {"base_url": "http://127.0.0.1:9", "models": ["m"], "efforts": [], "fast_model": "m", "auto_compact_window": 100000, "timeout_ms": 1000,
              "key_sources": ["env:FAKE_KEY"], "key_pattern": ".+", **overrides}
    return f"[{name}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items())


def test_split_list_keeps_the_stripped_non_empty_items():
    assert providers.split_list(" a, ,b ,,c d ") == ["a", "b", "c d"] and providers.split_list("") == [] and providers.split_list(None) == []


def test_write_private_creates_a_0600_file_with_lf_line_ends(tmp_path):
    path = tmp_path / "secret"
    providers.write_private(path, "a\nb\n")
    assert path.read_bytes() == b"a\nb\n" and mode(path) == 0o600
    providers.write_private(path, "c")
    assert path.read_bytes() == b"c"


def test_load_merges_providers_toml_and_rejects_plain_http_except_localhost(providers_toml):
    providers_toml("[deepseek]\ntimeout_ms = 5\n" + table("fake") + table("local", base_url="http://localhost:8080/x")
                   + table("remote", base_url="http://example.com") + '[half]\nbase_url = "https://x.example"\n')
    merged = providers.load("deepseek")
    assert merged["timeout_ms"] == 5 and merged["models"] == providers.BUILTIN["deepseek"]["models"] and providers.BUILTIN["deepseek"]["timeout_ms"] == 600000
    assert providers.load("fake")["base_url"] == "http://127.0.0.1:9" and providers.load("local")["base_url"] == "http://localhost:8080/x"
    assert providers.known() == ["deepseek", "fake", "half", "local", "remote"]
    with pytest.raises(providers.ProviderError, match="must be https"):
        providers.load("remote")
    with pytest.raises(providers.ProviderError, match="lacks models"):
        providers.load("half")
    with pytest.raises(providers.ProviderError, match="unknown provider 'nope'; known: deepseek, fake"):
        providers.load("nope")


def test_a_providers_toml_that_is_not_toml_is_named_with_the_home_directory_as_a_tilde(providers_toml):
    providers_toml("[fake\nbase_url = 1\n")
    for call in (lambda: providers.load("deepseek"), providers.known):
        with pytest.raises(providers.ProviderError, match="^" + tilde(".config", "deepcrew", "providers.toml") + " is not valid TOML: "):
            call()
    providers_toml("\ufeff" + table("fake"))
    assert providers.known() == ["deepseek", "fake"]
    with pytest.raises(providers.ProviderError, match=r"\(add one in " + tilde(".config", "deepcrew", "providers.toml") + r"\)$"):
        providers.load("nope")
    with pytest.raises(providers.ProviderError, match="^cannot store 'lower' in " + tilde(".config", "deepcrew", ".env") + "$"):
        providers.env_file_set({"lower": "x"})


def test_env_file_keeps_every_other_line_and_stays_private():
    path = providers.env_file_set({"DEEPCREW_MODEL": "m"})
    assert path == providers.ENV_FILE and path.read_text().splitlines()[1:] == ["DEEPCREW_MODEL=m"]
    assert mode(path) == 0o600 and mode(path.parent) == 0o700
    path.write_text("# mine\nA=1\n\nDEEPCREW_MODEL=old\nZ='quoted'\n")
    path.chmod(0o644)
    assert providers.env_file_set({"DEEPCREW_MODEL": "new", "B": "2"}) == path
    assert path.read_text() == "# mine\nA=1\n\nDEEPCREW_MODEL=new\nZ='quoted'\nB=2\n" and mode(path) == 0o600 and [p.name for p in path.parent.iterdir()] == [".env"]
    assert [providers.env_file_get(v) for v in ("A", "Z", "B", "NOPE")] == ["1", "quoted", "2", None]
    for var, value in (("lower", "x"), ("A-B", "x"), ("A", "x\ny")):
        with pytest.raises(providers.ProviderError):
            providers.env_file_set({var: value, "OK": "fine"})
    assert path.read_text() == "# mine\nA=1\n\nDEEPCREW_MODEL=new\nZ='quoted'\nB=2\n"


def test_choices_prefer_the_environment_and_an_empty_variable_counts_as_unset(monkeypatch):
    assert providers.choices() == {"provider": None, "model": None, "effort": None, "thinking": None}
    providers.env_file_set({"DEEPCREW_PROVIDER": "deepseek", "DEEPCREW_MODEL": "from-file", "DEEPCREW_EFFORT": "low"})
    monkeypatch.setenv("DEEPCREW_MODEL", "from-env")
    monkeypatch.setenv("DEEPCREW_EFFORT", "")
    assert providers.choices() == {"provider": "deepseek", "model": "from-env", "effort": "low", "thinking": None}


def test_resolve_key_takes_the_first_source_that_has_a_key(tmp_path, monkeypatch):
    keyfile = tmp_path / "key"
    p = {"key_sources": ["env:FAKE_KEY", "keychain:svc", "dotenv:FAKE_KEY", f"file:{keyfile}", "bogus:x"]}
    assert providers.resolve_key(p) is None and providers.key_source(p) is None
    keyfile.write_text("  from-file \n")
    assert providers.resolve_key(p) == (f"file:{keyfile}", "from-file")
    providers.env_file_set({"FAKE_KEY": "from-dotenv"})
    assert providers.resolve_key(p) == ("dotenv:FAKE_KEY", "from-dotenv")
    monkeypatch.setattr(providers, "_keychain_get", lambda service: "from-keychain" if service == "svc" else None)
    assert providers.key_source(p) == "keychain:svc"
    monkeypatch.setenv("FAKE_KEY", "from-env")
    assert providers.resolve_key(p) == ("env:FAKE_KEY", "from-env")
    monkeypatch.setenv("FAKE_KEY", "")
    assert providers.key_source(p) == "keychain:svc"


def test_a_key_is_stored_in_the_private_env_file_only_when_it_fits_the_provider():
    p = providers.load("deepseek")
    with pytest.raises(providers.ProviderError, match="does not look like a key"):
        providers.store_env_file(p, "sk-short")
    assert not providers.ENV_FILE.exists()
    assert providers.store_env_file(p, KEY) == "dotenv:DEEPSEEK_API_KEY" and providers.env_file_get("DEEPSEEK_API_KEY") == KEY and mode(providers.ENV_FILE) == 0o600
    providers.env_template(p)
    assert providers.env_file_get("DEEPSEEK_API_KEY") == KEY


def test_secret_locations_cover_the_config_dir_in_every_spelling_and_the_key_sources():
    places = providers.secret_locations({"key_sources": ["env:FAKE_KEY", "keychain:svc-name", "file:/k/key", "dotenv:FAKE_KEY"]})
    assert {str(providers.CONFIG_DIR), "~/.config/deepcrew", "$HOME/.config/deepcrew", "${HOME}/.config/deepcrew", "svc-name", "/k/key", "find-generic-password"} <= set(places)
    assert "FAKE_KEY" not in places and "" not in places and places == sorted(set(places))


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("outcome, expected", [
    (b'{"is_available": true, "balance_infos": [{"currency": "CNY", "total_balance": "12.30"}]}', {"ok": True, "status": 200, "balances": {"CNY": "12.30"}}),
    (b'{"is_available": false, "balance_infos": []}', {"ok": False, "status": 200, "balances": {}, "detail": "the balance is not enough for API calls"}),
    (urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None), {"ok": False, "status": 401, "detail": "key rejected"}),
    (urllib.error.HTTPError("https://x", 402, "Payment Required", {}, None), {"ok": False, "status": 402, "detail": "HTTP 402"}),
    (urllib.error.URLError("refused"), {"ok": False, "status": None, "detail": "unreachable: refused"}),
    (TimeoutError("timed out"), {"ok": False, "status": None, "detail": "unreachable: timed out"}),
    (b"<html>please sign in to the wifi</html>", {"ok": False, "status": 200, "detail": "the balance endpoint did not return a JSON object"}),
])
def test_balance_maps_every_outcome_and_sends_the_key_only_to_the_endpoint(monkeypatch, outcome, expected):
    seen = []

    def urlopen(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization"), timeout))
        if isinstance(outcome, BaseException):
            raise outcome
        return Response(outcome)

    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
    result = providers.balance({"balance_url": "https://api.example.com/user/balance"}, KEY)
    assert result == expected and KEY not in json.dumps(result) and seen == [("https://api.example.com/user/balance", f"Bearer {KEY}", 10.0)]


def test_balance_without_an_endpoint_or_with_a_plain_http_one_asks_nobody():
    assert providers.balance({}, KEY)["ok"] is True
    assert providers.balance({"balance_url": "http://api.example.com/b"}, KEY) == {"ok": False, "status": None, "detail": "balance_url must be https"}


IDENTITY = """
import json, sys
from deepcrew import board, cli, member, providers
from pathlib import Path, PureWindowsPath
project = Path(sys.argv[1])
print(json.dumps({"name": providers.NAME, "cmd": cli.CMD, "config": str(providers.CONFIG_DIR), "env_file": str(providers.ENV_FILE), "state": str(board.state_home()),
                  "shared": str(board.default_shared_dir(project, "main")), "install": member.install_env(), "prog": cli.parser().prog}))
"""


def identity(tmp_path, home, **env):
    root = Path(__file__).resolve().parent.parent
    base = {"PATH": os.environ["PATH"], "HOME": str(home), "USERPROFILE": str(home), "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-B", "-c", IDENTITY, str(tmp_path / "proj")], capture_output=True, text=True, env={**base, **env}, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_process_named_deepcrew_alt_uses_its_own_settings_state_and_team_folders(tmp_path):
    home = tmp_path / "home"
    default = identity(tmp_path, home)
    alt = identity(tmp_path, home, DEEPCREW_NAME="deepcrew-alt")
    assert (default["name"], default["cmd"], default["prog"]) == ("deepcrew", "deepcrew", "deepcrew")
    assert (alt["name"], alt["cmd"], alt["prog"]) == ("deepcrew-alt", "deepcrew-alt", "deepcrew-alt")
    assert default["config"] == str(home / ".config" / "deepcrew") and alt["config"] == str(home / ".config" / "deepcrew-alt")
    assert alt["env_file"] == str(home / ".config" / "deepcrew-alt" / ".env")
    assert default["state"] == str(home / ".deepcrew") and alt["state"] == str(home / ".deepcrew-alt")
    assert default["shared"] == str(tmp_path / "proj-deepcrew" / "main") and alt["shared"] == str(tmp_path / "proj-deepcrew-alt" / "main")
    assert alt["install"] == {"DEEPCREW_NAME": "deepcrew-alt", "DEEPCREW_CONFIG_DIR": alt["config"], "DEEPCREW_HOME": alt["state"]}


def test_the_explicit_folders_win_over_the_name(tmp_path):
    got = identity(tmp_path, tmp_path / "home", DEEPCREW_NAME="deepcrew-alt", DEEPCREW_CONFIG_DIR=str(tmp_path / "cfg"), DEEPCREW_HOME=str(tmp_path / "state"))
    assert (got["config"], got["state"]) == (str(tmp_path / "cfg"), str(tmp_path / "state")) and got["name"] == "deepcrew-alt"
    assert got["shared"] == str(tmp_path / "proj-deepcrew-alt" / "main")


def test_a_member_process_finds_its_installs_folders_whatever_home_it_has(tmp_path):
    home = tmp_path / "home"
    alt = identity(tmp_path, home, DEEPCREW_NAME="deepcrew-alt")
    member_view = identity(tmp_path, tmp_path / "elsewhere", **alt["install"])
    assert {k: member_view[k] for k in ("name", "cmd", "config", "env_file", "state", "shared", "install")} == {k: alt[k] for k in ("name", "cmd", "config", "env_file", "state", "shared", "install")}
    lost = identity(tmp_path, tmp_path / "elsewhere")
    assert lost["config"] != alt["config"] and lost["state"] != alt["state"] and lost["name"] == "deepcrew"


def test_shown_writes_the_home_directory_as_a_tilde_wherever_a_path_inside_it_appears():
    home = str(Path.home())
    assert providers.shown(Path(home) / ".config" / "deepcrew" / ".env") == "~/.config/deepcrew/.env"
    assert providers.shown(home) == "~" and providers.shown(f"cat '{home}/notes.md' > {home}/out") == "cat '~/notes.md' > ~/out"
    assert providers.shown(home + "2/x") == home + "2/x" and providers.shown("/elsewhere/x") == "/elsewhere/x"
    assert providers.shown({"file_path": f"{home}/a.py", "edits": [{"path": f"{home}/b"}], "n": 3}) == {"file_path": "~/a.py", "edits": [{"path": "~/b"}], "n": 3}
    assert providers.shown(None) is None and providers.shown(7) == 7


def test_a_pack_in_the_settings_folder_is_read_without_a_byte_order_mark_and_its_errors_name_pack_json():
    pack = providers.CONFIG_DIR / "packs" / "concise"
    pack.mkdir(parents=True)
    (pack / "leader.md").write_text("\ufeff# playbook for $team\n", encoding="utf-8")
    prompts.check_pack("concise")
    assert prompts.text("leader", {"pack": "concise"}) == "# playbook for $team" and prompts.render("leader", {"pack": "concise"}, team="t", project="p",
                                                                                                   provider="x", model="m", cmd="c") == "# playbook for t"
    (pack / "pack.json").write_text("\ufeff" + json.dumps({"allow": ["node"]}), encoding="utf-8")
    assert prompts.pack_settings("concise") == {"allow": ["node"]}
    for text, why in (("{nope", " is not valid JSON: "), ("[]", " must hold an object"), ('{"allow": "node"}', ": 'allow' must be one of allow, tools")):
        (pack / "pack.json").write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="^" + tilde(".config", "deepcrew", "packs", "concise", "pack.json") + re.escape(why)):
            prompts.pack_settings("concise")


def test_shown_leaves_a_home_path_inside_another_path_and_a_root_home_alone(monkeypatch):
    home = str(Path.home())
    for inside in (f"/Volumes/B{home}/x", f"./{home}/x", f"file://{home}/x", f"backup-{home}"):
        assert providers.shown(inside) == inside, inside
    assert providers.shown(f"cd {home} && ls '{home}/a' x={home}/b") == "cd ~ && ls '~/a' x=~/b"
    for root in ("/", ""):
        monkeypatch.setenv("HOME", root)
        assert providers.shown("/etc/hosts and /") == "/etc/hosts and /", root


def test_shown_on_windows_finds_the_home_directory_in_any_case_and_with_the_doubled_backslashes_of_an_error(monkeypatch):
    monkeypatch.setattr(providers, "is_windows", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: PureWindowsPath(r"C:\Users\me")))
    error = str(PermissionError(13, "Permission denied", r"C:\Users\me\.config\deepcrew\.env"))
    assert providers.shown(error) == "[Errno 13] Permission denied: '~/.config/deepcrew/.env'"
    assert providers.shown(r"type c:\users\ME\a\b.md C:/Users/me/c") == "type ~/a/b.md ~/c"
