"""MCP Apps, the host side in the conversation's process: the extension is offered and read, tools tied to a `ui://` page are shown as apps, the visibility of a tool
is kept, the page is fetched with its metadata and cached, what a view asks of the server is answered under the conversation's permissions, and what an app says to
the model reaches it once. Runs against the real demo server in tools/mcp-app-demo."""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo import capabilities as cap
from tadamoo import mcp_apps as apps
from tadamoo.agent_loop import AgentLoop
from tadamoo.mcp_client import MCPClient, MCPHub
from tadamoo.permissions import PermissionLevel, PermissionManager
from tadamoo.tools.registry import ToolRegistry

DEMO = Path(__file__).resolve().parent.parent / "tools" / "mcp-app-demo" / "server.py"
ID = "mcp:app-demo"


class Server:
    """What the conversation keeps of a started server (tadamoo/stack.py): its connection and its tools."""

    def __init__(self, command, extension=True, log=None):
        env = {"MCP_APP_DEMO_LOG": str(log)} if log else None
        self.client = MCPClient(list(command), env=env)
        self.client.app_extension = extension
        self.hub = MCPHub()
        self.hub.add("s", self.client)
        self.hub.connect_all()
        assert not self.hub.errors, self.hub.errors
        self.tools = self.hub.catalog["s"]

    def call(self, tool, arguments):
        return self.hub.call("s", tool, arguments)

    def close(self):
        self.hub.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"app-demo": {"command": sys.executable, "args": [str(DEMO)]}}}), encoding="utf-8")
    home, appdata = tmp_path / "home", tmp_path / "appdata"
    home.mkdir()
    appdata.mkdir()
    index = cap.CapabilityIndex(tmp_path / "caps", ws, home=home, appdata=appdata)
    started = []

    def factory(entry_id, command):
        server = Server(command, log=tmp_path / "server.log")
        started.append(server)
        return server

    registry = ToolRegistry()
    cap.register_capability_tools(registry, index, hub_factory=factory)
    events = []
    host = apps.AppHost(lambda: index.settings()["apps"], index.server_for, lambda entry_id: "app-demo", cache=apps.AppCache(tmp_path / "templates"))
    host.emit = events.append
    host.sidecar = tmp_path / "apps.json"
    registry.mcp_apps = host
    yield registry, index, host, events, started, tmp_path
    for s in started:
        s.close()


def test_the_extension_is_offered_in_the_handshake_and_the_servers_own_answer_is_read():
    client = MCPClient([sys.executable, str(DEMO)])
    client.app_extension = True
    try:
        client.start()
        client.initialize()
        assert client.client_capabilities()["extensions"] == {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}
        assert client.server_apps == {}                                                      # the server answered with the extension: {} (no settings)
        tools = {t["name"]: t for t in client.list_tools()}
        assert apps.tool_ui(tools["survey"]) == ("ui://demo/survey", ["model", "app"])
        assert apps.tool_ui(tools["compute_series"]) == ("ui://demo/survey", ["app"])
    finally:
        client.close()


def test_a_server_that_is_not_offered_the_extension_gives_the_tool_without_a_page():
    client = MCPClient([sys.executable, str(DEMO)])
    try:
        client.start()
        client.initialize()
        assert "extensions" not in client.client_capabilities() and client.server_apps is None
        tools = {t["name"]: t for t in client.list_tools()}
        assert set(tools) == {"survey", "plain_note"} and apps.tool_ui(tools["survey"]) == ("", ["model", "app"])
    finally:
        client.close()


def test_tool_metadata_is_read_in_both_forms_and_visibility_defaults_to_both():
    assert apps.tool_ui({"_meta": {"ui": {"resourceUri": "ui://a/b", "visibility": ["app"]}}}) == ("ui://a/b", ["app"])
    assert apps.tool_ui({"_meta": {"ui/resourceUri": "ui://old/one"}}) == ("ui://old/one", ["model", "app"])
    assert apps.tool_ui({"_meta": {"ui": {"resourceUri": "https://not-ui"}}}) == ("", ["model", "app"])
    assert apps.tool_ui({}) == ("", ["model", "app"])
    assert apps.visible_to({"_meta": {"ui": {"visibility": ["app"]}}}, "model") is False and apps.visible_to({"_meta": {"ui": {"visibility": ["app"]}}}, "app")
    assert apps.visible_to({"name": "x"}, "model") and apps.visible_to({"name": "x"}, "app")


