"""A plan step is given the knowledge archives it declares: the plan's, its own and those of the RAG nodes connected to it, by name, with the cloud rule."""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import NodeType, PlanDAG
from tadamoo.planner_tools import Confirmer, register_planner_tools
from tadamoo.rag import RagStore, project_key
from tadamoo.rag.recall import ProjectRecall
from tadamoo.tools.registry import ToolRegistry
from test_planner import node
from test_planner_tools import Asker, AgentState, world  # noqa: F401  (fixture)
from test_e2e_mock_server import start_server  # noqa: F401  (fixture)

PROJ = project_key("project-one")


def archives(tmp_path):
    rag = RagStore(tmp_path / "rag")
    ids = {}
    for name, cloud in (("Boats", True), ("Forests", False), ("Docs", True)):
        ids[name] = rag.create(name, "", project_key("elsewhere"), "Elsewhere")["id"]
        rag.update(ids[name], {"cloud_ok": cloud})
    return rag, ids


def server_plan(client, ids):
    dag = PlanDAG(plan_id="x", title="Piano con archivi", target_type="occasional")
    dag.add_node(node("s1", title="Cerca"))
    dag.add_node(node("s2", title="Scrivi"))
    dag.add_node(node("kb", NodeType.RAG, title="Fonti", spec={"archives": [ids["Docs"]]}))
    dag.add_edge("s1", "s2")
    dag.add_edge("kb", "s1")
    dag.update_node("s1", spec={"rag_archives": [ids["Forests"]]})
    pid = f"rag-{uuid.uuid4().hex[:6]}"
    data = dag.to_dict()
    client.create({"plan_id": pid, "title": data["title"], "target_type": "occasional", "nodes": data["nodes"]})
    client.event(pid, {"type": "plan:update_meta", "changes": {"profile": {"rag_archives": [ids["Boats"]]}}})
    return pid


def test_a_step_started_is_told_by_name_which_archives_it_has_and_lets_them_go_when_done(world, tmp_path):  # noqa: F811
    client, agent, build = world
    rag, ids = archives(tmp_path)
    where = {"local": True}
    recall = ProjectRecall(rag, PROJ, local=lambda: where["local"])
    reg = ToolRegistry()
    register_planner_tools(reg, client, Confirmer(agent, Asker()), step_archives=recall.bring)
    pid = server_plan(client, ids)
    agent.last_user_text = "inizia il passo uno, cerca"
    reg.execute("plan_get", {"plan_id": pid})
    out = reg.execute("plan_step_start", {"plan_id": pid, "node_id": "s1", "quote": "inizia il passo uno"})
    assert "Step s1 started" in out and "This step has these knowledge archives: Boats (project Elsewhere), Forests (project Elsewhere), Docs (project Elsewhere)" in out
    assert ids["Boats"] not in out and "Left out" not in out and "knowledge_search" in out
    assert sorted(recall.step) == sorted(ids.values()) and set(recall.ids()) == set(ids.values())     # searched with the project's own archives
    agent.last_user_text = "completalo"
    reg.execute("plan_get", {"plan_id": pid})
    reg.execute("plan_mark", {"plan_id": pid, "node_id": "s1", "status": "completed", "quote": "completalo"})
    assert recall.step == []


def test_on_a_route_outside_the_pc_the_step_gets_only_archives_cleared_for_it_and_is_told_what_was_left_out(tmp_path):
    rag, ids = archives(tmp_path)
    recall = ProjectRecall(rag, PROJ, local=lambda: False)
    said = recall.bring(list(ids.values()))
    assert "Boats (project Elsewhere), Docs (project Elsewhere)" in said and "Left out on this route: Forests (not cleared for cloud models)" in said
    assert recall.permit(recall.ids()) == [ids["Boats"], ids["Docs"]] or set(recall.permit(recall.ids())) == {ids["Boats"], ids["Docs"]}
    assert recall.bring([]) == "" and recall.step == []


def test_a_step_with_no_archive_adds_nothing_and_a_missing_archive_is_not_named(tmp_path):
    rag, ids = archives(tmp_path)
    recall = ProjectRecall(rag, PROJ)
    assert recall.bring([]) == ""
    said = recall.bring([ids["Boats"], "gone-123456"])
    assert "Boats" in said and "gone" not in said and recall.step == [ids["Boats"]]
