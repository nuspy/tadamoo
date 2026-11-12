"""The /api/import routes against the real server: import, re-import and delete of each kind, and that the harness sees the copies."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.capabilities_api import CapabilityApi
from serve.plans import PlanService
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_importer import digest_dir, make_personal, make_plan, make_plugin, make_skill, write
from test_importer import MCP_CONFIG, PLAN


class Api:
    def __init__(self, url):
        self.url = url

    def __call__(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
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
    monkeypatch.setattr(handler, "_PLANS", PlanService(data / "planner"))
    monkeypatch.setattr(handler, "_CAPS", CapabilityApi(data / "capabilities", tmp_path / "ws", home=tmp_path / "nohome", appdata=tmp_path / "noapp"))
    repo = tmp_path / "repo"
    repo.mkdir()
    return Api(start_server([answer("x")])), repo, data


def entry(api, entry_id):
    return next((e for e in api("GET", "/api/capabilities")[1]["entries"] if e["id"] == entry_id), None)


def test_nothing_is_imported_at_first_and_the_overview_says_where_the_copies_live(world):
    api, repo, data = world
    code, view = api("GET", "/api/import")
    assert code == 200 and view["kinds"] == {"skill": [], "plugin": [], "mcp": [], "plan": []}
    assert Path(view["where"]["copies"]) == data / "capabilities" / "imported" and Path(view["where"]["plans"]) == data / "planner" / "plans"
    assert api("GET", "/api/import/skill")[1] == {"items": []} and api("GET", "/api/import/nothing")[0] == 404


def test_a_skill_is_verified_imported_seen_by_the_harness_reimported_and_deleted(world):
    api, repo, data = world
    folder = make_skill(repo, "news-research", body="One.\n")
    before = digest_dir(repo)
    code, check = api("POST", "/api/import/skill/verify", {"path": str(folder)})
    assert code == 200 and check["can_import"] and check["snapshot"]["name"] == "news-research" and check["snapshot"]["files"] == 2
    assert not (data / "capabilities" / "imported").exists()                               # verifying writes nothing
    code, done = api("POST", "/api/import/skill/import", {"path": str(folder)})
    assert code == 200 and done["imported"]["source"] == str(folder)
    assert entry(api, "skill:news-research")["origin"] == "imported"                       # the capability index was told
    assert api("POST", "/api/import/skill/import", {"path": str(folder)})[0] == 409
    assert api("GET", "/api/import/skill")[1]["items"][0]["local_edited"] is False
    write(folder / "SKILL.md", "---\nname: news-research\ndescription: Reads a thing.\n---\n\nOne.\nTwo.\n")
    code, preview = api("POST", "/api/import/skill/reimport", {"name": "news-research"})
    assert code == 200 and preview["status"] == "preview" and preview["diff"]["changed"][0]["plus"] == 1
    code, out = api("POST", "/api/import/skill/reimport", {"name": "news-research", "confirm": True})
    assert code == 200 and out["status"] == "replaced"
    copy = data / "capabilities" / "imported" / "skills" / "news-research" / "SKILL.md"
    write(copy, "---\nname: news-research\ndescription: mine\n---\n\nedited here\n")
    write(folder / "SKILL.md", "---\nname: news-research\ndescription: Reads a thing.\n---\n\nThree.\n")
    code, err = api("POST", "/api/import/skill/reimport", {"name": "news-research", "confirm": True})
    assert code == 409 and err["error"]["code"] == "local_edits" and "edited here" in copy.read_text(encoding="utf-8")
    assert api("POST", "/api/import/skill/reimport", {"name": "news-research", "confirm": True, "overwrite_local": True})[0] == 200
    snapshot = digest_dir(repo)
    code, gone = api("POST", "/api/import/skill/delete", {"name": "news-research"})
    assert code == 200 and gone["deleted"] == "skill:news-research" and entry(api, "skill:news-research") is None and digest_dir(repo) == snapshot
    assert api("POST", "/api/import/skill/delete", {"name": "news-research"})[0] == 404
    assert before != snapshot                                                              # (the test edited the repo itself)


def test_an_mcp_server_starts_off_and_the_page_can_turn_it_on(world):
    api, repo, data = world
    cfg = write(repo / ".mcp.json", json.dumps(MCP_CONFIG))
    code, err = api("POST", "/api/import/mcp/verify", {"path": str(cfg)})
    assert code == 400 and err["error"]["code"] == "choose_server" and err["error"]["servers"] == ["remote", "stocks"]
    code, check = api("POST", "/api/import/mcp/verify", {"path": str(cfg), "server": "stocks"})
    assert code == 200 and check["snapshot"]["runs"].startswith("node server.js") and check["snapshot"]["needs_keys"][0]["entry"] == "STOCK_TOKEN"
    assert api("POST", "/api/import/mcp/import", {"path": str(cfg), "server": "stocks"})[0] == 200
    assert entry(api, "mcp:stocks")["state"] == "off" and api("GET", "/api/import/mcp")[1]["items"][0]["enabled"] is False
    assert api("POST", "/api/import/mcp/enable", {"name": "stocks", "enabled": True})[1]["enabled"] is True
    assert entry(api, "mcp:stocks")["state"] == "approved"
    assert api("POST", "/api/import/mcp/enable", {"name": "ghost", "enabled": True})[0] == 404
    assert api("POST", "/api/import/mcp/delete", {"name": "stocks"})[0] == 200 and entry(api, "mcp:stocks") is None


def test_a_plan_is_imported_into_the_planner_and_a_reimport_keeps_what_it_did(world):
    api, repo, data = world
    f = make_plan(repo)
    assert api("POST", "/api/import/plan/import", {"path": str(f)})[0] == 200
    code, listing = api("GET", "/api/plans")
    assert code == 200 and [p["plan_id"] for p in listing["plans"]] == ["newscast"]
    assert api("GET", "/api/plans/newscast")[1]["plan"]["title"] == "Newscast"
    handler._PLANS.store.get("newscast").dag.nodes["ask"].status = "completed"
    handler._PLANS.store.save("newscast")
    assert api("GET", "/api/import/plan")[1]["items"][0]["ran"] is True
    write(f, json.dumps({**PLAN, "title": "Newscast, second edition"}))
    code, out = api("POST", "/api/import/plan/reimport", {"name": "newscast", "confirm": True})
    assert code == 200 and out["status"] == "replaced"
    snap = api("GET", "/api/plans/newscast")[1]
    assert snap["plan"]["title"] == "Newscast, second edition" and snap["plan"]["nodes"]["ask"]["status"] == "completed"
    assert api("POST", "/api/import/plan/delete", {"name": "newscast"})[0] == 200 and api("GET", "/api/plans")[1]["plans"] == []


def test_a_plugin_is_imported_with_its_parts_and_dependencies_and_deleted_with_them(world):
    api, repo, data = world
    p = make_plugin(repo)
    make_skill(repo, "shared-research")
    code, check = api("POST", "/api/import/plugin/verify", {"path": str(p)})
    assert code == 200 and check["can_import"] and {x["kind"] for x in check["parts"]} == {"skill", "plan", "mcp"}
    assert check["snapshot"]["dependencies"][0] == {"kind": "skill", "name": "shared-research", "why": "research", "source": check["snapshot"]["dependencies"][0]["source"],
                                                   "exists": True, "imported": False}
    assert api("POST", "/api/import/plugin/import", {"path": str(p), "with_dependencies": True})[0] == 200
    assert entry(api, "skill:news-skill")["origin"] == "imported" and entry(api, "skill:shared-research") and entry(api, "mcp:news-feed")["state"] == "off"
    assert [x["plan_id"] for x in api("GET", "/api/plans")[1]["plans"]] == ["newscast"]
    assert api("POST", "/api/import/skill/delete", {"name": "news-skill"})[1]["error"]["code"] == "part"
    code, out = api("POST", "/api/import/plugin/delete", {"name": "news"})
    assert code == 200 and len(out["removed"]) == 4
    assert entry(api, "skill:news-skill") is None and entry(api, "mcp:news-feed") is None and entry(api, "skill:shared-research") and api("GET", "/api/plans")[1]["plans"] == []


def test_a_personal_skill_goes_among_the_personal_skills(world):
    api, repo, data = world
    assert api("POST", "/api/import/skill/import", {"path": str(make_personal(repo))})[0] == 200
    assert [s["slug"] for s in api("GET", "/api/personal-skills")[1]["skills"]] == ["daily-note"]
    assert api("POST", "/api/import/skill/delete", {"name": "daily-note"})[0] == 200 and api("GET", "/api/personal-skills")[1]["skills"] == []


def test_bad_requests_get_plain_answers_and_climbing_paths_are_not_followed(world):
    api, repo, data = world
    assert api("POST", "/api/import/skill/verify", {"path": str(repo / "nothing")})[1]["error"]["code"] == "no_source"
    assert api("POST", "/api/import/skill/verify", {})[1]["error"]["code"] == "bad_request"
    assert api("POST", "/api/import/skill/verify", raw=b"not json")[0] == 400
    assert api("POST", "/api/import/skill/reimport", {"name": "../../x", "confirm": True})[0] == 404
    assert api("POST", "/api/import/skill/delete", {"name": "..\\..\\x"})[0] == 404
    assert api("POST", "/api/import/widget/import", {"path": "x"})[0] == 404
    assert api("POST", "/api/import/skill/explode", {"name": "x"})[0] == 404
    p = make_plugin(repo, with_dep=False, mcp=False)
    spec = json.loads((p / "plugin.json").read_text(encoding="utf-8"))
    spec["plans"] = ["../../../outside.plan.json"]
    write(p / "plugin.json", json.dumps(spec))
    code, err = api("POST", "/api/import/plugin/import", {"path": str(p)})
    assert code == 400 and err["error"]["code"] == "outside" and api("GET", "/api/import")[1]["kinds"]["skill"] == []
