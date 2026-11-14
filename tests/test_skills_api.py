"""The /api/skills routes against the real server: the list with states and sizes, install as a job, turn on and off (seen by the harness), remove, an address read before
anything is installed, and the refusals."""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler, skills_api
from serve.capabilities_api import CapabilityApi
from tadamoo.skillmanager import SkillManager
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_importer import make_skill
from test_skillmanager import MADE, PAGE, Model, Net, skill_md, zip_of


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


@pytest.fixture
def world(tmp_path, monkeypatch, start_server):  # noqa: F811
    data = tmp_path / "data"
    monkeypatch.setenv("TADAMOO_DATA", str(data))
    monkeypatch.setattr(handler, "_IMPORT", None)
    caps = CapabilityApi(data / "capabilities", tmp_path / "ws", home=tmp_path / "nohome", appdata=tmp_path / "noapp")
    monkeypatch.setattr(handler, "_CAPS", caps)
    asked, gate = [], threading.Event()
    gate.set()

    def installer(name, job):
        asked.append(name)
        job.say(f"downloading for {name}")
        while not gate.wait(0.01):
            job.check()
        return {"ready": True, "by_hand": []}
    mgr = SkillManager(data, installer=installer, on_change=caps.rescan, fetcher=Net({"https://x.org/minutes": ("text/html", PAGE.encode()),
                                                                                       "https://github.com/o/r/archive/HEAD.zip": zip_of({"a/SKILL.md": skill_md("gh-one")})}))
    monkeypatch.setattr(handler, "_SKILLS", skills_api.SkillsApi(mgr))
    return Api(start_server([answer("x")])), tmp_path, asked, gate, mgr


def entry(api, entry_id):
    return next((e for e in api("GET", "/api/capabilities")[1]["entries"] if e["id"] == entry_id), None)


def wait_job(api, states=("done", "failed", "cancelled")):
    for _ in range(300):
        job = api("GET", "/api/skills/job")[1]["job"]
        if job and job["state"] in states:
            return job
        time.sleep(0.02)
    raise AssertionError("the job did not end")


def test_the_list_has_every_skill_that_ships_with_its_state_size_and_where_things_are(world):
    api, tmp, asked, gate, mgr = world
    code, view = api("GET", "/api/skills")
    rows = {r["name"]: r for r in view["skills"]}
    assert code == 200 and view["job"] is None and {r["state"] for r in rows.values()} == {"not_installed"}
    assert rows["video-modification"]["download_gb"] == 60 and rows["video-modification"]["has_models"] and rows["video-modification"]["default"] is False
    assert rows["news-research"]["has_models"] is False and rows["ai-newscast"]["will_install"][-1] == "ai-newscast"
    assert Path(view["where"]["state"]) == tmp / "data" / "skills" / "choices.json" and rows["image-generation"]["title"]["it"]
    assert api("GET", "/api/skills/nothing")[0] == 404


def test_installing_is_a_job_that_copies_downloads_only_what_was_asked_and_the_harness_sees_it(world):
    api, tmp, asked, gate, mgr = world
    code, started = api("POST", "/api/skills/install", {"names": ["news-research", "talking-head"]})
    assert code == 200 and started["job"]["state"] in ("running", "done") and started["job"]["names"] == ["news-research", "talking-head"]
    job = wait_job(api)
    assert job["state"] == "done" and job["result"]["installed"] == ["talking-head", "news-research"] and asked == ["talking-head"]
    assert "downloading for talking-head" in job["lines"]
    rows = {r["name"]: r for r in api("GET", "/api/skills")[1]["skills"]}
    assert rows["news-research"]["state"] == "installed" and rows["talking-head"]["models"] == "ready" and rows["video-modification"]["state"] == "not_installed"
    assert entry(api, "skill:news-research")["state"] == "approved"                    # the harness reads the new copy at once


def test_turning_a_skill_off_and_on_is_seen_by_the_harness_and_downloads_nothing(world):
    api, tmp, asked, gate, mgr = world
    api("POST", "/api/skills/install", {"names": ["news-research"]})
    wait_job(api)
    code, row = api("POST", "/api/skills/disable", {"name": "news-research"})
    assert code == 200 and row["state"] == "disabled" and entry(api, "skill:news-research")["state"] == "off"
    code, row = api("POST", "/api/skills/enable", {"name": "news-research"})
    assert row["state"] == "installed" and entry(api, "skill:news-research")["state"] == "approved" and asked == []
    assert api("POST", "/api/skills/disable", {"name": "talking-head"})[0] == 404          # not installed


def test_remove_says_whether_the_models_stay_and_the_skill_is_gone_from_the_harness(world):
    api, tmp, asked, gate, mgr = world
    api("POST", "/api/skills/install", {"names": ["talking-head"]})
    wait_job(api)
    code, out = api("POST", "/api/skills/remove", {"name": "talking-head"})
    assert code == 200 and out == {"removed": "talking-head", "models_kept": True, "needed_by": []}
    assert entry(api, "skill:talking-head") is None and api("POST", "/api/skills/remove", {"name": "talking-head"})[0] == 404


def test_an_address_is_read_before_anything_is_installed_and_the_choice_is_installed_by_its_token(world):
    api, tmp, asked, gate, mgr = world
    repo = tmp / "clone"
    make_skill(repo, "alpha")
    make_skill(repo, "beta")
    code, plan = api("POST", "/api/skills/plan", {"address": str(repo)})
    assert code == 200 and [c["id"] for c in plan["candidates"]] == ["alpha", "beta"] and entry(api, "skill:alpha") is None
    api("POST", "/api/skills/install", {"token": plan["token"], "only": ["beta"]})
    assert wait_job(api)["result"]["installed"] == ["beta"] and entry(api, "skill:beta")["state"] == "approved" and entry(api, "skill:alpha") is None
    assert api("POST", "/api/skills/discard", {"token": plan["token"]}) == (200, {"discarded": True})
    assert api("POST", "/api/skills/install", {"token": plan["token"]})[0] == 404           # forgotten


