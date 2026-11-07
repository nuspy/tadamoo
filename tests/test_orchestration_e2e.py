"""End to end, with the real scheduler, session manager and planner service (a stand-in process, no model, no network beyond
loopback): an intraday P&L plan wakes its session at its time, a repeating step checks in twice, the plan closes, the digest
shows it all, and a restart in the middle of the day changes nothing (the schedule survives, the fire is not repeated, no
message is lost)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve.plans import PlanService
from tadamoo.scheduler import Scheduler, SessionRoutineRunner
from test_scheduler import Clock, STUB, make, manager, ts, until  # noqa: F401,E402  (fixtures and helpers)

PLAN = {"plan_id": "pnl", "title": "P&L intraday", "target_type": "occasional",
        "nodes": {"check": {"id": "check", "type": "action", "title": "Check the P&L", "description": "", "assignee": "agent",
                            "dependencies": [], "outputs": ["close"], "repeat": {"every": 1800, "max_runs": 2}},
                  "close": {"id": "close", "type": "action", "title": "Close the day", "description": "", "assignee": "agent",
                           "dependencies": ["check"], "outputs": []}}}


@pytest.fixture
def planner(tmp_path):
    return PlanService(tmp_path / "planner")


def mark(svc, node_id, outputs=None):
    out = svc.handle_event("pnl", {"type": "plan:mark", "node_id": node_id, "status": "completed", "outputs": outputs or {}, "actor": "chat"})
    assert out["ok"], out
    return out


def test_the_intraday_pnl_plan_runs_closes_and_survives_a_restart(tmp_path, manager, planner):
    mgr, svc = manager, planner
    svc.create({"plan": PLAN, "created_by": "trader"})
    (tmp_path / "ws").mkdir()
    trader = mgr.create(str(tmp_path / "ws"), alias="trader")["id"]

    friday = ts(2026, 10, 23, 12, 0)
    sched, clock = make(tmp_path, SessionRoutineRunner(mgr, plan_paused=svc.is_paused, plan_context=svc.template_context), now=friday)
    rid = sched.add("pnl check-in", "Run the plan", "", "weekdays 14:50", deliver_to={"kind": "plan", "ref": "trader"},
                    message="Run the plan {plan_id}, ready step: {ready_step}.", link={"plan": "pnl"}, ttl_min=60)["id"]
    sched.start()

    # -- the schedule wakes the session at its time --------------------------------------------------------
    clock.now = ts(2026, 10, 23, 14, 50, 5)
    sched.check()
    item = until(lambda: next((d for d in mgr.deliveries.entries() if d["target"] == "trader"), None))
    until(lambda: mgr.deliveries.get(item["id"])["state"] == "delivered")
    assert item["provenance"] == "plan:pnl"
    digest = mgr.digest("trader")
    assert any("Run the plan pnl, ready step: check" in line for line in digest["lines"])

    # -- the session reads the plan and marks the ready step, with an output -------------------------------
    mark(svc, "check", {"bilancio": 120})
    snap = svc.snapshot("pnl")
    assert snap["plan"]["nodes"]["check"]["status"] == "pending" and snap["plan"]["nodes"]["check"]["state_variables"]["runs"] == 1
    assert "close" not in snap["ready"]                                       # the chain of checks is not over yet

    # -- 30 minutes later, the second check closes the repetition -------------------------------------------
    mark(svc, "check", {"bilancio": 150})
    snap = svc.snapshot("pnl")
    assert snap["plan"]["nodes"]["check"]["status"] == "completed" and "close" in snap["ready"]
    assert snap["plan"]["shared_state"]["bilancio"] == 150

    # -- closing the day -------------------------------------------------------------------------------------
    mark(svc, "close")
    assert svc.snapshot("pnl")["plan"]["nodes"]["close"]["status"] == "completed"

    digest = mgr.digest("trader")
    assert digest["pending_messages"] == 0 and digest["dead_letters"] == 0

    # -- the app restarts in the middle of the day: the schedule survives, the fire is not repeated ----------
    again, _ = make(tmp_path, SessionRoutineRunner(mgr, plan_paused=svc.is_paused, plan_context=svc.template_context), now=ts(2026, 10, 23, 16, 0))
    assert again.get(rid)["enabled"] is True
    before = len(again.fires_log(rid))
    again.check()
    assert len(again.fires_log(rid)) == before                                # the same fire, already claimed: nothing new
    assert again.view()["queued"] == []
    assert mgr.deliveries.dead_count() == 0

    sched.shutdown()


def test_a_fire_past_its_time_to_live_is_skipped_not_run_cold(tmp_path, manager, planner):
    mgr, svc = manager, planner
    svc.create({"plan": PLAN, "created_by": "trader"})
    (tmp_path / "ws").mkdir()
    mgr.create(str(tmp_path / "ws"), alias="trader")
    friday = ts(2026, 10, 23, 12, 0)
    sched, clock = make(tmp_path, SessionRoutineRunner(mgr, plan_paused=svc.is_paused, plan_context=svc.template_context), now=friday)
    rid = sched.add("pnl check-in", "Run the plan", "", "weekdays 14:50", deliver_to={"kind": "plan", "ref": "trader"},
                    link={"plan": "pnl"}, ttl_min=60)["id"]
    late, _ = make(tmp_path, SessionRoutineRunner(mgr, plan_paused=svc.is_paused, plan_context=svc.template_context), now=ts(2026, 10, 23, 16, 30))  # 100 minutes late
    late.check()
    assert late.view()["queued"] == [] and late.fires_log(rid)[0]["state"] == "skipped_late"
    assert mgr.deliveries.entries() == []
    sched.shutdown()
    late.shutdown()


def test_a_paused_plan_is_skipped_at_its_schedule_not_delivered(tmp_path, manager, planner):
    mgr, svc = manager, planner
    svc.create({"plan": PLAN, "created_by": "trader"})
    (tmp_path / "ws").mkdir()
    mgr.create(str(tmp_path / "ws"), alias="trader")
    svc.set_paused("pnl", "user", True)
    sched, clock = make(tmp_path, SessionRoutineRunner(mgr, plan_paused=svc.is_paused, plan_context=svc.template_context), now=ts(2026, 10, 23, 14, 49))
    rid = sched.add("pnl check-in", "Run the plan", "", "every 1h", deliver_to={"kind": "plan", "ref": "trader"}, link={"plan": "pnl"})["id"]
    sched.start()
    clock.now += 3700
    sched.check()
    until(lambda: sched.runs(rid) and sched.runs(rid)[0]["status"] == "skipped_paused")
    assert mgr.deliveries.entries() == []
    sched.shutdown()
