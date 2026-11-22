"""An update that keeps what the person changed: skills, personal skills, plugins and MCP servers, merged against the original they were imported from."""
import json
import shutil
from pathlib import Path

import pytest

from tadamoo.importer import Importer, Problem, Spaces
from tadamoo.importer.common import scan_tree
from tadamoo.personal_skills.store import PersonalSkills
from tadamoo.planner import PlanStore

from test_importer import PERSONAL_SCRIPT, make_personal, make_plugin, make_skill, write

BODY = "Line one.\nLine two.\nLine three.\nLine four.\nLine five.\nLine six.\nLine seven.\n"


def skill_md(body: str, description: str = "Reads a thing.") -> str:
    return f"---\nname: news-research\ndescription: {description}\n---\n\n{body}"


class Model:
    """A model that answers for the file the prompt names (a revision), or with a sentence (a description)."""

    def __init__(self, files=None, description="The new version adds a line."):
        self.files, self.description, self.asked = dict(files or {}), description, []

    def chat_completion(self, messages, **kw):
        user = messages[1]["content"]
        self.asked.append(user)
        if user.startswith("File: "):
            name = user.split("\n", 1)[0].removeprefix("File: ")
            answer = self.files[name]
            return {"choices": [{"message": {"content": answer(user) if callable(answer) else answer}}]}
        return {"choices": [{"message": {"content": self.description}}]}


@pytest.fixture
def make(tmp_path):
    def build(model=None):
        spaces = Spaces.from_data(tmp_path / "data")
        return Importer(spaces, PlanStore(spaces.plans_dir), model=(lambda: model) if model is not None else None)
    return build


@pytest.fixture
def imp(make):
    return make()


