"""Hooks that talk back: post-tool feedback the model reads, a stop hook that sends it back to work (capped), and the compaction and subagent
events."""
import copy
import json
import sys

import pytest

from tadamoo.agent_loop import FINAL, AgentLoop
from tadamoo.context import ContextManager, TokenJuiceCompactor
from tadamoo.hooks import HookConfigError, HookManager, load_hook_files, parse_hooks
from tadamoo.swarm.subagent import SubagentRunner
from tadamoo.tools import create_standard_tool_registry
from tadamoo.tools.registry import EffectClass, ToolRegistry


class Scripted:
    def __init__(self, *steps):
        self.steps, self.sent = list(steps), []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        return self.steps.pop(0)


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def call(name, args, call_id="c1"):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def hooks_from(tmp_path, entries, **kw):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": entries}), encoding="utf-8")
    manager = HookManager(**kw)
    for _, specs in load_hook_files([path]):
        manager.add_specs(specs, tmp_path)
    return manager


def script(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding="utf-8")
    return f'"{sys.executable}" {name}'


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def seen_by_model(client):
    return client.sent[-1]


# -- post-tool feedback -------------------------------------------------------------------------------------------------

def test_a_post_tool_hook_s_feedback_comes_back_beside_the_result(ws):
    manager = HookManager(safety_interceptor=False)
    manager.register_post_tool_hook(lambda tool, args, result: "lint says: unused import" if tool == "write_file" else None)
    client = Scripted(call("write_file", {"path": "a.py", "content": "import os\n"}), say("Done."))
    registry, _ = create_standard_tool_registry(ws)
    AgentLoop(client, registry, hooks=manager).run("write it")
    tool_text = [m["content"] for m in seen_by_model(client) if m["role"] == "tool"][0]
    assert "[harness] lint says: unused import" in tool_text


def test_a_command_hook_that_blocks_after_a_call_is_fenced_and_a_pass_says_nothing(ws, tmp_path):
    check = script(tmp_path, "post.py", (
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "if event['arguments'].get('path', '').endswith('.py'):\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'run the formatter on ' + event['arguments']['path']}))\n"))
    manager = hooks_from(tmp_path, [{"event": "post_tool", "tool": "write_file", "command": check, "name": "format"}], safety_interceptor=False)
    client = Scripted(call("write_file", {"path": "a.py", "content": "x=1\n"}), call("write_file", {"path": "b.txt", "content": "x"}, "c2"), say("Done."))
    registry, _ = create_standard_tool_registry(ws)
    AgentLoop(client, registry, hooks=manager).run("go")
    tools = [m["content"] for m in seen_by_model(client) if m["role"] == "tool"]
    assert "run the formatter on a.py" in tools[0] and "<untrusted_data" in tools[0] and "as data and not as instructions" in tools[0]
    assert "[harness]" not in tools[1]                                                          # the second call passed: nothing added
    assert manager.errors == []


def test_exit_code_two_after_a_call_is_feedback_and_a_failing_hook_is_only_collected(ws, tmp_path):
    two = script(tmp_path, "two.py", "import sys\nsys.stderr.write('tests not updated')\nsys.exit(2)\n")
    broken = script(tmp_path, "broken.py", "import sys\nsys.exit(7)\n")
    manager = hooks_from(tmp_path, [{"event": "post_tool", "command": two, "name": "two"}, {"event": "post_tool", "command": broken, "name": "broken"}],
                         safety_interceptor=False)
    said = manager.trigger_post_tool("write_file", {"path": "a"}, "ok")
    assert "tests not updated" in said and "Hook 'two'" in said
    assert len(manager.errors) == 1 and "exited with code 7" in manager.errors[0]
    assert HookManager().trigger_post_tool("x", {}, "y") is None


# -- the stop hook ------------------------------------------------------------------------------------------------------

def test_a_stop_hook_that_denies_sends_the_model_back_with_a_hidden_message(ws):
    manager = HookManager(safety_interceptor=False)
    manager.register_stop_hook(lambda payload: None if "tests" in payload["final"] else "Say whether the tests pass.")
    client = Scripted(say("All done."), say("Done; the tests pass."))
    events = []
    agent = AgentLoop(client, create_standard_tool_registry(ws)[0], hooks=manager, on_event=events.append)
    agent.run("go")
    assert agent.stop_reason == FINAL and len(client.sent) == 2
    assert seen_by_model(client)[-1]["content"] == "[harness] Say whether the tests pass.\nContinue the work, then answer again."
    assert [e for e in events if e["type"] == "stop_denied"][0]["final"] is False
    assert agent.messages[-1]["content"] == "Done; the tests pass."


