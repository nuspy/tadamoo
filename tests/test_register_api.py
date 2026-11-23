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
    assert view["counts"] == {"queued": 0, "running": 1, "waiting": 1, "blocked": 1, "review": 0, "decision": 0} and view["waiting"] == 1
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
    code, gone = api("POST", "/api/register/close", {"project": str(a), "id": top})
    assert code == 200 and len(gone["closed"]) == 3 and api("GET", "/api/register")[1]["projects"][0]["tasks"] == []
    assert "Write the novel" in (a / ".tadamoo" / "tasks.json").read_text(encoding="utf-8")            # closed: in the history
    code, gone = api("POST", "/api/register/delete", {"project": str(a), "id": top})
    assert code == 200 and len(gone["deleted"]) == 3
    assert "Write the novel" not in (a / ".tadamoo" / "tasks.json").read_text(encoding="utf-8")        # deleted for good


def test_a_task_linked_to_a_plan_is_a_line_with_the_plans_state_and_resume_names_the_plan(world):
    api, a, b, plans = world
    t = api("POST", "/api/register/add", {"project": str(a), "title": "Make the briefing"})[1]["id"]
    code, linked = api("POST", "/api/register/link-plan", {"project": str(a), "id": t, "plan_id": "briefing"})
    assert code == 200 and linked["task"]["state"] == "waiting" and linked["task"]["done"] == "1/3 steps"
    assert api("POST", "/api/register/link-plan", {"project": str(a), "id": t, "plan_id": "nothing"})[0] == 400
    code, words = api("POST", "/api/register/resume", {"project": str(a), "id": t})
    assert code == 200 and 'plan "Briefing" (briefing)' in words["text"] and words["project"] == str(a)
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


def test_a_node_is_hidden_and_shown_the_overview_asks_for_the_hidden_ones_and_counts_them(world):
    api, a, b, plans = world
    top = api("POST", "/api/register/add", {"project": str(a), "title": "Parent", "subtasks": [{"title": "Child"}]})[1]["id"]
    api("POST", "/api/register/add", {"project": str(a), "title": "Other"})
    code, hid = api("POST", "/api/register/hide", {"project": str(a), "id": top})
    assert code == 200 and hid["task"]["hidden"] is True
    view = api("GET", "/api/register")[1]
    assert [t["title"] for t in view["projects"][0]["tasks"]] == ["Other"] and view["hidden"] == 2 and view["projects"][0]["hidden"] == 2
    shown = api("GET", "/api/register?hidden=1")[1]["projects"][0]["tasks"]
    assert [t["title"] for t in shown] == ["Parent", "Child", "Other"] and shown[0]["hidden"] and shown[1]["hidden_by_parent"] and not shown[2]["hidden"]
    assert api("POST", "/api/register/show", {"project": str(a), "id": top})[1]["task"]["hidden"] is False
    assert len(api("GET", "/api/register")[1]["projects"][0]["tasks"]) == 3
    assert api("POST", "/api/register/hide", {"project": str(a), "id": "t-nope"})[0] == 400


def test_closing_goes_to_the_history_which_is_listed_reopened_and_deleted(world):
    api, a, b, plans = world
    top = api("POST", "/api/register/add", {"project": str(a), "title": "Chapter", "solved": "outline ok", "subtasks": [{"title": "Draft"}]})[1]["id"]
    assert api("POST", "/api/register/close", {"project": str(a), "id": top})[1]["closed"]
    assert api("GET", "/api/register")[1]["projects"][0]["tasks"] == []
    hist = api("GET", "/api/register/history")[1]["projects"][0]["history"]
    assert {r["title"] for r in hist} == {"Chapter", "Draft"} and next(r for r in hist if r["title"] == "Chapter")["notes"]["solved"] == "outline ok"
    assert api("GET", "/api/register")[1]["projects"][0]["closed"] == 2
    assert api("POST", "/api/register/reopen", {"project": str(a), "id": top})[0] == 200
    assert [t["title"] for t in api("GET", "/api/register")[1]["projects"][0]["tasks"]] == ["Chapter", "Draft"]
    api("POST", "/api/register/close", {"project": str(a), "id": top})
    assert api("POST", "/api/register/delete", {"project": str(a), "id": top})[1]["deleted"] and api("GET", "/api/register/history")[1]["projects"][0]["history"] == []
    assert api("POST", "/api/register/reopen", {"project": str(a), "id": top})[0] == 400 and api("GET", "/api/register/history?project=/nope")[0] == 400


