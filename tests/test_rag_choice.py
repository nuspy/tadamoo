"""Choosing knowledge archives for a conversation: matching a name, attach and drop without guessing, the tools a model calls, and the union with the
project's own archives in what a request takes in."""
import pytest

from tadamoo.rag import RagStore, project_key
from tadamoo.rag.choice import decide, known
from tadamoo.rag.match import find, fold
from tadamoo.rag.recall import ProjectRecall
from tadamoo.rag.tools import register_knowledge_tools
from tadamoo.stack import AgentConfig, build_agent
from tadamoo.tools.registry import ToolRegistry

HERE = project_key("here")
THERE = project_key("there")
FOREST = "Selective logging keeps the canopy intact. Forest management plans set the felling cycle and the width of the buffer strips along streams."
FIRE = "A firebreak is a gap in the vegetation. Forest fires spread slowly through a thin firebreak."
CAKE = "Sponge cake needs eggs, sugar and flour, beaten until pale and folded gently."


def rows(*items):
    return [{"id": i, "name": n, "description": d, "project": p, "project_name": pn} for i, n, d, p, pn in items]


CATALOG = rows(("a-1", "Gestione Foreste", "Manuale di selvicoltura", HERE, "Alpha"), ("b-2", "Forest Management", "", THERE, "Beta"),
               ("c-3", "Forest Fires", "", THERE, "Beta"), ("d-4", "Cucina", "Ricette", THERE, "Beta"))


def test_a_name_is_matched_exactly_then_by_a_unique_start_or_part_ignoring_case_and_accents():
    assert fold("  Gestióne   FORESTE! ") == "gestione foreste"
    cases = {"gestione foreste": "a-1", "GESTIONE": "a-1", "foreste": "a-1", "forest m": "b-2", "Forest Management": "b-2", "cuc": "cucina", "ricette": "d-4",
             "usa la knowledge sulla cucina": "d-4", "selvicoltura manuale": "a-1", "a-1": "a-1"}
    for text, want in cases.items():
        got = find(CATALOG, text)
        assert got["status"] == "one" and want in (got["archive"]["id"], got["archive"]["name"].lower()), text


def test_an_ambiguous_name_returns_the_candidates_and_a_missing_one_says_so():
    many = find(CATALOG, "forest")
    assert many["status"] == "many" and [c["id"] for c in many["candidates"]] == ["b-2", "c-3"] and find(CATALOG, "zzz")["status"] == "none"
    assert find(CATALOG, "")["status"] == "none" and find([], "forest")["status"] == "none"
    twins = rows(("x-1", "Notes", "", HERE, "Alpha"), ("x-2", "Notes", "", THERE, "Beta"))
    assert find(twins, "notes")["status"] == "many"                                   # the same name in two projects is not chosen for the person


def test_attach_detach_and_clear_never_guess():
    got = decide(CATALOG, HERE, [], "attach", "forest m")
    assert got["status"] == "attached" and got["ids"] == ["b-2"] and got["archive"] == {"id": "b-2", "name": "Forest Management", "project": "Beta"}
    assert decide(CATALOG, HERE, ["b-2"], "attach", "forest m")["status"] == "already"
    assert decide(CATALOG, HERE, [], "attach", "gestione")["status"] == "this_project"          # the project's own archive is used anyway
    amb = decide(CATALOG, HERE, ["b-2"], "attach", "forest")
    assert amb["status"] == "ambiguous" and amb["ids"] == ["b-2"] and len(amb["candidates"]) == 2
    assert decide(CATALOG, HERE, [], "attach", "nothing like it")["status"] == "none"
    assert decide(CATALOG, HERE, ["b-2", "c-3"], "detach", "management")["ids"] == ["c-3"]
    assert decide(CATALOG, HERE, ["b-2", "c-3"], "detach", "forest")["status"] == "ambiguous"
    assert decide(CATALOG, HERE, ["b-2"], "detach", "cucina")["status"] == "not_attached"
    assert decide(CATALOG, HERE, ["b-2"], "detach", "gestione")["status"] == "this_project"
    assert decide(CATALOG, HERE, ["b-2", "c-3"], "clear") == {"status": "cleared", "ids": [], "dropped": 2}
    with pytest.raises(ValueError):
        decide(CATALOG, HERE, [], "swap", "x")
    assert known(["b-2", "b-2", "gone", 5], CATALOG) == ["b-2"]


