"""Personal skills: the check of a script, the folder of skills, approval by hash and the .pskill file."""
import io
import json
import zipfile

import pytest

from harness.personal_skills import check
from harness.personal_skills.store import PersonalSkills, SkillError, slugify

GOOD = '''
from basalt_skill import tool, ai, say
import re

def main(params):
    text = tool("read_file", path=params.file)
    say("read it")
    return ai("Summarise: {t}", t=text)
'''
SPEC = {"name": "Summarise a file", "description": "reads a file and summarises it", "params": [{"name": "file", "type": "path", "explanation": "which file"}]}


def test_a_script_that_only_uses_the_harness_and_plain_python_is_fine():
    assert check.check(GOOD) == []
    assert check.tools_used(GOOD) == ["read_file"]


@pytest.mark.parametrize("source", [
    "import os\ndef main(params): return os.getcwd()",
    "import subprocess\ndef main(params): pass",
    "import urllib.request\ndef main(params): pass",
    "def main(params):\n    return open('x').read()",
    "def main(params):\n    return eval('1')",
    "from pathlib import Path\ndef main(params): return Path('x').read_text()",
])
def test_the_script_may_use_files_programs_and_the_network_like_any_program_of_the_person(source):
    assert check.check(source) == []


def test_a_script_needs_a_main_with_one_argument_and_valid_syntax():
    assert check.check("x = 1")[0]["message"].startswith("the script needs a function main")
    assert "one argument" in check.check("def main(a, b): pass")[0]["message"]
    assert "syntax error" in check.check("def main(:")[0]["message"]


def test_only_a_main_is_required():
    assert check.check("import os") != []
    assert check.check("x = 'a' * 300000\ndef main(params): return x") == []


def test_a_skill_is_saved_in_its_own_folder_and_is_approved_because_the_person_made_it(tmp_path):
    store = PersonalSkills(tmp_path)
    saved = store.save(SPEC, GOOD)
    assert saved["slug"] == "summarise-a-file" and saved["status"] == "approved" and saved["version"] == 1 and saved["tools"] == ["read_file"]
    assert (tmp_path / "summarise-a-file" / "skill.py").read_text(encoding="utf-8") == GOOD
    again = store.save({**SPEC, "name": "Summarise a file"}, GOOD)               # the same name: a skill of its own, not a replacement
    assert again["slug"] == "summarise-a-file-2"
    assert [s["slug"] for s in store.list()] == ["summarise-a-file", "summarise-a-file-2"]
    assert "script" not in store.list()[0]


def test_a_new_version_keeps_the_old_and_a_file_changed_by_hand_is_to_approve_again(tmp_path):
    store = PersonalSkills(tmp_path)
    store.save(SPEC, GOOD)
    store.save({**SPEC, "description": "second"}, GOOD + "\n# more\n", slug="summarise-a-file")
    assert store.get("summarise-a-file")["version"] == 2 and store.versions("summarise-a-file") == [1]
    assert store.restore("summarise-a-file", 1)["version"] == 3 and store.get("summarise-a-file")["description"] == "reads a file and summarises it"
    path = tmp_path / "summarise-a-file" / "skill.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# edited outside\n", encoding="utf-8")
    assert store.get("summarise-a-file")["status"] == "pending"
    assert store.approve("summarise-a-file")["status"] == "approved"
    store.note_run("summarise-a-file")                                          # counting a run does not withdraw the approval
    got = store.get("summarise-a-file")
    assert got["uses"] == 1 and got["last_run"] and got["status"] == "approved"


def test_what_cannot_work_is_not_saved(tmp_path):
    store = PersonalSkills(tmp_path)
    for spec, script in (({"name": ""}, GOOD), (SPEC, "x = 1"),
                         ({**SPEC, "params": [{"name": "A B"}]}, GOOD), ({**SPEC, "params": [{"name": "a"}, {"name": "a"}]}, GOOD),
                         ({**SPEC, "timeout": "soon"}, GOOD)):
        with pytest.raises(SkillError):
            store.save(spec, script)
    assert store.list() == []
    for bad in ("../x", "a/b", "", "A"):
        with pytest.raises(SkillError):
            store.get(bad)


def test_a_skill_has_no_time_limit_unless_the_person_sets_one(tmp_path):
    store = PersonalSkills(tmp_path)
    assert store.save(SPEC, GOOD)["timeout"] == 0
    assert store.save({**SPEC, "timeout": 1}, GOOD)["timeout"] == 1
    assert store.save({**SPEC, "timeout": 200000}, GOOD)["timeout"] == 200000


def test_delete_removes_the_folder_and_the_approval(tmp_path):
    store = PersonalSkills(tmp_path)
    store.save(SPEC, GOOD)
    store.delete("summarise-a-file")
    assert store.list() == [] and not (tmp_path / "summarise-a-file").exists() and "summarise-a-file" not in json.loads((tmp_path / "approved.json").read_text())
    with pytest.raises(SkillError):
        store.delete("summarise-a-file")


def test_a_skill_travels_as_one_file_and_arrives_to_be_approved(tmp_path):
    here, there = PersonalSkills(tmp_path / "a"), PersonalSkills(tmp_path / "b")
    here.save({**SPEC, "source": {"conversation": "secret-id"}}, GOOD)
    data = here.export_bytes("summarise-a-file")
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert sorted(names) == ["manifest.json", "skill.json", "skill.py"]
    assert b"secret-id" not in data                                              # where it came from does not travel
    arrived = there.import_bytes(data)
    assert arrived["status"] == "pending" and arrived["name"] == SPEC["name"] and arrived["script"] == GOOD
    assert there.approve(arrived["slug"])["status"] == "approved"


def test_a_file_that_was_changed_or_is_not_a_skill_is_refused(tmp_path):
    store = PersonalSkills(tmp_path / "a")
    store.save(SPEC, GOOD)
    good = store.export_bytes("summarise-a-file")
    other = PersonalSkills(tmp_path / "b")
    changed = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(changed, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name) + (b"\nprint('x')\n" if name == "skill.py" else b""))
    extra = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as src, zipfile.ZipFile(extra, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("evil.exe", b"MZ")
    for data, word in ((changed.getvalue(), "hash"), (extra.getvalue(), "something else"), (b"not a zip", "not a personal"), (b"x" * 700_000, "too large")):
        with pytest.raises(SkillError, match=word):
            other.import_bytes(data)
    assert other.list() == []


def test_names_become_safe_folder_names():
    assert slugify("Rassegna stampa — Ucraina!") == "rassegna-stampa-ucraina"
    assert slugify("   ") == "skill" and slugify("città") == "citt"
