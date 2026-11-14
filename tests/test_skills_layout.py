"""The layout of the skills and the newscast plugin: every skill has its SKILL.md, the plugin's dependencies exist, the plan template is a valid plan with its approval before the rendering
(what is installed, and how, is tests/test_skillmanager.py)."""
import json
from pathlib import Path

import pytest

from tadamoo import capabilities
from tadamoo.planner.dag import PlanDAG

SKILLS = Path(__file__).resolve().parents[1] / "skills"
PLUGIN = SKILLS / "plugins" / "ai-newscast"
REUSABLE = ("image-generation", "image-editing", "video-generation", "talking-head", "speech-synthesis", "video-editing", "news-research")


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


def test_the_modification_skill_is_standalone_and_the_newscast_does_not_depend_on_it():
    meta = header(SKILLS / "video-modification" / "SKILL.md")
    assert meta["name"] == "video-modification" and len(meta["description"]) > 60 and meta.get("requires") and "depends" not in meta
    assert (SKILLS / "video-modification" / "references" / "engines.md").is_file() and (SKILLS / "video-modification" / "scripts" / "modify_video.py").is_file()
    plugin = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert "video-modification" not in {d["name"] for d in plugin["dependencies"]["skills"]}


def test_the_plugin_names_the_skills_it_depends_on_and_says_it_brings_no_mcp_server():
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
    questions = [n for n in dag.nodes.values() if n.id.startswith("i_")]
    assert questions and all(n.type.value == "input" and n.assignee.value == "user" for n in questions)
    starts = dag.start_nodes()
    assert [s.id for s in starts] == ["start"] and starts[0].type.value == "start" and starts[0].spec == {"number": 1, "launch": "manual"}      # an explicit start node, not a step
    assert starts[0].dependencies == [] and starts[0].status == "pending" and set(starts[0].outputs) == {n.id for n in questions}
    assert all(n.dependencies == ["start"] and "clean" in n.outputs for n in questions)                    # asked when the plan starts; nothing runs before every one is answered
    assert dag.nodes["approval1"].assignee.value == "user" and dag.nodes["approval2"].assignee.value == "user"
    assert dag.nodes["gate1"].routes["go"] == ["video"] and dag.nodes["gate2"].routes["go"] == ["done"]
    assert {"approval1", "auto1"} <= set(dag.nodes["gate1"].dependencies) and {"approval2", "auto2"} <= set(dag.nodes["gate2"].dependencies)      # the approval, or the answer given in its place
    assert dag.nodes["approval2"].dependencies == ["need2"] and dag.nodes["need2"].dependencies == ["video"] and dag.nodes["approval1"].dependencies == ["need1"]
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
