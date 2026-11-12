"""The layout of the skills and the newscast plugin: every skill has its SKILL.md, the plugin's dependencies exist, the plan template is a valid plan with its approval before the rendering,
and the install puts everything where the person's skills live and can be run again."""
import json
import shutil
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


def test_the_plan_template_is_a_valid_plan_and_nothing_renders_before_the_approval():
    data = json.loads((PLUGIN / "plans" / "ai-newscast.plan.json").read_text(encoding="utf-8"))
    dag = PlanDAG.from_dict(data)
    assert dag.target_type == "occasional" and not [n for n in dag.nodes.values() if n.type.value.startswith("wait")]
    assert "approval" in dag.nodes["gate"].dependencies and dag.nodes["gate"].routes["go"] == ["render"]
    assert dag.nodes["approval"].spec["choices"] == ["go", "stop"] and "gate" in dag.nodes["render"].dependencies


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


def test_the_install_puts_skills_and_plugin_in_the_skills_folder_and_can_be_run_again(tmp_path, capsys):
    data, skills = tmp_path / "data", tmp_path / "skills"
    (data / "personal-skills" / "ai-newscast").mkdir(parents=True)
    (data / "personal-skills" / "ai-newscast" / "skill.json").write_text("{}", encoding="utf-8")
    assert cli.install_skill(data, skills) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["old_copy_removed"] and not (data / "personal-skills" / "ai-newscast").exists()
    for name in REUSABLE:
        assert (skills / name / "SKILL.md").is_file()
    assert (skills / "plugins" / "ai-newscast" / "plugin.json").is_file() and (skills / "personal" / "ai-newscast" / "skill.py").is_file()
    assert json.loads((data / "capabilities" / "settings.json").read_text(encoding="utf-8"))["skill_dirs"] == [str(skills.resolve())]
    assert json.loads((data / "personal-skills-dir.json").read_text(encoding="utf-8"))["dir"] == str(skills / "personal")
    assert cli.install_skill(data, skills) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["version"] == first["version"]                                  # nothing changed: no new version
    found = [e.name for e in capabilities.discover(tmp_path / "ws", home=tmp_path / "home", appdata=tmp_path / "ad", folders=[str(skills)]) if e.kind == "skill"]
    assert set(REUSABLE) <= set(found) and "ai-newscast" in found
