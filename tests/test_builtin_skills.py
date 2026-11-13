"""The built-in skills the setup offers: the catalogue matches the folders, a choice brings what it requires and nothing else, and what is chosen is imported as copies (run again: nothing twice)."""
import json
from pathlib import Path

import pytest

from tadamoo import capabilities
from tadamoo.importer import Importer, Spaces, builtin
from tadamoo.importer.__main__ import main
from tadamoo.importer.common import Problem

SKILLS = Path(__file__).resolve().parents[1] / "skills"
MEDIA = ("image-generation", "image-editing", "video-generation", "talking-head", "video-editing")


@pytest.fixture
def imp(tmp_path):
    return Importer(Spaces.from_data(tmp_path / "data"))


def names(imp, kind):
    return {r["name"] for r in imp.list(kind)}


def test_every_entry_of_the_catalogue_is_a_folder_and_every_top_folder_is_an_entry():
    entries = builtin.load()
    assert all(e["present"] for e in entries) and len({e["name"] for e in entries}) == len(entries)
    folders = {p.name for p in SKILLS.iterdir() if (p / "SKILL.md").is_file()}
    assert folders == {e["name"] for e in entries if e["kind"] == "skill"}
    for e in entries:
        assert set(e["requires"]) <= {x["name"] for x in entries} and e["title"]["en"] and e["title"]["it"] and e["summary"]["en"] and e["summary"]["it"]


def test_the_media_skills_are_separate_entries_each_one_that_can_be_chosen_alone():
    entries = {e["name"]: e for e in builtin.load()}
    assert set(MEDIA) <= set(entries) and all(entries[m]["requires"] == [] for m in MEDIA)


def test_what_a_skill_says_it_depends_on_is_what_the_catalogue_requires():
    entries = {e["name"]: e for e in builtin.load()}
    for name, e in entries.items():
        if e["kind"] != "skill":
            continue
        meta, _ = capabilities.parse_frontmatter((SKILLS / name / "SKILL.md").read_text(encoding="utf-8"))
        assert set(e["requires"]) == {d.strip() for d in meta.get("depends", "").split(",") if d.strip()}, name
    plugin = json.loads((SKILLS / "plugins" / "ai-newscast" / "plugin.json").read_text(encoding="utf-8"))
    assert set(entries["ai-newscast"]["requires"]) == {d["name"] for d in plugin["dependencies"]["skills"]}


def test_a_choice_brings_what_it_requires_once_in_the_order_of_the_catalogue():
    entries = builtin.load()
    assert [e["name"] for e in builtin.choose(entries, ["video-generation"])] == ["video-generation"]
    got = [e["name"] for e in builtin.choose(entries, ["ai-newscast", "talking-head"])]
    assert got[-1] == "ai-newscast" and len(got) == len(set(got)) == 8
    with pytest.raises(Problem):
        builtin.choose(entries, ["nothing"])


def test_only_the_chosen_skills_are_imported_and_a_second_run_imports_nothing_twice(imp):
    first = builtin.install(imp, ["image-generation", "video-generation"])
    assert [r["item"] for r in first["imported"]] == ["skill:image-generation", "skill:video-generation"] and not first["conflicts"]
    assert names(imp, "skill") == {"image-generation", "video-generation"} and names(imp, "plugin") == set()
    second = builtin.install(imp, ["image-generation", "video-generation"])
    assert not second["imported"] and len(second["skipped"]) == 2


def test_the_plugin_brings_the_skills_it_requires(imp):
    got = builtin.install(imp, ["ai-newscast"])
    assert not got["conflicts"] and "plugin:ai-newscast" in {r["item"] for r in got["imported"]}
    assert {e["name"] for e in builtin.load() if e["kind"] == "skill"} <= names(imp, "skill")


def test_a_dry_run_changes_nothing_and_nothing_chosen_imports_nothing(imp):
    assert builtin.install(imp, ["talking-head"], dry_run=True)["would_import"] == ["skill:talking-head"] and names(imp, "skill") == set()
    assert builtin.install(imp, [])["chosen"] == [] and names(imp, "skill") == set()


def test_the_shell_lists_and_installs(tmp_path, capsys):
    data = str(tmp_path / "data")
    assert main(["--data-dir", data, "builtin", "install", "image-editing", "talking-head"]) == 0
    assert json.loads(capsys.readouterr().out)["chosen"] == ["image-editing", "talking-head"]
    assert main(["--data-dir", data, "builtin", "list"]) == 0
    listed = {r["name"]: r for r in json.loads(capsys.readouterr().out)["builtin"]}
    assert listed["image-editing"]["imported"] and listed["talking-head"]["imported"] and not listed["video-generation"]["imported"]
    assert main(["--data-dir", data, "builtin", "install", "--none"]) == 0
    assert json.loads(capsys.readouterr().out)["chosen"] == []
    assert main(["--data-dir", data, "builtin", "install", "nope"]) == 1
