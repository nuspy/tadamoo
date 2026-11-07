"""A step marked `repeat` re-arms itself on completion instead of finishing, until its stop_when holds or its max_runs is used up;
what depends on it waits until then (tadamoo/planner/dag.py)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner.dag import Assignee, NodeType, PlanDAG, PlanError, PlanNode, clean_repeat

HOUR = 3600.0


def node(nid, typ=NodeType.ACTION, assignee=Assignee.AGENT, **kw):
    return PlanNode(id=nid, type=typ, title=kw.pop("title", nid.title()), description=kw.pop("description", ""), assignee=assignee, **kw)


def test_clean_repeat_accepts_every_and_checks_the_rest():
    assert clean_repeat(None) is None
    assert clean_repeat({"every": 1800}) == {"every": 1800.0}
    assert clean_repeat({"every": 1800, "max_runs": 3, "reset_from": "value:bilancio", "stop_when": "bilancio > 1000"}) == {
        "every": 1800.0, "max_runs": 3, "reset_from": "value:bilancio", "stop_when": "bilancio > 1000"}
    for bad in ({"every": 10}, {"every": 1800, "max_runs": 0}, {"every": 1800, "reset_from": "x"}, {"every": 1800, "stop_when": "1 =="},
               {"every": 1800, "other": 1}, "every 30m", {"every": 31 * 86400}):
        with pytest.raises(PlanError):
            clean_repeat(bad)


def test_completing_a_repeating_step_sends_it_back_to_pending_and_counts_the_run():
    dag = PlanDAG(plan_id="p", title="t", target_type="constant")
    dag.add_node(node("check", repeat={"every": HOUR}))
    now = 1_000_000.0
    retry = dag.mark("check", "completed", now=now)
    n = dag.nodes["check"]
    assert n.status == "pending" and n.state_variables["runs"] == 1 and n.state_variables["repeat_at"] == now + HOUR
    assert retry == {"repeated": True, "run": 1, "repeat_at": now + HOUR}
    assert dag.ready_nodes(now) == []                                       # not yet: it waits for repeat_at
    assert dag.ready_nodes(now + HOUR + 1) == ["check"]


def test_max_runs_ends_the_repetition_for_good():
    dag = PlanDAG(plan_id="p", title="t", target_type="constant")
    dag.add_node(node("check", repeat={"every": HOUR, "max_runs": 2}))
    now = 1_000_000.0
    dag.mark("check", "completed", now=now)
    assert dag.nodes["check"].status == "pending"
    retry = dag.mark("check", "completed", now=now + HOUR)
    assert retry is None and dag.nodes["check"].status == "completed"
    assert dag.nodes["check"].state_variables["runs"] == 2 and "repeat_at" not in dag.nodes["check"].state_variables


def test_stop_when_ends_the_repetition_once_the_plans_values_hold_it(capsys=None):
    dag = PlanDAG(plan_id="p", title="t", target_type="constant")
    dag.add_node(node("check", repeat={"every": HOUR, "stop_when": "bilancio >= 100"}))
    now = 1_000_000.0
    retry = dag.mark("check", "completed", {"bilancio": 40}, now=now)
    assert retry["repeated"] is True and dag.nodes["check"].status == "pending"
    retry = dag.mark("check", "completed", {"bilancio": 120}, now=now + HOUR)
    assert retry is None and dag.nodes["check"].status == "completed"


def test_a_stop_when_that_cannot_be_read_yet_does_not_block_the_repetition():
    dag = PlanDAG(plan_id="p", title="t", target_type="constant")
    dag.add_node(node("check", repeat={"every": HOUR, "stop_when": "not_set_yet > 1"}))
    retry = dag.mark("check", "completed", now=1_000_000.0)
    assert retry["repeated"] is True and dag.nodes["check"].status == "pending"


def test_a_value_written_by_one_run_is_there_for_the_next_run_of_the_same_step():
    """The channel is shared_state itself: plan_mark's outputs already persist there, so 'reset_from: value:bilancio' needs no
    extra code to carry the value forward — it is informational (where the fresh value is expected to come from)."""
    dag = PlanDAG(plan_id="p", title="t", target_type="constant")
    dag.add_node(node("check", repeat={"every": HOUR, "reset_from": "value:bilancio"}))
    dag.mark("check", "completed", {"bilancio": 120}, now=1_000_000.0)
    assert dag.shared_state["bilancio"] == 120
    dag.mark("check", "completed", {"bilancio": 80}, now=1_000_100.0)
    assert dag.shared_state["bilancio"] == 80


def test_a_step_after_a_repeating_one_waits_until_the_repetition_ends():
    dag = PlanDAG(plan_id="p", title="t", target_type="constant")
    dag.add_node(node("check", repeat={"every": HOUR, "max_runs": 2}))
    dag.add_node(node("close"))
    dag.add_edge("check", "close")
    now = 1_000_000.0
    dag.mark("check", "completed", now=now)
    assert dag.ready_nodes(now + HOUR + 1) == ["check"]                       # not "close": the parent is pending again, not done
    dag.mark("check", "completed", now=now + HOUR)
    assert dag.nodes["check"].status == "completed"
    assert dag.ready_nodes(now + HOUR) == ["close"]                          # the repetition is over: now it is


def test_repeat_is_kept_through_to_dict_and_from_dict():
    dag = PlanDAG(plan_id="p", title="t", target_type="constant")
    dag.add_node(node("check", repeat={"every": HOUR, "max_runs": 5}))
    dag.mark("check", "completed", now=1_000_000.0)
    again = PlanDAG.from_dict(dag.to_dict())
    n = again.nodes["check"]
    assert n.repeat == {"every": HOUR, "max_runs": 5} and n.state_variables["runs"] == 1 and n.status == "pending"


def test_repeat_can_be_set_through_update_node():
    dag = PlanDAG(plan_id="p", title="t", target_type="constant")
    dag.add_node(node("check"))
    dag.update_node("check", repeat={"every": 7200})
    assert dag.nodes["check"].repeat == {"every": 7200.0}
    dag.update_node("check", repeat=None)
    assert dag.nodes["check"].repeat is None
