"""A request of a plan to the person (an approval, or a value): when it is asking, what is shown, how the answer is checked and what it sets going."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import PlanDAG, PlanError
from tadamoo.planner.nodes import clean_answer
from serve.plans import PlanService
from test_planner_starts import Recorder, n, plan


def ask(nid, deps=(), **spec):
    return n(nid, "input", deps=deps, assignee="user", spec={"prompt": "Is it good?", **spec})


@pytest.mark.parametrize("value, expected", [(True, True), (False, False), ("yes", True), ("Sì", True), ("no", False), ("NO", False), ("approve", True)])
def test_an_approval_is_a_yes_or_a_no(value, expected):
    assert clean_answer({"ask": "approve"}, value) is expected


def test_what_is_neither_is_refused_and_a_value_is_checked_for_what_it_asks():
    with pytest.raises(PlanError, match="yes or no"):
        clean_answer({"ask": "approve"}, "maybe")
    assert clean_answer({"ask": "value", "kind": "number"}, "7") == 7 and clean_answer({"ask": "value", "kind": "number"}, "7.5") == 7.5
    with pytest.raises(PlanError, match="number"):
        clean_answer({"ask": "value", "kind": "number"}, "seven")
    assert clean_answer({"kind": "choice", "choices": ["a", "b"]}, "b") == "b"
    with pytest.raises(PlanError, match="one of: a, b"):
        clean_answer({"kind": "choice", "choices": ["a", "b"]}, "c")
    with pytest.raises(PlanError, match="text"):
        clean_answer({"kind": "text"}, "   ")


def test_a_request_is_asking_only_when_its_turn_has_come_and_shows_what_to_approve():
    dag = PlanDAG.from_dict(plan("p", n("write"), ask("ok", deps=["write"], ask="approve", into="script_ok", content="The script:\n{script}"), n("publish", deps=["ok"])))
    assert dag.human_requests() == []
    dag.mark("write", "completed", {"script": "Good evening."})
    [request] = dag.human_requests()
    assert (request["node_id"], request["ask"], request["into"]) == ("ok", "approve", "script_ok")
    assert request["prompt"] == "Is it good?" and request["content"] == "The script:\nGood evening."
    assert dag.answer("ok", "yes") == {"value": True, "name": "script_ok"}
    assert dag.shared_state["script_ok"] is True and dag.nodes["ok"].status == "completed" and dag.ready_nodes() == ["publish"]
    assert dag.nodes["ok"].state_variables["answer"]["value"] is True and dag.human_requests() == []
    with pytest.raises(PlanError, match="not asking now"):
        dag.answer("ok", "no")                                                       # an answer is given once


def test_a_value_is_written_to_the_name_the_request_gives_it():
    dag = PlanDAG.from_dict(plan("p", ask("n", ask="value", prompt="How many?", into="count", kind="number"), n("use", deps=["n"])))
    with pytest.raises(PlanError, match="number"):
        dag.answer("n", "many")
    assert dag.nodes["n"].status == "pending"                                       # a wrong answer changes nothing
    dag.answer("n", "12")
    assert dag.shared_state["count"] == 12 and dag.ready_nodes() == ["use"]
    with pytest.raises(PlanError, match="does not ask"):
        dag.answer("use", "x")


def test_an_approval_that_names_no_value_still_gives_its_answer_to_whoever_takes_it():
    dag = PlanDAG.from_dict(plan("p", ask("ok-1", ask="approve"), n("x")))
    [request] = dag.human_requests()
    assert request["into"] == "ok_1_approved"
    dag.answer("ok-1", False)
    assert dag.shared_state == {"ok_1_approved": False}


def test_a_request_stored_before_approvals_exist_loads_and_asks_for_a_value():
    old = plan("p", n("q", "input", assignee="user", spec={"prompt": "How much?", "into": "importo", "kind": "text"}))
    dag = PlanDAG.from_dict(old)
    [request] = dag.human_requests()
    assert (request["ask"], request["into"], request["kind"]) == ("value", "importo", "text")
    dag.answer("q", "10 euro")
    assert dag.shared_state["importo"] == "10 euro"
    assert dag.to_dict()["nodes"]["q"]["spec"]["prompt"] == "How much?"


def test_a_value_request_needs_its_name_and_an_approval_does_not():
    from tadamoo.planner.nodes import clean_spec
    with pytest.raises(PlanError, match="into"):
        clean_spec("input", {"prompt": "x"})
    assert clean_spec("input", {"prompt": "x", "ask": "approve"}) == {"ask": "approve", "prompt": "x"}
    with pytest.raises(PlanError, match="at least two"):
        clean_spec("input", {"prompt": "x", "into": "v", "kind": "choice", "choices": ["only"]})


# -- the service -------------------------------------------------------------------------------------------------------------------

def service(tmp_path):
    svc = PlanService(tmp_path / "planner")
    svc.launcher, svc.run_active = Recorder(), lambda plan_id: False
    return svc


def test_the_requests_of_all_plans_come_oldest_first_and_a_paused_plan_asks_nothing(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("one", ask("a", ask="approve"))})
    svc.create({"plan": plan("two", ask("b", ask="value", into="v"))})
    first, second = svc.bridge("one").dag.nodes["a"], svc.bridge("two").dag.nodes["b"]
    first.state_variables["asked_at"], second.state_variables["asked_at"] = 200.0, 100.0
    got = svc.human_requests()
    assert [(r["plan_id"], r["node_id"], r["plan_title"]) for r in got] == [("two", "b", "Two"), ("one", "a", "One")]
    svc.set_paused("two", "user", True)
    assert [r["plan_id"] for r in svc.human_requests()] == ["one"]


def test_an_answer_from_the_person_goes_through_the_plan_and_sets_the_agent_going(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", n("write"), ask("ok", deps=["write"], ask="approve", into="ok"), n("publish", deps=["ok"]))})
    svc.handle_event("p", {"type": "plan:mark", "node_id": "write", "status": "in_progress", "actor": "chat"})
    svc.handle_event("p", {"type": "plan:mark", "node_id": "write", "status": "completed", "actor": "chat"})
    assert [r["node_id"] for r in svc.human_requests()] == ["ok"] and svc.launcher.runs == []
    refused = svc.handle_event("p", {"type": "plan:answer", "node_id": "ok", "answer": "perhaps", "actor": "user"})
    assert refused["ok"] is False and "yes or no" in refused["error"]
    done = svc.handle_event("p", {"type": "plan:answer", "node_id": "ok", "answer": True, "actor": "user"})
    assert done["ok"] and done["value"] is True and done["ready"] == ["publish"]
    assert svc.human_requests() == []
    assert [r[0] for r in svc.launcher.runs] == ["p"] and "publish" in svc.launcher.runs[0][2]
    assert svc.bridge("p").history[-1]["type"] in ("answer", "auto", "mark") and any(h["type"] == "answer" for h in svc.bridge("p").history)


def test_an_answer_the_chat_gives_on_the_persons_behalf_starts_nothing_more(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", ask("ok", ask="approve", into="ok"), n("go", deps=["ok"]))})
    svc.handle_event("p", {"type": "plan:mark", "node_id": "ok", "status": "in_progress", "actor": "chat"})
    svc.handle_event("p", {"type": "plan:mark", "node_id": "ok", "status": "pending", "actor": "chat"})
    assert svc.handle_event("p", {"type": "plan:answer", "node_id": "ok", "answer": "yes", "actor": "chat"})["ok"]
    assert svc.launcher.runs == []
