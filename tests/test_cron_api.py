"""RoutinesClient against the real handler: a conversation creates, lists, paused/resumes and fires its own schedules."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from serve.routines_api import RoutinesApi
from tadamoo.integrations.routines_client import RoutinesClient
from tadamoo.scheduler import Scheduler, SessionRoutineRunner
from test_e2e_mock_server import start_server, ws  # noqa: F401,E402  (fixtures)


@pytest.fixture
def setup(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server(["unused"])
    mgr = SessionManager(tmp_path / "j", url, board=ApprovalBoard(), tick_s=0.2)
    sched = Scheduler(tmp_path / "routines", SessionRoutineRunner(mgr, poll_s=0.05), tick_s=3600, poll_s=0.05).start()
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    monkeypatch.setattr(handler, "_ROUTINES", RoutinesApi(tmp_path, mgr, scheduler=sched))
    yield RoutinesClient(url)
    sched.shutdown()
    mgr.shutdown()


def test_create_list_pause_resume_fire_and_delete(setup, ws):
    client = setup
    made = client.create({"name": "watch", "schedule": "weekdays 08:30", "workspace": str(ws), "prompt": "watch the build"})
    rid = made["routine"]["id"]
    assert made["routine"]["enabled"] is True
    assert [r["id"] for r in client.list()] == [rid]
    assert client.act(rid, "disable")["routine"]["enabled"] is False
    assert client.act(rid, "enable")["routine"]["enabled"] is True
    assert client.act(rid, "run")["routine"]["id"] == rid
    client.act(rid, "remove")
    assert client.list() == []


def test_a_session_delivery_schedule_is_created_through_the_api(setup, ws):
    client = setup
    made = client.create({"name": "tell night", "schedule": "every 1h", "workspace": str(ws), "prompt": "check", "message": "all good",
                          "deliver_to": {"kind": "session", "ref": "night"}})
    assert made["routine"]["deliver_to"] == {"kind": "session", "ref": "night"} and made["routine"]["message"] == "all good"
