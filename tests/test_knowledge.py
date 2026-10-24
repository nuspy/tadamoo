import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_services import FakeWikibridge
from harness.eval import EvalRunner, expand_strategies, generate_eval_report, load_task_set
from harness.integrations.wiki_tools import register_wiki_tools
from harness.integrations.wikibridge_client import WikiBridgeClient
from harness.knowledge import (CAG_MAX_FRACTION, LocalKnowledge, cag_prefix, choose_strategy, estimate_tokens, fingerprint,
                               register_local_search)
from harness.stack import AgentConfig, build_agent
from harness.tools.registry import ToolRegistry
from test_agent_loop import ScriptedClient, call, say

DOCS = {"b.md": "Second document about ports: the gateway uses 9314.", "a.md": "First document about people: Marta Ruzzi is on call."}


def test_the_reference_block_is_deterministic_and_changes_when_a_document_does():
    block = cag_prefix(DOCS)
    assert block == cag_prefix(dict(reversed(list(DOCS.items()))))
    assert block.index("## a.md") < block.index("## b.md")
    assert fingerprint(DOCS) == fingerprint(dict(reversed(list(DOCS.items()))))
    assert fingerprint(DOCS) != fingerprint({**DOCS, "a.md": DOCS["a.md"] + "!"})
    with pytest.raises(ValueError, match="more than the 5 allowed"):
        cag_prefix(DOCS, budget_tokens=5)


def test_the_strategy_follows_size_and_volatility():
    small = {"a.md": "x" * 700}
    assert choose_strategy({}, 10_000) == "none"
    assert choose_strategy(small, 10_000) == "cag"
    assert choose_strategy(small, 10_000, volatile=True) == "rag"
    big = {"a.md": "x" * int(10_000 * CAG_MAX_FRACTION * 3.5 * 1.2)}
    assert estimate_tokens(cag_prefix(big)) > 10_000 * CAG_MAX_FRACTION
    assert choose_strategy(big, 10_000) == "rag"
    assert choose_strategy(big, 10_000, max_fraction=0.9) == "cag"


def test_local_search_finds_passages_by_their_words_and_identifiers():
    kb = LocalKnowledge({**DOCS, "c.md": "The code word is LANTERN.\n\nAnother paragraph about nothing."})
    try:
        assert any("9314" in p for p in kb.search("gateway port"))
        assert any("LANTERN" in p for p in kb.search("code word"))
        assert kb.search("zzzzqqq") == []
        registry = ToolRegistry()
        register_local_search(registry, kb)
        tool = registry.get("kb_search")
        assert tool.untrusted and registry.execute("kb_search", {"query": "nonsenseword"}) == ["No passage matches."]
    finally:
        kb.close()


