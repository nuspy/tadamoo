"""Decisions with many outputs, typed connectors, and the RAG node: branch matching, the helper that builds conditions, the migration of stored decisions,
the rules for edges between connectors, and which archives a step gets."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import NodeType, PlanDAG, PlanError
from tadamoo.planner import nodes as plan_nodes
from tadamoo.planner.archives import feeders, of_step
from tadamoo.planner.dag import NoRoute, edge_parts
from tadamoo.planner.horizon import PlanProfile
from test_planner import node


def branching(mode="first", branches=None, subject="n"):
    """start -> choose (input n) with a branch per range, each connected to its own step."""
    dag = PlanDAG(plan_id="b", title="b", target_type="occasional")
    for nid in ("start", "small", "ten", "big", "mid", "rest"):
        dag.add_node(node(nid))
    dag.add_node(node("choose", NodeType.DECISION, spec={"mode": mode, "input": subject, "branches": branches or [
        {"label": "< 10", "op": "<", "value": 10}, {"label": "= 10", "op": "=", "value": 10}, {"label": "> 10", "op": ">", "value": 10},
        {"label": "10 to 30", "op": "between", "value": 10, "value2": 30}, {"label": "else", "op": "else"}]}))
    dag.add_edge("start", "choose")
    ids = [b["id"] for b in dag.nodes["choose"].spec["branches"]]
    for port, child in zip(ids, ("small", "ten", "big", "mid", "rest")):
        dag.add_edge("choose", child, port)
    return dag


def taken(dag, n):
    dag.mark("start", "completed")
    dag.mark("choose", "completed", {"n": n})
    return [nid for nid in ("small", "ten", "big", "mid", "rest") if dag.nodes[nid].status != "skipped"]


# -- branches ---------------------------------------------------------------------------------------------------

def test_in_first_mode_the_first_branch_that_holds_wins_even_when_others_overlap():
    assert taken(branching(), 5) == ["small"]
    assert taken(branching(), 10) == ["ten"]                                    # "= 10" comes before "10 to 30"
    assert taken(branching(), 15) == ["big"]                                    # "> 10" and "10 to 30" both hold: the order decides
    flipped = branching(branches=[{"label": "range", "op": "between", "value": 10, "value2": 30}, {"label": "> 10", "op": ">", "value": 10}])
    taken(flipped, 15)
    assert flipped.nodes["choose"].state_variables["selected"] == ["small"]                 # reversed order, reversed winner


def test_in_all_mode_every_branch_that_holds_fires_and_the_rest_are_skipped():
    assert taken(branching("all"), 15) == ["big", "mid"]
    assert taken(branching("all"), 10) == ["ten", "mid"]
    assert taken(branching("all"), 50) == ["big"]


def test_the_else_branch_is_taken_when_nothing_else_holds_and_only_then():
    dag = branching(branches=[{"label": "high", "op": ">", "value": 100}, {"label": "any other", "op": "else"}])
    taken(dag, 5)
    assert dag.nodes["choose"].state_variables["selected_branches"] == ["b2"] and dag.nodes["choose"].state_variables["selected"] == ["ten"]
    dag = branching(branches=[{"label": "high", "op": ">", "value": 100}, {"label": "any other", "op": "else"}])
    dag.mark("start", "completed")
    dag.mark("choose", "completed", {"n": 500})
    assert dag.nodes["choose"].state_variables["selected"] == ["small"]


def test_with_no_branch_that_holds_and_no_else_that_path_stops_and_the_rest_goes_on():
    dag = branching(branches=[{"label": "high", "op": ">", "value": 100}])
    dag.add_node(node("other"))
    dag.add_edge("start", "other")
    dag.mark("start", "completed")
    assert dag.mark("choose", "completed", {"n": 5}) is None
    choose = dag.nodes["choose"]
    assert choose.status == "completed" and choose.state_variables["selected"] == [] and "no_route" not in choose.state_variables
    assert [(r["title"], r["holds"]) for r in choose.state_variables["no_branch"]["exits"]] == [("high", False)]
    assert dag.nodes["small"].status == "skipped" and dag.nodes["other"].status == "pending"        # only the path below the decision stopped
    dag.rewind("choose")
    assert "no_branch" not in choose.state_variables


def test_with_on_no_match_ask_the_decision_goes_to_the_person_and_says_so():
    dag = branching(branches=[{"label": "high", "op": ">", "value": 100}])
    dag.update_node("choose", spec={**dag.nodes["choose"].spec, "on_no_match": "ask"})
    dag.mark("start", "completed")
    res = dag.mark("choose", "completed", {"n": 5})
    assert "no_route" in res and "no branch of 'choose' can be taken" in res["message"]
    assert [(r["title"], r["holds"]) for r in res["no_route"]["exits"]] == [("high", False)]
    assert dag.nodes["choose"].status == "pending" and dag.nodes["choose"].assignee.value == "user" and dag.nodes["small"].status == "blocked"
    with pytest.raises(PlanError, match="stop or ask"):
        plan_nodes.clean_spec("decision", {"on_no_match": "hang", "branches": [{"label": "a", "condition": "x"}]})


def test_a_value_that_is_not_there_yet_stops_the_decision_instead_of_taking_else():
    dag = branching()
    dag.mark("start", "completed")
    res = dag.mark("choose", "completed")
    assert res["no_route"]["unset"] == ["n"] and dag.nodes["rest"].status == "blocked"


def test_a_later_branch_that_cannot_be_read_does_not_matter_when_an_earlier_one_won_in_first_mode():
    dag = branching(branches=[{"label": "yes", "condition": "n > 1"}, {"label": "maybe", "condition": "missing > 1"}])
    taken(dag, 5)
    assert dag.nodes["choose"].state_variables["selected_branches"] == ["b1"]


@pytest.mark.parametrize("op, value, value2", [("<", 10, None), ("<=", "10", None), ("=", 10, None), ("!=", "red", None), (">=", 2.5, None), (">", 10, None),
                                               ("between", 10, 30), ("in", "a, b, 3", None), ("contains", "x", None)])
def test_the_helper_form_of_a_branch_becomes_the_three_terms(op, value, value2):
    spec = plan_nodes.clean_spec("decision", {"input": "n", "branches": [{"label": "L", "op": op, "value": value, "value2": value2}]})
    b = spec["branches"][0]
    assert b["left"] == {"kind": "python", "expr": "n"} and b["op"] == {"kind": "value", "value": op} and b["right"] == {"kind": "value", "value": value}
    assert ("right2" in b) == (op == "between") and spec["mode"] == "first" and spec["on_no_match"] == "stop"


def test_a_branch_is_checked():
    bad = [
        ([], "at least one branch"),
        ([{"label": "a", "condition": "x > 1"}, {"label": "A", "condition": "x < 1"}], "labels are unique"),
        ([{"label": "", "condition": "x > 1"}], "label"),
        ([{"label": "a", "op": "else"}, {"label": "b", "condition": "x"}], "last"),
        ([{"label": "a", "op": ">", "value": 1}], "left"),
        ([{"label": "a", "condition": "open('f')"}], "may not use"),
        ([{"label": "a", "op": "~", "value": 1}], "left"),
        ([{"label": "a", "id": "x", "condition": "y"}, {"label": "b", "id": "x", "condition": "z"}], "not used twice"),
    ]
    for branches, word in bad:
        with pytest.raises(PlanError, match=word):
            plan_nodes.clean_spec("decision", {"branches": branches})


def test_ids_stay_when_branches_are_reordered_and_new_ones_get_fresh_ids():
    dag = branching()
    first = dag.nodes["choose"].spec["branches"]
    reordered = [first[2], first[0], *first[1:2], first[3], first[4], {"label": "new", "condition": "n > 99"}]
    dag.update_node("choose", spec={"mode": "first", "input": "n", "branches": [dict(b) for b in reordered[:4]] + [{"label": "new", "condition": "n > 99"}, dict(first[4])]})
    got = dag.nodes["choose"].spec["branches"]
    assert [b["id"] for b in got][:2] == [first[2]["id"], first[0]["id"]] and got[4]["id"] not in {b["id"] for b in first} and got[-1]["op"] == "else"
    assert dag.nodes["choose"].routes[first[0]["id"]] == ["small"]


# -- connectors and edges -------------------------------------------------------------------------------------------

def test_an_edge_from_a_decision_must_name_its_branch_and_the_branch_must_exist():
    dag = branching()
    dag.add_node(node("extra"))
    with pytest.raises(PlanError, match="several output connectors"):
        dag.add_edge("choose", "extra")
    with pytest.raises(PlanError, match="no output connector 'nope'"):
        dag.add_edge("choose", "extra", "nope")
    dag.add_edge("choose", "extra", "b1")
    assert dag.nodes["choose"].routes["b1"] == ["small", "extra"] and "extra" in dag.nodes["choose"].outputs
    dag.remove_edge("choose", "extra", "b1")
    assert "extra" not in dag.nodes["choose"].outputs and dag.nodes["extra"].dependencies == []


def test_a_step_reached_from_two_branches_stays_a_dependency_until_both_edges_go():
    dag = branching()
    dag.add_edge("choose", "small", "b2")
    dag.remove_edge("choose", "small", "b1")
    assert "choose" in dag.nodes["small"].dependencies
    dag.remove_edge("choose", "small", "b2")
    assert "choose" not in dag.nodes["small"].dependencies


def test_edges_are_listed_with_their_ports_and_kinds():
    dag = branching()
    edges = dag.edges()
    assert {"from": {"node": "start", "port": "out"}, "to": {"node": "choose", "port": "in"}, "kind": "flow"} in edges
    assert {"from": {"node": "choose", "port": "b3"}, "to": {"node": "big", "port": "in"}, "kind": "flow"} in edges
    assert edge_parts(["a", "b"]) == ("a", "b", None) and edge_parts(["a", "b", "p"]) == ("a", "b", "p") and edge_parts({"from": {"node": "a", "port": "p"}, "to": {"node": "b"}}) == ("a", "b", "p")
    with pytest.raises(PlanError):
        edge_parts("a>b")


def test_the_table_describes_every_connector():
    table = {row["type"]: row["ports"] for row in plan_nodes.catalogue()}
    assert [p["id"] for p in table["action"]["in"]] == ["in", "data", "rag"] and [p["id"] for p in table["action"]["out"]] == ["out", "result"]
    assert table["action"]["in"][2] == {"id": "rag", "kind": "data", "accepts": ["archives"], "label": "archives"}
    assert table["rag"] == {"in": [], "out": [{"id": "rag", "kind": "data", "carries": "archives", "label": "rag"}]} and table["decision"]["out"] == "branches"
    assert [p["id"] for p in table["sensor"]["out"]] == ["out", "reading"] and [p["id"] for p in table["get_var"]["out"]] == ["out", "value"]
    assert [p["id"] for p in table["set_var"]["out"]] == ["out", "value"] and [p["id"] for p in table["input"]["out"]] == ["out", "answer"]
    assert plan_nodes.out_ports("decision", {"branches": [{"id": "a", "label": "yes"}]}) == [{"id": "a", "kind": "flow", "label": "yes"}]


# -- stored decisions ---------------------------------------------------------------------------------------------

def legacy_plan():
    base = PlanDAG(plan_id="old", title="old", target_type="occasional")
    for nid, title in (("start", "Start"), ("terrazza", "Terrace"), ("tavolo", "Table")):
        base.add_node(node(nid, title=title))
    data = base.to_dict()
    data["nodes"]["scelta"] = {"id": "scelta", "type": "decision", "title": "Where", "description": "", "assignee": "agent", "dependencies": ["start"], "outputs": ["terrazza", "tavolo"],
                               "conditions": {"terrazza": "n >= 2", "tavolo": "n < 2"}, "state_variables": {}, "status": "pending"}
    data["nodes"]["start"]["outputs"] = ["scelta"]
    for child in ("terrazza", "tavolo"):
        data["nodes"][child]["dependencies"] = ["scelta"]
    return data


def test_a_decision_stored_with_a_condition_for_each_exit_loads_as_branches():
    dag = PlanDAG.from_dict(legacy_plan())
    choose = dag.nodes["scelta"]
    assert choose.spec["mode"] == "all" and choose.conditions == {}
    assert [(b["id"], b["label"], b["condition"]) for b in choose.spec["branches"]] == [("terrazza", "Terrace", "n >= 2"), ("tavolo", "Table", "n < 2")]
    assert choose.routes == {"terrazza": ["terrazza"], "tavolo": ["tavolo"]}
    dag.mark("start", "completed")
    dag.mark("scelta", "completed", {"n": 3})
    assert dag.nodes["tavolo"].status == "skipped" and dag.nodes["terrazza"].status == "pending"
    again = PlanDAG.from_dict(json.loads(json.dumps(dag.to_dict())))
    assert again.to_dict() == dag.to_dict()                                     # stored in the new form, it loads as it is


def test_a_stored_decision_with_nothing_to_make_branches_from_is_left_to_be_set_up():
    data = legacy_plan()
    data["nodes"]["scelta"]["conditions"] = {}
    dag = PlanDAG.from_dict(data)
    assert dag.nodes["scelta"].state_variables.get("draft") is True and dag.nodes["scelta"].spec == {}


# -- the RAG node ---------------------------------------------------------------------------------------------------

def rag_plan():
    dag = PlanDAG(plan_id="r", title="r", target_type="occasional")
    for nid in ("a", "b", "c"):
        dag.add_node(node(nid))
    dag.add_edge("a", "b")
    dag.add_node(node("kb", NodeType.RAG, spec={"archives": ["boats-111111", "forests-222222"]}))
    return dag


def test_a_rag_node_feeds_actions_without_ordering_anything():
    dag = rag_plan()
    dag.add_edge("kb", "b")
    dag.add_edge("kb", "c")
    kb = dag.nodes["kb"]
    assert kb.routes == {"rag": ["b", "c"]} and kb.outputs == [] and dag.nodes["b"].dependencies == ["a"] and dag.nodes["c"].dependencies == []
    assert dag.edges()[-2:] == [{"from": {"node": "kb", "port": "rag"}, "to": {"node": "b", "port": "rag"}, "kind": "data", "carries": "archives"},
                                {"from": {"node": "kb", "port": "rag"}, "to": {"node": "c", "port": "rag"}, "kind": "data", "carries": "archives"}]
    assert dag.ready_nodes() == ["a", "c"]                                         # c is not held by the RAG node
    assert dag.advance(0)["done"] == ["kb"] and kb.status == "completed"           # it resolves itself
    assert dag.ready_nodes() == ["a", "c"] and dag.layers()[0].count("kb") == 1
    dag.ensure_start()
    assert PlanDAG.from_dict(json.loads(json.dumps(dag.to_dict()))).to_dict() == dag.to_dict()
    dag.remove_edge("kb", "c")
    assert dag.nodes["kb"].routes == {"rag": ["b"]}


def test_a_rag_node_only_feeds_actions_and_nothing_feeds_it():
    dag = rag_plan()
    dag.add_node(node("w", NodeType.LOG, spec={"message": "x"}))
    dag.add_node(node("s", NodeType.SENSOR))
    for target in ("w", "s"):
        with pytest.raises(PlanError, match="cannot take archives"):
            dag.add_edge("kb", target)
    with pytest.raises(PlanError, match="no output connector"):
        dag.add_edge("b", "kb", "rag")
    with pytest.raises(PlanError):
        dag.add_edge("b", "kb")
    assert dag.nodes["kb"].routes == {} and dag.nodes["b"].outputs == []


def test_a_rag_node_needs_its_archives_and_checks_them():
    for spec in ({}, {"archives": []}, {"archives": ["Bad Id"]}, {"archives": "boats-1"}, {"archives": ["a-1"] * 2, "feeds": []}):
        with pytest.raises(PlanError):
            plan_nodes.clean_spec("rag", spec)
    assert plan_nodes.clean_spec("rag", {"archives": ["a-1", "a-1", "b-2"]}) == {"archives": ["a-1", "b-2"]}
    assert plan_nodes.clean_spec("action", {"rag_archives": ["a-1"]}) == {"rag_archives": ["a-1"]} and plan_nodes.clean_spec("action", {}) == {}


def test_removing_a_step_or_the_rag_node_cleans_the_input_edges():
    dag = rag_plan()
    dag.add_edge("kb", "b")
    dag.remove_node("b")
    assert dag.nodes["kb"].routes == {}
    dag.add_edge("kb", "c")
    dag.remove_node("kb")
    assert "kb" not in dag.nodes and dag.validate_acyclic()


def test_a_diff_can_add_a_rag_node_and_connect_it_in_the_same_change():
    dag = rag_plan()
    diff = {"add_nodes": [{"id": "kb2", "type": "rag", "title": "Docs", "description": "", "assignee": "agent", "spec": {"archives": ["docs-333333"]}}],
            "add_edges": [{"from": {"node": "kb2", "port": "rag"}, "to": {"node": "c"}}]}
    assert set(dag.apply_diff(diff)) >= {"kb2", "c"} and dag.nodes["kb2"].routes == {"rag": ["c"]}
    with pytest.raises(PlanError):
        dag.apply_diff({"add_edges": [["kb2", "a", "rag"], ["kb2", "w-none"]]})
    assert dag.nodes["kb2"].routes == {"rag": ["c"]}                               # all or nothing


def test_the_archives_of_a_step_are_the_plans_its_own_and_those_of_the_rag_nodes_connected_to_it():
    dag = rag_plan()
    dag.add_node(node("kb2", NodeType.RAG, spec={"archives": ["docs-333333", "boats-111111"]}))
    dag.add_edge("kb", "b")
    dag.add_edge("kb2", "b")
    dag.update_node("b", spec={"rag_archives": ["own-444444"]})
    plan = dag.to_dict()
    profile = PlanProfile.from_dict({"rag_archives": ["whole-555555"]}).to_dict()
    assert of_step(plan, "b", profile) == ["whole-555555", "own-444444", "boats-111111", "forests-222222", "docs-333333"]
    assert of_step(plan, "c", profile) == ["whole-555555"] and of_step(plan, "kb", profile) == [] and of_step(plan, "nope") == []
    assert feeders(plan, "b") == ["kb", "kb2"] and feeders(plan, "c") == []
    with pytest.raises(ValueError):
        PlanProfile.from_dict({"rag_archives": ["Bad Id"]})


# -- proposals that carry edges with a connector ---------------------------------------------------------------------

def test_a_proposal_with_connector_edges_can_be_shown_classified_and_split():
    from tadamoo.planner.bridge import classify_diff
    from tadamoo.planner.involvement import split_diff
    from tadamoo.planner_tools import describe_diff
    diff = {"add_edges": [["choose", "small", "b1"], {"from": {"node": "kb", "port": "rag"}, "to": {"node": "mid"}}, ["start", "big"]], "remove_edges": [["choose", "ten", "b2"], "nonsense"]}
    assert classify_diff(diff)["updated"] == ["choose", "small", "kb", "mid", "start", "big", "ten"]       # one bad edge does not break the others
    dag = branching()
    nodes = dag.to_dict()["nodes"]
    split = split_diff(nodes, diff)
    assert split.safe["add_edges"] == diff["add_edges"] and not split.parked
    text = "; ".join(describe_diff(diff, nodes))
    assert 'links "Choose" (b1) -> "Small"' in text and 'links "kb" (rag) -> "Mid"' in text and 'unlinks "Choose" (b2) -> "Ten"' in text


def test_a_rag_node_stays_editable_after_it_has_resolved_itself():
    dag = rag_plan()
    dag.advance(0)
    assert dag.nodes["kb"].status == "completed"
    dag.update_node("kb", spec={"archives": ["docs-333333"]})
    assert dag.nodes["kb"].spec == {"archives": ["docs-333333"]}
    with pytest.raises(PlanError, match="already completed"):
        dag.update_node("a", title="x") if dag.mark("a", "completed") is None else None
