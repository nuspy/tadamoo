"""Where a plan begins: start nodes with a number and a way to start (the person, an event, intervals written in words, another plan), the implicit
start of a plan that had none, and what the service does about them with a clock it is given and a launcher that is a recorder."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import PlanDAG, PlanError
from tadamoo.planner.dag import node_from_dict
from serve.plans import PlanService


def n(nid, typ="action", deps=(), assignee="agent", **kw):
    return {"id": nid, "type": typ, "title": kw.pop("title", nid), "assignee": assignee, "dependencies": list(deps),
            "outputs": [], "status": "pending", **kw}


def plan(plan_id, *nodes):
    """A saved plan from nodes given with `dependencies`; the outputs are filled in."""
    by = {x["id"]: dict(x) for x in nodes}
    for x in by.values():
        for d in x["dependencies"]:
            by[d]["outputs"].append(x["id"])
    return {"plan_id": plan_id, "title": plan_id.title(), "target_type": "occasional", "nodes": by}


def start(nid, number=1, launch="manual", **spec):
    return n(nid, "start", spec={"number": number, "launch": launch, **spec}, title=f"Start {number}")


def two_starts():
    return plan("p", start("a", 1), start("b", 2), n("x", deps=["a"]), n("y", deps=["b"]), n("m", "merge", deps=["x", "y"]), n("z", deps=["m"]))


def at(text):
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


# -- the start of every plan ----------------------------------------------------------------------------------------

def test_a_plan_without_a_start_gets_one_that_is_already_taken_and_its_steps_are_where_they_were():
    old = {"plan_id": "p", "title": "P", "target_type": "occasional", "nodes": {k: {**v, "outputs": list(v["outputs"])} for k, v in plan("p", n("a"), n("b", deps=["a"]), n("c"))["nodes"].items()}}
    dag = PlanDAG.from_dict(old)
    start_node = dag.nodes["start"]
    assert start_node.status == "completed" and start_node.state_variables["implicit"] and start_node.spec == {"number": 1, "launch": "manual"}
    assert sorted(start_node.outputs) == ["a", "c"] and dag.nodes["a"].dependencies == ["start"] and dag.nodes["b"].dependencies == ["a"]
    assert dag.ready_nodes() == ["a", "c"]                                           # nothing waits for a start that was never there
    assert PlanDAG.from_dict(json.loads(json.dumps(dag.to_dict()))).to_dict() == dag.to_dict()      # and it is not made twice


def test_the_id_of_the_implicit_start_is_free_and_an_empty_plan_gets_none():
    dag = PlanDAG.from_dict(plan("p", n("start"), n("x", deps=["start"])))
    assert "start_2" in dag.nodes and dag.nodes["start_2"].outputs == ["start"]       # a step called start is a step like any other, and the root
    assert PlanDAG.from_dict({"plan_id": "q", "title": "Q", "target_type": "occasional", "nodes": {}}).nodes == {}


def test_a_plan_that_is_made_gets_its_start_and_the_steps_count_without_it(tmp_path):
    svc = PlanService(tmp_path / "planner")
    snap = svc.create({"plan": plan("p", n("a"), n("b", deps=["a"]))})
    assert "start" in snap["plan"]["nodes"] and snap["ready"] == ["a"]
    svc.handle_event("p", {"type": "plan:mark", "node_id": "a", "status": "completed"})
    row = svc.list()[0]
    assert (row["nodes"], row["completed"]) == (2, 1)
    assert list(svc.bridge("p").card.logistica_e_prenotazioni) == ["a", "b"]


# -- several starts ---------------------------------------------------------------------------------------------------

def test_a_plan_starts_at_one_start_and_the_others_and_what_only_they_lead_to_are_skipped():
    dag = PlanDAG.from_dict(two_starts())
    assert dag.run_state() == "idle" and dag.ready_nodes() == []                     # the steps wait for a start
    assert dag.begin(2, "the person") == "b"
    assert (dag.nodes["b"].status, dag.nodes["a"].status, dag.nodes["x"].status) == ("completed", "skipped", "skipped")
    assert dag.ready_nodes() == ["y"] and dag.run_state() == "running"
    dag.mark("y", "completed")
    assert dag.ready_nodes() == ["m"]                                                # the merge goes on with the branch that is alive
    dag.mark("m", "completed")
    dag.mark("z", "completed")
    assert dag.run_state() == "finished"


def test_a_running_plan_is_not_started_again_and_a_finished_one_starts_again_from_the_beginning():
    dag = PlanDAG.from_dict(two_starts())
    dag.begin(1, "the person")
    with pytest.raises(PlanError, match="already running"):
        dag.begin(2, "the person")
    for step in ("x", "m", "z"):
        dag.mark(step, "completed")
    assert dag.run_state() == "finished"
    dag.begin(2, "its schedule")
    assert dag.nodes["z"].status == "pending" and dag.nodes["x"].status == "skipped" and dag.nodes["b"].state_variables["runs"] == 1
    assert dag.ready_nodes() == ["y"]
    with pytest.raises(PlanError, match="no start number 7"):
        PlanDAG.from_dict(two_starts()).begin(7, "the person")


def test_every_start_has_its_own_number():
    dag = PlanDAG.from_dict(plan("p", start("a", 1), n("x", deps=["a"])))
    dag.apply_diff({"add_nodes": [n("b", "start", spec={"number": 1, "launch": "manual"})]})
    assert sorted(s.spec["number"] for s in dag.start_nodes()) == [1, 2]            # a start added with a taken number gets the next free one
    with pytest.raises(PlanError, match="already number"):
        dag.update_node("b", spec={"number": 1, "launch": "manual"})
    with pytest.raises(PlanError, match="already number"):
        dag.apply_diff({"update_nodes": {"b": {"spec": {"number": 1, "launch": "manual"}}}})
    with pytest.raises(PlanError, match="same number"):
        two = PlanDAG.from_dict(two_starts())
        two.nodes["b"].spec["number"] = 1
        two.validate_starts()


def test_a_start_takes_no_input_and_its_launch_is_checked():
    dag = PlanDAG.from_dict(plan("p", start("a", 1), n("x")))
    with pytest.raises(PlanError, match="cannot take"):
        dag.add_edge("x", "a")
    with pytest.raises(PlanError, match="launch"):
        node_from_dict(start("s", 1, "sometimes"))
    with pytest.raises(PlanError, match="condition"):
        node_from_dict(start("s", 1, "event"))
    with pytest.raises(PlanError, match="not a schedule I understand|for example"):
        node_from_dict(start("s", 1, "interval", words="when the moon is blue"))
    ok = node_from_dict(start("s", 1, "interval", words="ogni primo lunedì alle 9"))
    assert ok.spec["schedule"] == "cron 0 9 * * 1#1" and ok.spec["words"] == "ogni primo lunedì alle 9"
    assert node_from_dict(start("s", 1, "caller")).spec == {"number": 1, "launch": "caller"}


# -- an event starts a plan --------------------------------------------------------------------------------------------

def test_an_event_starts_the_plan_at_the_start_that_watches_for_it():
    dag = PlanDAG.from_dict(plan("p", start("a", 1, "event", when="'invoice' in text"), start("b", 2, "event", when="'order' in text"), n("x", deps=["a"]), n("y", deps=["b"])))
    assert dag.feed_event({"text": "nothing to see"}) == []
    assert dag.feed_event({"text": "A new ORDER arrived", "source": "mail"}) == ["b"]
    assert dag.nodes["b"].state_variables["started"]["cause"] == "event" and dag.nodes["b"].state_variables["started"]["matched"]["source"] == "mail"
    assert dag.nodes["a"].status == "skipped" and dag.ready_nodes() == ["y"]
    assert dag.feed_event({"text": "an invoice"}) == []                              # it is running: another event does not start it again


# -- the service: intervals, a call from another plan, the launcher ---------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.runs = []
        self.busy = False

    def __call__(self, plan_id, name, text):
        self.runs.append((plan_id, name, text))


def service(tmp_path):
    svc = PlanService(tmp_path / "planner")
    svc.launcher, svc.run_active = Recorder(), lambda plan_id: svc.launcher.busy
    return svc


def rearm(svc, plan_id, node_id, when):
    """The moment a schedule is counted from, as if the plan had been seen first at `when`."""
    node = svc.bridge(plan_id).dag.nodes[node_id]
    node.state_variables["armed"]["at"] = when
    node.state_variables.pop("last_fire", None)
    svc._index(plan_id, when)


def test_an_interval_start_starts_the_plan_at_its_time_once_and_the_next_is_counted_from_there(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", start("a", 1, "interval", words="every first Monday at 9"), n("x", deps=["a"]))})
    rearm(svc, "p", "a", at("2026-11-13 12:00"))
    svc.watch_tick(at("2026-11-13 12:00"))                                           # the first look: nothing is due
    assert svc.bridge("p").dag.nodes["a"].status == "pending"
    svc.watch_tick(at("2026-12-07 08:59"))
    assert svc.bridge("p").dag.nodes["a"].status == "pending" and svc.launcher.runs == []
    svc.watch_tick(at("2026-12-07 09:00") + 5)
    dag = svc.bridge("p").dag
    assert dag.nodes["a"].status == "completed" and dag.nodes["a"].state_variables["started"]["cause"] == "its schedule"
    assert [r[0] for r in svc.launcher.runs] == ["p"] and "Steps ready for you: x" in svc.launcher.runs[0][2]
    svc.watch_tick(at("2026-12-07 09:00") + 60)
    assert len(svc.launcher.runs) == 1                                               # the same time is not fired twice


def test_an_interval_that_comes_while_the_plan_is_running_is_skipped_and_one_missed_while_the_program_was_off_fires_once(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", start("a", 1, "interval", words="every 2 hours"), n("x", deps=["a"]), n("y", deps=["x"]))})
    base = at("2026-11-13 12:00")
    rearm(svc, "p", "a", base)
    svc.watch_tick(base)
    svc.watch_tick(base + 2 * 3600 + 1)
    assert svc.bridge("p").dag.run_state() == "running" and len(svc.launcher.runs) == 1
    svc.watch_tick(base + 4 * 3600 + 2)                                              # still running: skipped, and said so
    assert len(svc.launcher.runs) == 1 and any("not started" in j["text"] for j in svc.bridge("p").dag.journal)
    # the program stops and starts again a day later: the plan, as saved, is read by a new service
    again = service(tmp_path)
    again.watch_tick(base + 30 * 3600)
    again.bridge("p").dag.mark("x", "completed")
    again.bridge("p").dag.mark("y", "completed")                                    # the run finished meanwhile
    again.watch_tick(base + 33 * 3600)
    assert again.bridge("p").dag.nodes["a"].state_variables["runs"] == 2             # it started once for the many times that were missed
    assert len(again.launcher.runs) == 1


def test_a_step_that_starts_another_plan_names_the_start_to_use_and_goes_on_at_once(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("callee", start("s1", 1, "caller"), start("s2", 2, "caller"), n("one", deps=["s1"]), n("two", deps=["s2"]))})
    svc.create({"plan": plan("caller", n("go", "start_plan", spec={"plan": "callee", "start": 2}), n("after", deps=["go"]))})
    svc.tick("caller")
    callee, caller = svc.bridge("callee").dag, svc.bridge("caller").dag
    assert callee.nodes["s2"].state_variables["started"]["caller"] == {"plan": "caller", "node": "go"} and callee.ready_nodes() == ["two"]
    assert callee.nodes["s1"].status == "skipped"
    assert caller.nodes["go"].status == "completed" and caller.ready_nodes() == ["after"]
    assert [r[0] for r in svc.launcher.runs] == ["callee"]


def test_a_call_that_cannot_be_made_is_tried_again_and_then_fails_with_its_reason(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("caller", n("go", "start_plan", spec={"plan": "nobody", "start": 1}, retry_policy={"max_attempts": 2, "wait_seconds": 60, "backoff": 1}))})
    go = svc.bridge("caller").dag.nodes["go"]                                       # the plan was looked at as it was made: the first try failed
    assert go.status == "pending" and go.state_variables["attempts"] == 1 and "no plan 'nobody'" in go.state_variables["failures"][0]["reason"]
    now = go.state_variables["retry_at"]
    svc.tick("caller", now - 1)
    assert go.status == "pending" and go.state_variables["attempts"] == 1           # not before its wait
    svc.tick("caller", now + 1)
    assert go.status == "failed"
    svc.create({"plan": plan("busy", start("s1", 1, "caller"), n("one", deps=["s1"]))})
    svc.start_plan("busy", 1, "the person")
    svc.create({"plan": plan("c2", n("go", "start_plan", spec={"plan": "busy", "start": 1}))})
    assert "already running" in svc.bridge("c2").dag.nodes["go"].state_variables["failures"][0]["reason"]


def test_the_agent_is_started_only_for_steps_that_are_for_it(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", start("a", 1), n("ask", "input", spec={"prompt": "Ok?", "ask": "approve"}, deps=["a"], assignee="user"))})
    svc.start_plan("p", 1, "the person")
    assert svc.launcher.runs == []                                                  # the only step waits for the person
    svc.create({"plan": plan("q", start("a", 1), n("work", deps=["a"]))})
    svc.start_plan("q", 1, "the person")
    svc.launcher.busy = True
    svc.create({"plan": plan("r", start("a", 1), n("work", deps=["a"]))})
    svc.start_plan("r", 1, "the person")
    assert [r[0] for r in svc.launcher.runs] == ["q"]                               # not while a conversation is already at work on it


def test_a_start_the_person_presses_and_one_the_chat_makes(tmp_path):
    svc = service(tmp_path)
    svc.create({"plan": plan("p", start("a", 1), n("x", deps=["a"]))})
    res = svc.handle_event("p", {"type": "plan:start", "number": 1, "actor": "user"})
    assert res["ok"] and res["started"] == "a" and len(svc.launcher.runs) == 1
    assert svc.bridge("p").run["active"] and svc.handle_event("p", {"type": "plan:start", "number": 1, "actor": "user"})["ok"] is False
    svc.create({"plan": plan("q", start("a", 1), n("x", deps=["a"]))})
    assert svc.handle_event("q", {"type": "plan:start", "number": 1, "actor": "chat"})["ok"]
    assert len(svc.launcher.runs) == 1                                              # the chat that started it carries on by itself