def test_groups_notes_and_states_are_set_by_the_person(world):
    api, a, b, plans = world
    code, g = api("POST", "/api/register/group-add", {"project": str(a), "title": "The engine"})
    assert code == 200 and g["groups"][0]["title"] == "The engine"
    gid = g["id"]
    made = api("POST", "/api/register/add", {"project": str(a), "title": "Fuse", "group": gid, "state": "decision", "decisions": "which format?"})[1]
    assert made["task"]["group"] == gid and made["task"]["state"] == "decision" and made["task"]["notes"]["decisions"] == "which format?" and made["task"]["num"] == 1
    upd = api("POST", "/api/register/update", {"project": str(a), "id": made["id"], "state": "review", "solved": "done", "risks": "may drift"})[1]["task"]
    assert upd["state"] == "review" and upd["notes"] == {"solved": "done", "decisions": "which format?", "risks": "may drift"}
    view = api("GET", "/api/register?state=review")[1]
    assert view["counts"]["review"] == 1 and view["projects"][0]["groups"] == [{"id": gid, "title": "The engine", "order": 1, "open": 1, "closed": 0}]
    assert api("POST", "/api/register/group-rename", {"project": str(a), "id": gid, "title": "Engine"})[1]["groups"][0]["title"] == "Engine"
    assert api("POST", "/api/register/group-delete", {"project": str(a), "id": gid})[1]["groups"] == []
    assert api("GET", "/api/register")[1]["projects"][0]["tasks"][0]["group"] == ""
    assert api("POST", "/api/register/add", {"project": str(a), "title": "x", "group": "g-nope"})[0] == 400
    assert api("GET", "/api/register?state=finished")[0] == 400


def test_the_conversations_own_tasks_can_be_asked_for(world):
    api, a, b, plans = world
    board = TaskBoard(a)
    mine, other = board.add("Mine", session="s1"), board.add("Theirs", session="s2")
    assert [t["title"] for t in api("GET", "/api/register?session=s1")[1]["projects"][0]["tasks"]] == ["Mine"] and other
    board.close(mine)
    assert [r["title"] for r in api("GET", "/api/register/history?session=s1")[1]["projects"][0]["history"]] == ["Mine"]
    assert api("GET", "/api/register/history?session=s2")[1]["projects"][0]["history"] == []


def test_the_roadmap_files_are_written_and_read_from_the_page(world):
    api, a, b, plans = world
    api("POST", "/api/register/add", {"project": str(a), "title": "Fuse", "state": "running", "solved": "- one"})
    out = api("POST", "/api/register/roadmap-write", {"project": str(a)})[1]
    assert out["nodes"] == 1 and (a / "docs" / "roadmap.json").exists() and (a / "docs" / "roadmap.md").exists()
    api("POST", "/api/register/delete", {"project": str(a), "id": api("GET", "/api/register")[1]["projects"][0]["tasks"][0]["id"]})
    assert api("POST", "/api/register/roadmap-load", {"project": str(a)})[1] == {"made": 1, "updated": 0}
    assert [t["title"] for t in api("GET", "/api/register")[1]["projects"][0]["tasks"]] == ["Fuse"]
    assert api("POST", "/api/register/roadmap-load", {"project": str(b)})[0] == 400                        # no file there


def test_the_address_of_the_remote_has_no_credentials_and_keeps_its_scheme():
    assert register_api.web_url("https://nuspy:SECRET@github.com/nuspy/tadamoo.git") == "https://github.com/nuspy/tadamoo"
    assert register_api.web_url("git@github.com:nuspy/tadamoo.git") == "https://github.com/nuspy/tadamoo"
    assert register_api.web_url("ssh://git@github.com/nuspy/tadamoo.git") == "https://github.com/nuspy/tadamoo"
    assert register_api.web_url("http://nuspy@100.84.254.116:8086/git/nuspy/x.git") == "http://100.84.254.116:8086/git/nuspy/x"
    assert register_api.web_url("C:/somewhere/else") == "" and register_api.web_url("") == ""


def test_the_person_sets_the_branch_and_commits_of_a_task_and_the_overview_carries_them_with_the_remote_page(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    api = register_api.RegisterApi(lambda: [{"path": str(folder), "name": "proj"}], tmp_path / "plans")
    code, made = api.route("POST", "/api/register/add", {}, {"project": str(folder), "title": "Ship"})
    assert code == 200
    code, body = api.route("POST", "/api/register/update", {}, {"project": str(folder), "id": made["id"], "git_branch": "feat/ship", "commits": ["abcdef1", "zzz"]})
    assert code == 200 and body["task"]["links"]["git_branch"] == "feat/ship" and body["task"]["links"]["commits"] == ["abcdef1"]
    register_api._REPO_URLS[str(folder)] = (__import__("time").monotonic(), "https://github.com/nuspy/tadamoo")
    project = api.overview(str(folder))["projects"][0]
    task = project["tasks"][0]
    assert project["repo_url"] == "https://github.com/nuspy/tadamoo" and task["links"]["git_branch"] == "feat/ship" and task["links"]["commits"] == ["abcdef1"]
