"""The stateless MCP revision (2026-07-28) and the detection of the era a server speaks."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_mcp_http import FakeMcpHttp
from fake_mcp_modern_http import FakeMcpModernHttp
from harness.mcp_client import (MCPClient, MCPError, MCPHttpClient, MCPHub, annotated_values, header_annotations, header_value,
                                is_modern_error)
from harness.tools.registry import ToolRegistry

MODERN = str(Path(__file__).resolve().parent / "fake_mcp_modern.py")
LEGACY = str(Path(__file__).resolve().parent / "fake_mcp_server.py")


def stdio(mode="", supported=None, modern=True, script=MODERN):
    env = {"FAKE_MCP_MODE": mode}
    if supported:
        env["FAKE_MCP_SUPPORTED"] = supported
    c = MCPClient([sys.executable, script], env=env, modern=modern)
    c.start()
    return c


def received(client):
    time.sleep(0.2)
    return [l for l in client.stderr_tail if l.startswith("recv")]


@pytest.fixture
def modern():
    c = stdio()
    c.initialize()
    yield c
    c.close()


# -- stdio ------------------------------------------------------------------------------------------------------------

def test_a_stateless_server_is_detected_and_no_handshake_is_sent(modern):
    assert (modern.era, modern.server_version) == ("modern", "2026-07-28")
    assert modern.server_info == {"name": "fake-modern", "version": "2"} and modern.instructions == "Use echo for echoing."
    assert modern.capabilities == {"tools": {}}
    seen = received(modern)
    assert seen == ["recv server/discover id=1"]                         # not initialize, not notifications/initialized


def test_every_request_carries_its_version_identity_and_capabilities(modern):
    meta = json.loads(modern.call_tool("meta", {}))
    assert meta == {"io.modelcontextprotocol/clientCapabilities": {}, "io.modelcontextprotocol/clientInfo": {"name": "BasaltHarness", "version": "1.0"},
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    assert [t["name"] for t in modern.list_tools()] == ["echo", "meta", "fail"]          # both pages
    assert modern.call_tool("echo", {"text": "hi"}) == "echo: hi"
    with pytest.raises(MCPError, match="it broke"):
        modern.call_tool("fail", {})


def test_state_the_server_hands_back_is_echoed_exactly_on_a_retry_with_a_new_id():
    c = stdio("state")
    try:
        c.initialize()
        assert c.call_tool("echo", {"text": "x"}) == "echo: x (state opaque-state-1)"
        calls = [l for l in received(c) if "tools/call" in l]
        assert len(calls) == 2 and calls[0].split("id=")[1].split()[0] != calls[1].split("id=")[1].split()[0]
        assert "state=opaque-state-1" in calls[1] and "state=" not in calls[0]
    finally:
        c.close()


def test_a_request_for_input_the_client_cannot_give_is_an_error_not_a_loop():
    c = stdio("elicit")
    try:
        c.initialize()
        with pytest.raises(MCPError, match="sampling, elicitation or roots"):
            c.call_tool("echo", {"text": "x"})
        assert len([l for l in received(c) if "tools/call" in l]) == 1
    finally:
        c.close()


def test_an_unknown_result_type_is_invalid():
    c = stdio("weird")
    try:
        c.initialize()
        with pytest.raises(MCPError, match="unknown result type"):
            c.call_tool("echo", {"text": "x"})
    finally:
        c.close()


def test_a_server_of_both_eras_that_does_not_speak_ours_is_spoken_to_the_old_way():
    c = stdio("dual", supported="2025-11-25")                            # discover: unsupported version, lists an older one
    try:
        c.initialize()
        assert (c.era, c.server_version, c.server_info["name"]) == ("legacy", "2025-11-25", "fake-dual")
        assert [l.split(" id")[0] for l in received(c)] == ["recv server/discover", "recv initialize", "recv notifications/initialized"]
    finally:
        c.close()


def test_a_server_that_speaks_nothing_we_know_is_refused_with_what_it_does_speak():
    c = stdio("unsupported", supported="1999-01-01")
    try:
        with pytest.raises(MCPError, match="1999-01-01"):
            c.initialize()
    finally:
        c.close()


def test_a_legacy_server_is_detected_by_the_failed_probe():
    c = stdio(script=LEGACY)
    try:
        c.initialize()
        assert (c.era, c.server_version) == ("legacy", "2025-11-25")
        assert c.call_tool("echo", {"text": "old"}) == "echo: old"
        assert "_meta" not in json.dumps(c.send_request("tools/list", {}))          # nothing modern leaks into legacy traffic
    finally:
        c.close()


def test_the_probe_can_be_turned_off():
    c = stdio("dual", modern=False)
    try:
        c.initialize()
        assert c.era == "legacy" and "recv server/discover" not in [l.split(" id")[0] for l in received(c)]
    finally:
        c.close()


def test_a_request_that_times_out_is_cancelled_at_the_server():
    c = stdio("slow")
    try:
        c.initialize()
        with pytest.raises(TimeoutError):
            c.call_tool("echo", {"text": "x"}, timeout=0.4)
        assert any(l.startswith("recv notifications/cancelled") and "cancels=2" in l for l in received(c))
    finally:
        c.close()


def test_a_hub_connects_servers_of_both_eras_side_by_side():
    hub = MCPHub()
    hub.add("new", MCPClient([sys.executable, MODERN]))
    hub.add("old", MCPClient([sys.executable, LEGACY]))
    try:
        hub.connect_all()
        assert hub.errors == {} and hub.servers["new"].era == "modern" and hub.servers["old"].era == "legacy"
        assert hub.call("new", "echo", {"text": "a"}) == "echo: a" and hub.call("old", "echo", {"text": "b"}) == "echo: b"
    finally:
        hub.close()


# -- header encoding and annotations ----------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("us-west1", "us-west1"),
    ("Hello, 世界", "=?base64?SGVsbG8sIOS4lueVjA==?="),
    (" padded ", "=?base64?IHBhZGRlZCA=?="),
    ("line1\nline2", "=?base64?bGluZTEKbGluZTI=?="),
    ("=?base64?literal?=", "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?="),
    ("", "=?base64??="),
])
def test_header_values_follow_the_specs_table(value, expected):
    assert header_value(value) == expected


def test_only_properly_placed_annotations_are_accepted():
    prop = lambda **kw: {"type": "object", "properties": {"a": {"type": "string", **kw}}}
    assert header_annotations(prop(**{"x-mcp-header": "Region"})) == [("Region", ("a",))]
    assert header_annotations({"type": "object", "properties": {"o": {"type": "object", "properties": {"s": {"type": "integer", "x-mcp-header": "Deep"}}}}}) == [("Deep", ("o", "s"))]
    assert header_annotations({"type": "object", "properties": {"a": {"type": "string"}}}) == []
    for bad in (prop(**{"x-mcp-header": ""}), prop(**{"x-mcp-header": "a b"}), prop(**{"x-mcp-header": "a\nb"}), prop(**{"x-mcp-header": 5}),
                {"type": "object", "properties": {"n": {"type": "number", "x-mcp-header": "N"}}},
                {"type": "object", "properties": {"x": {"type": "array", "items": {"type": "string", "x-mcp-header": "X"}}}},
                {"type": "object", "properties": {"x": {"anyOf": [{"type": "string", "x-mcp-header": "X"}]}}},
                {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "Same"}, "b": {"type": "string", "x-mcp-header": "SAME"}}},
                {"type": "object", "x-mcp-header": "Root"}):
        assert header_annotations(bad) is None, bad


def test_annotated_values_are_mirrored_and_null_or_missing_ones_are_left_out():
    ann = [("Region", ("region",)), ("Limit", ("limit",)), ("Dry", ("dry",)), ("Deep", ("o", "s")), ("Big", ("big",))]
    args = {"region": "us-west1", "limit": 3, "dry": False, "o": {"s": "ünï"}, "big": 2 ** 60}
    assert annotated_values(args, ann) == {"Mcp-Param-Region": "us-west1", "Mcp-Param-Limit": "3", "Mcp-Param-Dry": "false",
                                           "Mcp-Param-Deep": header_value("ünï")}
    assert annotated_values({"region": None}, ann) == {}


# -- Streamable HTTP --------------------------------------------------------------------------------------------------

@pytest.fixture
def http_server():
    made = []

    def make(**kw):
        server = FakeMcpModernHttp(**kw)
        made.append(server)
        return server

    yield make
    for s in made:
        s.close()


def test_http_requests_carry_the_headers_and_there_is_no_session(http_server):
    server = http_server()
    c = MCPHttpClient(server.url)
    c.initialize()
    assert c.era == "modern" and c.session_id is None
    c.list_tools()
    assert c.call_tool("echo", {"text": "hi"}) == "echo: hi"
    c.close()
    posts = [(h, b) for m, h, b in server.requests if m == "POST"]
    assert [b["method"] for _, b in posts] == ["server/discover", "tools/list", "tools/call"]
    for h, b in posts:
        assert h["mcp-protocol-version"] == "2026-07-28" and h["mcp-method"] == b["method"] and "mcp-session-id" not in h
        assert "application/json" in h["accept"] and "text/event-stream" in h["accept"]
    assert posts[2][0]["mcp-name"] == "echo"
    assert not server.deleted and all(m == "POST" for m, _, _ in server.requests)          # no GET stream, no DELETE


def test_tools_with_invalid_header_annotations_are_left_out_of_the_list(http_server):
    c = MCPHttpClient(http_server().url)
    c.initialize()
    assert [t["name"] for t in c.list_tools()] == ["echo", "execute_sql"]


def test_annotated_parameters_are_mirrored_into_headers(http_server):
    server = http_server()
    c = MCPHttpClient(server.url)
    c.initialize()
    c.list_tools()
    assert c.call_tool("execute_sql", {"region": "us-west1", "query": "select 1", "limit": 5, "dry": True}) == "echo: execute_sql"
    h = [h for m, h, b in server.requests if b and b.get("method") == "tools/call"][0]
    assert (h["mcp-param-region"], h["mcp-param-limit"], h["mcp-param-dry"], h["mcp-name"]) == ("us-west1", "5", "true", "execute_sql")
    assert c.call_tool("execute_sql", {"region": "Zürich", "query": "q"}) == "echo: execute_sql"      # encoded, and the server decodes it
    h = [h for m, h, b in server.requests if b and b.get("method") == "tools/call"][-1]
    assert h["mcp-param-region"] == header_value("Zürich") and "mcp-param-limit" not in h


def test_an_event_stream_answer_and_the_key_work_as_before(http_server):
    server = http_server(sse=True, token="secret")
    c = MCPHttpClient(server.url, token="secret")
    c.initialize()
    assert c.call_tool("echo", {"text": "streamed"}) == "echo: streamed"
    assert all(h.get("authorization") == "Bearer secret" for m, h, b in server.requests)
    with pytest.raises(MCPError, match="401"):
        MCPHttpClient(server.url).initialize()


def test_http_state_is_echoed_and_unmet_input_is_refused(http_server):
    c = MCPHttpClient(http_server(mode="state").url)
    c.initialize()
    assert c.call_tool("echo", {"text": "x"}) == "echo: x (state opaque-state-1)"
    e = MCPHttpClient(http_server(mode="elicit").url)
    e.initialize()
    with pytest.raises(MCPError, match="does not offer"):
        e.call_tool("echo", {"text": "x"})


def test_a_legacy_server_that_answers_400_with_an_older_error_is_spoken_to_the_old_way(http_server):
    server = http_server(mode="legacy")
    c = MCPHttpClient(server.url)
    c.initialize()
    assert (c.era, c.session_id, c.server_version) == ("legacy", "sess-1", "2025-11-25")
    c.close()
    assert server.deleted


def test_a_server_of_both_eras_that_lists_only_older_versions_is_spoken_to_the_old_way(http_server):
    c = MCPHttpClient(http_server(mode="dual", supported=("2025-11-25",)).url)
    c.initialize()
    assert c.era == "legacy" and c.call_tool("echo", {"text": "x"}) == "legacy echo: x"


def test_a_server_of_both_eras_is_spoken_to_the_new_way(http_server):
    c = MCPHttpClient(http_server(mode="dual").url)
    c.initialize()
    assert c.era == "modern" and c.call_tool("echo", {"text": "x"}) == "echo: x"


def test_a_modern_server_with_no_version_in_common_is_an_error_naming_its_versions(http_server):
    with pytest.raises(MCPError, match="2031-01-01"):
        MCPHttpClient(http_server(mode="unsupported", supported=("2031-01-01",)).url).initialize()


def test_an_old_http_server_that_never_heard_of_headers_is_still_reached(http_server):
    fake = FakeMcpHttp()
    try:
        c = MCPHttpClient(fake.url)
        c.initialize()
        assert c.era == "legacy" and c.call_tool("echo", {"text": "old"}) == "echo: old"
    finally:
        fake.close()


def test_a_server_that_is_not_there_is_not_taken_for_a_legacy_one():
    with pytest.raises(MCPError, match="Cannot reach"):
        MCPHttpClient("http://127.0.0.1:59994/mcp").initialize()


def test_which_errors_identify_a_stateless_server():
    for code in (-32020, -32021, -32022):
        assert is_modern_error(MCPError("x", code, 400))
    assert is_modern_error(MCPError("x", -32601, 404))
    assert not is_modern_error(MCPError("x", -32601, 200))
    assert not is_modern_error(MCPError("x", -32000, 400))                  # what an older server says about a missing session
    assert not is_modern_error(MCPError("x", 404, 404))
    assert not is_modern_error(MCPError("x"))