def test_long_documents_are_split_in_passages_that_keep_their_source():
    kb = LocalKnowledge({"long.md": "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(12))}, chunk_chars=500)
    try:
        hits = kb.search("Paragraph 7")
        assert hits and all(h.startswith("[long.md#") for h in hits)
        assert kb.store.conn.execute("select count(*) from memories").fetchone()[0] > 3
    finally:
        kb.close()


def test_the_agent_gets_the_documents_in_the_prefix_or_a_search_tool(tmp_path):
    def build(strategy):
        return build_agent(AgentConfig(workspace=str(tmp_path), data_dir=str(tmp_path / strategy), reference_docs=DOCS,
                                       reference_strategy=strategy, use_memory=False, use_checkpoints=False),
                           client=ScriptedClient(say("ok")))

    cag = build("cag")
    assert "# Reference Material" in cag.agent.messages[0]["content"] and "Marta Ruzzi" in cag.agent.messages[0]["content"]
    assert "kb_search" not in cag.agent.tools.names()
    rag = build("rag")
    assert "Marta Ruzzi" not in rag.agent.messages[0]["content"] and "kb_search" in rag.agent.tools.names() and rag.kb
    none = build("none")
    assert "Marta Ruzzi" not in none.agent.messages[0]["content"] and "kb_search" not in none.agent.tools.names()
    auto = build("auto")
    assert "# Reference Material" in auto.agent.messages[0]["content"]               # small and stable: a fixed block
    for b in (cag, rag, none, auto):
        b.close()
    with pytest.raises(ValueError, match="strategy"):
        build("sideways")


def test_the_reference_block_sits_after_the_project_rules_and_before_the_skills(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Be precise.", encoding="utf-8")
    b = build_agent(AgentConfig(workspace=str(tmp_path), data_dir=str(tmp_path / "d"), reference_docs=DOCS, reference_strategy="cag",
                                use_memory=False, use_checkpoints=False), client=ScriptedClient(say("ok")))
    text = b.agent.messages[0]["content"]
    assert text.index("# Project Rules") < text.index("# Reference Material")
    b.close()


# -- the comparison -----------------------------------------------------------------------------------------------

def test_the_knowledge_set_asks_every_question_three_ways():
    meta, tasks = load_task_set("knowledge")
    assert len(tasks) == 8 and all(t.knowledge for t in tasks)
    expanded = expand_strategies(tasks, ["none", "rag", "cag"])
    assert len(expanded) == 24
    assert {t.category for t in expanded} == {"knowledge/none", "knowledge/rag", "knowledge/cag"}
    assert {t.id for t in expanded if t.strategy == "cag"} == {f"{t.id}@cag" for t in tasks}
    with pytest.raises(ValueError):
        expand_strategies(tasks, ["telepathy"])
    _, core = load_task_set("core")
    assert expand_strategies(core, ["rag"]) == core                                  # tasks without documents are left alone


ANSWERS = {"port_metrics": "9314", "oncall_week": "Teodor Lund", "freeze_code_word": "LANTERN", "branch_prefix": "fix/",
           "api_limit": "240", "backup_retention": "35", "finance_extension": "4410", "reef_meaning": "A reef is a read-only replica of a database."}


def solver(task, rep):
    base = task.id.split("@")[0]
    if task.strategy == "none":
        return ScriptedClient(say("I do not have that information."))
    if task.strategy == "cag":
        return ScriptedClient(say(ANSWERS[base]))                                   # the answer is already in front of it
    return ScriptedClient(call("kb_search", {"query": task.prompt}), say(ANSWERS[base]))


def test_a_model_that_uses_the_documents_beats_the_control_and_the_report_shows_it():
    _, tasks = load_task_set("knowledge")
    expanded = expand_strategies(tasks, ["none", "rag", "cag"])
    results = EvalRunner(client_factory=solver).run_set(expanded, k=1)
    by = {c: [r for r in results if r.category == c] for c in ("knowledge/none", "knowledge/rag", "knowledge/cag")}
    assert not any(r.pass_pow_k for r in by["knowledge/none"])
    assert all(r.pass_pow_k for r in by["knowledge/rag"]) and all(r.pass_pow_k for r in by["knowledge/cag"])
    assert all(t.tool_calls == 1 for r in by["knowledge/rag"] for t in r.trials)       # it searched
    report = generate_eval_report(results)
    for cat in ("knowledge/none", "knowledge/rag", "knowledge/cag"):
        assert f"| {cat} |" in report


def test_the_rag_tool_really_returns_the_passage_with_the_answer(tmp_path):
    _, tasks = load_task_set("knowledge")
    task = expand_strategies([next(t for t in tasks if t.id == "freeze_code_word")], ["rag"])[0]
    seen = []

    class Spy(ScriptedClient):
        def chat_completion(self, messages, tools=None, **kw):
            seen.append(messages)
            return super().chat_completion(messages, tools, **kw)

    EvalRunner(client_factory=lambda t, r: Spy(call("kb_search", {"query": "freeze hotfix code word"}), say("LANTERN"))).evaluate_task(task, 1)
    tool_msg = [m for m in seen[-1] if m["role"] == "tool"][0]["content"]
    assert "LANTERN" in tool_msg and tool_msg.startswith("<untrusted_data source=\"kb_search\">")


# -- wikibridge tools ----------------------------------------------------------------------------------------------

@pytest.fixture
def wiki():
    fake = FakeWikibridge(key="k")
    yield fake
    fake.close()


def test_wiki_tools_search_read_and_fetch_context_as_untrusted_data(wiki):
    registry = ToolRegistry()
    register_wiki_tools(registry, WikiBridgeClient(wiki.url, api_key="k"), default_project="cervinia")
    assert registry.names() == ["wiki_context", "wiki_page", "wiki_search"]
    assert all(registry.get(n).untrusted for n in registry.names())
    assert registry.execute("wiki_search", {"query": "diana"})[0]["path"] == "entities/diana.md"
    assert registry.execute("wiki_page", {"path": "entities/diana.md"}) == "Allergica agli ovini."
    ctx = registry.execute("wiki_context", {"query": "allergie"})
    assert ctx["blocks"][0]["text"] == "Allergica agli ovini." and ctx["truncated"] is False
    first = ToolRegistry()
    register_wiki_tools(first, WikiBridgeClient(wiki.url, api_key="k"))                  # no default: the first project
    assert first.execute("wiki_search", {"query": "x"})[0]["title"] == "Diana"


def test_wiki_tools_say_what_is_wrong_instead_of_returning_nothing(wiki):
    registry = ToolRegistry()
    register_wiki_tools(registry, WikiBridgeClient(wiki.url, api_key="wrong"), default_project="cervinia")
    with pytest.raises(RuntimeError, match="unauthorized"):
        registry.execute("wiki_search", {"query": "x"})
    with pytest.raises(RuntimeError, match="BASALT_WIKIBRIDGE_KEY"):
        registry.execute("wiki_context", {"query": "x"})
    off = ToolRegistry()
    register_wiki_tools(off, WikiBridgeClient("http://127.0.0.1:59993", timeout=0.2))
    with pytest.raises(RuntimeError, match="offline"):
        off.execute("wiki_search", {"query": "x"})
    ok = ToolRegistry()
    register_wiki_tools(ok, WikiBridgeClient(wiki.url, api_key="k"), default_project="cervinia")
    with pytest.raises(RuntimeError):
        ok.execute("wiki_page", {"path": "entities/missing.md"})
    with pytest.raises(Exception):
        ok.execute("wiki_page", {"path": "../x"})


def test_the_stack_can_offer_the_wiki(wiki, tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_WIKIBRIDGE_URL", wiki.url)
    monkeypatch.setenv("BASALT_WIKIBRIDGE_KEY", "k")
    b = build_agent(AgentConfig(workspace=str(tmp_path), data_dir=str(tmp_path / "d"), wiki=True, wiki_project="cervinia",
                                use_memory=False, use_checkpoints=False), client=ScriptedClient(call("wiki_search", {"query": "diana"}), say("ok")))
    b.agent.run("who is diana")
    assert "<untrusted_data source=\"wiki_search\">" in [m for m in b.agent.messages if m["role"] == "tool"][0]["content"]
    b.close()
