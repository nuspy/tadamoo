"""The steps a plan works out by itself: waits that end, values set and read, result and log lines, a loop that is not entered, and what the agent is offered."""
import pytest

from tadamoo.planner.dag import PlanDAG, node_from_dict

T0 = 1_800_000_000.0


def n(nid, kind, spec=None, parents=(), **extra):
    return node_from_dict({"id": nid, "type": kind, "title": extra.pop("title", nid), "description": "", "assignee": "agent",
                           "dependencies": list(parents), **({"spec": spec} if spec is not None else {}), **extra})


def plan(*steps):
    dag = PlanDAG("p", "P", "occasional")
    for step in steps:
        dag.add_node(step)
    return dag


def test_a_wait_of_a_time_ends_when_the_time_has_passed_and_lets_the_next_step_start():
    dag = plan(n("w", "wait_duration", {"seconds": 600}), n("after", "action", parents=["w"]))
    first = dag.advance(T0)
    assert first["done"] == [] and first["next_due"] == T0 + 600 and dag.ready_nodes(T0) == []
    assert dag.nodes["w"].state_variables["wake_at"] == T0 + 600
    assert dag.advance(T0 + 599)["done"] == []
    last = dag.advance(T0 + 600)
    assert last["done"] == ["w"] and dag.nodes["w"].status == "completed" and dag.ready_nodes(T0 + 600) == ["after"]
    assert any("waited 600 s" in j["text"] for j in dag.journal)


def test_a_wait_for_a_date_uses_the_local_time_given():
    from datetime import datetime
    when = datetime(2030, 1, 5, 14, 50).astimezone().timestamp()
    dag = plan(n("w", "wait_until", {"at": "2030-01-05 14:50"}))
    assert dag.advance(when - 1)["next_due"] == when
    assert dag.advance(when)["done"] == ["w"]


def test_the_agent_is_never_offered_a_step_the_plan_resolves_itself():
    dag = plan(n("w", "wait_duration", {"seconds": 60}), n("s", "set_var", {"name": "x", "value": 1}), n("a", "action"))
    assert dag.ready_nodes(T0) == ["a"]


def test_a_value_is_set_from_a_constant_or_an_expression_and_read_into_another_name():
    dag = plan(n("one", "set_var", {"name": "base", "value": 40}), n("two", "set_var", {"name": "total", "expr": "base + 2"}, ["one"]),
               n("read", "get_var", {"name": "total", "into": "copy"}, ["two"]))
    out = dag.advance(T0)
    assert out["done"] == ["one", "two", "read"]                                  # each one lets the next start in the same pass
    assert dag.shared_state == {"base": 40, "total": 42, "copy": 42}
    assert dag.nodes["read"].state_variables["value"] == 42


def test_a_value_that_cannot_be_worked_out_yet_waits_and_says_why():
    dag = plan(n("two", "set_var", {"name": "total", "expr": "base + 2"}))
    assert dag.advance(T0)["done"] == [] and "base" in dag.nodes["two"].state_variables["auto_error"]
    dag.shared_state["base"] = 1
    assert dag.advance(T0 + 1)["done"] == ["two"] and dag.shared_state["total"] == 3
    assert "auto_error" not in dag.nodes["two"].state_variables


def test_a_result_and_a_log_line_are_written_with_the_plans_values():
    dag = plan(n("v", "set_var", {"name": "bilancio", "value": 120}), n("o", "output", {"name": "risultato", "template": "P&L {bilancio}"}, ["v"]),
               n("l", "log", {"message": "{plan_title}: checked, {bilancio}"}, ["o"]))
    dag.advance(T0)
    assert dag.shared_state["risultato"] == "P&L 120"
    kinds = [(j["kind"], j["text"]) for j in dag.journal]
    assert ("output", "risultato: P&L 120") in kinds and ("log", "P: checked, 120") in kinds


