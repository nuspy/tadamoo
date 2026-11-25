"""A plan step is given the skills it declares: the whole plan's (to every step, once for the conversation) and its own (to that step only)."""
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import PlanDAG
from tadamoo.planner import skills as plan_skills
from tadamoo.planner.dag import PlanError
from tadamoo.planner.horizon import PlanProfile
from tadamoo.planner_tools import Confirmer, register_planner_tools
from tadamoo.tools.registry import ToolRegistry
from test_planner import node
from test_planner_tools import Asker, AgentState, world  # noqa: F401  (fixture)
from test_e2e_mock_server import start_server  # noqa: F401  (fixture)

TEXTS = {"alpha": "ALPHA RULES: write short.", "beta": "BETA RULES: cite sources."}


def read(name):
    if name not in TEXTS:
        raise KeyError(name)
    return TEXTS[name]


def server_plan(client, whole, own):
    dag = PlanDAG(plan_id="x", title="Piano con skill", target_type="occasional")
    dag.add_node(node("s1", title="Cerca"))
    dag.add_node(node("s2", title="Scrivi"))
    dag.add_edge("s1", "s2")
    dag.update_node("s1", spec={"skills": own})
    pid = f"sk-{uuid.uuid4().hex[:6]}"
    data = dag.to_dict()
    client.create({"plan_id": pid, "title": data["title"], "target_type": "occasional", "nodes": data["nodes"]})
    client.event(pid, {"type": "plan:update_meta", "changes": {"profile": {"skills": whole}}})
    return pid


def test_a_step_has_the_plans_skills_and_its_own_and_a_skill_in_both_is_the_plans():
    plan = {"nodes": {"a": {"type": "action", "spec": {"skills": ["beta", "alpha", "beta"]}}, "b": {"type": "action", "spec": {}}, "w": {"type": "wait_duration", "spec": {"skills": ["beta"]}}}}
    profile = {"skills": ["alpha", "alpha"]}
    assert plan_skills.of_step(plan, "a", profile) == (["alpha"], ["beta"])
    assert plan_skills.of_step(plan, "b", profile) == (["alpha"], [])
    assert plan_skills.of_step(plan, "w", profile) == ([], []) and plan_skills.of_step(plan, "gone", profile) == ([], [])
    assert plan_skills.of_step(plan, "a", None) == ([], ["beta", "alpha"])


def test_the_skills_of_a_step_and_of_a_plan_are_names_checked_when_saved():
    dag = PlanDAG(plan_id="x", title="t", target_type="occasional")
    dag.add_node(node("s1", title="Uno"))
    dag.update_node("s1", spec={"skills": ["alpha", "alpha", "document-editing"]})
    assert dag.to_dict()["nodes"]["s1"]["spec"]["skills"] == ["alpha", "document-editing"]
    for bad in ("alpha", ["a b"], [3], [f"s{i}" for i in range(21)]):
        with pytest.raises(PlanError):
            dag.update_node("s1", spec={"skills": bad})
    assert PlanProfile.from_dict({"skills": ["alpha", "beta", "alpha"]}).to_dict()["skills"] == ["alpha", "beta"]
    assert PlanProfile.from_dict({}).to_dict()["skills"] == []
    with pytest.raises(ValueError):
        PlanProfile.from_dict({"skills": "alpha"})


def test_a_step_started_gets_the_instructions_of_its_skills_and_the_plans_come_once_per_conversation(world):  # noqa: F811
    client, agent, build = world
    reg = ToolRegistry()
    register_planner_tools(reg, client, Confirmer(agent, Asker()), skill_text=read)
    pid = server_plan(client, ["alpha"], ["beta", "gone"])
    agent.last_user_text = "inizia il passo uno, cerca"
    reg.execute("plan_get", {"plan_id": pid})
    out = reg.execute("plan_step_start", {"plan_id": pid, "node_id": "s1", "quote": "inizia il passo uno"})
    assert "Step s1 started" in out
    assert "Skills of the whole plan (apply them to every step of it):\n### alpha\nALPHA RULES" in out
    assert "Skills of this step (they apply to this step only):\n### beta\nBETA RULES" in out
    assert "### gone\nThis skill is not available here" in out
    agent.last_user_text = "completalo"
    reg.execute("plan_get", {"plan_id": pid})
    reg.execute("plan_mark", {"plan_id": pid, "node_id": "s1", "status": "completed", "quote": "completalo"})
    agent.last_user_text = "inizia il passo due"
    reg.execute("plan_get", {"plan_id": pid})
    second = reg.execute("plan_step_start", {"plan_id": pid, "node_id": "s2", "quote": "inizia il passo due"})
    assert "Step s2 started" in second and "ALPHA RULES" not in second and "BETA RULES" not in second and "Skills of" not in second     # the plan's were given already; beta was s1's only


def test_without_a_way_to_read_skills_a_step_starts_as_before(world):  # noqa: F811
    client, agent, build = world
    reg = ToolRegistry()
    register_planner_tools(reg, client, Confirmer(agent, Asker()))
    pid = server_plan(client, ["alpha"], ["beta"])
    agent.last_user_text = "inizia il passo uno, cerca"
    reg.execute("plan_get", {"plan_id": pid})
    out = reg.execute("plan_step_start", {"plan_id": pid, "node_id": "s1", "quote": "inizia il passo uno"})
    assert "Step s1 started" in out and "Skills of" not in out