@pytest.fixture
def src(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    return base


def copy_of(imp, name="news-research"):
    return imp.spaces.root / "skills" / name


def test_an_import_keeps_the_original_it_was_made_from_and_a_delete_takes_it_away(imp, src):
    imp.import_("skill", make_skill(src, "news-research", body=BODY))
    assert (imp.originals("skill", "news-research") / "SKILL.md").is_file()
    write(src / "news-research" / "SKILL.md", skill_md("Changed.\n"))
    imp.reimport("skill", "news-research", confirm=True)
    assert "Changed." in (imp.originals("skill", "news-research") / "SKILL.md").read_text()          # the original follows the source it was last made from
    imp.delete("skill", "news-research")
    assert not imp.originals("skill", "news-research").exists()


def test_both_sides_changing_different_lines_is_merged_and_the_person_keeps_their_change(imp, src):
    folder = make_skill(src, "news-research", body=BODY)
    imp.import_("skill", folder)
    mine = copy_of(imp) / "SKILL.md"
    mine.write_text(mine.read_text().replace("Line two.", "Line two, in my words."))                                  # the person's edit, made here
    write(folder / "SKILL.md", skill_md(BODY.replace("Line six.", "Line six, updated.")))
    write(folder / "new.md", "added upstream\n")
    plan = imp.update_plan("skill", "news-research")
    assert plan["status"] == "proposed" and plan["local_edited"] and plan["has_base"] and plan["needs_choice"] == 0
    unit = plan["units"][0]
    assert unit["counts"]["merged"] == 1 and unit["counts"]["upstream"] == 1 and unit["base_from"] == "original"
    row = next(f for f in unit["files"] if f["path"] == "SKILL.md")
    assert row["outcome"] == "merged" and "in my words" in row["merged"] and "updated" in row["merged"] and "updated" not in row["installed"] and "in my words" not in row["new"]
    done = imp.update_apply(plan["token"])
    assert done["status"] == "merged"
    text = mine.read_text()
    assert "Line two, in my words." in text and "Line six, updated." in text and (copy_of(imp) / "new.md").read_text() == "added upstream\n"
    entry = imp.manifest.load()["skill:news-research"]
    assert entry["hash"] == scan_tree(folder).hash and entry["copy_hash"] == scan_tree(copy_of(imp)).hash       # recorded as the source it merged, the copy as it now is
    assert imp.list("skill")[0]["local_edited"] is False
    assert not (imp.spaces.root / ".merge" / plan["token"]).exists()
    assert "Line six, updated." in (imp.originals("skill", "news-research") / "SKILL.md").read_text() and "in my words" not in (imp.originals("skill", "news-research") / "SKILL.md").read_text()
    again = imp.update_plan("skill", "news-research")                                                              # the next update still sees the person's change as theirs
    assert again["status"] == "unchanged" or again["units"][0]["counts"]["kept"] >= 1


def test_the_same_lines_changed_on_both_sides_need_a_choice_per_file(imp, src):
    folder = make_skill(src, "news-research", body=BODY)
    imp.import_("skill", folder)
    mine = copy_of(imp) / "SKILL.md"
    mine.write_text(mine.read_text().replace("Line four.", "Line four, mine."))
    write(folder / "SKILL.md", skill_md(BODY.replace("Line four.", "Line four, theirs.")))
    plan = imp.update_plan("skill", "news-research")
    row = plan["units"][0]["files"][0]
    assert row["outcome"] == "conflict" and row["needs_choice"] and plan["needs_choice"] == 1
    assert "<<<<<<< installed" in row["merged"] and "Line four, mine." in row["merged"] and "Line four, theirs." in row["merged"]
    with pytest.raises(Problem) as e:
        imp.update_apply(plan["token"])
    assert e.value.code == "conflicts" and e.value.data["files"] == ["SKILL.md"]
    with pytest.raises(Problem):
        imp.update_apply(plan["token"], resolutions={"SKILL.md": "merged"})                       # the merge still holds markers: not a choice
    assert "Line four, mine." in mine.read_text() and "theirs" not in mine.read_text()           # nothing was written by the refusals
    imp.update_apply(plan["token"], resolutions={"SKILL.md": "theirs"})
    assert "Line four, theirs." in mine.read_text() and "mine" not in mine.read_text()


def test_overwrite_and_cancel_do_what_they_say(imp, src):
    folder = make_skill(src, "news-research", body=BODY)
    imp.import_("skill", folder)
    mine = copy_of(imp) / "SKILL.md"
    mine.write_text(mine.read_text().replace("Line two.", "Line two, mine."))
    write(folder / "SKILL.md", skill_md(BODY.replace("Line six.", "Line six, updated.")))
    plan = imp.update_plan("skill", "news-research")
    assert imp.update_apply(plan["token"], "cancel")["status"] == "cancelled" and "mine" in mine.read_text()
    with pytest.raises(Problem) as e:
        imp.update_apply(plan["token"])
    assert e.value.code == "no_plan"
    plan = imp.update_plan("skill", "news-research")
    assert imp.update_apply(plan["token"], "overwrite")["status"] == "replaced"
    assert "mine" not in mine.read_text() and "updated" in mine.read_text()


def test_a_copy_or_a_source_that_changed_after_the_proposal_is_refused(imp, src):
    folder = make_skill(src, "news-research", body=BODY)
    imp.import_("skill", folder)
    write(folder / "SKILL.md", skill_md(BODY.replace("Line six.", "Line six, updated.")))
    plan = imp.update_plan("skill", "news-research")
    (copy_of(imp) / "SKILL.md").write_text("edited meanwhile\n")
    with pytest.raises(Problem) as e:
        imp.update_apply(plan["token"])
    assert e.value.code == "stale"
    plan = imp.update_plan("skill", "news-research")
    write(folder / "other.md", "the source moved again\n")
    with pytest.raises(Problem) as e:
        imp.update_apply(plan["token"])
    assert e.value.code == "stale"


def test_nothing_to_do_says_so_and_a_part_or_a_plan_is_not_merged_alone(imp, src):
    folder = make_skill(src, "news-research", body=BODY)
    imp.import_("skill", folder)
    assert imp.update_plan("skill", "news-research")["status"] == "unchanged"
    imp.import_("plan", make_plan_file(src))
    with pytest.raises(Problem) as e:
        imp.update_plan("plan", "newscast")
    assert e.value.code == "not_mergeable"


def make_plan_file(src):
    from test_importer import make_plan
    return make_plan(src)


def test_a_copy_imported_before_originals_were_kept_uses_the_unedited_imported_copy_as_the_original(imp, src):
    folder = make_skill(src, "news-research", body=BODY)
    imp.import_("skill", folder)
    shutil.rmtree(imp.root / ".originals")                                                        # how a copy imported by the older version is
    write(folder / "SKILL.md", skill_md(BODY.replace("Line six.", "Line six, updated.")))
    plan = imp.update_plan("skill", "news-research")
    assert plan["has_base"] and plan["units"][0]["base_from"] == "copy" and plan["units"][0]["counts"]["upstream"] == 1
    mine = copy_of(imp) / "SKILL.md"
    mine.write_text(mine.read_text().replace("Line two.", "Line two, mine."))
    shutil.rmtree(imp.root / ".originals", ignore_errors=True)
    plan = imp.update_plan("skill", "news-research")                                                 # edited and no original: every difference waits for a choice
    assert not plan["has_base"] and plan["units"][0]["counts"]["conflict"] == 1 and plan["needs_choice"] == 1


def test_a_personal_skill_is_merged_where_it_is_edited_in_the_store(imp, src, tmp_path):
    folder = make_personal(src, "daily-note", script="def main(params):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    return params.topic\n")
    imp.import_("skill", folder)
    store = PersonalSkills(imp.spaces.personal_dir)
    have = store.get("daily-note")
    store.save({**{k: have[k] for k in ("name", "description", "params")}}, have["script"].replace("a = 1", "a = 10  # mine"), slug="daily-note")         # edited in the app
    (folder / "skill.py").write_text("def main(params):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 50\n    return params.topic\n", encoding="utf-8")
    plan = imp.update_plan("skill", "daily-note")
    unit = plan["units"][0]
    assert unit["counts"]["merged"] == 1 and plan["needs_choice"] == 0
    row = next(f for f in unit["files"] if f["path"] == "skill.py")
    assert "a = 10  # mine" in row["merged"] and "e = 50" in row["merged"]
    imp.update_apply(plan["token"])
    saved = store.get("daily-note")["script"]
    assert "a = 10  # mine" in saved and "e = 50" in saved
    assert imp.list("skill")[0]["local_edited"] is False


def test_a_plugin_merges_its_folder_and_the_skills_and_servers_it_brought(imp, src):
    plugin = make_plugin(src, with_dep=False)
    steps = "Step one.\nStep two.\nStep three.\nStep four.\nStep five.\nStep six.\n"
    write(plugin / "skills" / "news-skill" / "SKILL.md", f"---\nname: news-skill\ndescription: Reads a thing.\n---\n\n{steps}")
    imp.import_("plugin", plugin)
    readme = imp.spaces.root / "plugins" / "news" / "README.md"
    readme.write_text("Readme.\nMy own note.\n")                                                    # the person's edit of the plugin's folder
    part = imp.spaces.root / "skills" / "news-skill" / "SKILL.md"
    part.write_text(part.read_text().replace("Step two.", "Step two, my way."))                       # and of a skill it brought
    write(plugin / "README.md", "Readme, new title.\n")
    write(plugin / "skills" / "news-skill" / "SKILL.md", f"---\nname: news-skill\ndescription: Reads a thing.\n---\n\n{steps}And a new step.\n")
    plan = imp.update_plan("plugin", "news")
    keys = [u["key"] for u in plan["units"]]
    assert keys[0] == "plugin:news" and "skill:news-skill" in keys and "mcp:news-feed" in keys
    by = {u["key"]: u for u in plan["units"]}
    assert by["plugin:news"]["counts"]["conflict"] == 1                                              # the readme: both changed its same line
    assert by["skill:news-skill"]["counts"]["merged"] == 1
    with pytest.raises(Problem) as e:
        imp.update_apply(plan["token"])
    assert e.value.code == "conflicts" and e.value.data["files"] == ["plugin:news::README.md"]
    imp.update_apply(plan["token"], resolutions={"plugin:news::README.md": "ours"})
    assert "My own note." in readme.read_text() and "new title" not in readme.read_text()
    assert "my way" in part.read_text() and "And a new step." in part.read_text()
    assert imp.list("plugin")[0]["local_edited"] is False


def test_an_mcp_record_is_merged_and_a_server_that_now_starts_something_else_is_turned_off(imp, src):
    config = src / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"feed": {"command": "python", "args": ["-m", "feed"], "env": {"FEED_REGION": "eu", "FEED_LANG": "en"}}}}))
    imp.import_("mcp", config, server="feed")
    imp.set_enabled("feed", True)
    record = imp.kinds["mcp"].read("feed")
    record["env"] = [{**e, "value": "it"} if e["name"] == "FEED_REGION" else e for e in record["env"]]
    (imp.spaces.root / "mcp" / "feed.json").write_text(json.dumps(record))                         # the person set a region
    config.write_text(json.dumps({"mcpServers": {"feed": {"command": "python", "args": ["-m", "feed", "--fast"], "env": {"FEED_REGION": "eu", "FEED_LANG": "fr"}}}}))
    plan = imp.update_plan("mcp", "feed")
    assert plan["has_base"] and plan["needs_choice"] == 0 and plan["units"][0]["files"][0]["outcome"] == "merged"
    imp.update_apply(plan["token"])
    merged = imp.kinds["mcp"].read("feed")
    values = {e["name"]: e.get("value") for e in merged["env"]}
    assert values == {"FEED_LANG": "fr", "FEED_REGION": "it"} and merged["command"] == ["python", "-m", "feed", "--fast"]
    assert imp.kinds["mcp"].enabled("feed") is False                                               # it starts something else now: the person turns it on again
    assert imp.list("mcp")[0]["local_edited"] is False