def test_the_metadata_of_a_page_comes_from_the_content_and_falls_back_on_the_listing():
    listing = {"_meta": {"ui": {"csp": {"connectDomains": ["https://a.example"]}, "prefersBorder": True, "domain": "d.example"}}}
    content = {"_meta": {"ui": {"csp": {"resourceDomains": ["https://cdn.example"]}, "permissions": {"camera": {}, "bogus": {}}, "prefersBorder": False}}}
    merged = apps.ui_meta(listing, content)
    assert merged == {"csp": {"resourceDomains": ["https://cdn.example"]}, "permissions": {"camera": {}}, "domain": "d.example", "prefersBorder": False}
    assert apps.ui_meta(listing, None)["csp"] == {"connectDomains": ["https://a.example"]} and apps.ui_meta(None, None) == {}


def test_a_page_is_html_of_the_right_type_by_text_or_blob():
    ok = {"uri": "ui://x/y", "mimeType": "text/html;profile=mcp-app", "text": "<p>a</p>"}
    assert apps.page_html(ok) == "<p>a</p>"
    import base64
    assert apps.page_html({**ok, "text": None, "blob": base64.b64encode("<b>é</b>".encode()).decode()}) == "<b>é</b>"
    for bad in ({**ok, "mimeType": "text/html"}, {**ok, "uri": "file:///x"}, {"uri": "ui://x/y", "mimeType": "text/html;profile=mcp-app"}, None):
        with pytest.raises(apps.AppError):
            apps.page_html(bad)


def test_the_model_does_not_see_or_call_a_tool_only_the_app_may_use(world):
    registry, index, host, events, started, tmp = world
    listing = registry.execute("call_capability", {"id": ID})
    assert "survey(" in listing and "plain_note" in listing and "compute_series" not in listing and "save_answers" not in listing
    assert "has no tool 'compute_series'" in registry.execute("call_capability", {"id": ID, "tool": "compute_series", "arguments": {}})
    assert "has no tool 'save_answers'" in registry.execute("call_capability", {"id": ID, "tool": "save_answers", "arguments": {"choice": "x"}})
    assert "has no tool" in registry.execute("read_capability", {"id": ID, "tool": "compute_series", "arguments": {}})
    assert any(t["name"] == "compute_series" for t in started[0].tools)                      # it is there, for the app


def test_a_tool_with_a_page_shows_the_app_while_it_runs_and_hands_it_the_result(world):
    registry, index, host, events, started, tmp = world
    host.current_call = "call-1"
    out = registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "Lunch", "options": ["Pizza", "Soup"]}})
    assert "Survey 'Lunch' is open" in out                                                    # the text result stands for a host that shows no app
    kinds = [e["event"] for e in events]
    assert kinds == ["mcp_app", "mcp_app_result"]
    shown, result = events
    assert shown["call_id"] == "call-1" and shown["server"] == ID and shown["server_name"] == "app-demo" and shown["key"] == f"{ID}|ui://demo/survey" and shown["state"] == "running"
    assert shown["input"] == {"title": "Lunch", "options": ["Pizza", "Soup"]} and shown["tool"]["name"] == "survey" and shown["on"] is False
    assert shown["ui"] == {"csp": {}, "prefersBorder": True}                                  # the page's own metadata over the listing's
    assert result["app"] == shown["app"] and result["result"]["structuredContent"]["title"] == "Lunch" and len(result["result"]["structuredContent"]["series"]) == 8
    page = host.cache.get(shown["template"])
    assert page["uri"] == "ui://demo/survey" and page["html"].lstrip().startswith("<!doctype html>") and page["meta"] == shown["ui"]
    saved = json.loads((tmp / "apps.json").read_text(encoding="utf-8"))
    assert saved["call-1"]["state"] == "done" and saved["call-1"]["result"]["structuredContent"]["title"] == "Lunch"


