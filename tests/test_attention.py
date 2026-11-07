"""What the tabs of Plans and Cron Jobs flash for: the time of the latest result that was not the person's own doing."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve.plans import PlanService
from test_scheduler import Clock, STUB, make, manager, ts, until  # noqa: F401,E402  (fixtures and helpers)
from test_e2e_mock_server import start_server  # noqa: F401,E402  (fixture)
from tadamoo.scheduler import SessionRoutineRunner

PLAN = {"plan_id": "pnl", "title": "P&L intraday", "target_type": "occasional",
        "nodes": {"check": {"id": "check", "type": "action", "title": "Check the P&L", "description": "", "assignee": "agent", "dependencies": [], "outputs": []}}}


@pytest.fixture
def svc(tmp_path):
    service = PlanService(tmp_path / "planner")
    service.create({"plan": PLAN})
    return service


def test_nothing_has_come_out_of_a_new_plan(svc):
    assert svc.attention() == {"t": 0.0}


def test_a_step_done_by_the_chat_is_a_result_and_the_persons_own_marking_is_not(svc):
    svc.handle_event("pnl", {"type": "plan:mark", "node_id": "check", "status": "in_progress", "actor": "user"})
    assert svc.attention()["t"] == 0.0
    svc.handle_event("pnl", {"type": "plan:mark", "node_id": "check", "status": "completed", "outputs": {}, "actor": "chat"})
    got = svc.attention()
    assert got["t"] > 0 and got["plan"] == "pnl" and got["title"] == "P&L intraday" and got["summary"]


def test_editing_the_plan_is_not_a_result(svc):
    svc.handle_event("pnl", {"type": "plan:update_node", "node_id": "check", "fields": {"title": "Check again"}, "actor": "chat"})
    assert svc.attention()["t"] == 0.0


def test_a_finished_run_of_a_routine_is_a_result(tmp_path, manager):
    from serve.routines_api import RoutinesApi
    api = RoutinesApi(tmp_path / "journal", manager)
    try:
        assert api.attention() == {"t": 0.0, "name": "", "status": ""}
        rid = api.scheduler.add("morning check", "say hi", str(tmp_path), "every 1h")["id"]
        api.scheduler.run_now(rid)
        until(lambda: api.scheduler.runs(rid))
        got = api.attention()
        assert got["t"] > 0 and got["name"] == "morning check" and got["status"]
    finally:
        api.shutdown()


def test_the_route_reads_both_and_says_zero_while_nothing_exists_yet(tmp_path, monkeypatch, start_server):  # noqa: F811
    from serve import handler
    from test_agent_gui_api import Api
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server(["unused"])
    monkeypatch.setattr(handler, "_ROUTINES", None)
    monkeypatch.setattr(handler, "_PLANS", None)
    client = Api(url)
    assert client("GET", "/api/attention") == (200, {"cron": {"t": 0.0}, "plans": {"t": 0.0}})
