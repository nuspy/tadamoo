"""Decision nodes with their exits imposed, a decision no exit of which holds that goes to the user, and the plan's values an agent can write."""
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.planner import Assignee, NodeType, PlanCanvasBridge, PlanDAG, PlanError, PlanStore
from harness.planner.dag import MAX_OUTPUTS, MAX_STATE_BYTES, clean_outputs
from harness.planner.replan import parse_proposal
from harness.planner_tools import render_plan
from test_planner import cervinia, chain, node
from test_planner_tools import Asker, AgentState, world  # noqa: F401  (fixture)
from test_e2e_mock_server import start_server  # noqa: F401  (fixture)


def decision_plan():
    dag = PlanDAG(plan_id="d", title="d", target_type="occasional")
    for nid in ("start", "yes", "no"):
        dag.add_node(node(nid))
    dag.add_node(node("choose", NodeType.DECISION, conditions={"yes": "count >= 2", "no": "count < 2"}))
    dag.add_edge("start", "choose")
    dag.add_edge("choose", "yes")
    dag.add_edge("choose", "no")
    return dag


# -- the rule is imposed where plans are made and changed ---------------------------------------------------

def test_a_plan_created_with_a_one_exit_decision_is_refused(tmp_path):
    dag = chain("a", "b")
    dag.nodes["a"].type = NodeType.DECISION
    dag.nodes["a"].conditions = {"b": "x > 1"}
    with pytest.raises(PlanError, match="at least two exits"):
        PlanStore(tmp_path).create(dag)
    assert not (tmp_path / "p.json").exists()


def test_a_plan_created_with_an_exit_that_has_no_condition_or_a_bad_one_is_refused(tmp_path):
    dag = decision_plan()
    del dag.nodes["choose"].conditions["no"]
    with pytest.raises(PlanError, match="no condition for its exit 'no'"):
        PlanStore(tmp_path).create(dag)
    dag.nodes["choose"].conditions["no"] = "__import__('os')"
    with pytest.raises(PlanError, match="may not use"):
        PlanStore(tmp_path).create(dag)
    dag.nodes["choose"].conditions["no"] = "count < 2"
    PlanStore(tmp_path).create(dag)


def test_a_diff_that_adds_a_decision_with_one_exit_is_dropped_whole():
    dag = chain("a", "b")
    before = json.dumps(dag.to_dict()["nodes"], sort_keys=True)
    diff = {"add_nodes": [{"id": "d", "type": "decision", "title": "D", "description": "", "assignee": "agent", "dependencies": ["a"],
                           "outputs": ["b"], "conditions": {"b": "x > 1"}}]}
    with pytest.raises(PlanError, match="at least two exits"):
        dag.apply_diff(diff)
    assert json.dumps(dag.to_dict()["nodes"], sort_keys=True) == before and "d" not in dag.nodes
    with pytest.raises(PlanError, match="at least two exits"):
        dag.preview_diff(diff)


def test_a_diff_that_builds_a_decision_in_steps_passes_when_the_result_is_valid():
    dag = chain("a", "b")
    dag.add_node(node("c"))
    diff = {"add_nodes": [{"id": "d", "type": "decision", "title": "D", "description": "", "assignee": "agent", "dependencies": ["a"],
                           "outputs": ["b", "c"], "conditions": {"b": "x > 1", "c": "x <= 1"}}]}
    assert "d" in dag.apply_diff(diff) and dag.nodes["d"].outputs == ["b", "c"]


def test_removing_an_exit_with_a_diff_is_refused_but_an_unrelated_change_is_not_held_up_by_an_old_bad_decision():
    dag = decision_plan()
    with pytest.raises(PlanError, match="at least two exits"):
        dag.apply_diff({"remove_nodes": ["no"]})
    assert "no" in dag.nodes
    legacy = PlanDAG.from_dict(chain("a", "b").to_dict())
    legacy.nodes["a"].type = NodeType.DECISION                      # a stored plan from before the rule
    assert legacy.apply_diff({"update_nodes": {"b": {"title": "B2"}}}) == ["b"]


