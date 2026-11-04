"""MCP in the Chat: the model reads a tool's schema before it calls, results keep their structure, a tool's own failure is told apart from a
broken server, and a server that stops to ask for a value asks the person (elicitation); tasks of revision 2025-11-25."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_mcp_http import FakeMcpHttp
from tadamoo import capabilities as cap
from tadamoo.mcp_client import MCPClient, MCPError, MCPHttpClient, MCPToolError, _Client, flatten_result
from tadamoo.mcp_elicit import ELICIT_PREFIX, checked_content, elicit_through, form_fields
from tadamoo.stack import AgentConfig, build_agent
from tadamoo.tools.registry import ToolRegistry

HERE = Path(__file__).resolve().parent
LEGACY = str(HERE / "fake_mcp_server.py")
MODERN = str(HERE / "fake_mcp_modern.py")


def stdio(mode="", script=LEGACY):
    return MCPClient([sys.executable, script], env={"FAKE_MCP_MODE": mode})


@pytest.fixture
def connected():
    made = []

    def make(mode="", script=LEGACY, elicit=None):
        c = stdio(mode, script)
        c.elicit = elicit
        c.start()
        c.initialize()
        made.append(c)
        return c

    yield make
    for c in made:
        c.close()


# -- results ---------------------------------------------------------------------------------------------------


def test_a_structured_result_is_kept_next_to_the_text_and_not_shown_twice():
    assert flatten_result({"content": [{"type": "text", "text": "Sunny"}], "structuredContent": {"temp": 21}}) == 'Sunny\nstructured result: {"temp": 21}'
    assert flatten_result({"structuredContent": {"temp": 21}}) == '{"temp": 21}'
    assert flatten_result({"content": [{"type": "text", "text": '{"temp": 21}'}], "structuredContent": {"temp": 21}}) == '{"temp": 21}'


def test_a_tool_that_fails_is_a_tool_error_and_a_broken_call_is_not(connected):
    c = connected()
    with pytest.raises(MCPToolError, match="it broke"):
        c.call_tool("fail", {})
    with pytest.raises(MCPError) as caught:
        c.call_tool("nope", {})
    assert not isinstance(caught.value, MCPToolError)


def test_a_server_that_hands_back_the_same_cursor_forever_is_listed_once_not_fifty_times():
    class Looping(_Client):
        pages = 0

        def send_request(self, method, params=None, timeout=None):
            self.pages += 1
            return {"tools": [{"name": f"t{self.pages}"}], "nextCursor": "same"}

    c = Looping(modern=False)
    assert [t["name"] for t in c.list_tools()] == ["t1", "t2"] and c.pages == 2


# -- the model reads the schema first ---------------------------------------------------------------------------


class Hub:
    """What `call_capability` is given by the factory: a server's tools and a call."""

    def __init__(self, tools, call=None):
        self.tools, self._call = tools, call or (lambda tool, arguments: f"{tool}/{arguments}")

    def call(self, tool, arguments):
        return self._call(tool, arguments)


WEATHER = {"name": "weather", "description": "Forecast for a city", "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}}, "required": ["city"]},
           "outputSchema": {"type": "object", "properties": {"temp": {"type": "integer"}}}}


@pytest.fixture
def registry(tmp_path):
    ws, home = tmp_path / "ws", tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"wx": {"command": "python", "args": ["-m", "wx"]}}}), encoding="utf-8")
    idx = cap.CapabilityIndex(tmp_path / "caps", ws, home=home, appdata=tmp_path / "appdata")

    def make(hub):
        reg = ToolRegistry()
        cap.register_capability_tools(reg, idx, hub_factory=lambda id, cmd: hub)
        return reg
    return make


def test_the_list_shows_each_tools_arguments_and_a_tool_can_be_described_without_calling_it(registry):
    called = []
    reg = registry(Hub([WEATHER], lambda tool, arguments: called.append(tool) or "x"))
    listing = reg.execute("call_capability", {"id": "mcp:wx"})
    assert "weather(city*: string, days: integer): Forecast for a city" in listing
    full = reg.execute("call_capability", {"id": "mcp:wx", "tool": "weather", "describe": True})
    assert '"required": ["city"]' in full and "result schema:" in full and '"temp"' in full and called == []


