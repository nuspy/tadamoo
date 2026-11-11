"""Archive passages and the route of a conversation: what a request outside this PC may carry, what compaction forgets, what the state note says."""
import json

from tadamoo.council import Council
from tadamoo.rag import RagStore, project_key
from tadamoo.rag.recall import ProjectRecall
from tadamoo.rag.tools import knowledge_index, register_knowledge_tools
from tadamoo.rag.withhold import PLACEHOLDER
from tadamoo.stack import AgentConfig, build_agent

PROJ, OTHER = project_key("project-one"), project_key("project-two")
KEEL = "The keel of the private yacht is made of lead and weighs eleven tonnes, bolted through the hull."
SAIL = "A sloop has one mast and two sails, the mainsail and the jib, and sails close to the wind."


def make(tmp_path, *rows):
    rag = RagStore(tmp_path / "rag")
    ids = []
    for name, project, text, cloud in rows:
        aid = rag.create(name, "about " + name, project, "Project " + ("One" if project == PROJ else "Two"))["id"]
        src = tmp_path / (aid + ".txt")
        src.write_text(text, encoding="utf-8")
        rag.add_file(aid, src.name, src)
        rag.update(aid, {"cloud_ok": cloud})
        ids.append(aid)
    return rag, ids


class Where:
    local = True

    def __call__(self):
        return self.local


def test_passages_already_in_the_history_are_withdrawn_when_the_route_leaves_the_pc(tmp_path):
    rag, _ = make(tmp_path, ("Private", PROJ, KEEL, False), ("Open", PROJ, SAIL, True))
    where = Where()
    recall = ProjectRecall(rag, PROJ, local=where)
    block = recall.message("the keel of the private yacht, and the sloop sails")
    history = [{"role": "user", "content": "tell me"}, {"role": "user", "content": block, "untrusted": True}]
    assert recall.withdrawn(history) is history                                         # local: sent as kept
    where.local = False
    sent = recall.withdrawn(history)
    assert KEEL not in sent[1]["content"] and PLACEHOLDER in sent[1]["content"] and "Private" not in sent[1]["content"]
    assert SAIL in sent[1]["content"] and "[Open / " in sent[1]["content"]              # an archive cleared for the cloud stays
    assert sent[0] is history[0] and KEEL in history[1]["content"]                      # what is kept is not changed: the local prefix stays the same
    where.local = True
    assert recall.withdrawn(history)[1]["content"] == block


def tool_registry(rag, recall):
    from tadamoo.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_knowledge_tools(reg, rag, recall)
    return reg


def test_a_search_result_of_a_private_archive_is_withdrawn_too(tmp_path):
    rag, _ = make(tmp_path, ("Private", PROJ, KEEL, False))
    where = Where()
    recall = ProjectRecall(rag, PROJ, local=where)
    reg = tool_registry(rag, recall)
    found = reg.get("knowledge_search").handler("keel of the private yacht lead")
    assert found["passages"] and found["passages"][0]["text"] == KEEL
    result = {"role": "tool", "tool_call_id": "c1", "name": "knowledge_search", "content": json.dumps(found, ensure_ascii=False)}
    where.local = False
    sent = recall.withdrawn([result])[0]
    assert KEEL not in sent["content"] and sent["tool_call_id"] == "c1" and PLACEHOLDER in sent["content"]
    assert reg.get("knowledge_search").handler("keel of the private yacht lead")["searched"] == 0       # and nothing new is searched on that route


def test_a_passage_lost_to_compaction_can_be_sent_again(tmp_path):
    rag, _ = make(tmp_path, ("Boats", PROJ, SAIL, True))
    recall = ProjectRecall(rag, PROJ)
    assert recall.message("the mainsail and the jib of a sloop") is not None
    assert recall.message("the mainsail and the jib of a sloop") is None
    recall.forget_sent()
    assert recall.message("the mainsail and the jib of a sloop") is not None


def test_the_state_note_names_the_archives_and_says_what_the_route_leaves_out(tmp_path):
    rag, (mine, forests) = make(tmp_path, ("Boats", PROJ, SAIL, True), ("Forests", OTHER, KEEL, False))
    where = Where()
    recall = ProjectRecall(rag, PROJ, local=where, attached=[forests])
    index = knowledge_index(rag, recall)
    note = index()
    assert "Boats (1 documents" in note and "Forests (project Project Two; 1 documents" in note and forests not in note and "not available" not in note
    where.local = False
    away = index()
    assert "Forests" in away.split("not available on this route:")[1] and "not cleared for cloud models" in away and away.startswith("Boats")


def test_the_agent_sends_what_the_recall_lets_it_send(tmp_path):
    rag, _ = make(tmp_path, ("Private", PROJ, KEEL, False))
    cfg = dict(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, rag_dir=str(tmp_path / "rag"), rag_project=PROJ,
               endpoint="http://127.0.0.1:9")
    (tmp_path / "w").mkdir()
    built = build_agent(AgentConfig(**cfg))
    try:
        assert built.agent.outgoing == built.project_recall.withdrawn and built.agent.on_compaction == built.project_recall.forget_sent
    finally:
        built.close()


def test_a_council_role_outside_the_pc_reads_no_archive_passage(tmp_path):
    rag, _ = make(tmp_path, ("Private", PROJ, KEEL, False))
    recall = ProjectRecall(rag, PROJ)
    recall.message("the keel of the private yacht lead")

    class Client:
        def __init__(self, local):
            self.local, self.seen = local, []

        def stays_local(self):
            return self.local

        def chat_completion(self, messages, **kw):
            self.seen.append(messages[-1]["content"])
            return {"choices": [{"message": {"content": "ok"}}]}
    clients = {"council:near": Client(True), "council:far": Client(False)}
    council = Council(lambda function: clients[function], redact=recall.private.redact)
    body = f"<question>is the keel heavy?</question>\n<context>\n{KEEL}\nthe harbour is calm\n</context>"
    council._ask("council:near", "s", body, 50)
    council._ask("council:far", "s", body, 50)
    assert KEEL in clients["council:near"].seen[0]
    far = clients["council:far"].seen[0]
    assert KEEL not in far and PLACEHOLDER in far and "the harbour is calm" in far