def test_editing_a_decisions_conditions_cannot_leave_an_exit_without_one():
    dag = decision_plan()
    with pytest.raises(PlanError, match="no condition for its exit 'no'"):
        dag.update_node("choose", conditions={"yes": "count >= 2"})
    assert dag.nodes["choose"].conditions == {"yes": "count >= 2", "no": "count < 2"}
    dag.update_node("choose", conditions={"yes": "count >= 3", "no": "count < 3"})
    assert dag.nodes["choose"].conditions["yes"] == "count >= 3"


def test_a_stored_decision_that_breaks_the_rule_cannot_be_completed():
    dag = chain("a", "b")
    dag.nodes["a"].type = NodeType.DECISION
    with pytest.raises(PlanError, match="at least two exits"):
        dag.mark("a", "completed")
    assert dag.nodes["a"].status == "pending"


def test_a_model_alternative_with_a_bad_decision_is_not_offered():
    dag = chain("a", "b")
    bad = {"title": "t", "description": "d", "diff": {"add_nodes": [{"id": "d", "type": "decision", "title": "D", "description": "", "assignee": "agent",
                                                                     "dependencies": ["a"], "outputs": [], "conditions": {}}]}}
    out = parse_proposal(json.dumps({"diagnosis": "x", "alternatives": [bad, {"title": "ok", "description": "d", "diff": {"update_nodes": {"b": {"title": "B"}}}}]}), dag)
    assert [a.title for a in out.alternatives] == ["ok"] and "at least two exits" in out.rejected[0]


# -- no exit holds ----------------------------------------------------------------------------------------------

def test_a_decision_with_no_true_condition_is_not_taken_and_says_why():
    dag = decision_plan()
    dag.mark("start", "completed")
    res = dag.mark("choose", "completed", {"count": 5, "other": 1}) or {}
    assert "no_route" not in res and dag.nodes["choose"].state_variables["selected"] == ["yes"]
    dag = decision_plan()
    dag.nodes["choose"].conditions = {"yes": "count >= 2 and ok", "no": "count < 0"}
    dag.mark("start", "completed")
    res = dag.mark("choose", "completed", {"count": 1, "ok": True})
    facts = res["no_route"]
    assert [(r["to"], r["holds"]) for r in facts["exits"]] == [("yes", False), ("no", False)]
    assert facts["values"] == {"count": 1, "ok": True} and facts["unset"] == []
    assert "'count < 0' does not hold" in res["message"]


def test_the_failed_decision_goes_to_the_user_and_nothing_below_it_is_skipped():
    dag = decision_plan()
    dag.nodes["choose"].conditions = {"yes": "count >= 2", "no": "count < 0"}
    dag.mark("start", "completed")
    res = dag.mark("choose", "completed", {"count": 1})
    node_ = dag.nodes["choose"]
    assert node_.status == "pending" and node_.assignee == Assignee.USER and node_.state_variables["no_route"]["was"] == "agent"
    assert "count" not in dag.shared_state                          # the values given with the refused completion are not kept
    assert dag.nodes["yes"].status == "blocked" and dag.nodes["no"].status == "blocked"      # waiting, not skipped
    assert dag.ready_nodes() == ["choose"]


def test_the_person_can_correct_the_value_and_complete_the_decision_again():
    dag = decision_plan()
    dag.nodes["choose"].conditions = {"yes": "count >= 2", "no": "count < 0"}
    dag.mark("start", "completed")
    dag.mark("choose", "completed", {"count": 1})
    assert dag.mark("choose", "completed", {"count": 3}) is None
    assert dag.nodes["choose"].status == "completed" and "no_route" not in dag.nodes["choose"].state_variables
    assert dag.nodes["no"].status == "skipped" and dag.nodes["yes"].status == "pending" and dag.shared_state == {"count": 3}


