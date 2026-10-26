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


SKILL_SPEC = {"name": "Daily report", "description": "writes the report of a day",
              "params": [{"name": "day", "type": "date", "explanation": "which day"}, {"name": "topic", "type": "text", "explanation": "about what"}]}
SKILL_SCRIPT = "def main(params):\n    return 'ok'\n"


@pytest.fixture
def skills_api(tmp_path, monkeypatch, start_server):  # noqa: F811
    url = start_server(["unused"])
    mgr = SessionManager(tmp_path / "j", url, board=ApprovalBoard(), tick_s=0.2, skills_dir=tmp_path / "skills",
                         command=lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history)])
    sched = Scheduler(tmp_path / "routines", SessionRoutineRunner(mgr, poll_s=0.05), tick_s=3600, poll_s=0.05).start()
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    monkeypatch.setattr(handler, "_ROUTINES", RoutinesApi(tmp_path, mgr, scheduler=sched))
    mgr.skills.save(SKILL_SPEC, SKILL_SCRIPT)
    yield Api(url), mgr
    sched.shutdown()
    mgr.shutdown()


def test_a_routine_can_run_a_personal_skill_and_the_date_in_words_is_worked_out_at_each_run(skills_api, ws):
    api, mgr = skills_api
    code, made = api("POST", "/api/routines", body(ws, skill="daily-report", values={"day": "today", "topic": "tests"}, prompt=""))
    assert code == 200
    routine = made["routine"]
    assert (routine["skill"], routine["values"]) == ("daily-report", {"day": "today", "topic": "tests"}) and routine["prompt"] == "Skill: daily-report"
    assert api("POST", f"/api/routines/{routine['id']}/run", {})[0] == 200
    wait_for(lambda: api("GET", f"/api/routines/runs?id={routine['id']}")[1]["runs"], seconds=30)
    run = api("GET", f"/api/routines/runs?id={routine['id']}")[1]["runs"][0]
    assert run["status"] == "done" and run["summary"].startswith("skill Daily report ")
    from datetime import date
    assert f'"value": "{date.today().isoformat()}"' in run["summary"]                  # 'today' became the day of this run, and the stored routine still says 'today'
    assert api("GET", "/api/routines")[1]["routines"][0]["values"]["day"] == "today"


def test_a_skill_routine_with_a_wrong_value_a_missing_skill_or_a_skill_not_approved_is_refused_when_saved(skills_api, ws):
    api, mgr = skills_api
    code, res = api("POST", "/api/routines", body(ws, skill="daily-report", values={"day": "when the moon is blue", "topic": "x"}))
    assert code == 400 and res["error"]["code"] == "bad_values" and "day" in res["error"]["message"]
    assert api("POST", "/api/routines", body(ws, skill="no-such-skill", values={}))[1]["error"]["code"] == "bad_skill"
    assert api("POST", "/api/routines", body(ws, values={"day": "today"}))[1]["error"]["code"] == "bad_values"            # values need a skill
    mgr.skills.save({**SKILL_SPEC, "name": "Unread"}, SKILL_SCRIPT + "# changed\n", approve=False)
    assert api("POST", "/api/routines", body(ws, skill="unread", values={"day": "today", "topic": "x"}))[1]["error"]["code"] == "needs_approval"
    made = api("POST", "/api/routines", body(ws, skill="daily-report", values={"day": "tomorrow", "topic": "x"}))[1]["routine"]
    code, res = api("POST", f"/api/routines/{made['id']}/update", {"values": {"day": "nonsense", "topic": "x"}})
    assert code == 400 and res["error"]["code"] == "bad_values"
    assert api("POST", f"/api/routines/{made['id']}/update", {"skill": "", "values": {}, "prompt": "plain task"})[1]["routine"]["skill"] == ""
