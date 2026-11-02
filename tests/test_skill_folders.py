"""Skills given with --skills: they are listed on the Capabilities page and load at once, a changed one loads as it is now."""
from pathlib import Path

import pytest

from tadamoo.capabilities import CapabilityIndex, add_skill_folders
from tadamoo.skills import SkillCatalog
from tadamoo.stack import AgentConfig, build_agent
from serve.capabilities_api import CapabilityApi, CapabilityError

TEXT = "---\nname: deploy\ndescription: Deploys the site\n---\nRun the deploy script, then check the page."


def write_skill(folder: Path, text: str = TEXT) -> Path:
    path = folder / "deploy" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_index(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return CapabilityIndex(tmp_path / "caps", tmp_path / "ws", home=home, appdata=tmp_path / "none")


def test_a_skill_folder_is_listed_on_the_capabilities_page_and_usable(tmp_path):
    folder = tmp_path / "mine"
    write_skill(folder)
    (tmp_path / "ws").mkdir()
    add_skill_folders(tmp_path / "caps", [folder, tmp_path / "missing"])
    entry = make_index(tmp_path).get("skill:deploy")
    assert entry["origin"] == "folder" and entry["state"] == "approved"
    assert add_skill_folders(tmp_path / "caps", [folder]) == [str(folder.resolve())]          # naming it again changes nothing


def test_a_changed_skill_still_loads(tmp_path):
    folder = tmp_path / "mine"
    path = write_skill(folder)
    catalog = SkillCatalog([folder])
    catalog.discover_skills()
    assert catalog.body("deploy").startswith("Run the deploy script")
    path.write_text(TEXT + "\nThen check the logs.", encoding="utf-8")
    catalog.discover_skills()
    assert catalog.body("deploy").endswith("Then check the logs.")


def test_the_agent_built_with_a_skills_folder_loads_its_skills(tmp_path):
    ws, folder, caps = tmp_path / "ws", tmp_path / "mine", tmp_path / "caps"
    ws.mkdir()
    write_skill(folder)
    cfg = AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, skill_paths=[str(folder)], capabilities_dir=str(caps))
    built = build_agent(cfg, client=object())
    try:
        assert built.agent.tools.execute("load_skill", {"name": "deploy"}).startswith("Run the deploy script")
        entry = CapabilityIndex(caps, ws, home=tmp_path / "h", appdata=tmp_path / "none").get("skill:deploy")
        assert entry["state"] == "approved"                          # the run listed it on the page, usable
    finally:
        built.close()


def test_the_page_saves_skill_folders_and_refuses_one_that_does_not_exist(tmp_path):
    folder = tmp_path / "mine"
    write_skill(folder)
    (tmp_path / "ws").mkdir()
    api = CapabilityApi(tmp_path / "caps", tmp_path / "ws", home=tmp_path / "home", appdata=tmp_path / "none")
    assert not any(e["id"] == "skill:deploy" for e in api.overview()["entries"])
    assert api.settings({"skill_dirs": [str(folder)]})["settings"]["skill_dirs"] == [str(folder.resolve())]
    assert any(e["id"] == "skill:deploy" and e["state"] == "approved" for e in api.overview()["entries"])
    with pytest.raises(CapabilityError, match="does not exist"):
        api.settings({"skill_dirs": [str(tmp_path / "nowhere")]})
    with pytest.raises(CapabilityError):
        api.settings({"skill_dirs": "D:/x"})
    assert api.settings({"visual": "builtin"})["settings"]["skill_dirs"] == [str(folder.resolve())]       # other settings keep the folders


def test_a_folder_named_by_a_run_shows_up_on_a_page_that_is_already_open(tmp_path):
    folder = tmp_path / "mine"
    write_skill(folder)
    (tmp_path / "ws").mkdir()
    api = CapabilityApi(tmp_path / "caps", tmp_path / "ws", home=tmp_path / "home", appdata=tmp_path / "none")
    api.overview()
    add_skill_folders(tmp_path / "caps", [folder])                  # what a --skills option does
    assert any(e["id"] == "skill:deploy" for e in api.overview()["entries"])
