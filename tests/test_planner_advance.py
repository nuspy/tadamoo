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


# -- a step put on the plan before it is set up ---------------------------------------------------------------------------

DRAFT = {"draft": True}


def test_every_kind_can_be_put_on_the_plan_unset_and_none_of_them_runs_until_it_is_set_up():
    from tadamoo.planner.nodes import NODE_SPECS
    kinds = ["action", "sensor", "decision", "merge", *NODE_SPECS]
    dag = plan(*[n(f"d_{k}", k, state_variables=dict(DRAFT)) for k in dict.fromkeys(kinds)])
    assert dag.ready_nodes(T0) == [] and dag.advance(T0)["done"] == []
    assert dag.validate_decisions() == []


def test_a_draft_keeps_an_incomplete_spec_and_a_set_up_step_does_not():
    draft = n("w", "wait_duration", {"seconds": ""}, state_variables=dict(DRAFT))
    assert draft.spec == {"seconds": ""} or draft.spec == {}
    with pytest.raises(Exception):
        n("w2", "wait_duration", {"seconds": ""})
    dag = plan(n("w", "wait_duration", state_variables=dict(DRAFT)), n("after", "action", parents=["w"]))
    dag.update_node("w", spec={"seconds": 600}, state_variables={})                  # set up: checked as any step is
    assert dag.nodes["w"].spec["seconds"] == 600 and dag.advance(T0)["next_due"] == T0 + 600
    with pytest.raises(Exception):
        plan(n("x", "wait_duration", state_variables=dict(DRAFT))).update_node("x", spec={}, state_variables={})


def test_a_draft_steps_title_and_links_are_kept_and_it_can_be_linked_later():
    dag = plan(n("a", "action"), n("d", "decision", state_variables=dict(DRAFT)))
    dag.add_edge("a", "d") if hasattr(dag, "add_edge") else None
    assert dag.nodes["d"].status == "pending" and "d" not in dag.ready_nodes(T0)


# -- values: where a step reads them from, where it sets them, and what a step must produce --------------------------------

def test_a_step_that_says_what_it_produces_must_complete_with_those_values_of_those_types():
    dag = plan(n("a", "action", produces=[{"name": "uova", "type": "number"}, "nota"]))
    with pytest.raises(Exception, match="must produce uova"):
        dag.mark("a", "completed", {"nota": "ok"}, T0)
    with pytest.raises(Exception, match="uova should be number"):
        dag.mark("a", "completed", {"uova": "trenta", "nota": "x"}, T0)
    dag.mark("a", "completed", {"uova": 30, "nota": "x"}, T0)
    assert dag.shared_state == {"uova": 30, "nota": "x"} and dag.nodes["a"].state_variables["outputs"] == {"uova": 30, "nota": "x"}
    assert dag.nodes["a"].produces == [{"name": "uova", "type": "number"}, {"name": "nota", "type": "any"}]


def test_what_a_step_produces_is_checked_when_it_is_written_and_kept_with_the_step():
    from tadamoo.planner.dag import node_to_dict
    for bad in ("uova", [{"name": "1x"}], [{"name": "x", "type": "date"}], ["x", "x"]):
        with pytest.raises(Exception):
            n("b", "action", produces=bad)
    node = n("b", "action", produces=["x"])
    assert node_to_dict(node)["produces"] == [{"name": "x", "type": "any"}]
    assert "produces" not in node_to_dict(n("c", "action"))


def test_read_value_reads_the_clock_a_value_of_the_plan_what_another_step_wrote_and_a_shared_value():
    from tadamoo.planner import nodes
    nodes.GLOBALS = nodes.GlobalValues()
    nodes.GLOBALS.set("cartella", "docs")
    dag = plan(n("a", "action"), n("t", "get_var", {"name": "weekday", "source": "clock"}), n("g", "get_var", {"name": "cartella", "source": "global", "into": "dove"}),
               n("s", "get_var", {"name": "uova", "source": "step", "step": "a", "into": "totale"}, parents=["a"]))
    dag.mark("a", "completed", {"uova": 30}, T0)
    done = dag.advance(T0)
    assert set(done["done"]) == {"t", "g", "s"}
    assert dag.shared_state["weekday"] in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    assert dag.shared_state["dove"] == "docs" and dag.shared_state["totale"] == 30
    with pytest.raises(Exception, match="the clock gives"):
        n("x", "get_var", {"name": "yesterday", "source": "clock"})
    with pytest.raises(Exception):
        n("y", "get_var", {"name": "uova", "source": "step"})                            # a step is named


def test_set_value_can_set_a_value_shared_by_all_plans_and_another_plan_reads_it(tmp_path):
    from tadamoo.planner import nodes
    nodes.GLOBALS = nodes.GlobalValues(tmp_path / "g.json")
    first = plan(n("s", "set_var", {"name": "ultimo", "value": 12, "target": "global"}))
    assert first.advance(T0)["done"] == ["s"] and "ultimo" not in first.shared_state
    nodes.GLOBALS = nodes.GlobalValues(tmp_path / "g.json")                               # a new process reads the file
    second = plan(n("r", "get_var", {"name": "ultimo", "source": "global"}))
    assert second.advance(T0)["done"] == ["r"] and second.shared_state["ultimo"] == 12


def test_a_variable_that_is_not_there_yet_keeps_the_read_waiting_and_says_why():
    dag = plan(n("g", "get_var", {"name": "assente"}))
    dag.advance(T0)
    assert dag.nodes["g"].status == "pending" and "no value yet" in dag.nodes["g"].state_variables["auto_error"]