def make(tmp_path):
    store = RagStore(tmp_path / "rag")
    ids = {}
    for name, doc, text, project, pname in (("Forest Management", "plans.txt", FOREST, THERE, "Beta"), ("Forest Fires", "fires.txt", FIRE, THERE, "Beta"),
                                            ("Baking", "cake.txt", CAKE, HERE, "Alpha")):
        aid = store.create(name, "", project, pname)["id"]
        src = tmp_path / "src" / doc
        src.parent.mkdir(exist_ok=True)
        src.write_text(text, encoding="utf-8")
        store.add_file(aid, doc, src)
        ids[name] = aid
    return store, ids


def test_the_recall_takes_the_projects_archives_and_the_attached_ones_each_passage_once(tmp_path):
    store, ids = make(tmp_path)
    recall = ProjectRecall(store, HERE)
    assert recall.message("how long is the felling cycle in forest management plans") is None            # not this project's
    recall.attach([ids["Forest Management"], "gone-000000"])
    assert recall.ids() == [ids["Baking"], ids["Forest Management"]]
    first = recall.message("how long is the felling cycle in forest management plans")
    assert "[Forest Management / plans.txt]" in first and "Baking" not in first
    assert recall.message("how long is the felling cycle in forest management plans") is None             # each passage once
    assert "[Baking / cake.txt]" in recall.message("eggs sugar and flour for sponge cake")                 # the project's own still comes
    recall.attach([])
    assert recall.ids() == [ids["Baking"]]


def test_an_attached_archive_follows_the_cloud_rule(tmp_path):
    store, ids = make(tmp_path)
    away = ProjectRecall(store, HERE, local=lambda: False, attached=[ids["Forest Management"]])
    assert away.message("felling cycle of forest management plans") is None
    store.update(ids["Forest Management"], {"cloud_ok": True})
    assert "[Forest Management / plans.txt]" in away.message("felling cycle of forest management plans")


def tools(tmp_path):
    store, ids = make(tmp_path)
    recall = ProjectRecall(store, HERE)
    seen = []
    recall.on_change = lambda attached, kind, name: seen.append((list(attached), kind, name))
    registry = ToolRegistry()
    register_knowledge_tools(registry, store, recall)
    return registry, recall, ids, seen


def test_the_tools_are_described_so_a_model_knows_when_to_call_them(tmp_path):
    registry, *_ = tools(tmp_path)
    assert {"knowledge_list", "knowledge_use", "knowledge_drop", "knowledge_search"} <= set(registry.names())
    use = registry.get("knowledge_use").description
    for word in ("RAG", "usa il RAG", "any language", "several archives match"):
        assert word in use
    assert "non usare più" in registry.get("knowledge_drop").description and "cerca nella knowledge" in registry.get("knowledge_search").description
    assert registry.get("knowledge_search").untrusted is True and registry.get("knowledge_use").untrusted is False