def test_words_find_tools_of_a_large_server_with_their_whole_definition(registry):
    many = [{"name": f"tool_{i}", "description": f"does thing {i}", "inputSchema": {"type": "object", "properties": {}}} for i in range(60)] + [WEATHER]
    reg = registry(Hub(many))
    listing = reg.execute("call_capability", {"id": "mcp:wx"})
    assert listing.count("tool_") == cap.LIST_TOOLS_MAX and "21 more tools" in listing and "weather(" not in listing
    found = reg.execute("call_capability", {"id": "mcp:wx", "query": "forecast city"})
    assert "weather(city*: string" in found and "input schema:" in found
    assert "No tool of this server matches" in reg.execute("call_capability", {"id": "mcp:wx", "query": "zebra"})


def test_a_call_with_wrong_arguments_comes_back_with_the_definition_so_one_repair_is_enough(registry):
    from tadamoo.tools.registry import ToolArgumentError

    def call(tool, arguments):
        raise ToolArgumentError(f"Tool '{tool}' is missing required argument(s): city.")
    reg = registry(Hub([WEATHER], call))
    out = reg.execute("call_capability", {"id": "mcp:wx", "tool": "weather", "arguments": {}})
    assert "missing required argument(s): city" in out and "input schema:" in out and "weather(city*: string" in out


def test_a_tools_own_failure_and_an_unknown_tool_are_said_plainly(registry):
    def call(tool, arguments):
        raise MCPToolError("quota exceeded")
    reg = registry(Hub([WEATHER], call))
    assert reg.execute("call_capability", {"id": "mcp:wx", "tool": "weather", "arguments": {"city": "Rome"}}) == "The tool 'weather' ran and reported an error: quota exceeded"
    assert "has no tool 'rain'. Its tools: weather" in reg.execute("call_capability", {"id": "mcp:wx", "tool": "rain", "arguments": {}})


def test_a_server_that_runs_through_stdio_answers_with_its_structured_result(connected):
    c = connected("structured")
    assert c.call_tool("data", {}) == 'Sunny\nstructured result: {"temp": 21}'


# -- elicitation ---------------------------------------------------------------------------------------------------


def test_a_server_that_stops_to_ask_gets_the_answer_of_the_person_and_knows_the_client_can_ask(connected):
    seen = []
    c = connected("elicit", elicit=lambda params: seen.append(params) or {"action": "accept", "content": {"name": "Ada"}})
    text = c.call_tool("ask", {})
    assert seen[0]["message"] == "Your name?"
    assert 'caps={"elicitation": {"form": {}}}' in text and '"action": "accept"' in text and '"name": "Ada"' in text


def test_a_declined_or_failed_question_is_told_to_the_server_and_without_a_way_to_ask_it_is_refused(connected):
    c = connected("elicit", elicit=lambda params: {"action": "decline"})
    assert '"action": "decline"' in c.call_tool("ask", {})

    def boom(params):
        raise RuntimeError("no screen")
    assert '"action": "cancel"' in connected("elicit", elicit=boom).call_tool("ask", {})
    assert "answer=null" in connected("elicit").call_tool("ask", {}) and "caps={}" in connected("elicit").call_tool("ask", {})      # not offered, so the request is refused


def test_a_tool_waiting_for_the_person_does_not_run_out_of_time(connected):
    import time

    def slow(params):
        time.sleep(0.9)
        return {"action": "accept", "content": {"name": "Ada"}}
    c = connected("elicit", elicit=slow)
    assert '"action": "accept"' in c.call_tool("ask", {}, timeout=0.5)            # the wait for the person is not counted against the call


def test_the_stateless_revision_retries_with_the_answers_and_the_state(connected):
    c = connected("elicit", MODERN, elicit=lambda params: {"action": "accept", "content": {"who": "me"}})
    text = c.call_tool("echo", {"text": "x"})
    assert '"who": {"action": "accept", "content": {"who": "me"}}' in text and "state=s-9" in text and "elicitation" in text


def test_a_request_in_an_http_event_stream_is_answered_with_a_post():
    server = FakeMcpHttp(sse=True, elicit=True)
    try:
        c = MCPHttpClient(server.url)
        c.elicit = lambda params: {"action": "accept", "content": {"name": "Ada"}}
        c.initialize()
        assert c.call_tool("echo", {"text": "x"}) == 'got {"action": "accept", "content": {"name": "Ada"}}'
        assert server.answers[0]["id"] == "e1"
    finally:
        server.close()


