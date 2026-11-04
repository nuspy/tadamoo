"""The agent writes a personal skill: all the problems come back at once, and the places left to the model are counted."""
from tadamoo.personal_skills.authoring import ai_calls, register_authoring_tool
from tadamoo.personal_skills.store import PersonalSkills
from tadamoo.tools.registry import ToolRegistry

GOOD = "from tadamoo_skill import tool, ai\n\ndef main(params):\n    text = tool('read_file', path=params.file)\n    return ai('summarise', text)\n"


def make(tmp_path):
    store = PersonalSkills(tmp_path / "skills")
    reg = ToolRegistry()
    register_authoring_tool(reg, store)
    return store, reg.get("create_skill").handler


def test_a_good_script_is_saved_as_the_agents_and_the_model_calls_are_counted(tmp_path):
    store, create = make(tmp_path)
    got = create("Read notes", "reads and summarises a file", GOOD, [{"name": "file", "label": "File", "type": "text", "default": "a.txt"}])
    assert got["saved"] and got["ai_calls"] == 1 and "model" in got["note"]
    assert store.get(got["skill"])["source"] == {"agent": True}


def test_a_script_with_problems_is_not_saved_and_the_problems_come_back_with_the_guide(tmp_path):
    store, create = make(tmp_path)
    got = create("Broken", "x", "def helper():\n    pass\n")
    assert not got["saved"] and got["problems"] and "ai(" in got["guide"] and store.list() == []
    assert not create("Syntax", "x", "def main(params:\n")["saved"]


def test_the_ai_calls_are_counted_by_name_not_by_substring():
    assert ai_calls("x = ai('a')\ny = obj.ai('b')\nmain_ai(1)\nai('c')") == 2
