"""Repair of tool calls that are slightly wrong: JSON, value types, tool names; and what a batch with bad calls in it does."""
import copy
import json

import pytest

from tadamoo.agent_loop import AgentLoop, FINAL, LOOP
from tadamoo.audit import AuditLedger
from tadamoo.tools import create_standard_tool_registry
from tadamoo.tools.registry import EffectClass, ToolRegistry
from tadamoo.tools.repair import coerce_arguments, coerce_value, repair_json, resolve_tool_name

NAMES = ["read_file", "write_file", "edit_file", "shell_command", "list_directory", "browse_url", "search_web"]


# -- JSON --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"a": 1,}', {"a": 1}),
    ('{"a": [1, 2,],}', {"a": [1, 2]}),
    ('{"a": "line one\nline two"}', {"a": "line one\nline two"}),
    ('{"a": "tab\there"}', {"a": "tab\there"}),
    ('{"a": [1, 2', {"a": [1, 2]}),
    ('{"a": {"b": [1, {"c": 2', {"a": {"b": [1, {"c": 2}]}}),
    ('{"a": 1}}', {"a": 1}),
    ('{"a": [1]}]}', {"a": [1]}),
    ('  {"a": "x,}"}  ', {"a": "x,}"}),
    ('{"p": "x", "c": "l1\nl2",}', {"p": "x", "c": "l1\nl2"}),
])
def test_json_is_mended_by_passes(text, expected):
    assert repair_json(text) == expected


@pytest.mark.parametrize("text", ['{"a": "cut in a string', '{"a":', '{"a": 1, "b"', '{"a": tru', "not json at all", "", '{"a": 1} and then words'])
def test_unrecoverable_text_is_never_guessed(text):
    assert repair_json(text) is None


def test_a_comma_inside_a_string_is_not_a_trailing_comma():
    assert repair_json('{"a": "x, ]", "b": [1,]}') == {"a": "x, ]", "b": [1]}


# -- value types -------------------------------------------------------------------------------------------------------

SCHEMA = {"properties": {
    "n": {"type": "integer"}, "x": {"type": "number"}, "f": {"type": "boolean"}, "l": {"type": "array", "items": {"type": "string"}},
    "ints": {"type": "array", "items": {"type": "integer"}}, "o": {"type": "object"}, "id": {"type": "string"},
    "mode": {"type": "string", "enum": ["a", "b"]}, "level": {"type": "integer", "enum": [1, 2]}}}


@pytest.mark.parametrize("given,expected", [
    ({"n": "42"}, {"n": 42}), ({"n": " -7 "}, {"n": -7}), ({"n": 3.0}, {"n": 3}),
    ({"x": "3.5"}, {"x": 3.5}), ({"x": "2"}, {"x": 2}), ({"x": "1e3"}, {"x": 1000.0}),
    ({"f": "true"}, {"f": True}), ({"f": "False"}, {"f": False}),
    ({"l": '["a", "b"]'}, {"l": ["a", "b"]}), ({"l": "solo"}, {"l": ["solo"]}),
    ({"ints": ["1", "2"]}, {"ints": [1, 2]}), ({"ints": 5}, {"ints": [5]}), ({"ints": "3"}, {"ints": [3]}),
    ({"o": '{"k": 1}'}, {"o": {"k": 1}}),
])
def test_values_take_the_type_the_schema_asks_for(given, expected):
    out, changed = coerce_arguments(SCHEMA, given)
    assert out == expected and changed


@pytest.mark.parametrize("given", [
    {"id": "00123"}, {"id": 123}, {"n": "007"}, {"n": "4.5"}, {"n": "abc"}, {"x": "nan"}, {"f": "yes"}, {"f": 1}, {"n": True},
    {"mode": "a"}, {"mode": 1}, {"level": "1"}, {"o": "not json"}, {"l": None}, {"ints": "x"}, {"n": None},
])
def test_what_is_in_doubt_is_left_as_it_was(given):
    out, changed = coerce_arguments(SCHEMA, given)
    assert out == given and not changed


def test_an_enum_value_and_a_string_id_are_never_changed():
    assert coerce_value({"type": "integer", "enum": [1, 2]}, "1") == ("1", False)
    assert coerce_value({"type": "string"}, "00123") == ("00123", False)