def test_the_form_a_server_asks_for_is_reduced_to_what_a_person_can_fill_and_the_answer_is_checked():
    schema = {"type": "object", "required": ["name"], "properties": {
        "name": {"type": "string", "title": "Your name"}, "age": {"type": "integer"}, "ok": {"type": "boolean"},
        "colour": {"enum": ["red", "blue"]}, "nested": {"type": "object"}, "list": {"type": "array"}}}
    fields = form_fields(schema)
    assert [f["name"] for f in fields] == ["name", "age", "ok", "colour"] and fields[0]["required"] and fields[3]["options"] == ["red", "blue"]
    assert checked_content(fields, {"name": "Ada", "age": 3, "colour": "red", "extra": "dropped"}) == {"name": "Ada", "age": 3, "colour": "red"}
    for bad in ({}, {"name": "Ada", "age": "3"}, {"name": "Ada", "colour": "green"}, {"name": "Ada", "ok": 1}, "text"):
        with pytest.raises(ValueError):
            checked_content(fields, bad)


def test_the_question_goes_to_the_person_with_the_servers_name_and_its_words_and_an_invalid_answer_is_a_cancel():
    asked = []
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def person(answer):
        return elicit_through(lambda payload: asked.append(payload) or answer, "wx")({"message": "Your name?", "requestedSchema": schema})
    assert person(ELICIT_PREFIX + json.dumps({"name": "Ada", "token": "stolen"})) == {"action": "accept", "content": {"name": "Ada"}}
    payload = asked[0]
    assert payload["kind"] == "mcp_elicitation" and payload["free_prefix"] == ELICIT_PREFIX and payload["facts"]["server"] == "wx" and payload["facts"]["message"] == "Your name?"
    assert [o["value"] for o in payload["options"]] == ["decline", "cancel"]
    assert person("decline") == {"action": "decline"} and person("cancel") == {"action": "cancel"} and person("") == {"action": "cancel"}
    assert person(ELICIT_PREFIX + "{}") == {"action": "cancel"} and person(ELICIT_PREFIX + "not json") == {"action": "cancel"}


def test_in_the_chat_a_server_of_the_capabilities_asks_through_the_conversations_question(tmp_path):
    ws, home = tmp_path / "ws", tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    script = tmp_path / "fake_elicit.py"                                      # the server starts with a scrubbed environment: its mode is in the file
    script.write_text(Path(LEGACY).read_text(encoding="utf-8").replace('os.environ.get("FAKE_MCP_MODE", "")', '"elicit"'), encoding="utf-8")
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [str(script)]}}}), encoding="utf-8")
    cfg = AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, report=False, capabilities=True,
                      capabilities_dir=str(tmp_path / "caps"), capabilities_home=str(home))
    built = build_agent(cfg, client=object())
    asked = []
    try:
        index = built.capabilities
        tools = built.agent.tools
        tools.mcp_asker.ask = lambda payload: asked.append(payload) or ELICIT_PREFIX + json.dumps({"name": "Ada"})
        out = tools.execute("call_capability", {"id": "mcp:fake", "tool": "ask", "arguments": {}})
        tools.mcp_asker.ask = None                                              # no way to ask: the server's request is declined
        declined = tools.execute("call_capability", {"id": "mcp:fake", "tool": "ask", "arguments": {}})
    finally:
        built.close()
    assert asked[0]["kind"] == "mcp_elicitation" and asked[0]["facts"]["server"] == "mcp:fake" and asked[0]["facts"]["message"] == "Your name?"
    assert '"name": "Ada"' in out and '"action": "accept"' in out and '"action": "decline"' in declined


# -- tasks ---------------------------------------------------------------------------------------------------------


def test_a_tool_the_server_runs_only_as_a_task_is_asked_for_as_a_task_and_polled_to_its_result(connected):
    c = connected("tasks")
    assert c.list_tools() and c.task_tools == {"job"}
    assert c.call_tool("job", {"n": 1}) == "job done"


def test_a_task_that_does_not_end_in_time_is_cancelled(connected):
    c = connected("tasks")
    c.list_tools()
    with pytest.raises(TimeoutError, match="did not finish"):
        c.call_tool("job", {"n": 1}, timeout=0.01)
