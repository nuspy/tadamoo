"""The Settings page's routes for capabilities: look, approve what was read, choose visual analysis, only from this PC and the page itself."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.capabilities_api import CapabilityApi
from test_capabilities import skill
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)


class Api:
    def __init__(self, url):
        self.url = url

    def __call__(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    home, ws = tmp_path / "home", tmp_path / "ws"
    ws.mkdir()
    skill(home / ".claude" / "skills", "ocr-reader", "Read text from a screenshot with OCR.")
    skill(home / ".claude" / "skills", "pdf-tools", "Extract tables from a PDF.")
    monkeypatch.setattr(handler, "_CAPS", CapabilityApi(tmp_path / "caps", ws, home=home, appdata=tmp_path / "none"))
    return Api(start_server([answer("x")]))


def test_the_page_sees_the_tree_and_never_a_path_on_disk(api):
    code, view = api("GET", "/api/capabilities")
    assert code == 200 and view["roots"]["vision"]["count"] >= 2 and view["settings"]["visual"] == "builtin"
    assert all("path" not in e for e in view["entries"]) and "approved.json" not in json.dumps(view["entries"])
    assert {o["id"] for o in view["visual_options"]} >= {"builtin", "none", "skill:ocr-reader"}


def test_approving_needs_the_hash_that_was_shown_and_then_the_skill_can_be_the_visual_choice(api):
    assert api("POST", "/api/capabilities/settings", {"visual": "skill:ocr-reader"})[1]["error"]["code"] == "needs_approval"
    code, review = api("GET", "/api/capabilities/review?id=skill%3Aocr-reader")
    assert code == 200 and "Do the thing." in review["text"] and "path" not in review["entry"]
    code, body = api("POST", "/api/capabilities/approve", {"id": "skill:ocr-reader", "sha": "0" * 64})
    assert code == 409 and body["error"]["code"] == "changed"
    code, view = api("POST", "/api/capabilities/approve", {"id": "skill:ocr-reader", "sha": review["sha"]})
    assert code == 200 and next(e for e in view["entries"] if e["id"] == "skill:ocr-reader")["state"] == "approved"
    code, view = api("POST", "/api/capabilities/settings", {"visual": "skill:ocr-reader"})
    assert code == 200 and view["settings"]["visual"] == "skill:ocr-reader"
    assert api("POST", "/api/capabilities/settings", {"visual": "skill:nothing"})[1]["error"]["code"] == "bad_choice"
    code, view = api("POST", "/api/capabilities/revoke", {"id": "skill:ocr-reader"})
    assert next(e for e in view["entries"] if e["id"] == "skill:ocr-reader")["state"] == "pending"
    assert api("GET", "/api/capabilities/review?id=ghost")[0] == 404 and api("POST", "/api/capabilities/approve", {"id": "ghost", "sha": "x"})[0] == 409


def test_moving_and_turning_off_and_looking_again(api, tmp_path):
    code, view = api("POST", "/api/capabilities/move", {"id": "skill:pdf-tools", "node": "data"})
    assert code == 200 and next(e for e in view["entries"] if e["id"] == "skill:pdf-tools")["node"] == "data"
    assert api("POST", "/api/capabilities/move", {"id": "skill:pdf-tools", "node": "nowhere"})[1]["error"]["code"] == "bad_node"
    code, view = api("POST", "/api/capabilities/settings", {"off": ["skill:pdf-tools", "skill:ghost"]})
    assert view["settings"]["off"] == ["skill:pdf-tools"] and next(e for e in view["entries"] if e["id"] == "skill:pdf-tools")["state"] == "off"
    assert api("POST", "/api/capabilities/settings", {"off": "all"})[1]["error"]["code"] == "bad_off"
    skill(tmp_path / "home" / ".claude" / "skills", "new-one", "A new skill about documents.")
    code, view = api("POST", "/api/capabilities/rescan", {})
    assert "skill:new-one" in {e["id"] for e in view["entries"]}


def test_only_the_pages_own_origin_can_change_anything(api):
    code, body = api("POST", "/api/capabilities/settings", {"visual": "none"}, headers={"Origin": "http://evil.example"})
    assert code == 403
    assert api("GET", "/api/capabilities")[1]["settings"]["visual"] == "builtin"
    assert api("POST", "/api/capabilities/nothing", {})[0] == 404