def test_a_condition_that_cannot_be_read_yet_also_stops_the_decision():
    dag = decision_plan()
    dag.mark("start", "completed")
    res = dag.mark("choose", "completed")
    assert res["no_route"]["unset"] == ["count"] and "not set yet" in res["no_route"]["exits"][0]["error"]
    assert dag.nodes["yes"].status == "blocked"
    # a decision where one exit holds but the other cannot be read is not taken either: the unread exit would be skipped unseen
    dag2 = decision_plan()
    dag2.nodes["choose"].conditions = {"yes": "True", "no": "missing > 1"}
    dag2.mark("start", "completed")
    assert "no_route" in dag2.mark("choose", "completed")


def test_a_decision_with_a_true_exit_does_not_hide_a_second_true_exit():
    dag = decision_plan()
    dag.nodes["choose"].conditions = {"yes": "count >= 1", "no": "count >= 0"}
    dag.mark("start", "completed")
    dag.mark("choose", "completed", {"count": 3})
    assert dag.nodes["choose"].state_variables["selected"] == ["yes", "no"]


def test_the_bridge_reports_the_block_and_the_history_names_it(tmp_path):
    dag = decision_plan()
    dag.nodes["choose"].conditions = {"yes": "count >= 2", "no": "count < 0"}
    bridge = PlanCanvasBridge(dag)
    seen = []
    bridge.subscribe(seen.append)
    bridge.handle({"type": "plan:mark", "node_id": "start", "status": "completed"})
    res = bridge.handle({"type": "plan:mark", "node_id": "choose", "status": "completed", "outputs": {"count": 1}, "actor": "chat"})
    assert res["ok"] and res["completed"] is False and res["no_route"]["values"] == {"count": 1} and res["status"] == "pending"
    assert bridge.history[-1]["type"] == "no_route" and bridge.history[-1]["actor"] == "chat"
    assert any(e.get("no_route", {}).get("node_id") == "choose" for e in seen)
    snap = bridge.snapshot()
    assert "NO EXIT HOLDS" in render_plan(snap)


# -- the plan's values -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"1x": 1}, {"a b": 1}, {"class": 1}, {"text": "reserved for sensors"}, {"x" * 65: 1}, {"x": object()}, {"x": float("nan")},
    {"x": "y" * 4001}, {"x": [[[[1]]]]}, {"x": {1: 2}}, {"v%d" % i: 1 for i in range(MAX_OUTPUTS + 1)}, ["not", "a", "dict"],
])
def test_values_a_step_writes_must_be_plain_named_data(bad):
    with pytest.raises(PlanError):
        clean_outputs(bad)


def test_values_are_written_only_when_a_step_is_completed_and_the_plan_keeps_its_size_bounded():
    dag = chain("a", "b")
    with pytest.raises(PlanError, match="when it is completed"):
        dag.mark("a", "in_progress", {"x": 1})
    assert dag.shared_state == {}
    for i in range(12):
        dag.shared_state[f"v{i}"] = "z" * 3900
    with pytest.raises(PlanError, match="may not be larger"):
        dag.mark("a", "completed", {"more": "q" * 3900})
    assert dag.nodes["a"].status == "pending" and "more" not in dag.shared_state and MAX_STATE_BYTES > 0
    assert dag.mark("a", "completed", {"ok": 1}) is None and dag.shared_state["ok"] == 1


def test_a_value_written_by_a_step_reaches_a_merge_and_a_decision():
    dag = cervinia()
    dag.mark("meteo", "completed")
    dag.mark("invita_gianna", "completed")
    dag.mark("invita_lucia", "completed")
    assert "esiti" not in dag.ready_nodes()                          # the merge waits for the value it requires
    dag2 = cervinia()
    dag2.mark("meteo", "completed")
    dag2.mark("invita_gianna", "completed", {"invitati_confermati": ["Gianna"]})
    dag2.mark("invita_lucia", "completed", {"invitati_confermati": ["Gianna", "Lucia"]})
    assert "esiti" in dag2.ready_nodes() and dag2.nodes["invita_lucia"].state_variables["outputs"] == {"invitati_confermati": ["Gianna", "Lucia"]}
    dag2.mark("esiti", "completed")
    dag2.mark("scelta", "completed")
    assert dag2.nodes["scelta"].state_variables["selected"] == ["terrazza"]