def test_a_wait_for_a_condition_ends_when_it_holds_and_does_not_when_a_value_is_missing():
    dag = plan(n("w", "wait_condition", {"expr": "bilancio > 100", "poll_s": 120}), n("after", "action", parents=["w"]))
    first = dag.advance(T0)
    assert first["done"] == [] and first["next_due"] == T0 + 120 and first["judge"] == []
    dag.shared_state["bilancio"] = 50
    assert dag.advance(T0 + 10)["done"] == []
    dag.shared_state["bilancio"] = 150
    assert dag.advance(T0 + 20)["done"] == ["w"] and dag.ready_nodes(T0 + 20) == ["after"]


def test_a_wait_in_words_asks_for_a_judgement_and_a_wait_with_both_does_not():
    words = plan(n("w", "wait_condition", {"describe": "the market opens higher"}))
    assert words.advance(T0)["judge"] == ["w"]
    both = plan(n("w", "wait_condition", {"describe": "the market opens higher", "expr": "up == True"}))
    assert both.advance(T0)["judge"] == []


def test_a_wait_that_runs_out_of_time_times_out_or_is_skipped():
    failing = plan(n("w", "wait_condition", {"expr": "x > 1", "timeout_s": 300}), n("after", "action", parents=["w"]))
    assert failing.advance(T0)["next_due"] == T0 + 300
    failing.advance(T0 + 300)
    assert failing.nodes["w"].status == "blocked" and "timed_out" in failing.nodes["w"].state_variables
    skipping = plan(n("w", "wait_condition", {"expr": "x > 1", "timeout_s": 300, "on_timeout": "skip"}))
    skipping.advance(T0)
    skipping.advance(T0 + 301)
    assert skipping.nodes["w"].status == "skipped"


def test_a_loop_that_does_not_hold_on_arrival_is_not_entered():
    dag = plan(n("l", "loop", {"while": "stock < 3"}), n("after", "action", parents=["l"]))
    dag.shared_state["stock"] = 10
    assert dag.advance(T0)["done"] == ["l"] and dag.nodes["l"].status == "completed" and dag.nodes["l"].state_variables["runs"] == 0
    assert dag.ready_nodes(T0) == ["after"]


def test_a_loop_that_holds_is_worked_again_until_it_stops_holding():
    dag = plan(n("l", "loop", {"while": "stock < 3", "every": 60, "max_iter": 5}), n("after", "action", parents=["l"]))
    dag.shared_state["stock"] = 1
    assert dag.advance(T0)["done"] == [] and dag.ready_nodes(T0) == ["l"]
    dag.mark("l", "completed", {"stock": 2}, T0)                                  # one pass done, still holds
    assert dag.nodes["l"].status == "pending" and dag.nodes["l"].state_variables["runs"] == 1
    assert dag.ready_nodes(T0 + 30) == [] and dag.ready_nodes(T0 + 60) == ["l"]
    dag.mark("l", "completed", {"stock": 3}, T0 + 60)                             # no longer holds: it ends
    assert dag.nodes["l"].status == "completed" and dag.ready_nodes(T0 + 61) == ["after"]


def test_a_loop_is_capped_by_max_iter():
    dag = plan(n("l", "loop", {"while": "True", "every": 60, "max_iter": 2}))
    dag.mark("l", "completed", None, T0)
    dag.mark("l", "completed", None, T0 + 60)
    assert dag.nodes["l"].status == "completed" and dag.nodes["l"].state_variables["runs"] == 2


def test_the_journal_is_saved_with_the_plan_and_kept_short():
    dag = plan(n("l", "log", {"message": "hello"}))
    dag.advance(T0)
    again = PlanDAG.from_dict(dag.to_dict())
    assert [j["text"] for j in again.journal] == ["hello"]
    for i in range(300):
        dag.note("l", "log", f"line {i}", T0 + i)
    assert len(dag.journal) == 200 and dag.journal[-1]["text"] == "line 299"
    assert "journal" not in PlanDAG("q", "Q", "occasional").to_dict()


def test_a_question_to_the_person_is_ready_for_them_not_resolved_by_the_plan():
    dag = plan(n("q", "input", {"prompt": "How much?", "into": "importo"}))
    assert dag.advance(T0)["done"] == [] and dag.ready_nodes(T0) == ["q"]
    dag.mark("q", "completed", {"importo": 12}, T0)
    assert dag.shared_state["importo"] == 12
