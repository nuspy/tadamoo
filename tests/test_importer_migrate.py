"""The one-time move from a folder the harness read directly (skill_dirs, a personal-skills override) to imported copies."""
import json
from pathlib import Path

import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.importer import Importer, Spaces
from tadamoo.importer.__main__ import main
from tadamoo.importer.migrate import migrate
from tadamoo.personal_skills.store import PersonalSkills
from tadamoo.planner import PlanDAG, PlanStore
from test_importer import PERSONAL_SCRIPT, PERSONAL_SPEC, PLAN, digest_dir, make_personal, make_plan, make_skill, write


@pytest.fixture
def world(tmp_path):
    """A folder of skills laid out like the owner's (skills, plugins, personal), a data folder with the two settings, and a plan already in the planner."""
    root = tmp_path / "skills"
    make_skill(root, "shared-research")
    plugin = root / "plugins" / "news"
    make_skill(plugin / "skills", "news-skill")
    make_plan(plugin / "plans", "news.plan.json")
    write(plugin / "plugin.json", json.dumps({"name": "news", "version": "1", "skills": ["skills/news-skill"], "plans": [{"file": "plans/news.plan.json"}],
                                              "dependencies": {"skills": [{"name": "shared-research", "path": "../../shared-research"}]}}))
    make_personal(root / "personal", "daily-note")
    data = tmp_path / "data"
    write(data / "capabilities" / "settings.json", json.dumps({"skill_dirs": [str(root), str(tmp_path / "mine")], "off": ["mcp:keep"]}))
    write(data / "personal-skills-dir.json", json.dumps({"dir": str(root / "personal")}))
    spaces = Spaces.from_data(data)
    PlanStore(spaces.plans_dir).create(PlanDAG.from_dict(PLAN))                               # made earlier by the old import script
    return root, data, Importer(spaces, PlanStore(spaces.plans_dir))


def test_a_dry_run_changes_nothing(world):
    root, data, imp = world
    before = (digest_dir(root), digest_dir(data))
    report = migrate(imp, root, dry_run=True)
    assert set(report["would_import"]) == {"skill:shared-research", "plugin:news", "skill:daily-note"} and report["imported"] == [] and len(report["settings"]) == 2
    assert (digest_dir(root), digest_dir(data)) == before


def test_the_folder_is_imported_as_copies_and_taken_out_of_the_settings(world):
    root, data, imp = world
    before = digest_dir(root)
    report = migrate(imp, root)
    assert report["conflicts"] == [] and report["source_untouched"] is True and digest_dir(root) == before
    assert {r["name"] for r in imp.list("skill")} == {"shared-research", "news-skill", "daily-note"}
    news = next(r for r in imp.list("plugin") if r["name"] == "news")
    assert set(news["parts"]) == {"skill:news-skill", "plan:newscast"}
    assert next(r for r in imp.list("plan"))["adopted"] == "same"                          # the plan the old script made is now the imported copy
    assert PersonalSkills(imp.spaces.personal_dir).get("daily-note")["script"] == PERSONAL_SCRIPT
    settings = json.loads((data / "capabilities" / "settings.json").read_text(encoding="utf-8"))
    assert settings["skill_dirs"] == [str(data.parent / "mine")] and settings["off"] == ["mcp:keep"]         # his other folder and settings stay
    assert not (data / "personal-skills-dir.json").exists() and (data / "personal-skills-dir.json.undone").is_file()


def test_running_it_again_imports_nothing_twice(world):
    root, data, imp = world
    migrate(imp, root)
    again = migrate(imp, root)
    assert again["imported"] == [] and again["conflicts"] == [] and {s["item"] for s in again["skipped"]} >= {"skill:shared-research", "plugin:news", "skill:daily-note"}
    assert again["settings"] == []


def test_a_personal_skill_that_is_the_plugins_own_is_not_imported_twice(world):
    root, data, imp = world
    plugin = root / "plugins" / "news" / "skills" / "news-skill"
    write(plugin / "skill.json", json.dumps({**PERSONAL_SPEC, "name": "news-skill"}))
    write(plugin / "skill.py", PERSONAL_SCRIPT)
    make_personal(root / "personal", "news-skill")
    report = migrate(imp, root)
    assert any(s["item"] == "skill:news-skill" and "already imported" in s["why"] for s in report["skipped"]) and report["conflicts"] == []


def test_the_command_line_does_the_same(world, capsys):
    root, data, imp = world
    assert main(["--data-dir", str(data), "migrate", str(root), "--dry-run"]) == 0
    assert "would_import" in json.loads(capsys.readouterr().out)
    assert main(["--data-dir", str(data), "migrate", str(root)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", str(data), "list", "skill"]) == 0
    assert {r["name"] for r in json.loads(capsys.readouterr().out)["imported"]} == {"shared-research", "news-skill", "daily-note"}
    assert main(["--data-dir", str(data), "delete", "skill", "daily-note"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", str(data), "delete", "skill", "daily-note"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_imported"
    assert Path(root / "personal" / "daily-note" / "skill.py").is_file()
