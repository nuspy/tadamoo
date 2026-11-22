"""Updating an installed skill with what the person changed kept: the skill manager and its routes."""
from pathlib import Path

import pytest

from serve.skills_api import SkillsApi
from tadamoo.importer import Importer, Problem, Spaces
from tadamoo.planner import PlanStore
from tadamoo.skillmanager import SkillManager

from test_importer import make_skill, write

BODY = "Line one.\nLine two.\nLine three.\nLine four.\nLine five.\nLine six.\nLine seven.\n"


def md(body: str) -> str:
    return f"---\nname: news-research\ndescription: Reads a thing.\n---\n\n{body}"


@pytest.fixture
def world(tmp_path):
    data = tmp_path / "data"
    spaces = Spaces.from_data(data)
    imp = Importer(spaces, PlanStore(spaces.plans_dir))
    mgr = SkillManager(data, program=tmp_path / "program", importer=imp)
    src = tmp_path / "mine"
    src.mkdir()
    folder = make_skill(src, "news-research", body=BODY)
    plan = mgr.plan(str(folder))
    job = mgr.install(token=plan["token"], models=False)
    assert job["installed"] == ["news-research"]
    return mgr, SkillsApi(mgr), folder, spaces


def test_the_route_proposes_the_merge_and_applies_it_and_the_installed_choice_follows(world):
    mgr, api, folder, spaces = world
    copy = spaces.root / "skills" / "news-research" / "SKILL.md"
    copy.write_text(copy.read_text().replace("Line two.", "Line two, mine."))
    write(folder / "SKILL.md", md(BODY.replace("Line six.", "Line six, updated.")))
    code, plan = api.route("POST", "/api/skills/update/plan", {"name": "news-research", "language": "it"})
    assert code == 200 and plan["status"] == "proposed" and plan["units"][0]["counts"]["merged"] == 1
    code, done = api.route("POST", "/api/skills/update/apply", {"token": plan["token"]})
    assert code == 200 and done["status"] == "merged"
    text = copy.read_text()
    assert "Line two, mine." in text and "Line six, updated." in text
    assert mgr.choices.load()["news-research"]["hash"] == mgr.imp.manifest.load()["skill:news-research"]["hash"]
    assert api.route("POST", "/api/skills/update/plan", {"name": "news-research"})[1]["status"] in ("unchanged", "proposed")


def test_a_conflict_is_answered_as_a_conflict_and_the_choice_resolves_it(world):
    mgr, api, folder, spaces = world
    copy = spaces.root / "skills" / "news-research" / "SKILL.md"
    copy.write_text(copy.read_text().replace("Line four.", "Line four, mine."))
    write(folder / "SKILL.md", md(BODY.replace("Line four.", "Line four, theirs.")))
    plan = api.route("POST", "/api/skills/update/plan", {"name": "news-research"})[1]
    code, err = api.route("POST", "/api/skills/update/apply", {"token": plan["token"], "choice": "merge"})
    assert code == 409 and err["error"]["code"] == "conflicts" and err["error"]["files"] == ["SKILL.md"]
    code, done = api.route("POST", "/api/skills/update/apply", {"token": plan["token"], "resolutions": {"SKILL.md": "ours"}})
    assert code == 200 and "Line four, mine." in copy.read_text()


def test_cancel_overwrite_and_a_stale_token_are_answered(world):
    mgr, api, folder, spaces = world
    write(folder / "SKILL.md", md(BODY.replace("Line six.", "Line six, updated.")))
    plan = api.route("POST", "/api/skills/update/plan", {"name": "news-research"})[1]
    assert api.route("POST", "/api/skills/update/apply", {"token": plan["token"], "choice": "cancel"})[1]["status"] == "cancelled"
    assert api.route("POST", "/api/skills/update/apply", {"token": plan["token"]})[0] == 404
    plan = api.route("POST", "/api/skills/update/plan", {"name": "news-research"})[1]
    assert api.route("POST", "/api/skills/update/apply", {"token": plan["token"], "choice": "overwrite"})[1]["status"] == "replaced"
    write(folder / "SKILL.md", md(BODY.replace("Line six.", "Line six, again.")))
    plan = api.route("POST", "/api/skills/update/plan", {"name": "news-research"})[1]
    assert api.route("POST", "/api/skills/update/apply", {"token": plan["token"], "choice": "bogus"})[0] == 400


def test_the_kept_instructions_are_listed_replaced_and_cleared(world):
    mgr, api, folder, spaces = world
    assert api.route("POST", "/api/skills/update/notes", {"name": "news-research"})[1] == {"notes": []}
    assert api.route("POST", "/api/skills/update/notes", {"name": "news-research", "notes": ["keep my tone", "  "]})[1] == {"notes": ["keep my tone"]}
    assert api.route("POST", "/api/skills/update/notes", {"name": "news-research"})[1] == {"notes": ["keep my tone"]}
    assert api.route("POST", "/api/skills/update/notes", {"name": "news-research", "notes": []})[1] == {"notes": []}
    assert api.route("POST", "/api/skills/update/notes", {"name": "news-research", "notes": "x"})[0] == 400
    assert api.route("POST", "/api/skills/update/plan", {"name": "nothing-here"})[0] == 404
