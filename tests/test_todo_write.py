"""todo_write: a small checklist the model keeps, in the fixed group of tools, with one reminder before a turn ends on open items."""
import copy
import json

import pytest

from harness.agent_loop import FINAL, AgentLoop
from harness.permissions import PermissionLevel, PermissionManager
from harness.stack import AgentConfig, build_agent
from harness.todo import TodoList
from harness.tools import create_standard_tool_registry
from harness.tools.registry import STABLE


class Scripted:
    def __init__(self, *steps):
        self.steps, self.sent = list(steps), []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        return self.steps.pop(0)


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def todo(items, call_id="t1"):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "todo_write", "arguments": json.dumps({"todos": items})}}]}}]}


def item(content, status="pending"):
    return {"content": content, "status": status}


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def test_the_list_is_replaced_whole_and_a_bad_list_changes_nothing():
    checklist = TodoList()
    text = checklist.write([item("read the code", "completed"), item("fix it", "in_progress"), item("test it")])
    assert text.startswith("Checklist saved: 1 done, 1 in progress, 1 to do.") and "[x] read the code" in text and "[~] fix it" in text and "[ ] test it" in text
    for bad, why in (("nope", "list"), ([item("a", "in_progress"), item("b", "in_progress")], "Only one"), ([{"status": "pending"}], "needs a text"),
                     ([item("a", "later")], "status"), ([item("x")] * 51, "at most 50"), ([5], "needs a text")):
        with pytest.raises(ValueError, match=why):
            checklist.write(bad)
    assert [it["content"] for it in checklist.items] == ["read the code", "fix it", "test it"]
    assert [it["content"] for it in checklist.open_items()] == ["fix it", "test it"] and checklist.render(open_only=True) == "[~] fix it\n[ ] test it"
    assert checklist.write(["a plain string"]).startswith("Checklist saved: 0 done, 0 in progress, 1 to do.")
    long = TodoList()
    long.write([item("x" * 500)])
    assert len(long.items[0]["content"]) == 200


def test_the_tool_is_in_the_fixed_group_so_the_start_of_the_prompt_does_not_move(ws):
    registry, _ = create_standard_tool_registry(ws)
    assert registry.get("todo_write").tier == STABLE and hasattr(registry, "todo")
    before = json.dumps(registry.schemas(), sort_keys=True)
    registry.execute("todo_write", {"todos": [item("one")]})
    assert json.dumps(registry.schemas(), sort_keys=True) == before                              # using it never changes the tool list
    registry.register("zzz_late", "x", {"type": "object", "properties": {}}, registry.get("todo_write").effect, lambda: "x")
    names = [s["function"]["name"] for s in registry.schemas()]
    assert names.index("todo_write") < names.index("zzz_late") and names[-1] == "zzz_late"


def test_a_built_agent_has_it_at_every_permission_level_because_it_touches_nothing(tmp_path, ws):
    for mode in ("plan", "read_only", "default", "auto"):
        built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / mode), use_memory=False, permission_mode=mode), client=object())
        try:
            assert built.agent.permissions.evaluate("todo_write", built.agent.tools.get("todo_write").effect, {"todos": []})[0], mode
        finally:
            built.close()


def test_the_loop_shows_the_list_and_reminds_the_model_once_of_open_items(ws):
    registry, _ = create_standard_tool_registry(ws)
    client = Scripted(todo([item("write", "in_progress"), item("test")]), say("All done."), say("Writing is done; testing is not needed."))
    events = []
    agent = AgentLoop(client, registry, on_event=events.append)
    agent.run("do it")
    assert agent.stop_reason == FINAL and len(client.sent) == 3
    reminder = client.sent[2][-1]["content"]
    assert reminder.startswith("[harness] Your checklist still has unfinished items") and "[~] write" in reminder and "[ ] test" in reminder
    assert [e["items"][0]["content"] for e in events if e["type"] == "todo"] == ["write"] and any(e["type"] == "todo_open" for e in events)
    assert agent.messages[-1]["content"] == "Writing is done; testing is not needed."            # one reminder, then the answer stands


def test_a_finished_list_or_no_list_ends_the_turn_at_once(ws):
    registry, _ = create_standard_tool_registry(ws)
    client = Scripted(todo([item("write", "completed")]), say("Done."))
    AgentLoop(client, registry).run("do it")
    assert len(client.sent) == 2
    client2 = Scripted(say("Hello."))
    AgentLoop(client2, create_standard_tool_registry(ws)[0]).run("hi")
    assert len(client2.sent) == 1


def test_a_list_that_is_refused_comes_back_as_an_error_the_model_can_read(ws):
    registry, _ = create_standard_tool_registry(ws)
    client = Scripted(todo([item("a", "in_progress"), item("b", "in_progress")]), say("Fixed my list."))
    AgentLoop(client, registry, permissions=PermissionManager(level=PermissionLevel.READ_ONLY)).run("go")
    assert "Only one item can be in_progress" in [m["content"] for m in client.sent[1] if m["role"] == "tool"][0]
