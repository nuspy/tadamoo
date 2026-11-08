"""The Settings > Web search page's server side: the overview never holds a key and names the system, the choice and the keys go through, an install runs its steps
one by one for the detected system and stops with the reason, and the routes answer only the PC the server runs on."""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.websearch_api import WebSearchApi, WebSearchError
from tadamoo import websearch as ws
from tadamoo import websearch_runtime as rt
from tadamoo.llm.credentials import FileCredentials
from test_e2e_mock_server import _NoTelemetry, answer, start_server  # noqa: F401  (fixture)


class FakeRuntime(rt.Runtime):
    """Node.js is missing until its install steps have run; the steps only write a marker."""

    def __init__(self, base, os_name="windows", arch="x64", present=False, supported=True, note=""):
        super().__init__(base, {"os": os_name, "arch": arch, "supported": supported, "note": note})
        self.present = present

    def find(self):
        return {"node": "node", "npm": ["npm"], "bin": "bin", "version": "22.0.0", "source": "private"} if self.present else None

    def steps(self):
        if self.present:
            return []

        def get(say):
            say("downloaded")
            self.present = True
        return [("Download Node.js for this system and check its checksum", "https://nodejs.org/dist/<version>/node-<version>-win-x64.zip", get)]


class FakePackages(rt.Packages):
    def __init__(self, base, runtime, fail=False):
        super().__init__(base, runtime)
        self.have, self.fail = {}, fail

    def installed(self, package):
        return self.have.get(package, "")

    def steps(self, package):
        def install(say):
            if self.fail:
                raise rt.RuntimeProblem("npm could not install it (exit 1)")
            self.have[package] = "1.2.3"
            say(f"{package} 1.2.3 is installed")
        return [(f"Install the {package} server with npm, install scripts off", f"npm install --ignore-scripts {package}", install)]

    def remove(self, package):
        self.have.pop(package, None)


@pytest.fixture
def api(tmp_path):
    runtime = FakeRuntime(tmp_path)
    a = WebSearchApi(ws.Prefs(tmp_path / "w.json"), ws.Keys(FileCredentials(tmp_path / "c.json")), base_dir=tmp_path / "ws", runtime=runtime,
                     packages=FakePackages(tmp_path, runtime), up=lambda url: False, browser=lambda os_name: "msedge")
    a.fake_runtime = runtime
    return a


def wait(api, job_id):
    for _ in range(100):
        job = api.job(job_id)
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the job did not end")


def test_the_overview_names_the_system_lists_every_provider_and_never_holds_a_key(api):
    api.set_key("brave", "SECRETKEY123")
    view = api.overview()
    assert view["system"] == {"os": "windows", "arch": "x64", "supported": True, "note": ""} and view["runtime"] == {"found": False, "version": "", "source": ""}
    assert [p["id"] for p in view["providers"]] == list(ws.PROVIDERS) and view["search"] == "duckduckgo" and view["engines"][0] == "duckduckgo"
    assert "SECRETKEY123" not in json.dumps(view)
    brave = next(p for p in view["providers"] if p["id"] == "brave")
    assert brave["cloud"]["key_set"] and brave["cloud"]["keys_url"].startswith("https://") and brave["local"] is None and brave["modes"] == ["cloud"]
    open_search = next(p for p in view["providers"] if p["id"] == "openwebsearch")
    assert open_search["local"]["automatic"] and open_search["local"]["needs_present"] == {"node": False} and open_search["cloud"] is None and open_search["mode"] == "local"
    assert open_search["local"]["installed"] == "" and open_search["local"]["running"] is False
    searx = next(p for p in view["providers"] if p["id"] == "searxng")
    assert searx["local"]["automatic"] is False and searx["modes"] == ["local", "cloud"]
    assert next(p for p in view["providers"] if p["id"] == "firecrawl")["local"]["automatic"] is False
    assert "docker" not in json.dumps(view).lower()                                       # no Docker anywhere in what the page is told


