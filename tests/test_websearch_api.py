"""The Settings > Web search page's server side: the overview never holds a key, the choice and the keys go through, an install runs its steps one by one and
stops with the reason, and the routes answer only the PC the server runs on."""
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
from tadamoo.llm.credentials import FileCredentials
from test_e2e_mock_server import _NoTelemetry, answer, start_server  # noqa: F401  (fixture)


class Done:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout, self.stderr = code, out, ""


class Fake:
    """Stands in for the programs the installers run, and for the address they start."""

    def __init__(self, docker=True, fail=None):
        self.calls, self.fail, self.up_now = [], fail, False
        self.present = {"docker": docker, "git": True, "node": True, "npx": True}

    def run(self, argv, timeout):
        self.calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return Done(0, "tadamoo-searxng running\n" if self.up_now else "")
        if self.fail and argv[:2] == self.fail:
            return Done(1, "boom")
        if argv[:2] == ["docker", "run"]:
            self.up_now = True
        return Done(0, "ok")


@pytest.fixture
def api(tmp_path, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(ws, "searxng_search", lambda url, q, n: [{"title": "t", "url": "https://t.example/", "snippet": ""}])
    a = WebSearchApi(ws.Prefs(tmp_path / "w.json"), ws.Keys(FileCredentials(tmp_path / "c.json")), base_dir=tmp_path / "ws", run=fake.run,
                     tools=lambda: fake.present, up=lambda url: fake.up_now)
    a.fake = fake
    return a


def wait(api, job_id):
    for _ in range(100):
        job = api.job(job_id)
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the job did not end")


def test_the_overview_lists_every_provider_with_its_two_versions_and_never_a_key(api):
    api.set_key("brave", "SECRETKEY123")
    view = api.overview()
    assert [p["id"] for p in view["providers"]] == list(ws.PROVIDERS) and view["search"] == "duckduckgo" and view["tools"]["docker"]
    assert "SECRETKEY123" not in json.dumps(view)
    brave = next(p for p in view["providers"] if p["id"] == "brave")
    assert brave["cloud"]["key_set"] and brave["cloud"]["keys_url"].startswith("https://") and brave["local"] is None and brave["modes"] == ["cloud"]
    searx = next(p for p in view["providers"] if p["id"] == "searxng")
    assert searx["local"]["automatic"] and searx["local"]["needs_present"] == {"docker": True} and searx["modes"] == ["local", "cloud"] and searx["mode"] == "local"
    assert next(p for p in view["providers"] if p["id"] == "firecrawl")["local"]["automatic"] is False


def test_the_choice_and_the_keys_go_through_and_a_bad_one_is_refused(api):
    assert api.select({"search": "brave", "reader": "playwright"})["search"] == "brave"
    assert api.overview()["reader"] == "playwright"
    for bad in ({}, {"search": "bing"}, {"urls": {"searxng": "gopher://x"}}, []):
        with pytest.raises(WebSearchError):
            api.select(bad)
    with pytest.raises(WebSearchError):
        api.set_key("duckduckgo", "x")
    api.set_key("brave", "")                                                              # an empty key removes it
    assert not next(p for p in api.overview()["providers"] if p["id"] == "brave")["cloud"]["key_set"]


def test_the_plan_is_shown_before_anything_runs_and_a_provider_set_up_by_hand_says_so(api):
    plan = api.plan("searxng")
    assert plan["automatic"] and plan["steps"][0]["command"] == "docker version" and "127.0.0.1:8888:8080" in plan["steps"][4]["command"]
    assert api.fake.calls == []                                                           # reading the plan runs nothing
    assert api.plan("playwright")["automatic"] and not api.plan("firecrawl")["automatic"]
    with pytest.raises(WebSearchError, match="no version"):
        api.plan("brave")
    api.select({"urls": {"searxng": "http://192.168.1.9:8888"}})
    with pytest.raises(WebSearchError, match="only on this PC"):
        api.plan("searxng")
    with pytest.raises(WebSearchError) as manual:
        api.install("firecrawl")
    assert manual.value.code == "manual"


def test_installing_searxng_runs_the_steps_in_order_writes_its_settings_and_keeps_the_choice(api, tmp_path):
    job = wait(api, api.install("searxng")["id"])
    assert job["state"] == "done" and job["step"] == job["steps"] == 6, job
    commands = [" ".join(c[:2]) for c in api.fake.calls if c[0] == "docker" and c[1] != "ps"]
    assert commands == ["docker version", "docker pull", "docker rm", "docker run"]
    run = next(c for c in api.fake.calls if c[:2] == ["docker", "run"])
    assert "127.0.0.1:8888:8080" in run and "--restart" in run
    text = (tmp_path / "ws" / "searxng" / "settings.yml").read_text(encoding="utf-8")
    assert "- json" in text and "limiter: false" in text and "ultrasecretkey" not in text
    saved = api.prefs.read()
    assert saved["mode"]["searxng"] == "local" and saved["urls"]["searxng"] == "http://127.0.0.1:8888"
    again = (tmp_path / "ws" / "searxng" / "settings.yml").read_text(encoding="utf-8")
    wait(api, api.install("searxng")["id"])
    assert (tmp_path / "ws" / "searxng" / "settings.yml").read_text(encoding="utf-8") == again       # a second install keeps the secret key of the first


def test_an_install_stops_at_the_step_that_failed_and_says_which(api):
    api.fake.fail = ["docker", "pull"]
    job = wait(api, api.install("searxng")["id"])
    assert job["state"] == "failed" and any("docker failed" in line for line in job["lines"]) and not any(c[:2] == ["docker", "run"] for c in api.fake.calls)


def test_an_install_needs_its_programs_and_one_runs_at_a_time(api):
    api.fake.present["docker"] = False
    with pytest.raises(WebSearchError) as missing:
        api.install("searxng")
    assert missing.value.code == "missing_tool" and "docker" in str(missing.value)
    api.fake.present["docker"] = True
    release = []
    original = api.fake.run

    def blocking(argv, timeout):
        while not release:
            time.sleep(0.01)
        return original(argv, timeout)
    api._run = blocking
    first = api.install("searxng")
    with pytest.raises(WebSearchError) as busy:
        api.install("searxng")
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
    with pytest.raises(WebSearchError):
        api.test("nobody")


def test_uninstall_removes_the_container_and_refuses_what_it_did_not_install(api):
    api.uninstall("searxng")
    assert ["docker", "rm", "-f", "tadamoo-searxng"] in api.fake.calls
    with pytest.raises(WebSearchError):
        api.uninstall("brave")


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
    assert call(base, "GET", "/api/websearch/plan?provider=searxng")[1]["automatic"] is True
    assert call(base, "GET", "/api/websearch/plan?provider=brave")[0] == 400
    code, job = call(base, "POST", "/api/websearch/install", {"provider": "playwright"})
    assert code == 200 and job["provider"] == "playwright"
    assert call(base, "GET", "/api/websearch/job?id=" + job["id"])[0] == 200
    assert call(base, "GET", "/api/websearch/job?id=zzz")[0] == 404
    assert call(base, "POST", "/api/websearch/elsewhere", {})[0] == 404
