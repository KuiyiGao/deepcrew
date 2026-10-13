from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from deepcrew import cli, extensions, presets, providers, route

KEY = "sk-" + "a" * 32
UP = ("up", "--provider", "fake", "--model", "m", "--effort", "default", "--offline")
ADDON = '''
def route(board, plan):
    plan["notes"].append("review waits for " + ", ".join(sorted(m["name"] for m in board.members())))

def commands(add):
    add("hello", run, "say hello").add_argument("who")

def run(a):
    print("hello " + a.who)
    return 0

def playbook(board):
    return "Ask the reviewer before you commit."

def tools(board):
    return [{"name": "lint", "description": "lint a file", "inputSchema": {"type": "object"}, "call": lambda board, me, a: {"ok": me, **a}},
            {"name": "message", "call": lambda *a: {}}, "not a tool"]

def inbox(board):
    return {"ci": "ci: the nightly build failed on main"}

class Broken:
    @staticmethod
    def route(board, plan):
        raise RuntimeError("no luck")
'''


@pytest.fixture(autouse=True)
def addon(tmp_path, monkeypatch):
    (tmp_path / "addons").mkdir()
    (tmp_path / "addons" / "addon_demo.py").write_text(ADDON, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "addons"))
    monkeypatch.setattr(extensions, "_loaded", {})
    monkeypatch.setattr(extensions, "_failures", {})
    monkeypatch.setattr(extensions, "_said", set())


def test_nothing_loads_unless_declared(lead, deepcrew):
    assert extensions.declared() == [] and extensions.loaded() == [] and route.plan(lead)["notes"] == []
    code, out, _ = deepcrew("check", "--offline")
    assert code == 0 and "extensions" not in out


def test_config_set_extensions_loads_each_one_and_stores_them_for_this_project_only(deepcrew, project, tmp_path):
    assert deepcrew("config", "set", "extensions", "addon_demo, addon_demo:Broken")[0] == 0
    assert extensions.saved() == {str(project): ["addon_demo", "addon_demo:Broken"]} and extensions.declared(project) == ["addon_demo", "addon_demo:Broken"]
    assert extensions.declared(tmp_path) == [] and extensions.loaded(tmp_path) == []
    assert json.loads(deepcrew("config", "show", "--json")[1])["extensions"] == "addon_demo,addon_demo:Broken"
    assert providers.env_file_get(extensions.KEY) is None
    code, _, err = deepcrew("config", "set", "extensions", "addon_demo,../evil")
    assert code != 0 and "'../evil' is not package.module" in err
    code, _, err = deepcrew("config", "set", "extensions", "addon_demo,no_such_addon")
    assert code != 0 and "extension no_such_addon does not load (ModuleNotFoundError" in err and "nothing was saved" in err
    assert extensions.declared(project) == ["addon_demo", "addon_demo:Broken"]
    assert deepcrew("config", "set", "extensions", "none")[0] == 0 and extensions.declared(project) == [] and extensions.saved() == {}


def test_a_declaration_that_stops_loading_is_skipped_quietly_and_check_and_the_summary_name_it(deepcrew, lead, capsys):
    (providers.CONFIG_DIR).mkdir(parents=True, exist_ok=True)
    (providers.CONFIG_DIR / extensions.FILE).write_text(json.dumps({str(lead.root): ["addon_demo", "no_such_addon"]}))
    assert [spec for spec, _ in extensions.loaded(lead.root)] == ["addon_demo"] and extensions.loaded(lead.root) == extensions.loaded(lead.root)
    assert capsys.readouterr().err == ""
    assert deepcrew("inbox")[2] == "" and deepcrew("status")[2] == ""
    assert extensions.failures(lead.root) == {"no_such_addon": "ModuleNotFoundError: No module named 'no_such_addon'"}
    code, out, _ = deepcrew("check", "--offline")
    assert "not loaded: no_such_addon (ModuleNotFoundError" in out
    assert any(line.startswith("extensions not loaded: no_such_addon (ModuleNotFoundError") for line in cli.summary(lead, None))


