"""The /api/register routes against the real server: a tree for each folder of the Agentic coding tab, edits marked as the person's, delete means gone, plan lines, and nothing outside the
folders the person added."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler, register_api
from tadamoo.taskboard import TaskBoard
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_taskboard import make_plan


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
    a, b = tmp_path / "site", tmp_path / "app"
    a.mkdir()
    b.mkdir()
    plans = tmp_path / "plans"
    make_plan(plans)
    monkeypatch.setattr(handler, "_REGISTER", register_api.RegisterApi(lambda: [{"path": str(a), "name": "Site"}, {"path": str(b), "name": "App"}, {"path": str(tmp_path / "gone"), "name": "Gone"}], plans))
    return Api(start_server([answer("x")])), a, b, plans


def test_all_the_projects_are_listed_together_and_one_can_be_asked_for_with_a_state(world):
    api, a, b, plans = world
    TaskBoard(a, plans).add("Move the site", subtasks=[{"title": "DNS", "state": "waiting"}], state="running")
    TaskBoard(b, plans).add("Fix the build", state="blocked")
    code, view = api("GET", "/api/register")
    assert code == 200 and [p["name"] for p in view["projects"]] == ["Site", "App"]                      # a folder that is gone is not listed
    assert [t["title"] for t in view["projects"][0]["tasks"]] == ["Move the site", "DNS"] and view["projects"][0]["tasks"][1]["depth"] == 1
    assert view["counts"] == {"queued": 0, "running": 1, "waiting": 1, "blocked": 1} and view["waiting"] == 1
    only = api("GET", f"/api/register?project={urllib.request.quote(str(b))}")[1]
    assert [p["name"] for p in only["projects"]] == ["App"]
    blocked = api("GET", "/api/register?state=blocked")[1]
    assert [t["title"] for p in blocked["projects"] for t in p["tasks"]] == ["Fix the build"]
    assert api("GET", "/api/register?state=nope")[0] == 400 and api("GET", f"/api/register?project={urllib.request.quote(str(a.parent))}")[0] == 400


def test_the_person_adds_edits_reorders_and_deletes_and_what_they_change_is_marked_theirs(world):
    api, a, b, plans = world
    code, made = api("POST", "/api/register/add", {"project": str(a), "title": "Write the book", "description": "ten chapters", "subtasks": [{"title": "Outline"}, {"title": "Chapter 1"}]})
    assert code == 200 and made["task"]["by"] == "user" and made["task"]["children"] == 2
    top = made["id"]
    tasks = api("GET", "/api/register")[1]["projects"][0]["tasks"]
    outline, chapter = tasks[1]["id"], tasks[2]["id"]
    code, upd = api("POST", "/api/register/update", {"project": str(a), "id": top, "title": "Write the novel", "state": "running", "done": "outline", "missing": "chapters"})
    assert code == 200 and upd["task"]["title"] == "Write the novel" and upd["task"]["state"] == "running" and upd["task"]["missing"] == "chapters" and upd["task"]["by"] == "user"
    api("POST", "/api/register/move", {"project": str(a), "id": chapter, "before": outline})
    assert [t["title"] for t in api("GET", "/api/register")[1]["projects"][0]["tasks"]] == ["Write the novel", "Chapter 1", "Outline"]
    api("POST", "/api/register/subtasks", {"project": str(a), "id": top, "subtasks": [{"id": outline, "title": "Outline"}, {"title": "A new subtask"}]})
    assert [t["title"] for t in api("GET", "/api/register")[1]["projects"][0]["tasks"]] == ["Write the novel", "Outline", "A new subtask"]
    code, gone = api("POST", "/api/register/delete", {"project": str(a), "id": top})
    assert code == 200 and len(gone["deleted"]) == 3 and api("GET", "/api/register")[1]["projects"][0]["tasks"] == []
    assert "Write the novel" not in (a / ".tadamoo" / "tasks.json").read_text(encoding="utf-8")        # deleted, not archived


def test_a_task_linked_to_a_plan_is_a_line_with_the_plans_state_and_resume_names_the_plan(world):
    api, a, b, plans = world
    t = api("POST", "/api/register/add", {"project": str(a), "title": "Make the newscast"})[1]["id"]
    code, linked = api("POST", "/api/register/link-plan", {"project": str(a), "id": t, "plan_id": "newscast"})
    assert code == 200 and linked["task"]["state"] == "waiting" and linked["task"]["done"] == "1/3 steps"
    assert api("POST", "/api/register/link-plan", {"project": str(a), "id": t, "plan_id": "nothing"})[0] == 400
    code, words = api("POST", "/api/register/resume", {"project": str(a), "id": t})
    assert code == 200 and 'plan "Newscast" (newscast)' in words["text"] and words["project"] == str(a)
    plain = api("POST", "/api/register/add", {"project": str(a), "title": "Free work"})[1]["id"]
    text = api("POST", "/api/register/resume", {"project": str(a), "id": plain})[1]["text"]
    assert f"task_resume with that id" in text and plain in text
    assert api("GET", "/api/register")[1]["projects"][0]["tasks"][1]["links"]["session"] == ""             # resuming only words the request: the agent takes the task


def test_nothing_outside_the_folders_of_the_agent_tab_is_written_and_bad_requests_are_told(world, tmp_path):
    api, a, b, plans = world
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert api("POST", "/api/register/add", {"project": str(other), "title": "x"})[0] == 400 and not (other / ".tadamoo").exists()
    assert api("POST", "/api/register/add", {"project": str(a), "title": ""})[0] == 400
    assert api("POST", "/api/register/update", {"project": str(a), "id": "t-none", "title": "x"})[0] == 400
    assert api("POST", "/api/register/update", {"project": str(a), "id": api("POST", "/api/register/add", {"project": str(a), "title": "t"})[1]["id"], "state": "finished"})[0] == 400
    assert api("POST", "/api/register/subtasks", {"project": str(a), "id": "x", "subtasks": "no"})[0] == 400 and api("POST", "/api/register/other", {"project": str(a)})[0] == 400
