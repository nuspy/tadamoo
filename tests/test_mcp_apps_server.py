"""MCP Apps, the server's side: the sandbox origin (a second port, the policy built from what an app declared, the shim that passes messages on), what a view asks of the
conversation's process (answered, refused, never answered), the apps kept with a conversation and given back with its history, and the page's routes and settings."""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler, mcp_sandbox
from serve.agent_sessions import RunError, SessionManager
from tadamoo import mcp_apps
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)

STUB = Path(__file__).resolve().parent.parent / "serve" / "tests" / "session_stub.py"


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8")


def test_the_policy_is_the_specifications_default_when_the_app_declared_none_and_is_built_from_its_domains_when_it_did():
    assert mcp_sandbox.policy_for(None) == mcp_sandbox.DEFAULT_POLICY
    for part in ("default-src 'none'", "script-src 'self' 'unsafe-inline'", "connect-src 'none'", "object-src 'none'", "frame-src 'none'", "base-uri 'self'", "img-src 'self' data:"):
        assert part in mcp_sandbox.DEFAULT_POLICY
    policy = mcp_sandbox.policy_for({"connectDomains": ["https://api.weather.com", "wss://live.example.com"], "resourceDomains": ["https://cdn.jsdelivr.net", "https://*.cloudflare.com"],
                                     "frameDomains": ["https://www.youtube.com"], "baseUriDomains": ["https://cdn.example.com"]})
    assert "connect-src 'self' https://api.weather.com wss://live.example.com" in policy
    assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://*.cloudflare.com" in policy
    assert "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in policy and "font-src 'self' https://cdn.jsdelivr.net" in policy and "img-src 'self' data: https://cdn.jsdelivr.net" in policy
    assert "media-src 'self' data: https://cdn.jsdelivr.net" in policy and "frame-src https://www.youtube.com" in policy and "base-uri https://cdn.example.com" in policy
    assert "default-src 'none'" in policy and "object-src 'none'" in policy
    declared_but_empty = mcp_sandbox.policy_for({})
    assert "connect-src 'self'" in declared_but_empty and "frame-src 'none'" in declared_but_empty and "base-uri 'self'" in declared_but_empty


def test_a_domain_that_could_end_the_directive_is_not_written_into_the_policy():
    bad = ["https://ok.example.com; script-src *", "https://a.example.com, https://b.example.com", "*", "'unsafe-eval'", "javascript:alert(1)", "https://x.example.com/a b", "http://", 5, None]
    assert mcp_sandbox.clean_domains(bad) == []
    policy = mcp_sandbox.policy_for({"connectDomains": bad + ["https://fine.example.com:8443/api"]})
    assert "connect-src 'self' https://fine.example.com:8443/api;" in policy + ";" and "script-src *" not in policy and "unsafe-eval" not in policy and policy.count(";") == 9


def test_the_permissions_an_app_asked_for_become_the_allow_attribute():
    assert mcp_sandbox.allow_attribute({"camera": {}, "microphone": {}, "geolocation": {}, "clipboardWrite": {}, "bogus": {}}) == "camera; microphone; geolocation; clipboard-write"
    assert mcp_sandbox.allow_attribute({}) == "" and mcp_sandbox.allow_attribute(None) == ""


def test_the_sandbox_is_a_second_port_that_serves_the_shim_with_the_policy_as_a_header_and_nothing_else():
    origin = mcp_sandbox.SandboxOrigin("127.0.0.1")
    try:
        port = origin.ensure()
        assert port and port != 8080 and origin.ensure() == port                        # started once, kept
        base = f"http://127.0.0.1:{port}"
        code, headers, body = get(base + "/sandbox")
        assert code == 200 and headers["Content-Security-Policy"] == mcp_sandbox.DEFAULT_POLICY and "/sandbox.js" in body and headers["Cache-Control"] == "no-store"
        csp = json.dumps({"connectDomains": ["https://api.example.com"]})
        code, headers, _ = get(base + "/sandbox?host=http%3A%2F%2F127.0.0.1%3A1&csp=" + urllib.request.quote(csp))
        assert "connect-src 'self' https://api.example.com" in headers["Content-Security-Policy"]
        code, headers, _ = get(base + "/sandbox?csp=not-json")
        assert headers["Content-Security-Policy"] == mcp_sandbox.DEFAULT_POLICY
        code, headers, js = get(base + "/sandbox.js")
        assert code == 200 and "ui/notifications/sandbox-proxy-ready" in js and "ui/notifications/sandbox-resource-ready" in js and "sandbox-" in js
        assert get(base + "/api/capabilities")[0] == 404 and get(base + "/")[0] == 404          # it serves the shim and nothing of the server's
    finally:
        origin.close()
    assert origin.port == 0


