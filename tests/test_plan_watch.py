"""The plans' own clock: a wait ends at its time without anything waking a conversation, a plan with nothing waiting costs a comparison, a paused plan stands still."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve.plans import PlanService
from test_scheduler import Clock, STUB, make, manager, ts, until  # noqa: F401,E402  (fixtures and helpers)


def step(nid, kind="action", spec=None, parents=(), assignee="agent"):
    return {"id": nid, "type": kind, "title": nid, "description": "", "assignee": assignee, "dependencies": list(parents), "outputs": [],
            **({"spec": spec} if spec is not None else {})}


def body(plan_id, *steps):
    nodes = {s["id"]: s for s in steps}
    for s in steps:
        for p in s["dependencies"]:
            nodes[p]["outputs"].append(s["id"])
    return {"plan": {"plan_id": plan_id, "title": plan_id.upper(), "target_type": "occasional", "nodes": nodes}}


@pytest.fixture
def svc(tmp_path):
    return PlanService(tmp_path / "planner")


def status(svc, plan_id, nid):
    return svc.snapshot(plan_id)["plan"]["nodes"][nid]["status"]


def test_a_wait_ends_at_its_time_when_the_clock_looks_and_not_before(svc):
    t0 = time.time()
    svc.create(body("p", step("w", "wait_duration", {"seconds": 600}), step("after", parents=["w"])))
    assert svc.snapshot("p")["ready"] == [] and abs(svc._due["p"] - (t0 + 600)) < 5
    svc.watch_tick(t0 + 599)
    assert status(svc, "p", "w") == "pending"
    svc.watch_tick(t0 + 601)
    snap = svc.snapshot("p")
    assert snap["plan"]["nodes"]["w"]["status"] == "completed" and snap["ready"] == ["after"] and svc._due["p"] is None


def test_a_wait_after_a_step_starts_counting_when_that_step_is_marked_done(svc):
    svc.create(body("p", step("first"), step("w", "wait_duration", {"seconds": 60}, ["first"]), step("last", parents=["w"])))
    assert svc._due["p"] is None                                       # nothing is waiting yet: the wait has not been reached
    t0 = time.time()
    svc.handle_event("p", {"type": "plan:mark", "node_id": "first", "status": "completed", "actor": "chat"})
    assert abs(svc._due["p"] - (t0 + 60)) < 5
    svc.watch_tick(t0 + 61)
    assert svc.snapshot("p")["ready"] == ["last"]


def test_a_result_written_by_the_plan_is_something_that_came_out_for_the_tab_to_flash_for(svc):
    assert svc.attention()["t"] == 0.0
    svc.create(body("p", step("v", "set_var", {"name": "bilancio", "value": 5}), step("o", "output", {"name": "r", "template": "P&L {bilancio}"}, ["v"])))
    got = svc.attention()
    assert got["t"] > 0 and got["plan"] == "p" and got["summary"] == "r: P&L 5"


def test_a_plan_with_a_wait_that_only_set_a_value_does_not_flash_the_tab(svc):
    svc.create(body("p", step("v", "set_var", {"name": "x", "value": 1})))
    assert svc.attention()["t"] == 0.0


def test_a_paused_plan_stands_still_and_catches_up_when_resumed(svc):
    t0 = time.time()
    svc.create(body("p", step("w", "wait_duration", {"seconds": 60})))
    svc.set_paused("p", "user", True)
    svc.watch_tick(t0 + 120)
    assert status(svc, "p", "w") == "pending"
    svc.set_paused("p", "user", False)
    assert status(svc, "p", "w") == "pending"                         # the clock has not reached it by real time yet
    svc.watch_tick(t0 + 130)
    assert status(svc, "p", "w") == "completed"


def test_the_first_look_finds_waits_in_plans_saved_before_and_later_looks_only_touch_the_due_ones(tmp_path, monkeypatch):
    first = PlanService(tmp_path / "planner")
    t0 = time.time()
    first.create(body("a", step("w", "wait_duration", {"seconds": 600})))
    first.create(body("b", step("x")))
    again = PlanService(tmp_path / "planner")                          # a restart: nothing in memory
    looked = []
    real = again.tick
    monkeypatch.setattr(again, "tick", lambda plan_id, now=None: looked.append(plan_id) or real(plan_id, now))
    again.watch_tick(t0 + 10)
    assert sorted(looked) == ["a", "b"] and again._due["a"] == pytest.approx(t0 + 600, abs=5)
    looked.clear()
    again.watch_tick(t0 + 20)
    assert looked == []                                                # nothing due: no plan was opened
    again.watch_tick(t0 + 601)
    assert looked == ["a"] and status(again, "a", "w") == "completed"


def test_a_plan_that_cannot_be_read_does_not_stop_the_others(svc, monkeypatch):
    t0 = time.time()
    svc.create(body("a", step("w", "wait_duration", {"seconds": 60})))
    svc.create(body("b", step("w", "wait_duration", {"seconds": 60})))
    real = svc.tick

    def broken(plan_id, now=None):
        if plan_id == "a":
            raise RuntimeError("unreadable")
        return real(plan_id, now)
    monkeypatch.setattr(svc, "tick", broken)
    svc.watch_tick(t0 + 61)
    assert status(svc, "b", "w") == "completed"


def test_the_clock_of_the_routines_calls_the_hooks_and_a_failing_one_does_not_stop_it(tmp_path, manager):
    from tadamoo.scheduler import SessionRoutineRunner
    sched, clock = make(tmp_path, SessionRoutineRunner(manager), now=ts(2026, 10, 23, 12, 0))
    seen = []

    def bad(now):
        raise RuntimeError("boom")
    sched.tick_hooks += [bad, seen.append]
    sched.check()
    assert seen == [clock.now]
    sched.shutdown()
