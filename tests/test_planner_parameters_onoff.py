"""Parameters (values of the plan the person sets, with defaults, never asked at the start), the plan being on or off (Start plan, Stop plan, an End step), starts armed
together while it is on, and the migration of a plan whose questions were asked when it started."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import PlanDAG, PlanError
from serve.plans import PlanService
from test_planner_starts import n, plan, rearm, service, start


def param(nid, into, kind="text", default=None, **spec):
    body = {"prompt": nid, "into": into, "kind": kind, **spec}
    if default is not None:
        body["default"] = default
    return n(nid, "parameter", spec=body, status="completed")


def ask(nid, into, deps=(), kind="text", default=None, **spec):
    body = {"prompt": nid, "into": into, "kind": kind, **spec}
    if default is not None:
        body["default"] = default
    return n(nid, "input", deps=deps, assignee="user", spec=body)


def at(text):
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


# -- parameters ---------------------------------------------------------------------------------------------------------

def test_a_parameter_is_settled_from_the_start_with_its_default_as_the_plans_value_and_is_never_asked():
    dag = PlanDAG.from_dict(plan("p", start("s"), n("a", deps=["s"]), param("q_edition", "edition", "choice", "evening", choices=["morning", "evening", "night"]), param("q_n", "stories", "number", 2)))
    assert dag.shared_state["edition"] == "evening" and dag.shared_state["stories"] == 2
    assert dag.run_state() == "idle"                                                   # a parameter does not make the plan look started
    dag.begin(1, "the person")
    assert dag.human_requests() == [] and dag.ready_nodes() == ["a"]
    rows = {r["name"]: r for r in dag.parameters()}
    assert rows["edition"]["value"] == "evening" and rows["edition"]["default"] == "evening" and [c["value"] for c in rows["edition"]["choices"]] == ["morning", "evening", "night"]


def test_parameters_are_set_by_name_all_or_none_and_may_be_empty():
    dag = PlanDAG.from_dict(plan("p", start("s"), param("q_edition", "edition", "choice", "evening", choices=["morning", "evening", "night"], other=False), param("q_n", "stories", "number", 2),
                                 param("q_t", "brief", "text", "x")))
    assert dag.set_parameters({"edition": "night", "stories": "5", "brief": ""}) == {"edition": "night", "stories": 5, "brief": ""}
    assert dag.shared_state["edition"] == "night" and dag.shared_state["stories"] == 5 and dag.shared_state["brief"] == ""
    with pytest.raises(PlanError, match="one of"):
        dag.set_parameters({"stories": 9, "edition": "noon"})
    assert dag.shared_state["stories"] == 5                                            # nothing was kept
    with pytest.raises(PlanError, match="number"):
        dag.set_parameters({"stories": "many"})
    with pytest.raises(PlanError, match="no parameter"):
        dag.set_parameters({"nothing": 1})
    assert dag.set_parameters({"q_n": 3}) == {"stories": 3}                            # the node id works too


def test_a_plan_saved_with_its_questions_asked_at_the_start_gets_parameters_instead_and_what_waited_waits_for_the_start():
    dag = PlanDAG.from_dict(plan("p", start("s"), ask("q1", "edition", ["s"], default="evening"), ask("q2", "stories", ["s"], "number", 3), n("a", deps=["q1", "q2"]),
                                 ask("mid", "answer_x", ["a"]), n("b", deps=["mid"])))
    assert dag.migrate_initial_questions() == ["q1", "q2"]
    assert dag.nodes["q1"].type.value == "parameter" and dag.nodes["q1"].status == "completed" and dag.nodes["q1"].dependencies == [] and dag.nodes["q1"].outputs == []
    assert dag.nodes["a"].dependencies == ["s"] and dag.shared_state["edition"] == "evening" and dag.shared_state["stories"] == 3
    assert dag.nodes["mid"].type.value == "input" and dag.nodes["mid"].dependencies == ["a"]       # a question that comes later in the run stays a request
    dag.begin(1, "the person")
    assert dag.human_requests() == [] and dag.ready_nodes() == ["a"]
    assert dag.migrate_initial_questions() == []                                        # once is enough


def test_a_question_already_answered_keeps_its_answer_as_the_value():
    data = plan("p", start("s"), ask("q1", "edition", ["s"], default="evening"), n("a", deps=["q1"]))
    data["shared_state"] = {"edition": "night"}
    dag = PlanDAG.from_dict(data)
    dag.migrate_initial_questions()
    assert dag.shared_state["edition"] == "night"


# -- on and off -------------------------------------------------------------------------------------------------------

def test_a_plan_saved_before_on_and_off_is_on_when_it_has_a_start_that_watches_or_counts_time_and_beginning_switches_it_on():
    manual = PlanDAG.from_dict(plan("p", start("s"), n("a", deps=["s"])))
    timed = PlanDAG.from_dict(plan("q", start("s", 1, "interval", words="every 6 hours"), n("a", deps=["s"])))
    assert manual.active is None and not manual.is_on() and timed.is_on()
    manual.begin(1, "the person")
    assert manual.active is True and manual.is_on()
    assert PlanDAG.from_dict(manual.to_dict()).active is True                           # kept with the plan


def test_stopping_switches_the_plan_off_and_skips_what_had_not_finished_and_the_next_start_begins_again():
    dag = PlanDAG.from_dict(plan("p", start("s"), n("a", deps=["s"]), n("b", deps=["a"]), param("q", "x", "text", "1")))
    dag.begin(1, "the person")
    dag.mark("a", "completed")
    assert dag.stop() == ["b"] and not dag.is_on() and dag.nodes["b"].status == "skipped" and dag.run_state() == "finished"
    assert dag.nodes["q"].status == "completed"                                         # a parameter is not a step
    dag.begin(1, "the person")                                                          # begins again from the top
    assert dag.is_on() and dag.nodes["a"].status == "pending" and dag.nodes["b"].status == "pending"


def test_reaching_an_end_step_switches_the_plan_off():
    dag = PlanDAG.from_dict(plan("p", start("s"), n("a", deps=["s"]), n("e", "end", deps=["a"], title="End")))
    dag.begin(1, "the person")
    assert dag.is_on()
    dag.mark("a", "completed")
    report = dag.advance(1000.0)
    assert "e" in report["done"] and dag.nodes["e"].status == "completed" and dag.active is False and not dag.is_on() and dag.run_state() == "finished"


def test_an_end_step_has_no_output_and_a_parameter_has_no_connector():
    dag = PlanDAG.from_dict(plan("p", start("s"), n("e", "end", deps=["s"]), n("a"), param("q", "x")))
    with pytest.raises(PlanError):
        dag.add_edge("e", "a")
    with pytest.raises(PlanError):
        dag.add_edge("q", "a")


def test_an_event_starts_a_plan_only_while_it_is_on():
    dag = PlanDAG.from_dict(plan("p", start("s", 1, "event", when="'rain' in text"), n("a", deps=["s"])))
    dag.active = False
    assert dag.feed_event({"text": "rain today", "source": "t"}) == [] and dag.nodes["s"].status == "pending"
    dag.active = True
    assert dag.feed_event({"text": "rain today", "source": "t"}) == ["s"] and dag.nodes["s"].status == "completed"


# -- the events, the snapshot and the service -----------------------------------------------------------------------------

def test_start_plan_stop_plan_and_parameters_through_the_service(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", start("s", 1), start("t", 2, "interval", words="every 6 hours"), n("a", deps=["s"]), n("b", deps=["t"]), param("q", "edition", "text", "evening"))})
    snap = svc.snapshot("p")
    assert snap["active"] is True and snap["parameters"][0]["value"] == "evening"       # it has a start that counts time
    res = svc.handle_event("p", {"type": "plan:stop_plan"})
    assert res["ok"] and res["active"] is False and svc.snapshot("p")["active"] is False and svc.snapshot("p")["run"]["state"] == "idle"
    res = svc.handle_event("p", {"type": "plan:start_plan"})                            # the first manual start (number 1)
    assert res["ok"] and res["started"] == "s" and svc.snapshot("p")["active"] is True
    dag = svc.bridge("p").dag
    assert dag.nodes["t"].status == "skipped"                                           # the other start is left for this run
    res = svc.handle_event("p", {"type": "plan:parameters", "values": {"edition": "night"}})
    assert res["ok"] and svc.snapshot("p")["parameters"][0]["value"] == "night"
    assert not svc.handle_event("p", {"type": "plan:parameters", "values": {"edition": 5, "nothing": 1}})["ok"]
    assert svc.launcher.runs                                                            # the agent was offered what became ready


def test_start_plan_on_a_plan_with_only_a_timed_start_just_arms_it(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", start("t", 1, "interval", words="every 6 hours"), n("b", deps=["t"]))})
    svc.handle_event("p", {"type": "plan:stop_plan"})
    res = svc.handle_event("p", {"type": "plan:start_plan"})
    assert res["ok"] and res["started"] is None and res["active"] is True and svc.bridge("p").dag.run_state() == "idle"


def test_a_plan_that_is_off_arms_no_interval_and_one_that_is_on_fires_it(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", start("a", 1, "interval", words="every first Monday at 9"), n("x", deps=["a"]))})
    rearm(svc, "p", "a", at("2026-11-13 12:00"))
    svc.handle_event("p", {"type": "plan:stop_plan"})
    svc._index("p", at("2026-11-13 12:00"))
    svc.watch_tick(at("2026-12-07 09:00") + 5)
    assert svc.bridge("p").dag.nodes["a"].status != "completed" and svc.launcher.runs == []      # off: nothing fires
    svc.handle_event("p", {"type": "plan:start_plan"})
    assert svc.bridge("p").dag.is_on()


def test_a_plan_loaded_from_disk_with_questions_at_its_start_is_migrated_once(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", start("s"), ask("q1", "edition", ["s"], default="evening"), n("a", deps=["q1"]))})
    svc.store._bridges.clear()
    fresh = PlanService(tmp_path / "planner")
    dag = fresh.bridge("p").dag
    assert dag.nodes["q1"].type.value == "parameter" and dag.nodes["a"].dependencies == ["s"] and fresh.human_requests() == []
    assert PlanService(tmp_path / "planner").bridge("p").dag.nodes["q1"].type.value == "parameter"      # and it was saved
