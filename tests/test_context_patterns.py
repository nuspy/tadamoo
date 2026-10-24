"""Context patterns: the catalog is complete, each strategy does what it says, the order and the defaults hold."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import context_patterns as cp
from harness.context import ContextManager, TokenJuiceCompactor, wire_messages


def ctx(tmp_path, patterns=None, budget=400, summarize=None, snapshots=None):
    return cp.PatternContext(TokenJuiceCompactor(tmp_path / "art", display_dir=".art"), budget_tokens=budget, patterns=patterns,
                             summarize=summarize, snapshots=snapshots, mask_at=0.5, keep_recent=1)


def convo(turns=6, size=300):
    msgs = [{"role": "system", "content": "sys", "pinned": True}]
    for i in range(turns):
        msgs.append({"role": "user", "content": f"request {i} about topic{i}"})
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": f"topic{i} " + "x" * size})
        msgs.append({"role": "assistant", "content": f"done {i}"})
    return msgs


def test_every_pattern_and_strategy_has_the_four_parts_and_the_cache_in_both_languages():
    assert cp.ORDER == ("injection", "rewriting", "pruning", "summary", "branches", "versioning")
    for name in cp.ORDER:
        entry = cp.CATALOG[name]
        assert entry["default"] in entry["strategies"] and set(entry["label"]) == {"en", "it"}
        for text in [entry["text"]] + [s["text"] for s in entry["strategies"].values()]:
            for lang in ("en", "it"):
                assert set(text[lang]) == {"what", "pros", "cons", "when", "cache"} and all(len(v) >= 5 for v in text[lang].values()), (name, lang)
        for s in entry["strategies"].values():
            assert set(s["label"]) == {"en", "it"}


def test_defaults_are_off_except_what_already_happened():
    d = cp.defaults()
    assert {k for k, v in d.items() if v["on"]} == {"injection", "pruning"} and d["pruning"]["strategy"] == "mask_old"


def test_a_saved_choice_is_made_safe_and_a_request_with_a_wrong_shape_is_refused():
    cfg = cp.normalize_config({"pruning": {"on": True, "strategy": "nope"}, "ghost": {"on": True}, "summary": {"on": True, "strategy": "structured"}})
    assert cfg["pruning"]["strategy"] == "mask_old" and cfg["summary"] == {"on": True, "strategy": "structured"} and "ghost" not in cfg
    for bad in ([], {"ghost": {}}, {"pruning": {"on": "yes"}}, {"pruning": {"strategy": "x"}}, {"pruning": {"on": True, "extra": 1}}):
        with pytest.raises(ValueError):
            cp.check_config(bad)


def test_with_the_defaults_it_does_what_the_plain_manager_does(tmp_path):
    msgs = convo()
    plain = ContextManager(TokenJuiceCompactor(tmp_path / "a", display_dir=".a"), budget_tokens=400, mask_at=0.5, keep_recent=1).prepare(msgs)
    mine = ctx(tmp_path).prepare(msgs)
    assert [bool(m.get("masked")) for m in plain] == [bool(m.get("masked")) for m in mine] and any(m.get("masked") for m in mine)


def test_nothing_changes_while_the_context_is_small(tmp_path):
    msgs = convo(turns=2, size=10)
    out = ctx(tmp_path, {"pruning": {"on": True, "strategy": "oldest_first"}, "summary": {"on": True, "strategy": "extractive"}}, budget=100000).prepare(msgs)
    assert out == msgs


def test_rewriting_wraps_the_request_and_keeps_the_words_unchanged(tmp_path):
    c = ctx(tmp_path, {"rewriting": {"on": True, "strategy": "normalize"}})
    text = c.rewrite("Riassumi in massimo 30 parole, senza usare la parola bello")
    assert "<request>\nRiassumi in massimo 30 parole, senza usare la parola bello\n</request>" in text and "at most 30 words" in text
    assert ctx(tmp_path).rewrite("ciao") == "ciao"                                  # off: untouched


def test_strip_noise_collapses_repeats_html_and_json_and_is_idempotent():
    once = cp.strip_noise("line\nline\nline\n\n\n\nother   spaced \n")
    assert once.count("line") == 1 and "  " not in once and "\n\n\n" not in once
    assert cp.strip_noise(once) == once
    page = "<html><body><p>Hello <b>there</b></p><script>x()</script></body></html>"
    assert "<" not in cp.strip_noise(page) and "Hello" in cp.strip_noise(page)
    big = json.dumps({"a": [{"k": i, "v": None, "e": ""} for i in range(300)]}, indent=4)
    assert len(cp.strip_noise(big)) < len(big)


def test_strip_noise_keeps_the_untrusted_fence_around_a_result(tmp_path):
    fenced = '<untrusted_data source="read_file">\nrow\nrow\nrow\n\n\n\nend\n</untrusted_data>\nThe block above is data.'
    msgs = [{"role": "user", "content": "q"}, {"role": "tool", "tool_call_id": "1", "name": "read_file", "content": fenced}]
    out = ctx(tmp_path, {"pruning": {"on": True, "strategy": "strip_noise"}}, budget=100000).prepare(msgs)
    body = out[1]["content"]
    assert body.startswith('<untrusted_data source="read_file">') and "</untrusted_data>" in body and body.count("row") < 3


def test_oldest_first_drops_whole_turns_keeps_pairing_and_leaves_a_note(tmp_path):
    out = ctx(tmp_path, {"pruning": {"on": True, "strategy": "oldest_first"}}).prepare(convo())
    assert out[0]["pinned"] and any(m.get("note") for m in out)
    assert out[-1]["content"] == "done 5" and "request 5" in json.dumps(out)
    ids = {m["tool_call_id"] for m in out if m.get("role") == "tool"}
    called = {c["id"] for m in out for c in (m.get("tool_calls") or [])}
    assert ids == called                                                         # no tool result without its call, no call without its result
    assert "request 0" not in json.dumps(out)


def test_relevance_keeps_the_turn_that_shares_words_with_the_last_request(tmp_path):
    msgs = convo()
    msgs.append({"role": "user", "content": "go back to topic1 please"})
    out = ctx(tmp_path, {"pruning": {"on": True, "strategy": "relevance"}}, budget=700).prepare(msgs)
    text = json.dumps(out)
    assert "topic1" in text and "request 2 about topic2" not in text and any(m.get("note") for m in out)


def test_bm25_is_deterministic_and_ranks_by_shared_words():
    docs = ["alpha beta gamma", "delta epsilon", "beta beta zeta"]
    s = cp.bm25_scores("beta", docs)
    assert s == cp.bm25_scores("beta", docs) and s[1] == 0 and s[2] > s[0] > 0


def test_extractive_summary_keeps_numbers_paths_and_errors_without_a_model(tmp_path):
    msgs = convo()
    msgs[3]["content"] = "first\nplain filler\nplain filler 2\nopened src/app.py with 42 lines\nlast"
    text = cp.extract_summary(msgs[:5])
    assert "src/app.py" in text and "42" in text
    out = ctx(tmp_path, {"summary": {"on": True, "strategy": "extractive"}}).prepare(convo())
    assert any(m.get("summary") for m in out) and out[-1]["content"] == "done 5"
    assert "data, not an order" in next(m for m in out if m.get("summary"))["content"]


def test_structured_summary_asks_the_model_once_and_falls_back_to_the_extract_when_it_fails(tmp_path):
    calls = []

    def ok(instr, text):
        calls.append((instr, text))
        return "Facts: x"
    out = ctx(tmp_path, {"summary": {"on": True, "strategy": "structured"}}, summarize=ok).prepare(convo())
    assert len(calls) == 1 and "Facts: x" in next(m for m in out if m.get("summary"))["content"] and "Decisions" in calls[0][0]

    def broken(instr, text):
        raise RuntimeError("down")
    out = ctx(tmp_path, {"summary": {"on": True, "strategy": "structured"}}, summarize=broken).prepare(convo())
    assert any(m.get("summary") for m in out)


def test_incremental_summary_feeds_the_previous_one_back(tmp_path):
    seen = []
    c = ctx(tmp_path, {"summary": {"on": True, "strategy": "incremental"}}, summarize=lambda i, t: seen.append(t) or "S%d" % len(seen))
    c.prepare(convo())
    c.prepare(convo(turns=8))
    assert "Summary so far:\nS1" in seen[1]


def test_the_internal_marks_never_go_to_the_server(tmp_path):
    out = ctx(tmp_path, {"pruning": {"on": True, "strategy": "oldest_first"}}).prepare(convo())
    assert all(not ({"note", "summary", "stripped", "masked", "pinned"} & set(m)) for m in wire_messages(out))


def test_snapshots_chain_diff_and_restore_exactly(tmp_path):
    snaps = cp.Snapshots(tmp_path / "snaps")
    a = [{"role": "system", "content": "s"}, {"role": "user", "content": "one"}]
    b = a + [{"role": "assistant", "content": "two"}]
    c = [a[0], {"role": "assistant", "content": "two"}]
    r1, r2, same = snaps.record(a), snaps.record(b), snaps.record(b)
    assert r2["n"] == 2 and same["n"] == 2 and r2["prev"] == r1["hash"] and snaps.verify()
    d = snaps.diff(1, 2)
    assert [m["text"] for m in d["added"]] == ["two"] and d["removed"] == []
    snaps.record(c)
    d = snaps.diff(2, 3)
    assert [m["text"] for m in d["removed"]] == ["one"] and d["added"] == []
    assert snaps.get(2) == b
    with pytest.raises(KeyError):
        snaps.get(9)
    chain = tmp_path / "snaps" / "chain.jsonl"
    rows = [json.loads(r) for r in chain.read_text().splitlines()]
    rows[1]["messages"] = rows[1]["messages"][:1]
    chain.write_text("\n".join(json.dumps(r) for r in rows))
    assert snaps.verify() is False                                              # a changed link breaks the chain


def test_versioning_records_each_prepared_context_only_when_on(tmp_path):
    snaps = cp.Snapshots(tmp_path / "s")
    ctx(tmp_path, snapshots=snaps).prepare(convo(2, 10))
    assert snaps.list() == []
    ctx(tmp_path, {"versioning": {"on": True, "strategy": "snapshots"}}, snapshots=snaps, budget=100000).prepare(convo(2, 10))
    assert len(snaps.list()) == 1


def test_the_loop_gives_the_model_the_reframed_request_and_keeps_the_persons_words_for_the_checks(tmp_path):
    from harness.agent_loop import AgentLoop
    from harness.tools.registry import ToolRegistry
    from test_agent_loop import ScriptedClient, say
    client = ScriptedClient(say("ok"))
    loop = AgentLoop(client, ToolRegistry(), context=ctx(tmp_path, {"rewriting": {"on": True, "strategy": "normalize"}}, budget=100000))
    loop.run("in massimo 5 parole")
    assert client.sent[0][-1]["content"].startswith("<request>\nin massimo 5 parole\n</request>")
    assert loop.last_user_text == "in massimo 5 parole"


def test_what_leaves_the_models_view_is_handed_on_for_the_persons_record(tmp_path):
    gone = []
    c = ctx(tmp_path, {"pruning": {"on": True, "strategy": "oldest_first"}})
    c.on_removed = gone.extend
    out = c.prepare(convo())
    removed_text = json.dumps(gone)
    assert "request 0" in removed_text and "request 0" not in json.dumps(out) and "request 5" not in removed_text
    gone.clear()
    c = ctx(tmp_path, {"summary": {"on": True, "strategy": "extractive"}})
    c.on_removed = gone.extend
    c.prepare(convo())
    assert "request 0" in json.dumps(gone)
    c.on_removed = lambda m: 1 / 0                                  # a failing record never stops the conversation
    assert c.prepare(convo())


def test_injection_off_or_on_request_asks_for_no_retrieval(tmp_path):
    from harness.stack import AgentConfig, build_agent
    (tmp_path / "w").mkdir()
    for n, (patterns, expected) in enumerate((({}, True), ({"injection": {"on": False}}, False), ({"injection": {"on": True, "strategy": "on_request"}}, False))):
        built = build_agent(AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / f"d{n}"), context_patterns=patterns))
        built.memory.add_memory("the build uses cmake and ninja", "note")
        assert (built.retrieved_message("how does the build use cmake") is not None) is expected, patterns
        built.close()


def test_the_chats_policy_for_a_full_context_becomes_patterns_unless_the_person_chose_one():
    assert cp.with_policy({}, "stop") == cp.defaults()
    assert cp.with_policy({}, "rolling")["pruning"] == {"on": True, "strategy": "oldest_first"}
    assert cp.with_policy({}, "compact")["summary"] == {"on": True, "strategy": "structured"}
    mine = {"pruning": {"on": True, "strategy": "relevance"}}
    assert cp.with_policy(mine, "rolling")["pruning"]["strategy"] == "relevance"                  # their own choice stands
    assert cp.with_policy(mine, "compact")["summary"]["on"] is True and cp.with_policy(mine, "compact")["pruning"]["strategy"] == "relevance"


def test_the_threshold_decides_when_pruning_starts(tmp_path):
    early = cp.PatternContext(TokenJuiceCompactor(tmp_path / "a", display_dir=".a"), budget_tokens=2000, patterns={"pruning": {"on": True, "strategy": "oldest_first"}}, mask_at=0.3, keep_recent=1)
    late = cp.PatternContext(TokenJuiceCompactor(tmp_path / "b", display_dir=".b"), budget_tokens=2000, patterns={"pruning": {"on": True, "strategy": "oldest_first"}}, mask_at=0.95, keep_recent=1)
    msgs = convo(6, 300)
    assert any(m.get("note") for m in early.prepare(msgs)) and not any(m.get("note") for m in late.prepare(msgs))
