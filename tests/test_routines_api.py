"""/api/routines through the real handler, with a stand-in agent process behind the conversation manager."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.scheduler import Scheduler, SessionRoutineRunner
from serve import handler
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from serve.routines_api import RoutinesApi
from test_agent_gui_api import Api, wait_for  # noqa: E402
from test_e2e_mock_server import start_server, ws  # noqa: F401,E402  (fixtures)

STUB = str(Path(__file__).resolve().parent.parent / "serve" / "tests" / "session_stub.py")


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    url = start_server(["unused"])
    mgr = SessionManager(tmp_path / "j", url, board=ApprovalBoard(), tick_s=0.2,
                         command=lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history)])
    sched = Scheduler(tmp_path / "routines", SessionRoutineRunner(mgr, poll_s=0.05), tick_s=3600, poll_s=0.05).start()
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    monkeypatch.setattr(handler, "_ROUTINES", RoutinesApi(tmp_path, mgr, scheduler=sched))
    yield Api(url)
    sched.shutdown()
    mgr.shutdown()


def body(ws, **kw):
    return {"name": "morning", "prompt": "summarise the tests", "workspace": str(ws), "schedule": "weekdays 08:30", **kw}


def test_create_list_enable_run_and_remove(api, ws):
    code, made = api("POST", "/api/routines", body(ws))
    assert code == 200 and made["routine"]["enabled"] is False and made["routine"]["mode"] == "read_only"
    rid = made["routine"]["id"]
    listed = api("GET", "/api/routines")[1]
    assert [r["id"] for r in listed["routines"]] == [rid] and listed["routines"][0]["next_run"] is None      # disabled: no next run
    enabled = api("POST", f"/api/routines/{rid}/enable", {})[1]["routine"]
    assert enabled["enabled"] is True and api("GET", "/api/routines")[1]["routines"][0]["next_run"] > time.time()
    assert api("POST", f"/api/routines/{rid}/run", {})[0] == 200
    wait_for(lambda: api("GET", f"/api/routines/runs?id={rid}")[1]["runs"], seconds=30)
    run = api("GET", f"/api/routines/runs?id={rid}")[1]["runs"][0]
    assert run["status"] == "done" and run["summary"] == "echo: summarise the tests"
    assert api("POST", f"/api/routines/{rid}/disable", {})[1]["routine"]["enabled"] is False
    assert api("POST", f"/api/routines/{rid}/update", {"schedule": "every 3h"})[1]["routine"]["schedule"] == "every 3h"
    assert api("POST", f"/api/routines/{rid}/remove", {})[1] == {"removed": rid}
    assert api("GET", "/api/routines")[1]["routines"] == []


def test_mistakes_are_answered_with_a_message_and_a_code(api, ws):
    code, data = api("POST", "/api/routines", body(ws, schedule="whenever"))
    assert code == 400 and data["error"]["code"] == "bad_schedule"
    code, data = api("POST", "/api/routines", body(ws, mode="auto"))
    assert code == 400 and data["error"]["code"] == "auto_confirm"
    code, data = api("POST", "/api/routines/nope/enable", {})
    assert code == 404 and data["error"]["code"] == "gone"
    assert api("POST", "/api/routines/abc/explode", {})[0] == 400
    assert api("GET", "/api/routines/runs?limit=x")[0] == 400


def test_a_form_that_is_not_json_is_refused(api, ws):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(api.url + "/api/routines", data=b"name=x", method="POST", headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code in (403, 415)
