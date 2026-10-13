import json
import re
from pathlib import Path

from deepcrew import cli

ROOT = Path(__file__).resolve().parents[1]
VERSION = cli.__version__
SKILL = ROOT / "mod" / "skills" / "deepcrew" / "SKILL.md"


def read(rel: str):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_the_marketplace_lists_the_mod_by_its_name_at_the_version_of_the_package():
    plugin, market = read("mod/.claude-plugin/plugin.json"), read(".claude-plugin/marketplace.json")
    assert (plugin["name"], plugin["version"]) == ("deepcrew", VERSION)
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [("deepcrew", "./mod")]
    assert all((ROOT / "mod" / "hooks" / m).is_file() for m in read("mod/hooks/hooks.json")["modules"])
    assert (plugin["userConfig"]["command"]["default"], plugin["userConfig"]["team"]["default"]) == ("deepcrew", "main")


def test_the_plugin_carries_the_deepcrew_skill_so_one_install_gives_the_skill_and_the_mod():
    front = SKILL.read_text(encoding="utf-8").split("---")[1]
    assert re.search(r"^name: deepcrew$", front, re.M) and re.search(r"^description: .+", front, re.M)


def test_status_json_carries_what_the_mod_shows_and_answers(lead, deepcrew, monkeypatch):
    monkeypatch.setattr(cli, "page_url", lambda team_dir: "http://127.0.0.1:9/?t=stand-in")
    lead.post("build", "", ["src/"], [], "leader")
    lead.request("perms", "alpha", {"tool": "Bash", "input": {"command": "npm test", "description": "Run the tests"}, "tool_use_id": None,
                                    "always": False})
    lead.request("questions", "beta", {"question": "Which one?", "task": None, "answer": None, "options": ["A", "B"]})
    data = json.loads(deepcrew("status", "--json")[1])
    assert {"members", "usage", "tasks", "permission_requests", "questions", "budget", "paused", "page_url"} <= set(data)
    assert [(m["name"], m["state"], m["task"]) for m in data["members"]] == [("alpha", "idle", None), ("beta", "idle", None)]
    assert [t["state"] for t in data["tasks"]] == ["open"] and set(data["usage"]) == {"alpha", "beta"}
    (perm,) = data["permission_requests"]
    assert (perm["member"], perm["tool"], perm["input"]["command"], perm["input"]["description"]) == ("alpha", "Bash", "npm test", "Run the tests")
    assert perm["id"].startswith("P") and isinstance(perm["widening"]["text"], str)
    (q,) = data["questions"]
    assert q["id"].startswith("Q") and (q["member"], q["question"], q["options"]) == ("beta", "Which one?", ["A", "B"])
    assert {"usd", "spent"} <= set(data["budget"]) and data["paused"] is None and data["page_url"] == "http://127.0.0.1:9/?t=stand-in"