def test_the_route_hook_reaches_the_inbox_and_status_and_a_hook_that_raises_is_said_once(lead, deepcrew, capsys):
    (providers.CONFIG_DIR).mkdir(parents=True, exist_ok=True)
    (providers.CONFIG_DIR / extensions.FILE).write_text(json.dumps({str(lead.root): ["addon_demo:Broken", "addon_demo"]}))
    assert route.plan(lead)["notes"] == ["review waits for alpha, beta"] and route.plan(lead)["notes"] == ["review waits for alpha, beta"]
    assert capsys.readouterr().err.count("extension hook route failed (RuntimeError: no luck)") == 1
    code, out, _ = deepcrew("inbox")
    assert code == 0 and "note: review waits for alpha, beta" in out
    assert "note: review waits for alpha, beta" in deepcrew("status")[1]
    assert json.loads(deepcrew("inbox", "--json")[1])["notes"] == ["review waits for alpha, beta"]


def test_the_commands_and_playbook_hooks_add_a_command_and_text_for_the_leader(deepcrew, project, providers_toml, monkeypatch):
    assert deepcrew("config", "set", "extensions", "addon_demo")[0] == 0
    assert deepcrew("hello", "crew") == (0, "hello crew\n", "")
    providers_toml()
    monkeypatch.setenv("FAKE_KEY", KEY)
    code, out, _ = deepcrew(*UP)
    assert code == 0 and out.rstrip().endswith("Ask the reviewer before you commit.")