def test_the_choice_and_the_keys_go_through_and_a_bad_one_is_refused(api):
    assert api.select({"search": "brave", "reader": "playwright", "engine": "bing"})["search"] == "brave"
    view = api.overview()
    assert view["reader"] == "playwright" and view["engine"] == "bing"
    assert api.select({"reader": "openwebsearch"})["reader"] == "openwebsearch"
    for bad in ({}, {"search": "bing"}, {"urls": {"searxng": "gopher://x"}}, {"engine": "yahoo"}, []):
        with pytest.raises(WebSearchError):
            api.select(bad)
    with pytest.raises(WebSearchError):
        api.set_key("duckduckgo", "x")
    api.set_key("brave", "")
    assert not next(p for p in api.overview()["providers"] if p["id"] == "brave")["cloud"]["key_set"]


def test_the_plan_is_shown_before_anything_runs_and_says_what_is_by_hand_and_why(api):
    plan = api.plan("openwebsearch")
    assert plan["automatic"] and plan["system"]["os"] == "windows" and "Detected system: windows x64" in plan["note"]
    assert [s["label"] for s in plan["steps"]][0].startswith("Download Node.js") and "install scripts off" in plan["steps"][1]["label"]
    assert not api.fake_runtime.present                                                   # reading the plan ran nothing
    searx = api.plan("searxng")
    assert not searx["automatic"] and searx["steps"] == [] and "no Windows support" in searx["note"] and "no Docker is needed" in searx["note"]
    assert "uses Docker" in api.plan("firecrawl")["note"]
    with pytest.raises(WebSearchError, match="no version"):
        api.plan("brave")
    for pid in ("searxng", "firecrawl"):
        with pytest.raises(WebSearchError) as manual:
            api.install(pid)
        assert manual.value.code == "manual"


def test_a_system_without_a_build_is_refused_with_the_reason(tmp_path):
    runtime = FakeRuntime(tmp_path, "linux", "x64", supported=False, note="This Linux uses musl")
    api = WebSearchApi(ws.Prefs(tmp_path / "w.json"), ws.Keys(FileCredentials(tmp_path / "c.json")), base_dir=tmp_path, runtime=runtime, packages=FakePackages(tmp_path, runtime))
    with pytest.raises(WebSearchError, match="musl") as refused:
        api.install("openwebsearch")
    assert refused.value.code == "unsupported"


def test_installing_runs_node_then_the_package_and_keeps_the_choice(api):
    job = wait(api, api.install("openwebsearch")["id"])
    assert job["state"] == "done" and job["step"] == job["steps"] == 2, job
    assert any("downloaded" in l for l in job["lines"]) and any("1.2.3 is installed" in l for l in job["lines"])
    assert api.fake_runtime.present and api.prefs.read()["mode"]["openwebsearch"] == "local"
    view = api.overview()
    local = next(p for p in view["providers"] if p["id"] == "openwebsearch")["local"]
    assert local["installed"] == "1.2.3" and view["runtime"]["found"] and local["needs_present"] == {"node": True}
    again = api.plan("openwebsearch")
    assert [s["label"] for s in again["steps"]] == ["Install the open-websearch server with npm, install scripts off"]      # Node is there: only the package is left


def test_the_playwright_plan_follows_the_browser_that_is_on_the_system(tmp_path):
    for channel, wanted in (("msedge", "Microsoft Edge"), ("chrome", "Google Chrome")):
        runtime = FakeRuntime(tmp_path, present=True)
        api = WebSearchApi(ws.Prefs(tmp_path / "w.json"), ws.Keys(FileCredentials(tmp_path / "c.json")), base_dir=tmp_path, runtime=runtime,
                           packages=FakePackages(tmp_path, runtime), browser=lambda os_name, c=channel: c)
        plan = api.plan("playwright")
        assert wanted in plan["note"] and not any("Chromium" in s["label"] for s in plan["steps"])
    api = WebSearchApi(ws.Prefs(tmp_path / "w.json"), ws.Keys(FileCredentials(tmp_path / "c.json")), base_dir=tmp_path, runtime=runtime,
                       packages=FakePackages(tmp_path, runtime), browser=lambda os_name: "chromium")
    plan = api.plan("playwright")
    assert "Chromium" in plan["steps"][-1]["label"] and "needs its own Chromium" in plan["note"]