@pytest.fixture
def mgr(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server(["unused"])
    manager = SessionManager(tmp_path / "j", url, tick_s=0.2, default_workspace=tmp_path / "chat",
                             command=lambda sid, rec, history: [sys.executable, str(STUB), "--session-id", sid, "--history-dir", str(history)])
    monkeypatch.setattr(handler, "_SESSIONS", manager)
    yield manager, url, tmp_path
    manager.shutdown()


def test_what_a_view_asks_goes_to_the_conversations_process_and_its_answer_comes_back_a_refusal_as_an_error(mgr):
    manager, url, tmp = mgr
    ws = tmp / "ws"
    ws.mkdir()
    sid = manager.create(str(ws), title="A")["id"]
    got = manager.app_call(sid, "tools/call", {"app": "a1", "server": "mcp:x", "name": "save", "arguments": {"c": 1}})
    assert got == {"result": {"op": "tools/call", "params": {"app": "a1", "server": "mcp:x", "name": "save", "arguments": {"c": 1}}}}
    assert manager.app_call(sid, "context", {"app": "a1", "server": "mcp:x"})["result"]["op"] == "context"
    assert manager.app_call(sid, "tools/call", {"app": "a1", "server": "mcp:x", "name": "refused"}) == {"error": {"code": -32000, "message": "Permission denied"}}
    assert not manager._app_waiters                                                       # nothing is left waiting
    for op, params in (("shell", {}), ("tools/call", "x"), (None, {})):
        with pytest.raises(RunError) as e:
            manager.app_call(sid, op, params)
        assert e.value.code == "bad_app_request"
    manager.close(sid)
    with pytest.raises(RunError) as e:
        manager.app_call(sid, "tools/call", {})
    assert e.value.code == "closed"


def test_a_request_the_process_never_answers_ends_when_the_process_goes(mgr, monkeypatch):
    manager, url, tmp = mgr
    ws = tmp / "ws"
    ws.mkdir()
    sid = manager.create(str(ws), title="A")["id"]
    manager.app_call(sid, "log", {"app": "a"})
    live = manager._live[sid]
    import threading
    box = []
    t = threading.Thread(target=lambda: box.append(manager.app_call(sid, "tools/call", {"app": "a", "server": "s", "name": "hang"})))
    t.start()
    time.sleep(0.6)
    live.proc.kill()
    t.join(20)
    assert box and box[0]["error"]["message"].startswith("The conversation stopped")


def test_the_apps_kept_with_a_conversation_come_back_on_the_tool_call_they_were_shown_in(mgr):
    manager, url, tmp = mgr
    ws = tmp / "ws"
    ws.mkdir()
    sid = manager.create(str(ws), title="A")["id"]
    where = manager.history_dir(sid)
    where.mkdir(parents=True, exist_ok=True)
    messages = [{"role": "user", "content": "open it"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "call_capability", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "name": "call_capability", "content": "text"},
                {"role": "tool", "tool_call_id": "c2", "name": "x", "content": "no app here"}]
    (where / "history.json").write_text(json.dumps({"session": sid, "turns": 1, "saved": time.time(), "messages": messages, "pending": []}), encoding="utf-8")
    (where / "apps.json").write_text(json.dumps({"c1": {"app": "ap1", "call_id": "c1", "name": "Survey", "state": "done", "template": "0" * 32}}), encoding="utf-8")
    shown = manager.history(sid)["messages"]
    assert shown[2]["app"]["app"] == "ap1" and shown[2]["app"]["name"] == "Survey" and "app" not in shown[3] and "app" not in shown[0]
    (where / "apps.json").write_text("not json", encoding="utf-8")
    assert all("app" not in m for m in manager.history(sid)["messages"])                   # a damaged file loses the apps, not the conversation


class Api:
    def __init__(self, url):
        self.url = url

    def __call__(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")


def test_the_page_gets_the_sandbox_port_the_pages_kept_and_the_choices_and_changes_them(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(handler, "_APPS", None)
    monkeypatch.setattr(handler, "_CAPS", None)
    api = Api(start_server([answer("x")]))
    code, config = api("GET", "/api/mcp-apps/config")
    try:
        assert code == 200 and config["sandbox_port"] > 0 and config["protocol"] == "2026-01-26" and config["settings"] == mcp_apps.DEFAULTS == config["defaults"] and config["known"] == []
        digest = mcp_apps.default_cache().put("mcp:x", "ui://x/y", "<p>hi</p>", {"prefersBorder": True}, "Hello")
        code, page = api("GET", f"/api/mcp-apps/template/{digest}")
        assert code == 200 and page == {"server": "mcp:x", "uri": "ui://x/y", "html": "<p>hi</p>", "meta": {"prefersBorder": True}}
        assert api("GET", "/api/mcp-apps/template/" + "f" * 32)[0] == 404 and api("GET", "/api/mcp-apps/template/..%2F..%2Fx")[0] == 404
        known = api("GET", "/api/mcp-apps/config")[1]["known"]
        assert [(k["key"], k["server"], k["uri"], k["name"]) for k in known] == [("mcp:x|ui://x/y", "mcp:x", "ui://x/y", "Hello")]
        code, over = api("GET", "/api/capabilities")
        assert code == 200 and over["settings"]["apps"] == mcp_apps.DEFAULTS and over["apps_known"][0]["name"] == "Hello" and over["apps_defaults"] == mcp_apps.DEFAULTS
        code, over = api("POST", "/api/capabilities/settings", {"apps": {"display": "dialog", "on": ["mcp:x|ui://x/y"]}})
        assert code == 200 and over["settings"]["apps"] == {"on": ["mcp:x|ui://x/y"], "display": "dialog", "tool_calls": "run"}
        code, over = api("POST", "/api/capabilities/settings", {"apps": {"tool_calls": "ask"}})
        assert over["settings"]["apps"] == {"on": ["mcp:x|ui://x/y"], "display": "dialog", "tool_calls": "ask"}                   # what was not said is kept
        code, over = api("POST", "/api/capabilities/settings", {"apps": {"display": "inline", "tool_calls": "x"}})
        assert over["settings"]["apps"]["display"] == "panel" and over["settings"]["apps"]["tool_calls"] == "run"                   # a value that is not allowed is the default
        assert api("POST", "/api/capabilities/settings", {"apps": "all"})[0] == 400
        assert api("GET", "/api/mcp-apps/config")[1]["settings"]["display"] == "panel"
    finally:
        if handler._APPS is not None:
            handler._APPS.sandbox.close()