def test_instructions_are_carried_out_on_the_merge_kept_with_the_item_and_carried_out_again_next_time(make, src):
    model = Model(files={"SKILL.md": lambda user: user.split("===== proposed merge =====\n", 1)[1].rsplit("\n=====", 1)[0].replace("Line two.", "Line two, in Hungarian.")})
    imp = make(model)
    folder = make_skill(src, "news-research", body=BODY)
    imp.import_("skill", folder)
    write(folder / "SKILL.md", skill_md(BODY.replace("Line six.", "Line six, updated.")))
    plan = imp.update_plan("skill", "news-research", instructions=["answer in Hungarian"], language="it")
    assert plan["notes"] == ["answer in Hungarian"] and plan["notes_result"]["skill:news-research"]["SKILL.md"] == "revised"
    assert plan["description"]["text"] == "The new version adds a line." and any("Answer in Italian" in a for a in model.asked)
    row = plan["units"][0]["files"][0]
    assert "in Hungarian" in row["merged"] and "in Hungarian" not in row["new"]                     # the three versions differ: installed, new, merged
    imp.update_apply(plan["token"], remember=True)
    assert "in Hungarian" in (copy_of(imp) / "SKILL.md").read_text()
    assert imp.merge_notes("skill", "news-research") == ["answer in Hungarian"]
    assert imp.list("skill")[0]["merge_notes"][0]["text"] == "answer in Hungarian"
    write(folder / "SKILL.md", skill_md(BODY.replace("Line six.", "Line six, updated again.")))
    again = imp.update_plan("skill", "news-research")                                                # no instruction this time: the kept one is carried out
    assert again["saved_notes"] == ["answer in Hungarian"] and any("answer in Hungarian" in a for a in model.asked[2:])
    assert imp.set_merge_notes("skill", "news-research", []) == [] and imp.merge_notes("skill", "news-research") == []


def test_instructions_without_a_model_are_told_and_nothing_is_proposed(imp, src):
    folder = make_skill(src, "news-research", body=BODY)
    imp.import_("skill", folder)
    write(folder / "SKILL.md", skill_md(BODY.replace("Line six.", "Line six, updated.")))
    with pytest.raises(Problem) as e:
        imp.update_plan("skill", "news-research", instructions=["keep my tone"])
    assert e.value.code == "no_model"
    assert not (imp.spaces.root / ".merge").exists() or not any((imp.spaces.root / ".merge").iterdir())
    plan = imp.update_plan("skill", "news-research")                                                  # without instructions the merge needs no model
    assert plan["status"] == "proposed" and plan["description"]["text"] == ""


def test_old_proposals_are_swept(imp, src):
    import os
    import time
    folder = make_skill(src, "news-research", body=BODY)
    imp.import_("skill", folder)
    write(folder / "SKILL.md", skill_md(BODY.replace("Line six.", "Line six, updated.")))
    first = imp.update_plan("skill", "news-research")["token"]
    old = imp.spaces.root / ".merge" / first
    os.utime(old, (time.time() - 7 * 3600, time.time() - 7 * 3600))
    imp.update_plan("skill", "news-research")
    assert not old.exists()