def test_an_install_stops_at_the_step_that_failed_and_says_which(tmp_path):
    runtime = FakeRuntime(tmp_path)
    api = WebSearchApi(ws.Prefs(tmp_path / "w.json"), ws.Keys(FileCredentials(tmp_path / "c.json")), base_dir=tmp_path, runtime=runtime, packages=FakePackages(tmp_path, runtime, fail=True))
    job = wait(api, api.install("openwebsearch")["id"])
    assert job["state"] == "failed" and any("stopped: npm could not install it" in l for l in job["lines"]) and "openwebsearch" not in api.prefs.read()["mode"]


def test_one_install_of_a_provider_runs_at_a_time(api):
    release = []
    original = api.fake_runtime.steps

    def slow():
        def wait_for(say):
            while not release:
                time.sleep(0.01)
        return [("wait", "x", wait_for)] + original()
    api.runtime.steps = slow
    first = api.install("openwebsearch")
    with pytest.raises(WebSearchError) as busy:
        api.install("openwebsearch")
    assert busy.value.code == "busy"
    release.append(1)
    wait(api, first["id"])
    with pytest.raises(WebSearchError) as gone:
        api.job("nope")
    assert gone.value.code == "not_found"


def test_the_test_button_reports_a_missing_key_or_the_results(api, monkeypatch):
    assert api.test("brave") == {"ok": False, "message": "it is not set up: a key or an address is missing"}
    monkeypatch.setattr(ws, "brave_search", lambda key, q, n: [{"url": "https://a.example/"}])
    api.set_key("brave", "K")
    out = api.test("brave")
    assert out["ok"] and out["count"] == 1 and out["what"] == "results"
    monkeypatch.setattr(ws, "brave_search", lambda key, q, n: (_ for _ in ()).throw(ws.SearchError("the key was refused")))
    assert api.test("brave") == {"ok": False, "message": "the key was refused"}
    monkeypatch.setattr(ws, "openwebsearch_search", lambda q, n, prefs=None: [{"url": "https://o.example/"}])
    assert api.test("openwebsearch")["ok"]
    with pytest.raises(WebSearchError):
        api.test("nobody")


def test_uninstall_removes_the_package_and_stops_the_servers_and_refuses_what_it_did_not_install(api):
    wait(api, api.install("openwebsearch")["id"])
    api.uninstall("openwebsearch")
    assert api.packages.installed("open-websearch") == ""
    api.stop("openwebsearch")
    for pid in ("searxng", "brave", "firecrawl"):
        with pytest.raises(WebSearchError):
            api.uninstall(pid)


# -- the routes -------------------------------------------------------------------------------------------------------


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_routes_work_end_to_end_and_hide_the_key(api, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setattr(handler, "_WEBSEARCH", api)
    base = start_server([answer("x")])
    assert call(base, "GET", "/api/websearch")[1]["search"] == "duckduckgo"
    code, body = call(base, "POST", "/api/websearch/key", {"provider": "tavily", "key": "TVLY-secret-1"})
    assert code == 200 and "TVLY-secret-1" not in json.dumps(body)
    assert call(base, "POST", "/api/websearch/select", {"search": "tavily"})[1]["search"] == "tavily"
    assert call(base, "POST", "/api/websearch/select", {"search": "bing"})[0] == 400
    assert call(base, "GET", "/api/websearch/plan?provider=openwebsearch")[1]["automatic"] is True
    assert call(base, "GET", "/api/websearch/plan?provider=brave")[0] == 400
    code, job = call(base, "POST", "/api/websearch/install", {"provider": "openwebsearch"})
    assert code == 200 and job["provider"] == "openwebsearch"
    assert call(base, "GET", "/api/websearch/job?id=" + job["id"])[0] == 200
    assert call(base, "GET", "/api/websearch/job?id=zzz")[0] == 404
    assert call(base, "POST", "/api/websearch/stop", {"provider": "openwebsearch"})[0] == 200
    assert call(base, "POST", "/api/websearch/elsewhere", {})[0] == 404