def test_a_github_address_and_a_page_the_model_writes_a_skill_from(world):
    api, tmp, asked, gate, mgr = world
    code, plan = api("POST", "/api/skills/plan", {"address": "https://github.com/o/r"})
    assert code == 200 and plan["type"] == "github" and plan["candidates"][0]["name"] == "gh-one"
    assert api("POST", "/api/skills/plan", {"address": "https://github.com/o/gone"})[0] == 502
    assert api("POST", "/api/skills/plan", {"address": "https://x.org/minutes"})[1]["error"]["code"] == "no_model"
    mgr.model = lambda: Model(MADE)
    code, plan = api("POST", "/api/skills/plan", {"address": "https://x.org/minutes"})
    assert code == 200 and plan["generated"] and "Collect the decisions" in plan["candidates"][0]["skill_md"] and entry(api, "skill:meeting-minutes") is None
    api("POST", "/api/skills/install", {"token": plan["token"]})
    assert wait_job(api)["state"] == "done" and entry(api, "skill:meeting-minutes")["state"] == "approved"


def test_one_installation_at_a_time_and_it_can_be_stopped(world):
    api, tmp, asked, gate, mgr = world
    gate.clear()
    api("POST", "/api/skills/install", {"names": ["talking-head"]})
    for _ in range(200):
        if "downloading for talking-head" in (api("GET", "/api/skills/job")[1]["job"] or {}).get("lines", []):
            break
        time.sleep(0.02)
    code, err = api("POST", "/api/skills/install", {"names": ["image-generation"]})
    assert code == 409 and err["error"]["code"] == "busy"
    code, out = api("POST", "/api/skills/job/cancel", {})
    assert out["cancelled"] is True
    assert wait_job(api)["state"] == "cancelled" and api("POST", "/api/skills/job/cancel", {})[1]["cancelled"] is False


def test_models_are_downloaded_or_checked_only_for_an_installed_skill(world, monkeypatch):
    from tadamoo.media import components
    api, tmp, asked, gate, mgr = world
    assert api("POST", "/api/skills/models", {"name": "talking-head"})[0] == 404 and asked == []                  # not installed: nothing downloaded
    api("POST", "/api/skills/install", {"names": ["image-editing"], "models": False})
    wait_job(api)
    assert asked == [] and {r["name"]: r for r in api("GET", "/api/skills")[1]["skills"]}["image-editing"]["models"] == "unknown"
    monkeypatch.setattr(components, "check", lambda name, settings, command=None: [{"id": "image-model", "label": "Q", "label_it": "Q", "present": False}])
    code, out = api("POST", "/api/skills/models/check", {"name": "image-editing"})
    assert code == 200 and out["ready"] is False and {r["name"]: r for r in api("GET", "/api/skills")[1]["skills"]}["image-editing"]["models"] == "missing"
    api("POST", "/api/skills/models", {"name": "image-editing"})
    assert wait_job(api)["result"] == {"name": "image-editing", "ready": True, "by_hand": []} and asked == ["image-editing"]
    assert api("POST", "/api/skills/models/check", {"name": "video-modification"})[0] == 404


def test_update_says_what_would_change_and_replaces_only_on_the_call(world):
    api, tmp, asked, gate, mgr = world
    src = make_skill(tmp / "src", "solo", body="One.")
    plan = api("POST", "/api/skills/plan", {"address": str(src)})[1]
    api("POST", "/api/skills/install", {"token": plan["token"]})
    wait_job(api)
    assert api("POST", "/api/skills/update", {"name": "solo"})[1]["status"] == "unchanged"
    (src / "SKILL.md").write_text(skill_md("solo", "Two."), encoding="utf-8")
    assert api("POST", "/api/skills/update", {"name": "solo", "check": True})[1]["source_changed"] is True
    assert api("POST", "/api/skills/update", {"name": "solo"})[1]["status"] == "updated"


def test_bad_requests_are_told_in_words(world):
    api, tmp, asked, gate, mgr = world
    assert api("POST", "/api/skills/install", {})[0] == 400
    assert api("POST", "/api/skills/install", {"names": "talking-head"})[0] == 400
    code, err = api("POST", "/api/skills/install", {"names": ["nothing"]})
    assert code == 404 and err["error"]["code"] == "unknown_skill" and err["error"]["type"] == "skills"
    assert api("POST", "/api/skills/plan", {})[0] == 400 and api("POST", "/api/skills/plan", {"address": "not an address"})[0] == 400
    assert api("POST", "/api/skills/remove", {"name": ""})[0] == 400 and api("POST", "/api/skills/other", {})[0] == 404
    assert asked == []


def test_only_the_pc_the_server_runs_on_may_manage_skills(world, monkeypatch):
    api, tmp, asked, gate, mgr = world
    monkeypatch.setattr(handler.settings, "is_loopback", lambda ip: False)
    code, err = api("GET", "/api/skills")
    assert code == 403 and err["error"]["type"] == "forbidden"
    assert api("POST", "/api/skills/install", {"names": ["news-research"]})[0] == 403 and asked == [] and not (tmp / "data" / "capabilities" / "imported").exists()