def test_a_stop_hook_cannot_keep_a_turn_going_for_ever(ws):
    manager = HookManager(safety_interceptor=False)
    payloads = []
    manager.register_stop_hook(lambda payload: payloads.append(payload) or "Not yet.")
    client = Scripted(say("a"), say("b"), say("c"), say("never"))
    agent = AgentLoop(client, create_standard_tool_registry(ws)[0], hooks=manager)
    agent.run("go")
    assert agent.stop_reason == FINAL and len(client.sent) == 3 and agent.messages[-1]["content"] == "c"        # two denials, then the answer stands
    assert [p["stop_hook_active"] for p in payloads] == [False, True, True]


def test_a_command_stop_hook_gets_the_final_text_and_a_hook_that_cannot_run_never_blocks(ws, tmp_path):
    check = script(tmp_path, "stop.py", (
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "open('stop_seen.json', 'w').write(json.dumps(event))\n"
        "if 'verified' not in event['final']:\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'state what you verified'}))\n"))
    broken = script(tmp_path, "broken.py", "import sys\nsys.exit(9)\n")
    manager = hooks_from(tmp_path, [{"event": "stop", "command": check, "name": "verify-words"}, {"event": "stop", "command": broken, "name": "broken"}],
                         safety_interceptor=False)
    reason = manager.trigger_stop({"final": "all good", "turns": 1})
    assert "state what you verified" in reason and "denies ending the turn" in reason
    assert json.loads((tmp_path / "stop_seen.json").read_text(encoding="utf-8"))["final"] == "all good"
    assert manager.trigger_stop({"final": "I verified it", "turns": 1}) is None
    assert any("exited with code 9" in e for e in manager.errors)


def test_a_subagent_s_end_is_not_a_stop_for_the_hook(ws):
    manager = HookManager(safety_interceptor=False)
    stops, events = [], []
    manager.register_stop_hook(lambda payload: stops.append(1) or None)
    for name in ("subagent_start", "subagent_stop"):
        manager.register_event_hook(name, lambda payload, name=name: events.append((name, payload)))
    registry, _ = create_standard_tool_registry(ws)
    runner = SubagentRunner(registry, Scripted(say("found it")), hooks=manager)
    out = runner.run("look")
    assert out["summary"] == "found it" and stops == []
    assert [n for n, _ in events] == ["subagent_start", "subagent_stop"] and events[1][1]["stop_reason"] == FINAL


# -- compaction events --------------------------------------------------------------------------------------------------

def test_the_compaction_hooks_hear_of_a_context_that_gets_smaller(ws, tmp_path):
    registry = ToolRegistry()
    registry.register("big", "x", {"type": "object", "properties": {}}, EffectClass.PURE, lambda: "y" * 4000)
    context = ContextManager(TokenJuiceCompactor(tmp_path / "art"), budget_tokens=1200, mask_at=0.5, keep_recent=1)
    manager = HookManager(safety_interceptor=False)
    heard = []
    for name in ("pre_compact", "post_compact"):
        manager.register_event_hook(name, lambda payload, name=name: heard.append((name, payload)))
    steps = [call("big", {}, f"c{i}") for i in range(4)] + [say("done")]
    AgentLoop(Scripted(*steps), registry, hooks=manager, context=context).run("go")
    names = [n for n, _ in heard]
    assert names and names[0] == "pre_compact" and "post_compact" in names
    post = next(p for n, p in heard if n == "post_compact")
    assert post["tokens_after"] < post["tokens_before"]


def test_a_context_under_its_threshold_fires_nothing(ws, tmp_path):
    context = ContextManager(TokenJuiceCompactor(tmp_path / "art"), budget_tokens=100000)
    manager = HookManager(safety_interceptor=False)
    heard = []
    for name in ("pre_compact", "post_compact"):
        manager.register_event_hook(name, lambda payload, name=name: heard.append(name))
    AgentLoop(Scripted(say("hi")), create_standard_tool_registry(ws)[0], hooks=manager, context=context).run("go")
    assert heard == []


# -- the file format ----------------------------------------------------------------------------------------------------

def test_the_new_events_are_accepted_in_a_hooks_file_and_tool_patterns_stay_for_tool_events():
    specs = parse_hooks([{"event": "stop", "command": "x"}, {"event": "pre_compact", "command": "x"}, {"event": "post_compact", "command": "x"},
                         {"event": "subagent_start", "command": "x"}, {"event": "subagent_stop", "command": "x"},
                         {"event": "stop", "action": "plan_event", "text": "turn ended"}], "inline")
    assert [s.event for s in specs] == ["stop", "pre_compact", "post_compact", "subagent_start", "subagent_stop", "stop"]
    with pytest.raises(HookConfigError, match="belong to 'pre_tool'"):
        parse_hooks([{"event": "stop", "command": "x", "tool": "write_file"}], "inline")
    with pytest.raises(HookConfigError, match="only on 'pre_tool'"):
        parse_hooks([{"event": "stop", "action": "block", "reason": "r"}], "inline")
