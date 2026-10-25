"""Settings signs in to an approved remote MCP server and the browser's answer comes back to the server."""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.llm.credentials import FileCredentials
from harness.mcp_oauth import OAuthStore
from serve import handler
from serve.capabilities_api import CapabilityApi
from serve.mcp_auth_api import McpAuthApi
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_mcp_oauth import World


def call(base, method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw.decode()), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if e.headers.get("Content-Type", "").startswith("application/json") else raw.decode()), e.headers


@pytest.fixture
def world():
    w = World()
    yield w
    w.close()


@pytest.fixture
def setup(tmp_path, monkeypatch, world, start_server):  # noqa: F811
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"remote": {"url": world.url + "?key=SECRETQUERY"}}}), encoding="utf-8")
    (tmp_path / "home").mkdir()
    caps = CapabilityApi(tmp_path / "caps", ws, home=tmp_path / "home", appdata=tmp_path / "appdata")
    base = start_server([answer("x")])
    port = int(base.rsplit(":", 1)[1])
    api = McpAuthApi(caps, OAuthStore(tmp_path / "oauth.json", FileCredentials(tmp_path / "c.json")), lambda: f"http://127.0.0.1:{port}/api/mcp/auth/callback")
    monkeypatch.setattr(handler, "_MCP_AUTH", api)
    monkeypatch.setattr(handler, "_CAPS", caps)
    return base, caps, api, tmp_path


def approve(caps):
    caps.approve("mcp:remote", caps.review("mcp:remote")["entry"]["sha"])


def test_only_an_approved_remote_server_can_be_signed_in_to(setup, world):
    base, caps, api, tmp = setup
    code, body, _ = call(base, "GET", "/api/mcp/auth")
    assert code == 200 and body["servers"] == [{"id": "mcp:remote", "name": "remote", "url": world.url, "state": "pending", "signed_in": False}]
    assert "SECRETQUERY" not in json.dumps(body)
    code, body, _ = call(base, "POST", "/api/mcp/auth/start", {"id": "mcp:remote"})
    assert code == 409 and body["error"]["code"] == "needs_approval"
    assert call(base, "POST", "/api/mcp/auth/start", {"id": "mcp:nothing"})[0] == 409
    assert world.requests == []                                                           # nothing was contacted


def test_the_whole_sign_in_through_the_server_and_the_browsers_return(setup, world):
    base, caps, api, tmp = setup
    approve(caps)
    code, body, _ = call(base, "POST", "/api/mcp/auth/start", {"id": "mcp:remote"})
    assert code == 200
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(body["authorization_url"]).query).items()}
    assert q["redirect_uri"] == f"{base}/api/mcp/auth/callback" and q["resource"] == world.url
    world.challenge = q["code_challenge"]
    code, page, headers = call(base, "GET", "/api/mcp/auth/callback?" + urllib.parse.urlencode({"code": "good-code", "state": q["state"]}))
    assert code == 200 and "signed in" in page.lower() and headers["Content-Security-Policy"].startswith("default-src 'none'") and headers["Referrer-Policy"] == "no-referrer"
    assert call(base, "GET", "/api/mcp/auth")[1]["servers"][0]["signed_in"] is True
    assert "AT1" not in (tmp / "oauth.json").read_text(encoding="utf-8")
    code, body, _ = call(base, "POST", "/api/mcp/auth/signout", {"id": "mcp:remote"})
    assert code == 200 and body["servers"][0]["signed_in"] is False


def test_a_return_nobody_asked_for_or_one_that_failed_gets_a_plain_page(setup, world):
    base, caps, api, tmp = setup
    approve(caps)
    code, page, _ = call(base, "GET", "/api/mcp/auth/callback?code=x&state=nope")
    assert code == 400 and "not started here" in page
    code, body, _ = call(base, "POST", "/api/mcp/auth/start", {"id": "mcp:remote"})
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(body["authorization_url"]).query)["state"][0]
    code, page, _ = call(base, "GET", "/api/mcp/auth/callback?" + urllib.parse.urlencode({"error": "access_denied", "error_description": "<b>no</b>", "state": state}))
    assert code == 400 and "&lt;b&gt;no&lt;/b&gt;" in page and "<b>" not in page                           # the server's words are escaped
    code, page, _ = call(base, "GET", "/api/mcp/auth/callback?" + urllib.parse.urlencode({"code": "good-code", "state": state}))
    assert code == 400                                                                                    # that sign-in was dropped with the error


def test_a_server_that_asks_for_nothing_is_not_signed_in_to(setup, world):
    base, caps, api, tmp = setup
    approve(caps)
    world.tokens = {"": "x"}                                                                              # the empty bearer is accepted: no sign-in needed
    code, body, _ = call(base, "POST", "/api/mcp/auth/start", {"id": "mcp:remote"})
    assert code == 200 and body == {"signed_in": False, "needs_sign_in": False}


def test_changing_things_needs_the_own_page_and_json(setup):
    base, caps, api, tmp = setup
    approve(caps)
    code, body, _ = call(base, "POST", "/api/mcp/auth/start", {"id": "mcp:remote"}, {"Origin": "http://evil.example"})
    assert code == 403
    req = urllib.request.Request(base + "/api/mcp/auth/start", data=b"id=mcp:remote", method="POST", headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 415