# -- names -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("written", [
    "Read_File", "ReadFile", "read-file", "read file", "readfile", "read_file_tool", "ReadFileTool", "functions.read_file", "tools:read_file",
    'read_file"', "read_file>", "<read_file>", "`read_file`", "read_file(path)", "functions.ReadFile", "raed_file",
])
def test_a_name_that_is_nearly_right_is_resolved(written):
    assert resolve_tool_name(written, NAMES) == ("read_file", [])


def test_an_unknown_name_lists_near_matches_and_not_the_catalog():
    found, near = resolve_tool_name("functions.read", NAMES)
    assert found is None and near == ["read_file"]
    found, near = resolve_tool_name("rm_everything", NAMES)
    assert found is None and near == []
    assert resolve_tool_name("shell", NAMES) == (None, ["shell_command"])


def test_another_word_is_never_taken_for_a_misspelling():
    assert resolve_tool_name("read_file", ["write_file", "edit_file"])[0] is None          # a read is not a write
    assert resolve_tool_name("write_files", ["write_file", "edit_file"]) == ("write_file", [])
    assert resolve_tool_name("search", NAMES)[0] is None and resolve_tool_name("search", NAMES)[1] == ["search_web"]


def test_two_tools_that_fit_equally_are_not_chosen_between():
    assert resolve_tool_name("read_files", ["read_file", "read_filed"])[0] is None
    assert resolve_tool_name("readfile", ["read_file", "ReadFile"])[0] is None


# -- in the loop -------------------------------------------------------------------------------------------------------

class Client:
    def __init__(self, *steps):
        self.steps, self.sent = list(steps), []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        return self.steps.pop(0)


def msg(*calls, content=""):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a)}}
                                 for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": message}]}


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    return root


def make(ws, client, tmp_path, **kw):
    registry, _ = create_standard_tool_registry(ws)
    audit = AuditLedger(tmp_path / "audit.jsonl")
    return AgentLoop(client=client, tool_registry=registry, audit=audit, **kw), audit


def results(agent):
    return [m["content"] for m in agent.messages if m["role"] == "tool"]


def executed(audit):
    return [r["details"] for r in audit.records if r["action"] == "tool_executed"]


def test_json_with_a_trailing_comma_runs_and_the_audit_says_it_was_mended(ws, tmp_path):
    agent, audit = make(ws, Client(msg(("write_file", '{"path": "b.txt", "content": "hi",}')), msg(content="done")), tmp_path)
    turns = agent.run("go")
    assert (ws / "b.txt").read_text(encoding="utf-8") == "hi" and turns[0].tool_calls[0].repaired
    assert executed(audit)[0]["repaired"] is True
    assert agent.messages[2]["tool_calls"][0]["function"]["arguments"] == '{"path": "b.txt", "content": "hi"}'      # the conversation holds the call as it was run


def test_a_call_that_was_right_is_not_marked(ws, tmp_path):
    agent, audit = make(ws, Client(msg(("write_file", {"path": "b.txt", "content": "hi"})), msg(content="done")), tmp_path)
    turns = agent.run("go")
    assert not turns[0].tool_calls[0].repaired and "repaired" not in executed(audit)[0]


def test_text_with_raw_line_breaks_in_a_string_runs(ws, tmp_path):
    agent, audit = make(ws, Client(msg(("write_file", '{"path": "b.txt", "content": "l1\nl2"}')), msg(content="done")), tmp_path)
    agent.run("go")
    assert (ws / "b.txt").read_text(encoding="utf-8") == "l1\nl2"


def test_a_json_text_inside_a_json_string_is_read_as_the_object(ws, tmp_path):
    agent, audit = make(ws, Client(msg(("write_file", json.dumps('{"path": "b.txt", "content": "x"}'))), msg(content="done")), tmp_path)
    agent.run("go")
    assert (ws / "b.txt").read_text(encoding="utf-8") == "x" and executed(audit)[0]["repaired"]