def test_a_sensor_whose_set_cannot_be_written_waits_instead_of_failing_the_whole_event():
    dag = chain("a", "b")
    dag.add_node(node("s", NodeType.SENSOR, conditions={"when": "'x' in text", "set": {"text": True}}))
    assert dag.feed_event({"text": "x", "source": "t"}) == []
    assert "sensor_error" in dag.nodes["s"].state_variables and dag.nodes["s"].status == "pending"


# -- the tools of the Chat ---------------------------------------------------------------------------------

def server_plan(client):
    pid = f"dec-{uuid.uuid4().hex[:6]}"
    nodes = decision_plan().to_dict()["nodes"]
    nodes["choose"]["conditions"] = {"yes": "count >= 2", "no": "count < 0"}
    client.create({"plan_id": pid, "title": "Decisione", "target_type": "occasional", "nodes": nodes})
    return pid


def test_the_chat_can_write_plan_values_and_a_decision_reads_them(world):
    client, agent, build = world
    reg, ask = build("default", "confirm", "confirm", "confirm")
    pid = server_plan(client)
    reg.execute("plan_get", {"plan_id": pid})
    out = reg.execute("plan_mark", {"plan_id": pid, "node_id": "start", "status": "completed"})
    assert "marked completed" in out
    out = reg.execute("plan_mark", {"plan_id": pid, "node_id": "choose", "status": "completed", "outputs": {"count": 3}})
    assert "plan values set: count = 3" in out
    assert "count = 3" in ask.asked[1]["question"] and ask.asked[1]["facts"]["sets"] == {"count": 3}
    snap = client.snapshot(pid)
    assert snap["plan"]["shared_state"] == {"count": 3} and snap["plan"]["nodes"]["no"]["status"] == "skipped"
    assert "Plan values: count = 3" in reg.execute("plan_get", {"plan_id": pid})


def test_a_decision_no_exit_of_which_holds_is_reported_to_the_model_as_waiting_for_the_user(world):
    client, agent, build = world
    reg, ask = build("default", "confirm", "confirm")
    pid = server_plan(client)
    reg.execute("plan_get", {"plan_id": pid})
    reg.execute("plan_mark", {"plan_id": pid, "node_id": "start", "status": "completed"})
    out = reg.execute("plan_mark", {"plan_id": pid, "node_id": "choose", "status": "completed", "outputs": {"count": 1}})
    assert "NOT completed" in out and "waits for the user" in out and "'count >= 2' does not hold" in out and "do not choose an exit yourself" in out
    snap = client.snapshot(pid)
    assert snap["plan"]["nodes"]["choose"]["assignee"] == "user" and snap["plan"]["nodes"]["yes"]["status"] == "blocked"
    assert "NO EXIT HOLDS" in reg.execute("plan_get", {"plan_id": pid})


def test_bad_values_are_refused_before_the_user_is_asked(world):
    client, agent, build = world
    reg, ask = build("default")
    pid = server_plan(client)
    reg.execute("plan_get", {"plan_id": pid})
    assert "Not written" in reg.execute("plan_mark", {"plan_id": pid, "node_id": "start", "status": "completed", "outputs": {"not a name": 1}})
    assert "use status completed" in reg.execute("plan_mark", {"plan_id": pid, "node_id": "start", "status": "failed", "outputs": {"x": 1}})
    assert ask.asked == [] and client.snapshot(pid)["plan"]["shared_state"] == {}
