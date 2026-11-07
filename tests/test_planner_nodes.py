"""The kinds of step beyond action, sensor, decision and merge: what each may carry, what is refused, and that plans written before them load unchanged."""
import pytest

from tadamoo.planner import nodes
from tadamoo.planner.dag import PlanDAG, PlanError, node_from_dict, node_to_dict


def node(kind, spec=None, **extra):
    return {"id": "n1", "type": kind, "title": "T", "description": "", "assignee": "agent", **({"spec": spec} if spec is not None else {}), **extra}


def test_every_kind_is_in_the_table_with_a_family_and_a_summary():
    kinds = {row["type"] for row in nodes.catalogue()}
    assert kinds == {"action", "sensor", "decision", "merge", "wait_duration", "wait_until", "wait_condition", "loop", "set_var", "get_var", "input", "output", "log"}
    assert all(row["family"] in nodes.FAMILIES and row["summary"] for row in nodes.catalogue())
    assert set(nodes.AUTO) == {"wait_duration", "wait_until", "wait_condition", "set_var", "get_var", "output", "log"}


@pytest.mark.parametrize("kind, spec", [
    ("wait_duration", {"seconds": 90}),
    ("wait_until", {"at": "2030-01-05 14:50"}),
    ("wait_condition", {"expr": "bilancio > 100"}),
    ("wait_condition", {"describe": "the market opens higher", "watch": ["indice"], "timeout_s": 7200, "on_timeout": "skip"}),
    ("loop", {"while": "bilancio < 500", "every": 600, "max_iter": 12}),
    ("set_var", {"name": "stato", "value": "aperto"}),
    ("set_var", {"name": "x", "expr": "y + 1"}),
    ("get_var", {"name": "x", "into": "copy"}),
    ("input", {"prompt": "How much?", "into": "importo", "kind": "number"}),
    ("input", {"prompt": "Which?", "into": "scelta", "kind": "choice", "choices": ["a", "b"]}),
    ("output", {"name": "risultato", "template": "P&L {bilancio}"}),
    ("log", {"message": "checked at {data}"}),
])
def test_a_good_spec_is_accepted_and_survives_a_save(kind, spec):
    made = node_from_dict(node(kind, spec))
    again = node_from_dict(node_to_dict(made))
    assert again.spec == made.spec and made.type.value == kind
    assert all(v is not None for v in made.spec.values())


@pytest.mark.parametrize("kind, spec, word", [
    ("wait_duration", {}, "seconds"),
    ("wait_duration", {"seconds": 0}, "seconds"),
    ("wait_duration", {"seconds": True}, "seconds"),
    ("wait_until", {"at": "tomorrow-ish"}, "date and time"),
    ("wait_condition", {}, "needs"),
    ("wait_condition", {"expr": "__import__('os')"}, "may not use"),
    ("wait_condition", {"describe": "x", "on_timeout": "explode"}, "on_timeout"),
    ("wait_condition", {"describe": "x", "poll_s": 5}, "poll_s"),
    ("loop", {}, "condition"),
    ("loop", {"while": "x < 3", "max_iter": 0}, "max_iter"),
    ("set_var", {"name": "x"}, "exactly one"),
    ("set_var", {"name": "x", "value": 1, "expr": "2"}, "exactly one"),
    ("set_var", {"name": "1bad", "value": 1}, "name"),
    ("get_var", {"name": ""}, "name"),
    ("input", {"prompt": "Which?", "into": "scelta", "kind": "choice", "choices": ["only"]}, "at least two"),
    ("input", {"prompt": "Q", "into": "q", "kind": "maybe"}, "kind"),
    ("output", {"name": "r"}, "exactly one"),
    ("log", {"message": ""}, "message"),
    ("log", {"message": "x", "extra": 1}, "not extra"),
    ("action", {"anything": 1}, "no spec"),
])
def test_a_bad_spec_is_refused_with_a_reason(kind, spec, word):
    with pytest.raises(PlanError, match=word):
        node_from_dict(node(kind, spec))


def test_a_kind_that_needs_a_spec_cannot_be_made_without_one():
    with pytest.raises(PlanError):
        node_from_dict(node("wait_duration"))


def test_the_defaults_of_a_wait_for_a_condition_and_a_loop_are_filled_in():
    waiting = node_from_dict(node("wait_condition", {"describe": "calm"}))
    assert waiting.spec["poll_s"] == nodes.DEFAULT_POLL_S and waiting.spec["on_timeout"] == "fail"
    looping = node_from_dict(node("loop", {"while": "n < 3"}))
    assert looping.spec["every"] == 300 and looping.spec["max_iter"] == 1000


def test_an_unknown_kind_says_so_and_names_it():
    with pytest.raises(PlanError, match="unknown kind of step 'teleport'"):
        node_from_dict(node("teleport"))
    with pytest.raises(PlanError, match="unknown kind of step"):
        PlanDAG.from_dict({"plan_id": "p", "title": "P", "nodes": {"n1": node("teleport")}})


def test_a_plan_saved_before_these_kinds_loads_and_saves_the_same():
    old = {"plan_id": "p", "title": "P", "target_type": "occasional", "nodes": {
        "a": {"id": "a", "type": "action", "title": "A", "description": "", "assignee": "agent", "dependencies": [], "outputs": ["s"], "conditions": {},
              "state_variables": {}, "status": "pending", "retry_policy": None, "position": None, "call_request": None, "wiki_query": None, "repeat": None},
        "s": {"id": "s", "type": "sensor", "title": "S", "description": "", "assignee": "agent", "dependencies": ["a"], "outputs": [],
              "conditions": {"when": "len(text) > 2"}, "state_variables": {}, "status": "pending", "retry_policy": None, "position": None,
              "call_request": None, "wiki_query": None, "repeat": None}}}
    dag = PlanDAG.from_dict(old)
    saved = dag.to_dict()
    assert all("spec" not in nd for nd in saved["nodes"].values())
    assert PlanDAG.from_dict(saved).to_dict()["nodes"] == saved["nodes"]


def test_a_question_to_the_person_is_always_theirs():
    asked = node_from_dict(node("input", {"prompt": "How much?", "into": "importo"}, assignee="agent"))
    assert asked.assignee.value == "user"


def test_the_spec_is_editable_and_checked_when_changed():
    dag = PlanDAG("p", "P", "occasional")
    dag.add_node(node_from_dict(node("wait_duration", {"seconds": 60})))
    dag.update_node("n1", spec={"seconds": 120})
    assert dag.nodes["n1"].spec == {"seconds": 120}
    with pytest.raises(PlanError):
        dag.update_node("n1", spec={"seconds": -3})
    assert dag.nodes["n1"].spec == {"seconds": 120}
    result = dag.apply_diff({"add_nodes": [node("log", {"message": "hello"}, id="n2")], "add_edges": [["n1", "n2"]]})
    assert set(result) == {"n2", "n1"} and dag.nodes["n2"].spec == {"message": "hello"}
    with pytest.raises(PlanError):
        dag.apply_diff({"add_nodes": [node("log", {"message": ""}, id="n3")]})
    assert "n3" not in dag.nodes
