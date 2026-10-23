import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_mcp_http import FakeMcpHttp
from harness.agent_loop import AgentLoop
from harness.mcp_client import MCPClient, MCPError, MCPHttpClient, MCPHub, clean_description
from harness.permissions import PermissionLevel, PermissionManager
from harness.tools.registry import EffectClass, ToolRegistry
from harness.tools import create_standard_tool_registry
from test_agent_loop import ScriptedClient, call, say

FAKE = str(Path(__file__).resolve().parent / "fake_mcp_server.py")


def stdio(env=None):
    return MCPClient([sys.executable, FAKE], env=env)


@pytest.fixture
def client():
    c = stdio()
    c.start()
    c.initialize()
    yield c
    c.close()


def test_handshake_and_pagination(client):
    assert client.server_version == "2025-11-25" and client.server_info["name"] == "fake"
    names = [t["name"] for t in client.list_tools()]
    assert names == ["echo", "fail", "image", "slow", "weather", "bad name!"]      # both pages, in order


def test_calls_return_text_and_report_errors_and_non_text(client):
    assert client.call_tool("echo", {"text": "hi"}) == "echo: hi"
    with pytest.raises(MCPError, match="it broke"):
        client.call_tool("fail", {})
    assert client.call_tool("image", {}) == "[image content, image/png, not shown]"
    with pytest.raises(MCPError, match="unknown tool"):
        client.call_tool("nope", {})


def test_a_slow_tool_uses_the_longer_call_timeout_not_the_request_timeout(client):
    client.request_timeout = 0.5
    assert client.call_tool("slow", {}) == "slow done"
    with pytest.raises(TimeoutError):
        client.send_request("tools/call", {"name": "slow", "arguments": {}}, timeout=0.3)
    assert client.call_tool("echo", {"text": "still alive"}) == "echo: still alive"


def test_an_older_revision_is_accepted_and_an_unknown_one_refused():
    old = stdio({"FAKE_MCP_VERSION": "2025-03-26"})
    old.start()
    try:
        old.initialize()
        assert old.server_version == "2025-03-26"
    finally:
        old.close()
    future = stdio({"FAKE_MCP_VERSION": "2031-01-01"})
    future.start()
    try:
        with pytest.raises(MCPError, match="supported"):
            future.initialize()
    finally:
        future.close()


def test_a_server_that_floods_stderr_does_not_block():
    noisy = stdio({"FAKE_MCP_MODE": "noisy"})
    noisy.start()
    try:
        noisy.initialize()
        assert len(noisy.list_tools()) == 6
        time.sleep(0.2)
        assert 0 < len(noisy.stderr_tail) <= 200
    finally:
        noisy.close()


def test_a_server_request_with_a_colliding_id_is_not_taken_for_our_answer():
    c = stdio({"FAKE_MCP_MODE": "ping"})
    c.start()
    try:
        c.initialize()
        assert c.call_tool("echo", {"text": "x"}) == "echo: x"       # the server's ping used the same id
    finally:
        c.close()


def test_a_dying_server_wakes_the_caller_at_once():
    c = stdio({"FAKE_MCP_MODE": "die"})
    c.start()
    c.initialize()
    t0 = time.monotonic()
    with pytest.raises(MCPError, match="closed"):
        c.call_tool("echo", {"text": "x"}, timeout=30)
    assert time.monotonic() - t0 < 5
    with pytest.raises(MCPError, match="not running|closed"):          # which one depends on how fast the exit is seen
        c.call_tool("echo", {"text": "x"})
    c.close()


def test_the_server_starts_with_a_scrubbed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-canary")
    script = tmp_path / "env_server.py"
    script.write_text("import json,os,sys\nfor l in sys.stdin:\n m=json.loads(l)\n if m.get('id') is not None:\n"
                      "  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':'2025-11-25',"
                      "'serverInfo':{'name':json.dumps(sorted(os.environ))}}}),flush=True)\n", encoding="utf-8")
    c = MCPClient([sys.executable, str(script)], env={"EXPLICIT_VAR": "1"})
    c.start()
    try:
        c.initialize()
        env_names = c.server_info["name"]
        assert "leak-canary" not in env_names and "AWS_SECRET_ACCESS_KEY" not in env_names and "EXPLICIT_VAR" in env_names
    finally:
        c.close()


def test_registered_tools_are_irreversible_untrusted_and_sanitized(client):
    registry = ToolRegistry()
    names = client.register_into(registry, prefix="srv__")
    assert "srv__bad_name_" in names and registry.get("srv__bad name!") is None
    weather = registry.get("srv__weather")
    assert weather.effect == EffectClass.IRREVERSIBLE and weather.untrusted        # the readOnlyHint is not believed
    assert "\x07" not in weather.description and "\x1b" not in weather.description
    assert registry.execute("srv__weather", {"city": "Rome"}) == "Sunny in Rome"
    client.register_into(registry, prefix="srv__")                                  # no duplicates, no crash
    assert clean_description("a\x00b\x1bc\n d" * 400)[:6] == "abc\n d" and len(clean_description("x" * 5000)) == 1000