def test_a_file_inside_the_project_is_pinned_when_declared_and_stops_loading_once_changed(deepcrew, project, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (project / "tools").mkdir()
    ext = project / "tools" / "proj_addon_demo.py"
    ext.write_text("def playbook(board):\n    return 'Read tools/NOTES.md first.'\n", encoding="utf-8")
    assert deepcrew("config", "set", "extensions", "tools.proj_addon_demo")[0] == 0
    assert [spec for spec, _ in extensions.loaded(project)] == ["tools.proj_addon_demo"]
    assert str(project.resolve()) not in sys.path and "tools.proj_addon_demo" not in sys.modules
    assert extensions.loaded(tmp_path) == []
    ext.write_text("def playbook(board):\n    return 'changed by someone'\n", encoding="utf-8")
    monkeypatch.setattr(extensions, "_loaded", {})
    assert extensions.loaded(project) == [] and "changed since it was declared" in extensions.failures(project)["tools.proj_addon_demo"]
    assert deepcrew("config", "set", "extensions", "tools.proj_addon_demo")[0] == 0
    monkeypatch.setattr(extensions, "_loaded", {})
    assert extensions.loaded(project)[0][1].playbook(None) == "changed by someone"
    (project / "tools" / "__init__.py").write_text("")
    assert extensions.own_file("tools.proj_addon_demo", project) is None


def test_a_project_file_on_the_import_path_still_runs_only_from_its_pinned_bytes(deepcrew, project, monkeypatch):
    monkeypatch.setattr(sys, "path", [str(project), *sys.path])
    (project / "proj_tools").mkdir()
    ext = project / "proj_tools" / "pinned_demo.py"
    ext.write_text("def playbook(board):\n    return 'as declared'\n", encoding="utf-8")
    importlib.invalidate_caches()
    assert deepcrew("config", "set", "extensions", "proj_tools.pinned_demo")[0] == 0
    assert extensions.resolve("proj_tools.pinned_demo", project).playbook(None) == "as declared"
    ext.write_text("def playbook(board):\n    return 'changed by a member'\n", encoding="utf-8")
    with pytest.raises(ImportError, match="changed since it was declared"):
        extensions.resolve("proj_tools.pinned_demo", project)
    assert not {"proj_tools", "proj_tools.pinned_demo"} & set(sys.modules) and not (project / "proj_tools" / "__pycache__").exists()
    (project / "proj_tools" / "__init__.py").write_text("")
    importlib.invalidate_caches()
    with pytest.raises(ImportError, match="leads into the project, but not to one file"):
        extensions.resolve("proj_tools.pinned_demo", project)
    assert "proj_tools" not in sys.modules


def test_deepcrews_own_modules_count_as_installed_even_in_the_checkout_that_holds_them():
    checkout = Path(extensions.__file__).resolve().parents[1]
    assert extensions.own_file("deepcrew.presets", checkout) is None and extensions.resolve("deepcrew.presets", checkout) is presets


def test_a_pinned_file_may_define_a_dataclass(deepcrew, project):
    (project / "tools").mkdir()
    (project / "tools" / "proj_dataclass_demo.py").write_text(
        "from __future__ import annotations\n\nfrom dataclasses import dataclass\n\n\n@dataclass\nclass Note:\n    text: str\n\n\n"
        "def playbook(board):\n    return Note('from a dataclass').text\n", encoding="utf-8")
    assert deepcrew("config", "set", "extensions", "tools.proj_dataclass_demo")[0] == 0
    assert extensions.loaded(project)[0][1].playbook(None) == "from a dataclass" and "tools.proj_dataclass_demo" not in sys.modules


def test_a_refused_declaration_writes_paths_in_the_home_directory_with_a_tilde(deepcrew, project, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(Path.home() / "venv" / "bin" / "python"))
    (project / "tools").mkdir()
    (project / "tools" / "proj_broken_demo.py").write_text("def playbook(board:\n", encoding="utf-8")
    for spec in ("no_such_addon", "tools.proj_broken_demo"):
        code, _, err = deepcrew("config", "set", "extensions", spec)
        assert code != 0 and "one installed for ~/venv/bin/python" in err and str(Path.home()) not in err, err


def test_a_chained_hook_replaces_the_value_with_anything_but_none(project, tmp_path, monkeypatch):
    (tmp_path / "addons" / "addon_chain.py").write_text("class Keep:\n    @staticmethod\n    def route(board, plan):\n        return None\n\n\n"
                                                         "class Empty:\n    @staticmethod\n    def route(board, plan):\n        return {}\n", encoding="utf-8")
    importlib.invalidate_caches()
    monkeypatch.setenv(extensions.KEY, "addon_chain:Keep")
    assert extensions.chain("route", {"notes": ["kept"]}, None, project=project) == {"notes": ["kept"]}
    monkeypatch.setenv(extensions.KEY, "addon_chain:Empty")
    assert extensions.chain("route", {"notes": ["kept"]}, None, project=project) == {}


def test_the_tools_and_inbox_hooks_give_members_a_tool_and_the_leader_an_item(lead, deepcrew):
    from deepcrew import member
    (providers.CONFIG_DIR).mkdir(parents=True, exist_ok=True)
    (providers.CONFIG_DIR / extensions.FILE).write_text(json.dumps({str(lead.root): ["addon_demo"]}))
    names = [t["name"] for t in member.tools_for(lead)]
    assert names == ["message", "approve", "lint"] and all("call" not in t for t in member.tools_for(lead))
    assert member.call(lead, "alpha", "lint", {"path": "src/a.py"}) == {"ok": "alpha", "path": "src/a.py"}
    data = json.loads(deepcrew("inbox", "--json")[1])
    assert data["extensions"] == ["ci: the nightly build failed on main"] and "ext:ci" in data["new"]
    assert "ci: the nightly build failed on main   (shown before)" in deepcrew("inbox")[1]


def test_an_extension_that_needs_a_newer_api_is_refused_with_that_reason(deepcrew, project, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (project / "tools").mkdir()
    (project / "tools" / "later_demo.py").write_text("DEEPCREW_API = 99\n\ndef playbook(board):\n    return 'x'\n", encoding="utf-8")
    code, _, err = deepcrew("config", "set", "extensions", "tools.later_demo")
    assert code == 1 and f"it needs extension API 99; this deepcrew has API {extensions.API_VERSION}" in err and extensions.declared(project) == []


def test_config_set_extensions_global_declares_installed_modules_for_every_project_apart_from_each_projects_own(deepcrew, lead, project, tmp_path,
                                                                                                                 monkeypatch):
    code, out, err = deepcrew("config", "set", "extensions", "addon_demo", "--global")
    assert (code, err) == (0, "") and out == f"extensions of every project: addon_demo ({providers.shown(providers.ENV_FILE)}; each project loads its own too)\n"
    assert extensions.everywhere() == ["addon_demo"] and providers.env_file_get(extensions.KEY) == "addon_demo" and extensions.saved() == {}
    assert extensions.declared(project) == ["addon_demo"] and extensions.declared(tmp_path) == ["addon_demo"]
    assert route.plan(lead)["notes"] == ["review waits for alpha, beta"]
    assert deepcrew("config", "set", "extensions", "addon_demo:Broken")[0] == 0
    assert extensions.declared(project) == ["addon_demo", "addon_demo:Broken"] and extensions.declared(tmp_path) == ["addon_demo"]
    shown = json.loads(deepcrew("config", "show", "--json")[1])
    assert (shown["extensions"], shown["global_extensions"]) == ("addon_demo,addon_demo:Broken", "addon_demo")
    assert {"extensions: addon_demo,addon_demo:Broken", "global_extensions: addon_demo"} <= set(deepcrew("config", "show")[1].splitlines())
    monkeypatch.setenv(extensions.KEY, "addon_demo:run")
    assert extensions.declared(project) == ["addon_demo:run", "addon_demo", "addon_demo:Broken"]
    monkeypatch.delenv(extensions.KEY)
    assert deepcrew("config", "set", "extensions", "none")[0] == 0 and extensions.declared(project) == ["addon_demo"]
    assert deepcrew("config", "set", "extensions", "none", "--global") == (0, f"extensions of every project: none ({providers.shown(providers.ENV_FILE)}; "
                                                                              "each project loads its own too)\n", "")
    assert extensions.everywhere() == [] and extensions.declared(project) == [] and providers.env_file_get(extensions.KEY) is None


def test_a_global_declaration_is_checked_as_a_projects_is_and_a_file_of_the_project_is_refused_there(deepcrew, project, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (project / "tools").mkdir()
    (project / "tools" / "proj_global_demo.py").write_text("def playbook(board):\n    return 'pinned for one project'\n", encoding="utf-8")
    for spec, why in (("addon_demo,tools.proj_global_demo", "extension tools.proj_global_demo is tools/proj_global_demo.py of this project, whose bytes "
                                                            "are pinned for this project alone: declare it without --global; nothing was saved"),
                      ("addon_demo,../evil", "'../evil' is not package.module"),
                      ("addon_demo,no_such_addon", "extension no_such_addon does not load (ModuleNotFoundError")):
        code, out, err = deepcrew("config", "set", "extensions", spec, "--global")
        assert (code, out) == (1, "") and why in err, spec
    assert extensions.everywhere() == [] and providers.env_file_get(extensions.KEY) is None and not (providers.CONFIG_DIR / extensions.PINS).exists()
    assert deepcrew("config", "set", "extensions", "tools.proj_global_demo")[0] == 0
    assert [spec for spec, _ in extensions.loaded(project)] == ["tools.proj_global_demo"] and extensions.everywhere() == []


def test_global_is_only_for_config_set_extensions(deepcrew):
    for argv in (("set", "max_active", "3"), ("set", "pack", "basic"), ("show",), ("preset", "solo")):
        code, out, err = deepcrew("config", *argv, "--global")
        assert (code, out) == (1, "") and "--global is for `deepcrew config set extensions` only" in err, argv
    assert not providers.ENV_FILE.exists()


def test_a_global_declaration_that_stops_loading_is_named_with_the_command_that_takes_it_off(deepcrew, lead):
    providers.env_file_set({extensions.KEY: "no_such_global_addon"})
    line, = [s for s in cli.summary(lead, None) if s.startswith("extensions not loaded")]
    assert line.endswith("; deepcrew config set extensions none --global takes them off every project")
    (providers.CONFIG_DIR / extensions.FILE).write_text(json.dumps({str(lead.root): ["no_such_addon"]}))
    line, = [s for s in cli.summary(lead, None) if s.startswith("extensions not loaded")]
    assert line.endswith("; deepcrew config set extensions none takes them off this project, and with --global off every project")
    out = deepcrew("check", "--offline")[1]
    assert "no_such_global_addon is declared for every project: deepcrew config set extensions ... --global" in " ".join(out.split())
