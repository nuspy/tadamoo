"""The layout of the skills and the newscast plugin: every skill has its SKILL.md, the plugin's dependencies exist, the plan template is a valid plan with its approval before the rendering,
and the install puts everything where the person's skills live and can be run again."""
import json
from pathlib import Path

import pytest

from tadamoo import capabilities
from tadamoo.newscast import __main__ as cli
from tadamoo.planner.dag import PlanDAG

SKILLS = Path(__file__).resolve().parents[1] / "skills"
PLUGIN = SKILLS / "plugins" / "ai-newscast"
REUSABLE = ("media-generation", "speech-synthesis", "video-editing", "news-research")


def header(path: Path) -> dict:
    meta, _ = capabilities.parse_frontmatter(path.read_text(encoding="utf-8"))
    return meta


@pytest.mark.parametrize("name", REUSABLE)
def test_a_reusable_skill_is_a_standalone_folder_with_its_documents(name):
    folder = SKILLS / name
    meta = header(folder / "SKILL.md")
    assert meta["name"] == name and len(meta["description"]) > 60 and meta.get("requires")
    assert any((folder / sub).is_dir() for sub in ("references", "scripts"))
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    assert "Heavy-swap" in text or "heavy-swap" in text.lower() or "heavy" in text.lower()


def test_the_plugin_names_the_four_skills_it_depends_on_and_says_it_brings_no_mcp_server():
    plugin = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert {d["name"] for d in plugin["dependencies"]["skills"]} == set(REUSABLE)
    for d in plugin["dependencies"]["skills"]:
        assert (PLUGIN / d["path"] / "SKILL.md").is_file()
    assert plugin["mcp_servers"] == [] and plugin["plans"][0]["schedule"] == "off"
    assert set(header(PLUGIN / "skills" / "ai-newscast" / "SKILL.md")["depends"].replace(" ", "").split(",")) == set(REUSABLE)


def test_the_plan_template_is_a_valid_plan_and_nothing_heavy_runs_before_an_approval():
    data = json.loads((PLUGIN / "plans" / "ai-newscast.plan.json").read_text(encoding="utf-8"))
    dag = PlanDAG.from_dict(data)
    assert dag.target_type == "occasional" and not [n for n in dag.nodes.values() if n.type.value.startswith("wait")]
    assert len([n for n in dag.nodes.values() if n.id.startswith("i_")]) == 15 and all(n.type.value == "input" and n.assignee.value == "user" for n in dag.nodes.values() if n.id.startswith("i_"))
    assert dag.nodes["approval1"].assignee.value == "user" and dag.nodes["approval2"].assignee.value == "user"
    assert dag.nodes["gate1"].routes["go"] == ["render1"] and dag.nodes["gate2"].routes["go"] == ["render_rest"]
    assert "approval1" in dag.nodes["gate1"].dependencies and "approval2" in dag.nodes["gate2"].dependencies
    assert dag.nodes["approval2"].dependencies == ["render1"]
    assert len(dag.nodes["research"].inputs) == 15                                      # every question feeds the research step
    positions = [(n.position["x"], n.position["y"]) for n in dag.nodes.values()]
    assert len(set(positions)) == len(positions)                                     # no two steps on the same spot


def test_the_parameter_files_use_only_parameters_of_the_skill():
    spec = json.loads((PLUGIN / "skills" / "ai-newscast" / "skill.json").read_text(encoding="utf-8"))
    names = {p["name"] for p in spec["params"]}
    for f in (PLUGIN / "parameters").glob("*.json"):
        assert set(json.loads(f.read_text(encoding="utf-8"))) <= names | {"output_dir"}, f.name


def test_no_skill_refers_to_a_temporary_folder():
    for f in SKILLS.rglob("*"):
        if f.is_file() and f.suffix in (".md", ".py", ".json"):
            text = f.read_text(encoding="utf-8")
            assert "c:\temp" not in text.lower() and "scratchpad" not in text and "appdata" not in text.lower().replace("%appdata%", ""), f


def test_the_install_copies_to_the_source_folder_and_the_importer_makes_the_copies_the_harness_runs_from(tmp_path, capsys):
    data, skills = tmp_path / "data", tmp_path / "skills"
    assert cli.install_skill(data, skills) == 0
    first = json.loads(capsys.readouterr().out)
    for name in REUSABLE:
        assert (skills / name / "SKILL.md").is_file()
    assert (skills / "plugins" / "ai-newscast" / "plugin.json").is_file() and not (skills / "personal").exists()
    imported = {r["item"] for r in first["import"]["imported"]}
    assert "plugin:ai-newscast" in imported and {f"skill:{n}" for n in REUSABLE} <= imported and not first["import"]["conflicts"]
    assert (data / "personal-skills" / "ai-newscast" / "skill.py").is_file()                 # the runnable copy lives in the user data folder, not in the source folder
    assert not (data / "personal-skills-dir.json").exists()
    assert cli.install_skill(data, skills) == 0
    again = json.loads(capsys.readouterr().out)
    assert not again["import"]["imported"] and not again["import"]["updated"]              # nothing changed: nothing imported twice


def test_the_imported_copy_of_the_skill_finds_the_skills_it_uses(tmp_path, capsys, monkeypatch):
    import importlib.util
    import sys
    import types
    data, skills = tmp_path / "data", tmp_path / "skills"
    assert cli.install_skill(data, skills) == 0
    capsys.readouterr()
    stub = types.ModuleType("tadamoo_skill")
    stub.tool = stub.ai = stub.say = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "tadamoo_skill", stub)
    monkeypatch.delitem(sys.modules, "research", raising=False)
    copy = data / "personal-skills" / "ai-newscast" / "skill.py"
    monkeypatch.setattr(sys, "argv", ["entry.py", str(copy), "context.json"])
    spec = importlib.util.spec_from_file_location("imported_newscast_skill", copy)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.find_skill("news-research") == data / "capabilities" / "imported" / "skills" / "news-research"