def test_a_tool_name_in_the_wrong_shape_is_run_under_the_right_one(ws, tmp_path):
    agent, audit = make(ws, Client(msg(("functions.ReadFile", {"path": "a.txt"})), msg(content="done")), tmp_path)
    turns = agent.run("go")
    assert turns[0].tool_calls[0].tool_name == "read_file" and "one" in results(agent)[0]
    assert executed(audit)[0]["tool"] == "read_file" and executed(audit)[0]["repaired"] is True


def test_a_value_of_the_wrong_type_is_read_as_the_right_one_before_the_checks(ws, tmp_path):
    agent, audit = make(ws, Client(msg(("read_file", {"path": "a.txt", "offset": "2", "limit": "1"})), msg(content="done")), tmp_path)
    turns = agent.run("go")
    assert turns[0].tool_calls[0].arguments == {"path": "a.txt", "offset": 2, "limit": 1} and "two" in results(agent)[0]
    assert executed(audit)[0]["repaired"] is True and turns[0].tool_calls[0].repaired


def test_what_cannot_be_read_is_returned_to_the_model_and_nothing_runs(ws, tmp_path):
    agent, audit = make(ws, Client(msg(("write_file", '{"path": "b.txt", "content": "cut in a str')), msg(content="done")), tmp_path)
    agent.run("go")
    assert "not valid JSON" in results(agent)[0] and not (ws / "b.txt").exists()


def test_a_wrong_value_that_is_in_doubt_still_fails_the_check(ws, tmp_path):
    agent, _ = make(ws, Client(msg(("read_file", {"path": "a.txt", "offset": "two"})), msg(content="done")), tmp_path)
    agent.run("go")
    assert "must be of type integer" in results(agent)[0]


def test_an_unknown_tool_says_what_is_near_and_not_the_whole_list(ws, tmp_path):
    agent, _ = make(ws, Client(msg(("search_file", {"path": "a.txt"}), ("zzz", {})), msg(content="done")), tmp_path)
    agent.run("go")
    first, second = results(agent)
    assert "Unknown tool 'search_file'" in first and "Did you mean search_web" in first and first.count(",") <= 2
    assert "No tool has a similar name" in second and "read_file" not in second


def test_a_batch_runs_its_valid_calls_and_errors_only_the_invalid_ones(ws, tmp_path):
    agent, audit = make(ws, Client(msg(("write_file", {"path": "ok1.txt", "content": "1"}), ("write_file", {"path": "bad.txt"}), ("zzz", {}),
                                       ("write_file", {"path": "ok2.txt", "content": "2"})), msg(content="done")), tmp_path)
    turns = agent.run("go")
    assert (ws / "ok1.txt").exists() and (ws / "ok2.txt").exists() and not (ws / "bad.txt").exists()
    assert [c.decision for c in turns[0].tool_calls] == ["executed", "invalid", "invalid", "executed"] and agent.stop_reason == FINAL


def test_strikes_advance_only_when_no_call_of_the_turn_is_valid(ws, tmp_path):
    bad = ("write_file", {"path": "bad.txt"})
    good = ("write_file", {"path": "g{}.txt", "content": "x"})
    steps = [msg(bad, ("write_file", {"path": f"g{i}.txt", "content": "x"})) for i in range(6)] + [msg(content="done")]
    agent, _ = make(ws, Client(*steps), tmp_path)
    agent.run("go")
    assert agent.stop_reason == FINAL                          # the same error six times, but every turn also had a call that worked
    steps = [msg(bad) for _ in range(6)] + [msg(content="never")]
    agent, _ = make(ws, Client(*steps), tmp_path)
    agent.run("go")
    assert agent.stop_reason == LOOP                           # the same error with nothing that worked
    assert good


def test_the_registry_checks_after_the_safe_coercions_and_executes_with_them():
    registry = ToolRegistry()
    seen = []
    registry.register("add", "adds", {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]},
                      EffectClass.PURE, lambda a, b: seen.append((a, b)) or a + b)
    assert registry.validate("add", {"a": "17", "b": "25"}) == {"a": 17, "b": 25}
    assert registry.execute("add", {"a": "17", "b": "25"}) == 42 and seen == [(17, 25)]
    assert registry.coerce("add", {"a": 1, "b": 2}) == ({"a": 1, "b": 2}, False)
    assert registry.resolve("Add_Tool") == ("add", [])
