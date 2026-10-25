"""The Settings page's routes for the search online: its settings, and the capabilities Basalt installed."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.discovery.candidates import Candidate, CandidateStore
from harness.discovery.installer import Installer
from harness.discovery.scoring import Metrics
from serve import handler
from serve.capabilities_api import CapabilityApi
from test_discovery_install import Runner
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
def setup(tmp_path, monkeypatch, start_server):  # noqa: F811
    ws, caps = tmp_path / "ws", tmp_path / "caps"
    ws.mkdir()
    capi = CapabilityApi(caps, ws, home=tmp_path / "home", appdata=tmp_path / "none")
    monkeypatch.setattr(handler, "_CAPS", capi)
    store = CandidateStore()
    inst = Installer(caps, store, index=capi.index, runner=Runner(), which=lambda n: "npm" if n == "npm" else None)
    c = store.add(Candidate(kind="mcp", name="search-mcp", source="registry", route="npm", package="@acme/search-mcp", version="2.0.0", integrity="sha512-GOOD",
                            bins={"search-mcp": "x"}, env=[{"name": "SEARCH_API_KEY", "required": True, "secret": True}], metrics=Metrics()))
    inst.install(c.id)
    return Api(start_server([answer("x")])), caps


def test_the_page_sees_the_settings_the_installed_list_and_the_installers(setup):
    api, caps = setup
    code, view = api("GET", "/api/capabilities/online")
    assert code == 200 and view["settings"]["auto_install"] is False and view["settings"]["min_score"] == 50 and "base_urls" not in view["settings"]
    assert view["sources"] == ["registry", "npm", "pypi", "github", "skills"] and "downloads" in view["weights"] and set(view["tools"]) == {"npm", "uv", "pip"}
    item = view["installed"][0]
    assert item["id"] == "mcp:search-mcp" and item["version"] == "2.0.0" and item["source"] == "registry" and item["installed_at"]
    assert item["keys"] == [{"name": "SEARCH_API_KEY", "entry": "SEARCH_API_KEY"}]
    assert str(caps) not in json.dumps(view) and "command" not in item


def test_the_settings_are_saved_validated_and_the_base_urls_are_not_writable(setup):
    api, caps = setup
    code, view = api("POST", "/api/capabilities/online", {"min_score": 70, "sources": {"npm": False}, "weights": {"stars": 0}, "auto_install": True,
                                                         "marketplaces": ["acme/market"], "base_urls": {"npm": "http://evil.example"}})
    assert code == 200 and view["settings"]["min_score"] == 70 and view["settings"]["sources"]["npm"] is False and view["settings"]["auto_install"] is True
    assert view["settings"]["weights"]["stars"] == 0 and view["settings"]["marketplaces"] == ["acme/market"]
    assert "evil" not in (caps / "discovery.json").read_text(encoding="utf-8")
    for bad in ({"min_score": 500}, {"weights": {"stars": "lots"}}, {"sources": {"nope": True}}, {"auto_install": "yes"}, {"marketplaces": ["x y"]}):
        code, body = api("POST", "/api/capabilities/online", bad)
        assert code == 400 and body["error"]["code"] == "bad_settings", bad
    assert api("GET", "/api/capabilities/online")[1]["settings"]["min_score"] == 70


def test_removing_deletes_what_basalt_installed_and_only_that(setup):
    api, caps = setup
    assert (caps / "mcp" / "search-mcp").is_dir()
    code, view = api("POST", "/api/capabilities/remove", {"id": "mcp:search-mcp"})
    assert code == 200 and view["installed"] == [] and not (caps / "mcp" / "search-mcp").exists()
    assert "mcp:search-mcp" not in {e["id"] for e in api("GET", "/api/capabilities")[1]["entries"]}
    code, body = api("POST", "/api/capabilities/remove", {"id": "mcp:search-mcp"})
    assert code == 409 and body["error"]["code"] == "gone"
    assert api("POST", "/api/capabilities/remove", {"id": "skill:from-the-user"})[0] == 409


def test_the_installed_one_shows_in_the_tree_as_installed_by_basalt_and_approved(setup):
    api, _ = setup
    entry = next(e for e in api("GET", "/api/capabilities")[1]["entries"] if e["id"] == "mcp:search-mcp")
    assert entry["origin"] == "basalt" and entry["state"] == "approved"


def test_only_the_pages_own_origin_can_change_the_search_settings_or_remove(setup):
    api, _ = setup
    assert api("POST", "/api/capabilities/online", {"auto_install": True}, headers={"Origin": "http://evil.example"})[0] == 403
    assert api("POST", "/api/capabilities/remove", {"id": "mcp:search-mcp"}, headers={"Origin": "http://evil.example"})[0] == 403
    view = api("GET", "/api/capabilities/online")[1]
    assert view["settings"]["auto_install"] is False and view["installed"]