def test_the_model_lists_attaches_drops_and_searches(tmp_path):
    registry, recall, ids, seen = tools(tmp_path)
    listed = registry.execute("knowledge_list", {})["archives"]
    assert {a["name"]: a["state"] for a in listed} == {"Baking": "used (this project)", "Forest Fires": "not attached", "Forest Management": "not attached"}
    assert next(a for a in listed if a["name"] == "Forest Fires")["project"] == "Beta"
    out = registry.execute("knowledge_use", {"name": "forest management"})
    assert out["status"] == "attached" and recall.attached == [ids["Forest Management"]] and seen == [([ids["Forest Management"]], "attached", "Forest Management")]
    assert registry.execute("knowledge_use", {"name": "forest management"})["status"] == "already_attached" and len(seen) == 1
    amb = registry.execute("knowledge_use", {"name": "forest"})
    assert amb["status"] == "ambiguous" and {c["name"] for c in amb["candidates"]} == {"Forest Management", "Forest Fires"} and recall.attached == [ids["Forest Management"]]
    assert registry.execute("knowledge_use", {"name": "baking"})["status"] == "this_project"
    assert registry.execute("knowledge_use", {"name": "astronomy"})["status"] == "none"
    found = registry.execute("knowledge_search", {"query": "felling cycle of the forest management plans"})
    assert found["status"] == "ok" and found["searched"] == 2 and found["passages"][0]["archive"] == "Forest Management"
    direct = registry.execute("knowledge_search", {"query": "firebreak gap", "archive": "fires"})
    assert direct["searched"] == 1 and direct["passages"][0]["document"] == "fires.txt" and recall.attached == [ids["Forest Management"]]        # searched, not attached
    assert registry.execute("knowledge_search", {"query": "firebreak", "archive": "forest"})["status"] == "ambiguous"
    assert registry.execute("knowledge_search", {"query": "quantum chromodynamics"})["passages"] == []
    assert registry.execute("knowledge_drop", {"name": "baking"})["status"] == "this_project"
    assert registry.execute("knowledge_drop", {"name": "fires"})["status"] == "not_attached"
    assert registry.execute("knowledge_drop", {"name": "management"})["status"] == "dropped" and recall.attached == [] and seen[-1][1] == "dropped"
    registry.execute("knowledge_use", {"name": "forest management"})
    assert registry.execute("knowledge_drop", {"all": True}) == {"status": "dropped", "dropped": 1} and seen[-1][1:] == ("cleared", "")


def config(tmp_path, **more):
    (tmp_path / "w").mkdir(exist_ok=True)
    return dict(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, rag_dir=str(tmp_path / "rag"), rag_project=HERE, **more)


def test_the_harness_offers_the_tools_and_says_so_in_its_self_description(tmp_path):
    make(tmp_path)
    built = build_agent(AgentConfig(**config(tmp_path)))
    try:
        assert {"knowledge_list", "knowledge_use", "knowledge_drop", "knowledge_search"} <= set(built.agent.tools.names())
        assert "knowledge_use" in built.builder.blocks["self"]
    finally:
        built.close()
    bare = build_agent(AgentConfig(**{**config(tmp_path), "rag_dir": ""}))
    try:
        assert "knowledge_use" not in bare.agent.tools.names() and "knowledge_use" not in bare.builder.blocks["self"]
    finally:
        bare.close()


def test_a_started_conversation_takes_its_attached_archives_and_the_page_can_change_them(tmp_path):
    store, ids = make(tmp_path)
    built = build_agent(AgentConfig(**config(tmp_path, rag_attached=(ids["Forest Management"],))))
    try:
        assert built.project_recall.attached == [ids["Forest Management"]]
        assert "[Forest Management / plans.txt]" in built.retrieved_message("felling cycle of the forest management plans")["content"]
        note = built.knowledge_switch([ids["Forest Fires"]])
        assert "Now attached: Forest Fires" in note and "No longer attached: Forest Management" in note and note.startswith("[harness]")
        assert built.knowledge_switch([ids["Forest Fires"]]) == ""                                    # nothing changed: nothing to say
        assert "[Forest Fires / fires.txt]" in built.retrieved_message("a firebreak is a gap in the vegetation")["content"]
        assert built.retrieved_message("felling cycle of the forest management plans") is None
        out = built.agent.tools.execute("knowledge_use", {"name": "management"})                     # the model attaches one too
        assert out["status"] == "attached" and built.project_recall.attached == [ids["Forest Fires"], ids["Forest Management"]]
    finally:
        built.close()


def test_the_knowledge_tools_work_in_every_role(tmp_path):
    make(tmp_path)
    for role in ("build", "plan", "ask", "debug", "orchestrate", "general"):
        built = build_agent(AgentConfig(**config(tmp_path), role=role))
        try:
            allowed = set(built.role.allowed())
            assert {"knowledge_list", "knowledge_use", "knowledge_drop", "knowledge_search"} <= allowed, role
        finally:
            built.close()
