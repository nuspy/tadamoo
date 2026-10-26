"""The list of a plan's steps: the order the person gave, kept valid when links change or steps are removed."""
import pytest

from harness.planner.dag import Assignee, NodeType, PlanDAG, PlanError, PlanNode


def action(name, **kw):
    return PlanNode(id=name, type=NodeType.ACTION, title=name.upper(), description="", assignee=Assignee.AGENT, **kw)


def plan(*names):
    dag = PlanDAG(plan_id="p", title="P", target_type="occasional")
    for n in names:
        dag.add_node(action(n))
    return dag


def test_without_an_order_the_list_follows_the_layers():
    dag = plan("a", "b", "c")
    dag.add_edge("a", "c")
    assert dag.list_order() == [n for layer in dag.layers() for n in layer]


def test_a_stored_order_that_breaks_a_link_is_repaired_and_a_removed_step_is_dropped():
    dag = plan("a", "b", "c")
    dag.add_edge("c", "a")
    dag.order = ["a", "b", "ghost", "c"]
    assert dag.list_order() == ["b", "c", "a"] or dag.list_order() == ["c", "a", "b"]
    assert dag.list_order().index("c") < dag.list_order().index("a")
    assert "ghost" not in dag.to_dict()["order"]
    dag.remove_node("b")
    assert sorted(dag.list_order()) == ["a", "c"]


def test_the_order_survives_saving():
    dag = plan("a", "b")
    dag.move_node("b", -1)
    again = PlanDAG.from_dict(dag.to_dict())
    assert again.list_order() == ["b", "a"]


def test_moving_past_the_ends_and_with_a_wrong_direction_is_refused():
    dag = plan("a", "b")
    with pytest.raises(PlanError):
        dag.move_node("a", -1)
    with pytest.raises(PlanError):
        dag.move_node("a", 0)
    with pytest.raises(KeyError):
        dag.move_node("nope", 1)


def test_two_steps_of_a_chain_swap_with_their_neighbours_relinked():
    dag = plan("a", "b", "c", "d")
    for p, c in (("a", "b"), ("b", "c"), ("c", "d")):
        dag.add_edge(p, c)
    dag.move_node("c", -1)                                    # a -> c -> b -> d
    assert dag.list_order() == ["a", "c", "b", "d"]
    assert dag.nodes["a"].outputs == ["c"] and dag.nodes["c"].outputs == ["b"] and dag.nodes["b"].outputs == ["d"]
    assert dag.nodes["d"].dependencies == ["b"]


def test_a_swap_that_cannot_be_drawn_leaves_the_plan_as_it_was():
    dag = plan("a", "b", "c")
    dag.add_edge("a", "b")
    dag.add_edge("a", "c")                                    # a feeds two steps: not a plain chain
    before = dag.to_dict()
    with pytest.raises(PlanError):
        dag.move_node("b", -1)
    after = dag.to_dict()
    assert {k: v for k, v in after.items() if k != "updated_at"} == {k: v for k, v in before.items() if k != "updated_at"}