def test_the_apps_of_a_conversation_are_read_back_from_what_was_kept(world):
    registry, index, host, events, started, tmp = world
    host.current_call = "c9"
    registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "T"}})
    again = apps.AppHost(host.settings, host.server, host.describe, cache=host.cache)
    again.sidecar = host.sidecar
    again.load()
    (record,) = again.apps.values()
    assert record["call_id"] == "c9" and record["state"] == "done" and again.cache.get(record["template"])["html"]


def test_a_tool_without_a_page_and_a_call_that_fails_leave_no_app_or_say_it_was_cancelled(world):
    registry, index, host, events, started, tmp = world
    assert registry.execute("read_capability", {"id": ID, "tool": "plain_note", "arguments": {}}) == "a note"
    assert events == []
    registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "T"}})
    events.clear()

    def broken(*a, **k):
        raise TimeoutError("the server did not answer")
    started[0].hub.servers["s"].call_tool = broken
    said = registry.get("read_capability").handler(id=ID, tool="survey", arguments={"title": "T"})
    assert "could not be used" in said and "did not answer" in said
    assert [e["event"] for e in events] == ["mcp_app", "mcp_app_cancelled"] and "did not answer" in events[1]["reason"]


def test_a_page_that_cannot_be_fetched_is_no_app_and_the_text_stands_alone(world):
    registry, index, host, events, started, tmp = world
    registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "T"}})
    events.clear()
    started[0].client.read_resource = lambda uri: (_ for _ in ()).throw(RuntimeError("gone"))
    assert "is open" in registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "T"}}) and events == []


def test_what_a_view_asks_of_the_server_is_answered_resources_tools_and_the_list_of_app_tools(world):
    registry, index, host, events, started, tmp = world
    registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "T"}})
    read = host.handle("resources/read", {"server": ID, "uri": "ui://demo/survey"})
    assert read["contents"][0]["mimeType"] == "text/html;profile=mcp-app"
    assert {t["name"] for t in host.handle("tools/list", {"server": ID})["tools"]} == {"survey", "compute_series", "save_answers", "plain_note"}
    with pytest.raises(apps.AppError):
        host.handle("resources/read", {"server": ID})
    with pytest.raises(apps.AppError):
        host.handle("nothing/known", {"server": ID})
    host.handle("log", {"app": "a1", "level": "info", "data": "hello"})
    assert events[-1] == {"event": "mcp_app_log", "app": "a1", "level": "info", "text": "hello"}


def agent_for(registry, level, tmp_path):
    asked = []
    perms = PermissionManager(level=level, confirm_callback=lambda tool, args: asked.append((tool, args)) or False)
    agent = AgentLoop(None, registry, permissions=perms)
    return agent, asked, perms


def test_an_apps_tool_call_runs_under_the_conversations_permissions_without_asking(world):
    registry, index, host, events, started, tmp = world
    registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "T"}})
    events.clear()
    agent, asked, perms = agent_for(registry, PermissionLevel.READ_ONLY, tmp)
    from tadamoo.permissions import UNASKED
    from tadamoo.mcp_client import declares_read_only

    def run(server_id, tool, arguments):
        definition = next(t for t in started[0].tools if t["name"] == tool and apps.visible_to(t, "app"))
        name = "read_capability" if declares_read_only(definition) else "call_capability"
        token = UNASKED.set(bool(apps.APP_CALL.get()["unasked"]))
        try:
            ok, text = agent.run_for_app(name, {"id": server_id, "tool": tool, "arguments": arguments}, "app:" + server_id)
        finally:
            UNASKED.reset(token)
        return {"ok": ok, "text": text}
    host.run_call = run
    answer = host.handle("tools/call", {"app": "a1", "server": ID, "name": "save_answers", "arguments": {"choice": "Soup"}})          # not read-only: asks at this level
    assert answer["content"][0]["text"] == "saved Soup (1 so far)" and asked == []                                                       # ... and nobody was asked
    series = host.handle("tools/call", {"app": "a1", "server": ID, "name": "compute_series", "arguments": {"n": 5}})
    assert len(series["structuredContent"]["series"]) == 5
    assert [e["state"] for e in events if e["event"] == "mcp_app_call"] == ["started", "done", "started", "done"] and events[0]["unasked"] is True
    assert perms.denied == 0


