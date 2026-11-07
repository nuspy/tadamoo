"""Which session made a plan, pausing it, and the schedule a routine reads to fire into it (serve/plans.py, tadamoo/planner/bridge.py)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve.plans import PlanService
from test_planner import CARD  # noqa: E402

PLAN = {"plan_id": "cervinia", "title": "Weekend a Cervinia", "target_type": "occasional",
        "nodes": {"hotel": {"id": "hotel", "type": "action", "title": "Hotel", "description": "", "assignee": "agent", "dependencies": [], "outputs": []}}}


def make_service(tmp_path):
    return PlanService(tmp_path / "planner")


def test_the_session_that_creates_a_plan_is_its_owner(tmp_path):
    svc = make_service(tmp_path)
    svc.create({"plan": PLAN, "card": CARD, "created_by": "aa11bb22"})
    assert svc.snapshot("cervinia")["created_by"] == "aa11bb22"
    svc2 = make_service(tmp_path.parent / "other2")
    svc2.create({"plan": {**PLAN, "plan_id": "noone"}, "card": CARD})
    assert svc2.snapshot("noone")["created_by"] == ""


def test_pausing_and_resuming_a_plan_is_recorded_in_its_history(tmp_path):
    svc = make_service(tmp_path)
    svc.create({"plan": PLAN, "card": CARD})
    assert svc.snapshot("cervinia")["paused"] is False
    out = svc.set_paused("cervinia", "user", True)
    assert out["paused"] is True and svc.snapshot("cervinia")["paused"] is True
    assert svc.snapshot("cervinia")["history"][-1]["type"] == "pause"
    svc.set_paused("cervinia", "user", False)
    assert svc.snapshot("cervinia")["paused"] is False and svc.snapshot("cervinia")["history"][-1]["type"] == "resume"


def test_pause_and_schedule_survive_a_restart(tmp_path):
    svc = make_service(tmp_path)
    svc.create({"plan": PLAN, "card": CARD})
    svc.set_paused("cervinia", "user", True)
    svc.set_schedule("cervinia", "user", {"routine_id": "r1", "session_ref": "night", "instruction": "check in"})
    again = make_service(tmp_path)
    snap = again.snapshot("cervinia")
    assert snap["paused"] is True and snap["schedule"] == {"routine_id": "r1", "session_ref": "night", "instruction": "check in"}


def test_the_schedule_can_be_set_and_removed(tmp_path):
    svc = make_service(tmp_path)
    svc.create({"plan": PLAN, "card": CARD})
    assert svc.snapshot("cervinia")["schedule"] == {}
    svc.set_schedule("cervinia", "chat", {"routine_id": "r1", "session_ref": "night"})
    assert svc.snapshot("cervinia")["schedule"] == {"routine_id": "r1", "session_ref": "night"}
    svc.remove_schedule("cervinia", "chat")
    assert svc.snapshot("cervinia")["schedule"] == {}


def test_is_paused_reads_a_plans_own_state_and_is_false_for_one_that_is_gone(tmp_path):
    svc = make_service(tmp_path)
    svc.create({"plan": PLAN, "card": CARD})
    assert svc.is_paused("cervinia") is False
    svc.set_paused("cervinia", "user", True)
    assert svc.is_paused("cervinia") is True
    assert svc.is_paused("no-such-plan") is False


def test_a_duplicated_plan_is_owned_by_whoever_asked_for_the_copy_not_the_original(tmp_path):
    svc = make_service(tmp_path)
    svc.create({"plan": PLAN, "card": CARD, "created_by": "aa11"})
    svc.set_paused("cervinia", "user", True)
    copy = svc.duplicate("cervinia", actor="chat", title="Copy")
    assert copy["created_by"] == "" and copy["paused"] is False                       # a copy starts fresh: not owned, not paused
