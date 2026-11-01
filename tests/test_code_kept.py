"""Code in an agent session stays whole: the detector, masking that skips it, compaction that keeps it after the note, and the safety valve."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import codeblocks as cb
from harness import context_patterns as cp
from harness.agent_loop import AgentLoop
from harness.compaction import KEPT_CODE_HEAD, Compaction
from harness.context import ContextManager, TokenJuiceCompactor, code_indices, wire_messages
from harness.tools import create_standard_tool_registry
from test_compaction import paired

PY = "\n".join(["import os", "", "def load(path):", "    out = []", "    for line in open(path):", "        out.append(line.strip())", "    return out", "",
                "class Box:", "    def __init__(self):", "        self.items = []", "", "    def add(self, x):", "        self.items.append(x)", "        return x"])
PROSE = "The meeting is on Monday and we will talk about the budget, the plans for the summer and who will bring the cake for everyone."
FENCED = "Here is my function:\n```python\n" + PY + "\n```\nWhy does it fail?"


def call(i, name, **args):
    return {"role": "assistant", "content": f"step {i}", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def result(i, name, text):
    return {"role": "tool", "tool_call_id": f"c{i}", "name": name, "content": text}


def read(i, path, text):
    return [call(i, "read_file", path=path), result(i, "read_file", text)]


def start(goal="fix the loader"):
    return [{"role": "system", "content": "sys", "pinned": True}, {"role": "user", "content": goal}]


# -- the detector ------------------------------------------------------------------------------------------------------


def test_a_fenced_block_is_code_but_a_command_or_a_name_is_not():
    assert cb.fences(FENCED) and cb.fences(FENCED)[0][2] == "python" and "def load" in cb.fences(FENCED)[0][3]
    assert not cb.fences("Run `ls` or:\n```\nls -la\n```")
    unclosed = cb.fences("Look:\n```js\n" + "const a = 1;\n" * 10)
    assert unclosed and unclosed[0][2] == "js"                               # a block that was never closed runs to the end


def test_source_is_told_from_prose_and_logs_by_its_lines():
    assert cb.looks_like_source(PY)
    assert not cb.looks_like_source(PROSE * 3) and not cb.looks_like_source("\n".join(["2026-11-01 10:00:00 INFO started worker number %d" % i for i in range(20)]))
    assert cb.looks_like_source("#include <stdio.h>\nint main(void) {\n  printf(\"hi\");\n  return 0;\n}\n")
    assert not cb.looks_like_source("a\nb\nc")                               # too short to judge


def test_the_extension_of_the_path_decides_for_files_read_and_searched():
    assert cb.has_code_extension("pkg\\mod_001.py") and cb.has_code_extension("src/App.tsx") and cb.has_code_extension("Makefile")
    assert not cb.has_code_extension("LEDGER.md") and not cb.has_code_extension("data.json") and not cb.has_code_extension("notes")
    assert cb.is_code_result("read_file", {"path": "a.py"}, "x = 1\ny = 2\n" * 3)
    assert not cb.is_code_result("read_file", {"path": "notes.md"}, PROSE)
    assert cb.is_code_result("search_code", {"pattern": "x"}, "pkg/a.py:3: def load(path):\npkg/a.py:9: load(1)")
    assert not cb.is_code_result("search_code", {"pattern": "x"}, "docs/a.md:3: some words here\n")
    assert not cb.is_code_result("search_code", {"pattern": "x"}, "No matches.")
    assert cb.is_code_result("shell_command", {"command": "cat a.py"}, PY) and not cb.is_code_result("shell_command", {"command": "ls"}, PROSE)


def test_what_the_model_writes_is_code_when_the_file_is():
    def w(name, **args):
        return {"function": {"name": name, "arguments": json.dumps(args)}}
    assert cb.written_code(w("write_file", path="a.py", content="x = 1"))
    assert cb.written_code(w("edit_file", path="a.py", target="x", replacement="y"))
    assert not cb.written_code(w("edit_file", path="LEDGER.md", replacement="mod_000: 6 functions"))
    assert cb.written_code(w("write_file", path="notes.txt", content=PY))
    assert not cb.written_code(w("read_file", path="a.py"))


def test_each_kind_of_message_is_judged_with_the_call_it_answers():
    c0 = call(0, "read_file", path="a.py")
    calls = {"c0": c0["tool_calls"][0], "c1": call(1, "read_file", path="a.md")["tool_calls"][0]}
    assert cb.is_code_message(result(0, "read_file", PY), calls) and not cb.is_code_message(result(1, "read_file", PROSE), calls)
    assert cb.is_code_message({"role": "user", "content": FENCED}, {}) and cb.is_code_message({"role": "user", "content": PY}, {})
    assert not cb.is_code_message({"role": "user", "content": "please fix the loader", "note": True}, {})
    assert cb.is_code_message({"role": "assistant", "content": FENCED}, {}) and not cb.is_code_message({"role": "assistant", "content": PROSE}, {})
    assert cb.is_code_message(call(1, "write_file", path="b.py", content="x = 1"), {})


def test_a_description_can_be_made_from_the_code_alone():
    info = cb.outline("# loads the files\n" + PY)
    assert info["lines"] == 16 and info["functions"] == ["load", "__init__", "add"] and info["classes"] == ["Box"] and info["comment"] == "loads the files"
    assert cb.guess_language(PY) == "python" and cb.guess_language("x", "Rust") == "rust"


# -- masking ---------------------------------------------------------------------------------------------------------


def manager(tmp_path, keep_code=True, budget=1500):
    return ContextManager(TokenJuiceCompactor(tmp_path / "art", display_dir=".art"), budget_tokens=budget, mask_at=0.5, keep_recent=1, keep_code=keep_code)


def mixed(n=4):
    msgs = start()
    for i in range(n):
        msgs += read(2 * i, f"src/m{i}.py", PY + f"\n# marker {i}\n" + "\n".join(f"v{j} = {j}" for j in range(20)))
        msgs += read(2 * i + 1, f"notes{i}.txt", PROSE * 12)
    return msgs


def test_masking_skips_code_and_still_masks_the_rest(tmp_path):
    out = manager(tmp_path).prepare(mixed())
    tools = [m for m in out if m["role"] == "tool"]
    assert all(not m.get("masked") for m in tools if "marker" in m["content"])
    assert any(m.get("masked") for m in tools if m["name"] == "read_file" and "marker" not in m["content"])
    assert [i for i in code_indices(out)] and paired(out)


def test_without_the_rule_code_is_masked_like_anything_else(tmp_path):
    out = manager(tmp_path, keep_code=False).prepare(mixed())
    assert any(m.get("masked") for m in out if m["role"] == "tool" and m["tool_call_id"] == "c0")
    assert all("code" not in m for m in out)                                        # nothing is marked


def test_the_mark_is_bookkeeping_and_never_sent_to_the_server(tmp_path):
    out = manager(tmp_path).prepare(mixed())
    assert any(m.get("code") for m in out) and all("code" not in m for m in wire_messages(out))


# -- the safety valve --------------------------------------------------------------------------------------------------


def only_code(n=6):
    msgs = start()
    for i in range(n):
        msgs += read(i, f"src/m{i}.py", PY + f"\n# marker {i}\n" + "\n".join(f"v{j} = {j}" for j in range(30)))
    return msgs


def test_code_over_its_share_of_the_budget_has_its_oldest_results_masked_with_the_path(tmp_path):
    mgr = manager(tmp_path, budget=1500)
    out = mgr.prepare(only_code())
    gone = mgr.take_released()
    assert gone and all(g["how"] == "masked" and g["path"].startswith("src/m") for g in gone) and mgr.take_released() == []
    masked = [m for m in out if m["role"] == "tool" and m.get("masked")]
    assert len(masked) == len(gone) and "full text in .art/read_file_" in masked[0]["content"]
    assert [g["path"] for g in gone] == [f"src/m{i}.py" for i in range(len(gone))]            # the oldest first
    assert not out[-1].get("masked") and mgr.estimate_tokens([out[i] for i in code_indices(out)]) <= 0.6 * 1500
    assert paired(out)


def test_code_the_person_pasted_loses_its_protection_when_results_are_not_enough(tmp_path):
    mgr = manager(tmp_path, budget=800)
    msgs = start() + [{"role": "user", "content": FENCED + "\n" + "x = 1\n" * 400}, {"role": "assistant", "content": "ok"}]
    out = mgr.prepare(msgs)
    gone = mgr.take_released()
    assert [g["how"] for g in gone] == ["unprotected"] and not any(m.get("code") for m in out)
    assert out[2]["content"].startswith("Here is my function")                             # not masked: it only stops being exempt


def test_under_its_share_nothing_is_released(tmp_path):
    mgr = manager(tmp_path, budget=100000)
    mgr.prepare(only_code(3))
    assert mgr.take_released() == []


class Quiet:
    def chat_completion(self, **kw):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def test_the_loop_says_so_in_an_event_an_audit_record_and_once_to_the_model(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    registry, _ = create_standard_tool_registry(ws)
    ctx = cp.PatternContext(TokenJuiceCompactor(tmp_path / "art", display_dir=".art"), budget_tokens=1500, mask_at=0.5, keep_recent=1, keep_code=True)
    events, records = [], []
    audit = type("A", (), {"record": lambda self, actor, action, details: records.append((action, details))})()
    agent = AgentLoop(client=Quiet(), tool_registry=registry, max_turns=0, context=ctx, on_event=events.append, audit=audit)
    agent.messages += only_code(6)[1:]
    agent._prepare_context()
    agent.messages += sum((read(20 + i, f"src/n{i}.py", PY + f"\n# later {i}\n" + "\n".join(f"w{j} = {j}" for j in range(30))) for i in range(4)), [])
    agent._prepare_context()
    shown = [e for e in events if e["type"] == "code_masked"]
    assert len(shown) == 2 and shown[0]["count"] >= 1 and shown[0]["items"][0]["how"] == "masked"
    assert [a for a, _ in records].count("code_masked") == 2 and "paths" in records[0][1]
    notes = [m for m in agent.messages if str(m.get("content", "")).startswith("[harness] The code kept whole")]
    assert len(notes) == 1 and "src/m0.py" in notes[0]["content"]


# -- compaction --------------------------------------------------------------------------------------------------------


class Writer:
    def __init__(self):
        self.calls = []

    def __call__(self, instructions, text):
        self.calls.append(text)
        return "Goal: fix the loader\nDecisions: read every module\nOpen items: run the tests"


def long_job(code_every=3):
    msgs = start()
    for i in range(40):
        if i % code_every == 0:
            msgs += read(i, f"src/m{i}.py", f"CODEMARK{i}\n" + PY)
        else:
            msgs += read(i, f"notes{i}.txt", f"notes {i} " + "word " * 80)
    return msgs


def test_compaction_leaves_the_code_whole_after_the_note_and_folds_the_rest(tmp_path):
    writer = Writer()
    ctx = cp.PatternContext(TokenJuiceCompactor(tmp_path / "art", display_dir=".art"), budget_tokens=6000, mask_at=0.7, keep_recent=3,
                            compaction=Compaction(writer), keep_code=True)
    msgs = long_job()
    out = ctx.prepare(msgs)
    assert "compaction" in ctx.last_actions and paired(out)
    note_at = next(i for i, m in enumerate(out) if m.get("summary"))
    kept = [m for m in out if m["role"] == "tool" and "CODEMARK" in m["content"] and not m.get("masked")]
    assert len(kept) == len([i for i in range(40) if i % 3 == 0])                       # every code result is still there, as written
    assert all(PY in m["content"] for m in kept)
    assert [i for i, m in enumerate(out) if m["role"] == "tool" and "CODEMARK" in m["content"]][0] > note_at
    assert "CODEMARK" not in writer.calls[0] and "def load" not in writer.calls[0]       # the summary call never reads code
    assert KEPT_CODE_HEAD in out[note_at]["content"] and "src/m0.py" in out[note_at]["content"]
    assert not any("notes1.txt" in json.dumps(m.get("tool_calls") or "") for m in out[:note_at + 1])
    assert len(out) < len(msgs)


def test_a_second_compaction_keeps_the_same_code_and_does_not_read_it_back_from_the_originals(tmp_path):
    writer = Writer()
    ctx = cp.PatternContext(TokenJuiceCompactor(tmp_path / "art", display_dir=".art"), budget_tokens=6000, mask_at=0.7, keep_recent=3,
                            compaction=Compaction(writer), keep_code=True)
    out = ctx.prepare(long_job())
    more = out + sum((read(100 + i, f"notes{100 + i}.txt", "word " * 120) for i in range(30)), [])
    out2 = ctx.prepare(more)
    assert len(writer.calls) == 2 and "CODEMARK" not in writer.calls[1] and paired(out2)
    assert sum(1 for m in out2 if m["role"] == "tool" and "CODEMARK" in m["content"]) == 14


def test_without_the_rule_compaction_folds_code_like_anything_else(tmp_path):
    writer = Writer()
    ctx = cp.PatternContext(TokenJuiceCompactor(tmp_path / "art", display_dir=".art"), budget_tokens=6000, mask_at=0.7, keep_recent=3, compaction=Compaction(writer))
    out = ctx.prepare(long_job())
    assert any(m.get("summary") for m in out) and "CODEMARK" in writer.calls[0]


def test_code_the_person_pasted_in_the_middle_of_the_job_stays_whole(tmp_path):
    writer = Writer()
    ctx = cp.PatternContext(TokenJuiceCompactor(tmp_path / "art", display_dir=".art"), budget_tokens=6000, mask_at=0.7, keep_recent=3,
                            compaction=Compaction(writer), keep_code=True)
    msgs = long_job(code_every=99)
    msgs.insert(30, {"role": "user", "content": FENCED})
    msgs.insert(31, {"role": "assistant", "content": "I will look."})
    out = ctx.prepare(msgs)
    assert sum(1 for m in out if m["role"] == "user" and m["content"] == FENCED) == 1 and "def load" not in writer.calls[0]


def test_the_turn_based_summary_and_the_drop_of_old_turns_keep_the_code_too(tmp_path):
    msgs = start()
    for i in range(6):
        msgs.append({"role": "user", "content": f"request {i}"})
        msgs += read(i, f"src/m{i}.py", f"CODEMARK{i}\n" + PY)
        msgs.append({"role": "assistant", "content": f"done {i}"})
    for strategy, pruning in (("summary", None), ("pruning", "oldest_first")):
        patterns = {"summary": {"on": True, "strategy": "extractive"}} if pruning is None else {"pruning": {"on": True, "strategy": pruning}}
        ctx = cp.PatternContext(TokenJuiceCompactor(tmp_path / strategy, display_dir=".art"), budget_tokens=900, patterns=patterns, mask_at=0.3, keep_recent=1, keep_code=True)
        out = ctx.prepare([dict(m) for m in msgs])
        assert paired(out), strategy
        assert sum(1 for m in out if m["role"] == "tool" and "CODEMARK" in m["content"] and not m.get("masked")) >= 1, strategy


# -- the stack ---------------------------------------------------------------------------------------------------------


def test_the_agent_stack_turns_the_rule_on_with_file_tools_and_it_can_be_switched_off(tmp_path):
    from test_compaction_settings import Scripted, make
    _, _, built = make(tmp_path, Scripted())
    assert built.agent.context.keep_code is True
    _, _, off = make(tmp_path / "off", Scripted(), keep_code=False)
    assert off.agent.context.keep_code is False
