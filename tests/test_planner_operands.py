"""A decision's comparison in three terms, each typed (value, python expression, input from a wired step, agent description), and the data wired between steps."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import NodeType, PlanDAG, PlanError
from tadamoo.planner import nodes as plan_nodes
from tadamoo.planner import operands
from tadamoo.planner.dag import safe_eval
from test_planner import node

V = lambda x: {"kind": "value", "value": x}
PY = lambda e: {"kind": "python", "expr": e}
IN = lambda n: {"kind": "input", "from": n}
AG = lambda d: {"kind": "agent", "describe": d}


def run(branch, env=None, inputs=None, reported=None):
    return operands.evaluate(branch, env or {}, inputs or {}, reported or {}, safe_eval)


@pytest.mark.parametrize("left, op, right, right2, holds", [
    (V(5), V("<"), V(10), None, True), (V("5"), V(">="), V(5), None, True), (V(10), V("="), V("10"), None, True), (V(3), V("!="), V(3), None, False),
    (V(15), V("between"), V(10), V(30), True), (V(31), V("between"), V(10), V(30), False), (V("b"), V("in"), V("a, b, c"), None, True),
    (V("forest fire"), V("contains"), V("fire"), None, True), (PY("n * 2"), V(">"), V(10), None, True),
])
def test_a_comparison_of_three_terms_holds_or_not(left, op, right, right2, holds):
    branch = {"left": left, "op": op, "right": right, **({"right2": right2} if right2 else {})}
    assert run(branch, env={"n": 6}).holds is holds


def test_the_terms_come_from_values_python_wired_inputs_and_plan_values():
    branch = {"left": IN("total"), "op": V(">"), "right": {"kind": "input", "variable": "limit"}}
    res = run(branch, env={"limit": 5}, inputs={"total": 8})
    assert res.holds is True and res.compared == {"left": 8, "op": ">", "right": 5}
    missing = run(branch, env={}, inputs={"total": 8})
    assert missing.holds is None and missing.waits == [{"slot": "right", "kind": "input", "name": "limit"}]


def test_a_term_the_agent_works_out_makes_the_branch_wait_until_it_is_reported():
    branch = {"left": AG("how many tickets are left, from the page"), "op": V("<"), "right": V(10)}
    waiting = run(branch)
    assert waiting.holds is None and waiting.waits == [{"slot": "left", "kind": "agent", "describe": "how many tickets are left, from the page"}]
    assert run(branch, reported={"left": 4}).holds is True and run(branch, reported={"left": 40}).holds is False


def test_an_operator_that_is_not_known_or_terms_that_cannot_be_compared_stop_with_a_reason():
    assert "unknown operator '~'" in run({"left": V(1), "op": AG("pick the operator"), "right": V(2)}, reported={"op": "~"}).error
    assert "not all numbers" in run({"left": V("abc"), "op": V("<"), "right": V(2)}).error
    assert run({"left": PY("missing + 1"), "op": V("<"), "right": V(2)}).error.startswith("left:")


def test_the_terms_are_checked_when_the_decision_is_made():
    for bad, word in ((V(None), "value"), ({"kind": "python", "expr": "open('x')"}, "may not use"), ({"kind": "input", "from": "9x"}, "input"),
                      ({"kind": "agent", "describe": ""}, "describe"), ({"kind": "teleport"}, "python expression")):
        with pytest.raises(PlanError, match=word):
            plan_nodes.clean_spec("decision", {"branches": [{"label": "L", "left": bad, "op": V("<"), "right": V(1)}]})
    with pytest.raises(PlanError, match="second value"):
        plan_nodes.clean_spec("decision", {"branches": [{"label": "L", "left": V(1), "op": V("between"), "right": V(1)}]})
    with pytest.raises(PlanError, match="comparison is one of"):
        plan_nodes.clean_spec("decision", {"branches": [{"label": "L", "left": V(1), "op": V("~"), "right": V(1)}]})


def deciding(branches, **spec):
    dag = PlanDAG(plan_id="o", title="o", target_type="occasional")
    for nid in ("src", "hi", "lo"):
        dag.add_node(node(nid, title={"src": "Total"}.get(nid, nid)))
    dag.add_node(node("d", NodeType.DECISION, spec={"mode": "first", "branches": branches, **spec}))
    dag.add_edge("src", "d")
    ids = [b["id"] for b in dag.nodes["d"].spec["branches"]]
    dag.add_edge("d", "hi", ids[0])
    if len(ids) > 1:
        dag.add_edge("d", "lo", ids[1])
    return dag


def test_a_value_wired_from_a_step_is_an_input_the_decision_compares():
    dag = deciding([{"label": "high", "left": IN("Total"), "op": V(">"), "right": V(10)}, {"label": "else", "op": "else"}])
    dag.add_edge("src", "d", "result")
    d = dag.nodes["d"]
    assert d.inputs == {"Total": {"node": "src", "port": "result"}} and d.dependencies == ["src"]
    dag.mark("src", "completed", {"amount": 25})
    dag.mark("d", "completed")
    assert d.state_variables["selected_branches"] == ["b1"] and dag.nodes["lo"].status == "skipped"
    assert d.state_variables["compared"]["b1"] == {"left": 25, "op": ">", "right": 10, "holds": True}


def test_a_step_that_takes_a_value_does_not_run_before_the_value_is_there_and_says_what_it_waits_for():
    dag = deciding([{"label": "x", "left": V(1), "op": V("="), "right": V(1)}])
    dag.add_node(node("late"))
    dag.add_edge("src", "late", "result")
    assert "late" not in dag.ready_nodes() and dag.nodes["late"].state_variables["waiting_inputs"] == ["Total"]
    dag.mark("src", "completed", {"amount": 3})
    assert "late" in dag.ready_nodes() and "waiting_inputs" not in dag.nodes["late"].state_variables


def test_a_value_edge_cannot_go_round_and_only_goes_to_a_step_that_can_take_it():
    dag = deciding([{"label": "x", "left": V(1), "op": V("="), "right": V(1)}])
    dag.add_edge("src", "hi")
    with pytest.raises(PlanError, match="cannot come back round"):
        dag.add_edge("hi", "src", "result")
    dag.add_node(node("m", NodeType.MERGE))
    with pytest.raises(PlanError, match="cannot take a value"):
        dag.add_edge("src", "m", "result")
    assert dag.nodes["src"].inputs == {} and dag.nodes["m"].inputs == {}


def test_the_agent_reports_the_terms_that_are_its_own_and_the_decision_is_taken_then():
    dag = deciding([{"label": "few", "left": AG("tickets left"), "op": V("<"), "right": V(10)}, {"label": "else", "op": "else"}])
    dag.mark("src", "completed")
    res = dag.mark("d", "completed")
    assert "waiting" in res and res["waiting"]["waiting"][0]["describe"] == "tickets left" and dag.nodes["d"].status == "pending"
    with pytest.raises(PlanError, match="not a term of this decision"):
        dag.mark("d", "completed", {"b1.right": 5})
    with pytest.raises(PlanError, match="text, a number"):
        dag.mark("d", "completed", {"b1.left": {"a": 1}})
    assert dag.mark("d", "completed", {"b1.left": 4}) is None
    assert dag.nodes["d"].state_variables["selected_branches"] == ["b1"] and "b1.left" in dag.nodes["d"].state_variables["operands"]
    assert "b1.left" not in dag.shared_state                                          # a reported term is the decision's, not a plan value


def test_a_sensor_a_variable_and_an_answer_give_their_value_to_the_step_wired_to_them():
    dag = PlanDAG(plan_id="s", title="s", target_type="occasional")
    dag.add_node(node("s", NodeType.SENSOR, title="Alarm", conditions={"when": "'fire' in text"}))
    dag.add_node(node("g", NodeType.GET_VAR, title="Level", spec={"name": "level", "source": "variable"}))
    dag.add_node(node("a", title="Act"))
    dag.add_edge("s", "a", "reading")
    dag.add_edge("g", "a", "value")
    assert dag.nodes["a"].inputs == {"Alarm": {"node": "s", "port": "reading"}, "Level": {"node": "g", "port": "value"}}
    dag.shared_state["level"] = 7
    dag.feed_event({"text": "Fire in the barn", "source": "x"})
    dag.advance(0)
    assert dag.input_values(dag.nodes["a"]) == {"Alarm": "Fire in the barn", "Level": 7}
    edges = [e for e in dag.edges() if e["kind"] == "data"]
    assert {(e["from"]["node"], e["to"]["name"]) for e in edges} == {("s", "Alarm"), ("g", "Level")}


def test_inputs_survive_a_round_trip_and_go_when_the_step_they_come_from_goes():
    dag = deciding([{"label": "x", "left": IN("Total"), "op": V("="), "right": V(1)}])
    dag.add_edge("src", "d", "result")
    dag.ensure_start()
    again = PlanDAG.from_dict(json.loads(json.dumps(dag.to_dict())))
    assert again.to_dict() == dag.to_dict() and again.nodes["d"].inputs == {"Total": {"node": "src", "port": "result"}}
    dag.remove_edge("src", "d", "result")
    assert dag.nodes["d"].inputs == {}
    dag.add_edge("src", "d", "result")
    dag.remove_node("src")
    assert dag.nodes["d"].inputs == {} and dag.validate_acyclic()
