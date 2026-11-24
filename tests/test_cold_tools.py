"""Cold tools: one line and a loose schema in the list, the whole definition from tool_help, the real schema still checking the call."""
import json

import pytest

from tadamoo import cold_tools
from tadamoo.tools.registry import EffectClass, ToolArgumentError, ToolRegistry

FULL = {"type": "object", "properties": {"path": {"type": "string"}, "edits": {"type": "array"}}, "required": ["path", "edits"]}
LONG = "Changes a Word document in the workspace. " + "Every address refers to the document as it was before this call. " * 20


def registry():
    reg = ToolRegistry()
    reg.register("docx_edit", LONG, FULL, EffectClass.REVERSIBLE, lambda path, edits: f"{path}:{len(edits)}")
    reg.register("read_file", "Reads a file.", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, EffectClass.PURE, lambda path: path)
    return reg


def listed(reg):
    return {s["function"]["name"]: s["function"] for s in reg.schemas()}


def test_a_cold_tool_is_listed_with_one_line_and_the_skeleton_of_its_schema_and_the_others_are_not_touched():
    reg = registry()
    before = listed(reg)
    assert cold_tools.apply_cold(reg, ("docx_edit", "nothing_here")) == ["docx_edit"]
    now = listed(reg)
    assert now["read_file"] == before["read_file"]
    assert now["docx_edit"]["parameters"] == {"type": "object", "properties": {"path": {"type": "string"}, "edits": {"type": "array", "items": {"type": "object"}},
                                                             "description": {"type": "string"}},       # the sentence for the person every tool offers
                                              "required": ["path", "edits"]}
    assert now["docx_edit"]["description"].startswith("Changes a Word document in the workspace.") and "tool_help('docx_edit')" in now["docx_edit"]["description"]
    assert len(json.dumps(now["docx_edit"])) < len(json.dumps(before["docx_edit"])) / 3
    assert "tool_help" in now


def test_the_list_is_the_same_every_time_so_the_cache_of_the_prefix_stays():
    reg = registry()
    cold_tools.apply_cold(reg, ("docx_edit",))
    assert json.dumps(reg.schemas()) == json.dumps(reg.schemas())


def test_tool_help_gives_the_whole_definition_of_a_cold_tool_and_names_the_cold_ones_for_another():
    reg = registry()
    cold_tools.apply_cold(reg, ("docx_edit",))
    text = reg.execute("tool_help", {"name": "docx_edit"})
    assert LONG.strip() in text and json.dumps(FULL) in text
    other = reg.execute("tool_help", {"name": "read_file"})
    assert "not a cold tool" in other and "docx_edit" in other


def test_a_wrong_call_to_a_cold_tool_is_refused_by_the_real_schema_and_says_where_to_look():
    reg = registry()
    cold_tools.apply_cold(reg, ("docx_edit",))
    assert reg.execute("docx_edit", {"path": "a.docx", "edits": [1, 2]}) == "a.docx:2"
    with pytest.raises(ToolArgumentError) as e:
        reg.execute("docx_edit", {"path": "a.docx"})
    assert "tool_help('docx_edit')" in str(e.value)
    with pytest.raises(ToolArgumentError) as e2:
        reg.validate("docx_edit", {"edits": []})
    assert "tool_help('docx_edit')" in str(e2.value)


def test_nothing_changes_without_the_switch():
    reg = registry()
    assert not any(t.cold for t in (reg.get("docx_edit"), reg.get("read_file"))) and "tool_help" not in listed(reg)
    with pytest.raises(ToolArgumentError) as e:
        reg.execute("docx_edit", {"path": "a.docx"})
    assert "tool_help" not in str(e.value)


def test_the_real_stack_lists_fewer_characters_with_the_switch(tmp_path):
    from tadamoo.stack import AgentConfig, build_agent
    sizes = {}
    for cold in (False, True):
        ws = tmp_path / f"ws{cold}"
        ws.mkdir()
        built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / f"d{cold}"), use_memory=False, cold_tools=cold), client=object())
        try:
            sizes[cold] = len(json.dumps(built.agent.tools.schemas()))
            names = [s["function"]["name"] for s in built.agent.tools.schemas()]
        finally:
            built.close()
        assert ("tool_help" in names) is cold
    assert sizes[True] < sizes[False] * 0.7


def test_the_evaluation_carries_the_switch_and_a_resumed_run_cannot_mix_the_two_arms():
    from tadamoo.eval.runner import EvalRunner
    from tadamoo.eval.tasks import load_task_set
    tasks = load_task_set("documents")[1]
    a, b = EvalRunner(client=object()), EvalRunner(client=object(), cold_tools=True)
    assert a.fingerprint(tasks, 5) != b.fingerprint(tasks, 5)


def test_the_skeleton_keeps_names_types_enums_and_required_and_drops_the_rest():
    from tadamoo.tools.registry import skeleton
    full = {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "string", "description": "long text", "enum": ["x", "y"]},
        "b": {"type": "array", "description": "d", "items": {"type": "object", "properties": {"k": {"type": "string"}}, "required": ["k"]}},
        "c": {"type": "array", "items": {"type": "string"}},
        "d": {"type": "integer", "minimum": 1}}}
    assert skeleton(full) == {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "string", "enum": ["x", "y"]}, "b": {"type": "array", "items": {"type": "object"}}, "c": {"type": "array", "items": {"type": "string"}}, "d": {"type": "integer"}}}