def test_hub_deferred_search_and_call_keep_the_prompt_short(client):
    hub = MCPHub()
    hub.add("fake", client)
    hub.catalog["fake"] = client.list_tools()
    found = hub.search("weather forecast city")
    assert found[0]["tool"] == "weather" and found[0]["server"] == "fake" and "\x1b" not in found[0]["description"]
    assert hub.search("zzz nothing") == []
    registry = ToolRegistry()
    hub.register_deferred(registry)
    assert registry.names() == ["mcp_call", "mcp_search"]
    assert registry.execute("mcp_call", {"server": "fake", "tool": "echo", "arguments": {"text": "hey"}}) == "echo: hey"
    with pytest.raises(Exception, match="missing required"):
        registry.execute("mcp_call", {"server": "fake", "tool": "echo", "arguments": {}})
    with pytest.raises(KeyError, match="no tool"):
        registry.execute("mcp_call", {"server": "fake", "tool": "ghost", "arguments": {}})
    with pytest.raises(KeyError, match="Unknown or unavailable"):
        registry.execute("mcp_call", {"server": "other", "tool": "echo", "arguments": {}})


def test_hub_connects_what_it_can_and_records_the_rest(tmp_path):
    hub = MCPHub()
    hub.add("good", stdio())
    hub.add("broken", MCPClient([sys.executable, "-c", "import sys; sys.exit(1)"]))
    hub.connect_all()
    try:
        assert "good" in hub.catalog and "broken" in hub.errors and "broken" not in hub.catalog
        with pytest.raises(ValueError):
            hub.add("good", stdio())
        with pytest.raises(ValueError):
            hub.add("bad name", stdio())
    finally:
        hub.close()


def test_mcp_results_reach_the_model_fenced_and_taint_the_session(client, tmp_path):
    registry, _ = create_standard_tool_registry(tmp_path)
    hub = MCPHub()
    hub.add("fake", client)
    hub.catalog["fake"] = client.list_tools()
    hub.register_deferred(registry)
    perms = PermissionManager(level=PermissionLevel.AUTO, sensitive_data=True)
    agent = AgentLoop(ScriptedClient(call("mcp_call", {"server": "fake", "tool": "echo", "arguments": {"text": "x"}}), say("ok")),
                      registry, permissions=perms)
    agent.run("go")
    tool_msg = [m for m in agent.messages if m["role"] == "tool"][0]["content"]
    assert tool_msg.startswith("<untrusted_data source=\"mcp_call\">") and perms.rule_of_two_broken


# -- Streamable HTTP -------------------------------------------------------------------------------

@pytest.mark.parametrize("sse", [False, True])
def test_streamable_http_json_and_event_stream(sse):
    fake = FakeMcpHttp(sse=sse, token="tok")
    try:
        c = MCPHttpClient(fake.url, token="tok")
        c.initialize()
        assert c.session_id == "sess-123" and c.server_version == "2025-11-25"
        assert [t["name"] for t in c.list_tools()] == ["echo"]
        assert c.call_tool("echo", {"text": "hi"}) == "echo: hi"
        headers = [h for m, h, b in fake.requests if m == "POST" and b.get("method") == "tools/call"][0]
        assert headers["mcp-session-id"] == "sess-123" and headers["mcp-protocol-version"] == "2025-11-25"
        c.close()
        assert fake.deleted
    finally:
        fake.close()


def test_http_errors_are_clear():
    fake = FakeMcpHttp(token="right")
    try:
        with pytest.raises(MCPError, match="401"):
            MCPHttpClient(fake.url, token="wrong").initialize()
        c = MCPHttpClient(fake.url, token="right")
        c.initialize()
        fake.session = "another"
        with pytest.raises(MCPError, match="expired"):
            c.list_tools()
    finally:
        fake.close()
    with pytest.raises(MCPError, match="Cannot reach"):
        MCPHttpClient("http://127.0.0.1:59995/mcp").initialize()


@pytest.mark.parametrize("url", ["http://mcp.example.com/mcp", "ftp://x/y", "file:///etc/passwd", "https:///x"])
def test_remote_servers_need_https(url):
    with pytest.raises(ValueError):
        MCPHttpClient(url)
    assert MCPHttpClient("https://mcp.example.com/mcp") and MCPHttpClient("http://localhost:8811/mcp")
