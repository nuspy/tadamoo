"""MCP Apps in the conversation's session: what an app tells the model reaches it on its next turn, as data and once; what a view asks is answered as an event; an app's own tool
call is made under the conversation's permissions through the same tools the model uses, and a page the model's call returns is shown in that call's place."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo import mcp_apps as apps
from tadamoo.tools.registry import EffectClass
from test_session import Scripted, make, say
from test_session import _no_clock  # noqa: F401  (autouse fixture)


class FakeServer:
    tools = [{"name": "save", "inputSchema": {"type": "object"}, "_meta": {"ui": {"visibility": ["app"]}}},
             {"name": "peek", "annotations": {"readOnlyHint": True}, "_meta": {"ui": {"visibility": ["app"]}}},
             {"name": "model_only", "_meta": {"ui": {"visibility": ["model"]}}}]


def host_for(tmp_path):
    return apps.AppHost(lambda: apps.DEFAULTS, lambda server_id: FakeServer(), lambda server_id: "demo", cache=apps.AppCache(tmp_path / "templates"))


def attach(session, host):
    """What the session does at its start when its tools have a host of apps."""
    session.apps = host
    host.emit = lambda event: session.emit({**event, "turn": session._cur_turn()})
    host.run_call = session.run_app_call


def test_what_an_app_says_reaches_the_model_on_its_next_turn_once_framed_as_data(tmp_path):
    client = Scripted(say("one"), say("two"))
    session, ev, store, built = make(tmp_path, client)
    host = host_for(tmp_path)
    attach(session, host)
    host.apps["a1"] = {"name": "Survey", "server_name": "demo"}
    host.handle("context", {"app": "a1", "server": "mcp:demo", "content": [{"type": "text", "text": "The person chose Soup"}]})
    session.send("m1", "what did I choose?")
    ev.wait("done")
    notes = [m["content"] for m in client.sent[0] if m["role"] == "user" and m["content"].startswith("[app context]")]
    assert len(notes) == 1 and "The person chose Soup" in notes[0] and 'The app "Survey"' in notes[0] and "not the person's words" in notes[0]
    assert client.sent[0][-1]["content"] == "what did I choose?"                      # the note comes before the person's message
    session.send("m2", "and now?")
    ev.wait("done", after=ev.kinds().index("done") + 1)
    assert sum(1 for m in client.sent[1] if m["role"] == "user" and m["content"].startswith("[app context]")) == 1       # the same note stays in the history, it is not added again


def test_a_request_of_a_view_is_answered_as_an_event_with_its_id(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted(say("x")))
    attach(session, host_for(tmp_path))
    session.run_app_op("r1", "log", {"app": "a1", "level": "info", "data": "hi"})
    session.run_app_op("r2", "resources/read", {"server": "mcp:demo"})                           # no uri: refused
    session.run_app_op("r3", "unknown", {})
    session.apps = None
    session.run_app_op("r4", "log", {})
    results = [e for e in ev.items if e["event"] == "app_result"]
    assert [(e["id"], e["ok"]) for e in results] == [("r1", True), ("r2", False), ("r3", False), ("r4", False)]
    assert results[1]["code"] == -32602 and results[2]["code"] == -32601 and "no apps" in results[3]["message"]
    assert [e for e in ev.items if e["event"] == "mcp_app_log"][0]["text"] == "hi"


def test_a_tool_call_of_an_app_goes_through_the_tools_the_model_uses_and_the_gate_and_adds_nothing_to_the_conversation(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted(say("x")))
    host = host_for(tmp_path)
    attach(session, host)
    ran = []
    registry = built.agent.tools
    registry.register("call_capability", "calls", {"type": "object", "properties": {"id": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}}},
                      EffectClass.IRREVERSIBLE, lambda id, tool, arguments=None: ran.append(("call", id, tool, arguments)) or f"ran {tool}", untrusted=True, outward=True)
    registry.register("read_capability", "reads", {"type": "object", "properties": {"id": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}}},
                      EffectClass.PURE, lambda id, tool, arguments=None: ran.append(("read", id, tool, arguments)) or f"read {tool}", untrusted=True, outward=True)
    before = len(built.agent.messages)
    answer = host.handle("tools/call", {"app": "a1", "server": "mcp:demo", "name": "save", "arguments": {"x": 1}})
    assert answer["content"][0]["text"] == "ran save"
    answer = host.handle("tools/call", {"app": "a1", "server": "mcp:demo", "name": "peek", "arguments": {}})
    assert answer["content"][0]["text"] == "read peek"                                          # a tool its server declares read-only goes the way the model's read does
    assert ran == [("call", "mcp:demo", "save", {"x": 1}), ("read", "mcp:demo", "peek", {})]
    assert len(built.agent.messages) == before                                                    # nothing was added to what the model reads
    for bad in ("model_only", "missing"):
        with pytest.raises(apps.AppError) as e:
            host.handle("tools/call", {"app": "a1", "server": "mcp:demo", "name": bad, "arguments": {}})
        assert "that an app may call" in str(e.value)
    assert [e["state"] for e in ev.items if e["event"] == "mcp_app_call"][:2] == ["started", "done"]


def test_a_page_the_models_call_returns_is_shown_in_that_calls_place(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted(say("x")))
    host = host_for(tmp_path)
    attach(session, host)
    task = type("T", (), {"turn": "t1", "registered": None})()
    session._on_event({"type": "tool_start", "call_id": "call-7", "name": "call_capability", "arguments": {}, "effect": "irreversible"}, task)
    assert host.current_call == "call-7"
    host.emit({"event": "mcp_app", "app": "a", "call_id": host.current_call})
    session._on_event({"type": "tool_end", "call_id": "call-7", "name": "call_capability", "status": "completed", "decision": "executed", "output": "ok"}, task)
    assert host.current_call == "" and [e for e in ev.items if e["event"] == "mcp_app"][0]["call_id"] == "call-7"
