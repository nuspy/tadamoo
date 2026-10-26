"""The folders of the Agent tab: adding one gives it a registry, the registry follows its conversations, and a conversation is limited to its folder."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.agent_runs import RunError
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from serve.projects import ProjectStore
from serve.tasks import ChangeJournal, TaskJournal
from test_e2e_mock_server import answer, start_server, tool_call, ws  # noqa: F401  (fixtures)
from test_agent_gui_api import Api, wait_for  # noqa: E402


def registry(folder):
    return json.loads((Path(folder) / ".basalt" / "project.json").read_text(encoding="utf-8"))


def test_adding_a_folder_writes_its_registry_and_a_gitignore_and_lists_it(tmp_path):
    folder = tmp_path / "site"
    folder.mkdir()
    store = ProjectStore(tmp_path / "j")
    project = store.add(str(folder))
    assert project["name"] == "site" and project["exists"] is True
    assert registry(folder)["basalt_project"] == 1 and registry(folder)["conversations"] == []
    assert (folder / ".basalt" / ".gitignore").read_text(encoding="utf-8").strip() == "artifacts/"
    assert [p["path"] for p in ProjectStore(tmp_path / "j").list()] == [str(folder.resolve())]       # kept between runs
    store.add(str(folder))                                                                                # again: still one
    assert len(store.list()) == 1


def test_a_folder_that_is_not_one_the_agent_can_work_in_is_refused(tmp_path):
    store = ProjectStore(tmp_path / "j")
    for bad, code in (("", "folder_missing"), ("relative/dir", "folder_relative"), (str(tmp_path / "nope"), "folder_not_found"), (tmp_path.anchor, "folder_drive")):
        with pytest.raises(RunError) as err:
            store.add(bad)
        assert err.value.code == code
    assert store.list() == []


def test_the_registry_keeps_the_time_it_was_made_and_follows_the_conversations(tmp_path):
    folder = tmp_path / "site"
    folder.mkdir()
    store = ProjectStore(tmp_path / "j")
    store.add(str(folder))
    made = registry(folder)["created"]
    rows = [{"id": "a1", "workspace": str(folder.resolve()), "title": "First", "created": 1.0, "updated": 2.0, "mode": "default", "state": "idle"},
            {"id": "b2", "workspace": str(folder.resolve()), "title": "Gone", "created": 3.0, "updated": 4.0, "mode": "plan", "state": "closed"},
            {"id": "c3", "workspace": str(tmp_path / "elsewhere"), "title": "Other", "created": 5.0, "updated": 6.0, "mode": "default", "state": "idle"}]
    store.sync(str(folder.resolve()), rows)
    reg = registry(folder)
    assert reg["created"] == made
    assert [c["id"] for c in reg["conversations"]] == ["a1"]                                          # not a closed one, not another folder's
    assert reg["conversations"][0]["title"] == "First"


def test_removing_a_folder_forgets_it_and_leaves_its_files_and_registry(tmp_path):
    folder = tmp_path / "site"
    folder.mkdir()
    store = ProjectStore(tmp_path / "j")
    store.add(str(folder))
    assert store.remove(str(folder)) is True
    assert store.remove(str(folder)) is False
    assert store.list() == []
    assert (folder / ".basalt" / "project.json").is_file()
    store.sync(str(folder), [{"id": "x", "workspace": str(folder), "state": "idle"}])                    # no longer known: not rewritten
    assert registry(folder)["conversations"] == []


@pytest.fixture
def server(tmp_path, monkeypatch, start_server):  # noqa: F811
    state = {}

    def make(scripts):
        url = start_server(scripts)
        projects = ProjectStore(tmp_path / "j")
        mgr = SessionManager(tmp_path / "j", url, "", board=ApprovalBoard(), tick_s=0.2, router="rules", projects=projects, default_workspace=tmp_path / "chat-workspace")
        monkeypatch.setattr(handler, "_SESSIONS", mgr)
        monkeypatch.setattr(handler, "_PROJECTS", projects)
        state.update(mgr=mgr, api=Api(url, ""), projects=projects)
        return state

    yield make
    if "mgr" in state:
        state["mgr"].shutdown()


def test_the_routes_add_list_and_remove_and_a_conversation_is_listed_in_the_folders_registry(server, tmp_path):
    s = server([answer("ok")])
    api, folder = s["api"], tmp_path / "site"
    folder.mkdir()
    code, body = api("POST", "/api/agent/projects", {"path": str(folder)})
    assert code == 200 and body["project"]["name"] == "site"
    assert [p["name"] for p in api("GET", "/api/agent/projects")[1]["projects"]] == ["site"]
    code, body = api("POST", "/api/agent/sessions", {"workspace": str(folder), "mode": "read_only", "ask_only": True, "title": "Fix the menu"})
    sid = body["session"]["id"]
    assert [c["id"] for c in registry(folder)["conversations"]] == [sid]
    api("POST", f"/api/agent/sessions/{sid}/rename", {"title": "Menu"})
    assert registry(folder)["conversations"][0]["title"] == "Menu"
    listing = api("GET", "/api/agent/sessions")[1]
    assert listing["default_workspace"] == str(tmp_path / "chat-workspace")
    assert api("POST", f"/api/agent/sessions/{sid}/delete", {})[0] == 200
    assert registry(folder)["conversations"] == []
    code, body = api("POST", "/api/agent/projects", {"path": str(tmp_path / "missing")})
    assert code == 400 and body["error"]["code"] == "folder_not_found"                                    # the page shows the message
    code, body = api("POST", "/api/agent/projects/remove", {"path": str(folder)})
    assert (code, body["removed"]) == (200, True)
    assert api("GET", "/api/agent/projects")[1]["projects"] == []


def test_a_conversation_in_a_folder_shows_its_file_edits_in_the_changes_panel_and_no_task(server, tmp_path, monkeypatch):
    tasks, changes = TaskJournal(tmp_path / "jj"), ChangeJournal(tmp_path / "jj")
    monkeypatch.setattr(handler, "_TASKS", tasks)
    monkeypatch.setattr(handler, "_CHANGES", changes)
    s = server([tool_call("write_file", path="hello.txt", content="one two"), answer("Done.")])
    api, folder = s["api"], tmp_path / "site"
    folder.mkdir()
    sid = api("POST", "/api/agent/sessions", {"workspace": str(folder), "mode": "default"})[1]["session"]["id"]
    assert api("POST", f"/api/agent/sessions/{sid}/send", {"text": "make hello.txt"})[0] == 200
    wait_for(lambda: any(e["event"] == "done" for e in api("GET", f"/api/agent/sessions/{sid}/events?after=0")[1]["events"]), seconds=60)
    assert (folder / "hello.txt").read_text(encoding="utf-8") == "one two"
    assert [(e["file"], e["kind"]) for e in changes.load()["edits"]] == [("hello.txt", "added")]
    assert tasks.load()["tasks"] == []                                                                  # a conversation is not one task with an end
