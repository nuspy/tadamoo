"""A request of a plan to the person (an approval, or a value): when it is asking, what is shown, how the answer is checked and what it sets going."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import PlanDAG, PlanError
from tadamoo.planner.nodes import clean_answer
from serve.plans import PlanService
from test_planner_starts import Recorder, n, plan, start


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


# -- one question for each value, answered together --------------------------------------------------------------------------------

def q(nid, deps=(), **spec):
    return n(nid, "input", deps=deps, assignee="user", spec={"prompt": f"Question {nid}?", "into": nid, **spec})


def test_a_question_has_its_help_and_its_english_and_choices_that_show_a_label_never_the_value():
    from tadamoo.planner.nodes import clean_spec, shown_choices
    spec = clean_spec("input", {"prompt": "Quante notizie?", "help": "Da 1 a 9.", "prompt_en": "How many stories?", "help_en": "From 1 to 9.", "into": "count", "kind": "choice",
                                "choices": [{"value": "n3", "label": "Tre notizie", "label_en": "Three stories"}, {"value": "n6", "label": "Sei notizie"}, "n9"], "default": "n6", "other": False})
    assert spec["help_en"] == "From 1 to 9." and spec["default"] == "n6"
    assert shown_choices(spec) == [{"value": "n3", "label": "Tre notizie", "label_en": "Three stories"}, {"value": "n6", "label": "Sei notizie", "label_en": "Sei notizie"},
                                   {"value": "n9", "label": "n9", "label_en": "n9"}]
    assert clean_answer(spec, "n3") == "n3"
    with pytest.raises(PlanError, match="one of: n3, n6, n9"):
        clean_answer(spec, "Tre notizie")                                            # the page sends the value, not what it shows


def test_a_default_is_one_the_question_would_take_and_choices_have_their_own_values():
    from tadamoo.planner.nodes import clean_spec
    with pytest.raises(PlanError, match="one of"):
        clean_spec("input", {"prompt": "x", "into": "v", "kind": "choice", "choices": ["a", "b"], "default": "c"})
    with pytest.raises(PlanError, match="same value"):
        clean_spec("input", {"prompt": "x", "into": "v", "kind": "choice", "choices": [{"value": "a", "label": "A"}, "a"]})
    with pytest.raises(PlanError, match="number"):
        clean_spec("input", {"prompt": "x", "into": "v", "kind": "number", "default": "many"})
    assert clean_spec("input", {"prompt": "x", "ask": "approve", "default": "yes"})["default"] is True
    with pytest.raises(PlanError, match="value"):
        clean_spec("input", {"prompt": "x", "into": "v", "kind": "choice", "choices": [{"label": "no value"}, "b"]})


def test_a_plan_saved_with_choices_as_plain_texts_shows_them_as_they_are():
    dag = PlanDAG.from_dict(plan("p", q("k", kind="choice", choices=["red", "blue"])))
    [r] = dag.human_requests()
    assert r["choices"] == [{"value": "red", "label": "red", "label_en": "red"}, {"value": "blue", "label": "blue", "label_en": "blue"}]
    assert dag.to_dict()["nodes"]["k"]["spec"]["choices"] == ["red", "blue"]


def test_the_requests_come_in_the_order_of_the_plan_with_what_is_proposed():
    dag = PlanDAG.from_dict(plan("p", q("a", default="x"), q("b", ask="approve", default=True), q("c", kind="number"), n("work")))
    got = dag.human_requests()
    assert [r["node_id"] for r in got] == ["a", "b", "c"] and [r["index"] for r in got] == sorted(r["index"] for r in got)
    assert [r["proposed"] for r in got] == ["x", True, None]


def test_the_answers_of_the_last_time_are_what_is_proposed_when_the_plan_starts_again():
    dag = PlanDAG.from_dict(plan("p", start("go", 1), q("a", deps=["go"], default="x"), q("b", deps=["go"], kind="number")))
    dag.begin(1, "the person")
    dag.answer_many({"a": "mine", "b": "7"})
    assert dag.run_state() == "finished"
    dag.begin(1, "the person")
    assert {r["node_id"]: r["proposed"] for r in dag.human_requests()} == {"a": "mine", "b": 7}


def test_all_the_answers_are_taken_together_or_none():
    dag = PlanDAG.from_dict(plan("p", q("a"), q("b", kind="number"), q("c", ask="approve"), n("after", deps=["a", "b", "c"])))
    with pytest.raises(PlanError, match="'b': the answer is a number"):
        dag.answer_many({"a": "fine", "b": "many", "c": "yes"})
    assert [dag.nodes[i].status for i in "abc"] == ["pending"] * 3 and dag.shared_state == {}        # nothing was taken
    with pytest.raises(PlanError, match="does not ask"):
        dag.answer_many({"a": "x", "after": "y"})
    got = dag.answer_many({"c": "no", "a": "fine", "b": "3"})
    assert got == {"c": False, "a": "fine", "b": 3} and dag.shared_state == {"a": "fine", "b": 3, "c": False}
    assert dag.ready_nodes() == ["after"] and dag.human_requests() == []
    with pytest.raises(PlanError, match="already"):
        dag.answer_many({"a": "again"})
    with pytest.raises(PlanError, match="object"):
        dag.answer_many({})


def test_the_start_of_a_plan_is_never_among_the_questions_and_is_not_a_request(tmp_path):
    dag = PlanDAG.from_dict(plan("p", start("go", 1, "interval", words="ogni lunedì alle 9"), q("a", deps=["go"])))
    assert dag.human_requests() == []                                               # the start is not taken yet: nothing asks
    dag.begin(1, "the person")
    assert [r["node_id"] for r in dag.human_requests()] == ["a"]
    plain = PlanDAG.from_dict(plan("q", start("go2", 1), q("a")))
    assert "go2" not in [r["node_id"] for r in plain.human_requests()]


def test_the_service_takes_the_answers_sent_together_and_sets_the_agent_going(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", q("a"), q("b", kind="number"), n("go", deps=["a", "b"]))})
    svc.handle_event("p", {"type": "plan:mark", "node_id": "go", "status": "in_progress", "actor": "chat"})   # a run that is active
    svc.handle_event("p", {"type": "plan:mark", "node_id": "go", "status": "pending", "actor": "chat"})
    bad = svc.handle_event("p", {"type": "plan:answers", "answers": {"a": "x", "b": "many"}, "actor": "user"})
    assert bad["ok"] is False and "number" in bad["error"] and len(svc.human_requests()) == 2
    done = svc.handle_event("p", {"type": "plan:answers", "answers": {"a": "x", "b": "4"}, "actor": "user"})
    assert done["ok"] and done["values"] == {"a": "x", "b": 4} and done["ready"] == ["go"]
    assert svc.human_requests() == [] and [r[0] for r in svc.launcher.runs] == ["p"]


# -- "Other, I write", and the steps for the person ----------------------------------------------------------------------------------

def test_a_menu_of_more_than_two_choices_also_takes_what_the_person_writes_two_choices_and_a_closed_question_do_not():
    from tadamoo.planner.nodes import allows_other, clean_spec
    three = clean_spec("input", {"prompt": "x", "into": "v", "kind": "choice", "choices": ["a", "b", "c"]})
    assert allows_other(three) and clean_answer(three, "something of mine") == "something of mine"
    closed = clean_spec("input", {"prompt": "x", "into": "v", "kind": "choice", "choices": ["a", "b", "c"], "other": False})
    assert not allows_other(closed)
    with pytest.raises(PlanError, match="one of"):
        clean_answer(closed, "something of mine")
    two = clean_spec("input", {"prompt": "x", "into": "v", "kind": "choice", "choices": ["a", "b"]})
    assert not allows_other(two)
    with pytest.raises(PlanError, match="one of"):
        clean_answer(two, "c")
    with pytest.raises(PlanError, match="true or false"):
        clean_spec("input", {"prompt": "x", "into": "v", "kind": "choice", "choices": ["a", "b", "c"], "other": "yes"})
    dag = PlanDAG.from_dict(plan("p", q("k", kind="choice", choices=["a", "b", "c"]), q("t", kind="choice", choices=["a", "b"])))
    assert {r["node_id"]: r["other"] for r in dag.human_requests()} == {"k": True, "t": False}


def task(nid, deps=(), **kw):
    return n(nid, "action", deps=deps, assignee="user", description=kw.pop("description", "Call the notary and ask for {doc}."), **kw)


def test_a_step_for_the_person_is_asked_with_its_description_and_done_with_a_note_that_becomes_its_output():
    dag = PlanDAG.from_dict(plan("p", n("find", "set_var", spec={"name": "doc", "value": "the deed"}), task("call", deps=["find"]), n("after", deps=["call"])))
    dag.advance(0)
    [r] = dag.human_requests()
    assert (r["ask"], r["prompt"], r["content"], r["into"], r["other"]) == ("task", "call", "Call the notary and ask for the deed.", "call_note", False)
    with pytest.raises(PlanError, match="done"):
        dag.answer("call", {"done": False})
    assert dag.nodes["call"].status == "pending"
    dag.answer("call", {"done": True, "note": "  Appointment on Monday  "})
    assert dag.nodes["call"].status == "completed" and dag.shared_state["call_note"] == "Appointment on Monday"
    assert dag.nodes["call"].state_variables["outputs"] == {"call_note": "Appointment on Monday"} and dag.ready_nodes() == ["after"] and dag.human_requests() == []


def test_a_task_done_without_a_note_has_no_output_and_one_with_a_declared_output_gives_the_note_to_it():
    dag = PlanDAG.from_dict(plan("p", task("one"), task("two", produces=[{"name": "when", "type": "text"}]), task("three", produces=[{"name": "n", "type": "number"}])))
    assert sorted(r["node_id"] for r in dag.human_requests()) == ["one", "two"]          # a task with outputs the window cannot ask for stays for the Plans page
    got = dag.answer_many({"one": {"done": True}, "two": {"done": True, "note": "Monday"}})
    assert got == {"one": {"done": True, "note": ""}, "two": {"done": True, "note": "Monday"}}
    assert dag.shared_state == {"when": "Monday"} and "outputs" not in dag.nodes["one"].state_variables


def test_a_task_answers_with_the_questions_of_the_plan_together_or_none():
    dag = PlanDAG.from_dict(plan("p", q("a", kind="number"), task("t")))
    with pytest.raises(PlanError, match="'a': the answer is a number"):
        dag.answer_many({"a": "many", "t": {"done": True}})
    assert dag.nodes["t"].status == "pending"
    dag.answer_many({"a": "3", "t": {"done": True, "note": "ok"}})
    assert dag.shared_state == {"a": 3, "t_note": "ok"}
    third = PlanDAG.from_dict(plan("p", n("x", assignee="third_party"), n("y")))
    assert third.human_requests() == []                                              # what is for someone else, or for the agent, is not asked of the person