def test_with_the_person_choosing_to_be_asked_the_call_is_asked_like_the_models_and_a_refusal_is_the_apps_error(world):
    registry, index, host, events, started, tmp = world
    registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "T"}})
    index.save_settings(apps={"tool_calls": "ask"})
    agent, asked, perms = agent_for(registry, PermissionLevel.DEFAULT, tmp)

    def run(server_id, tool, arguments):
        ok, text = agent.run_for_app("call_capability", {"id": server_id, "tool": tool, "arguments": arguments}, "app:" + server_id)
        return {"ok": ok, "text": text}
    host.run_call = run
    with pytest.raises(apps.AppError) as e:
        host.handle("tools/call", {"app": "a1", "server": ID, "name": "save_answers", "arguments": {"choice": "x"}})
    assert asked and asked[0][0] == "call_capability" and "Permission denied" in str(e.value)
    index.save_settings(apps={"tool_calls": "refuse"})
    with pytest.raises(apps.AppError) as e:
        host.handle("tools/call", {"app": "a1", "server": ID, "name": "save_answers", "arguments": {"choice": "x"}})
    assert "may not call tools" in str(e.value)


def test_an_app_cannot_call_a_tool_that_is_not_for_apps_or_not_there(world):
    registry, index, host, events, started, tmp = world
    registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "T"}})
    agent, asked, perms = agent_for(registry, PermissionLevel.AUTO, tmp)
    host.run_call = lambda s, t, a: {"ok": True, "text": "ran"}
    result = host.handle("tools/call", {"app": "a1", "server": ID, "name": "plain_note", "arguments": {}})
    assert result["content"][0]["text"] == "ran"
    with pytest.raises(apps.AppError):
        host.handle("tools/call", {"app": "a1", "server": ID, "arguments": {}})
    with pytest.raises(apps.AppError):
        host.handle("tools/call", {"app": "a1", "server": ID, "name": "x", "arguments": [1]})


def test_what_an_app_says_to_the_model_is_given_once_the_last_thing_it_said(world):
    registry, index, host, events, started, tmp = world
    host.apps["a1"] = {"name": "Survey", "server_name": "app-demo"}
    assert host.context_note() is None
    host.handle("context", {"app": "a1", "server": ID, "content": [{"type": "text", "text": "chose Soup"}]})
    host.handle("context", {"app": "a1", "server": ID, "content": [{"type": "text", "text": "chose Pizza"}], "structuredContent": {"choice": "Pizza"}})
    note = host.context_note()
    assert note.startswith("[app context]") and "chose Pizza" in note and "chose Soup" not in note and '"choice": "Pizza"' in note and "Survey" in note
    assert host.context_note() is None                                                        # not repeated until it changes
    host.handle("context", {"app": "a1", "server": ID, "content": [{"type": "text", "text": "chose Pizza"}], "structuredContent": {"choice": "Pizza"}})
    assert host.context_note() is None
    with pytest.raises(apps.AppError):
        host.handle("context", {"app": "a1", "content": "text"})


def test_the_choices_about_apps_have_their_defaults_in_one_place_and_bad_values_fall_back():
    assert apps.normalize_settings(None) == {"on": [], "display": "panel", "tool_calls": "run"} == apps.DEFAULTS
    got = apps.normalize_settings({"on": ["mcp:a|ui://x", "no-bar", 4], "display": "dialog", "tool_calls": "ask"})
    assert got == {"on": ["mcp:a|ui://x"], "display": "dialog", "tool_calls": "ask"}
    assert apps.normalize_settings({"display": "inline", "tool_calls": "x", "on": "all"}) == apps.DEFAULTS


def test_the_servers_in_use_tell_their_apps_to_stop_when_they_are_dropped(world):
    registry, index, host, events, started, tmp = world
    registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "T"}})
    events.clear()
    assert index.drop_servers() == 1
    assert events == [{"event": "mcp_app_stop", "server": ID}]


def test_the_view_of_a_tool_is_shown_in_the_call_of_the_model_that_made_it_even_across_threads(world):
    registry, index, host, events, started, tmp = world
    host.current_call = "main"
    results = []
    t = threading.Thread(target=lambda: results.append(registry.execute("read_capability", {"id": ID, "tool": "survey", "arguments": {"title": "Other"}})))
    t.start()
    t.join(20)
    assert results and events[0]["call_id"] == "main"
