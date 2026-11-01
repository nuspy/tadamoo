"""Compaction: a summary that is cut off is not used, the Facts section is kept by the program, earlier notes are read again, pointers to the results."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import compaction_facts as cf
from harness.compaction import Compaction, SHORTER, SUMMARY_PROMPT, SummaryCut
from test_compaction import Writer, make, paired, single_turn, step


class Scripted:
    """The summary call with a script: each call takes the next answer; an exception in the script is raised."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, instructions, text):
        self.calls.append((instructions, text))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


NOTES = "Goal: fix the parser\nDecisions: keep the limit\nOpen items: run the tests"


def note_of(out):
    return next(m for m in out if m.get("summary"))["content"]


def grow(out, start=200, n=30):
    return out + sum((step(start + i, 800) for i in range(n)), [])


# -- a cut-off summary --------------------------------------------------------------------------------------------------


def test_a_cut_off_summary_is_asked_for_again_from_less_text(tmp_path):
    writer = Scripted(SummaryCut(), NOTES)
    ctx, comp = make(tmp_path, writer)
    out = ctx.prepare(single_turn())
    assert len(writer.calls) == 2 and comp.cuts == 1 and comp.count == 1 and comp.abandoned == 0
    first, second = writer.calls
    assert SHORTER not in first[0] and second[0] == SUMMARY_PROMPT + SHORTER
    assert len(second[1]) < len(first[1])
    assert "fix the parser" in note_of(out) and ctx.take_compaction()["summary_cuts"] == 1


def test_a_summary_cut_off_twice_gives_notes_made_without_the_model_and_keeps_the_facts(tmp_path):
    writer = Scripted(NOTES)
    ctx, comp = make(tmp_path, writer)
    out = ctx.prepare(single_turn())
    writer.answers = [SummaryCut()]
    out2 = ctx.prepare(grow(out))
    assert comp.cuts == 2 and comp.count == 2 and paired(out2)                # the conversation never runs out of room for want of a summary
    records = [m for m in out2 if m.get("summary")]
    assert len(records) == 1 and comp.reports[-1]["summary_by"] == "extract"



def test_the_gain_is_estimated_before_the_call(tmp_path):
    writer = Writer()
    ctx, comp = make(tmp_path, writer, budget=6000)
    msgs = single_turn(steps=6, size=60)
    assert comp.apply(ctx, msgs, "threshold") is None and not writer.calls


def test_other_failures_still_give_plain_notes(tmp_path):
    ctx, comp = make(tmp_path, Scripted(TimeoutError("slow")))
    out = ctx.prepare(single_turn())
    assert ctx.take_compaction()["summary_by"] == "extract" and "made without the model" in note_of(out) and comp.cuts == 0


# -- facts ---------------------------------------------------------------------------------------------------------------


def test_the_prompt_names_facts_first():
    assert SUMMARY_PROMPT.index("Facts (") < SUMMARY_PROMPT.index("Goal (") < SUMMARY_PROMPT.index("Open items")


def test_facts_come_first_in_the_note_and_the_rest_follows():
    facts, rest = cf.split("Facts:\nmod_001: 3 functions\nmod_002: 4 functions\n\nGoal: fix\nOpen items: test")
    assert facts == ["mod_001: 3 functions", "mod_002: 4 functions"] and rest == "Goal: fix\nOpen items: test"
    assert cf.with_facts(cf.render(facts, 1000), rest).startswith("Facts:\nmod_001: 3 functions")


def test_the_previous_facts_are_copied_unchanged_and_the_call_only_adds_new_ids(tmp_path):
    writer = Scripted("Facts:\nmod_001: 3 functions\nmod_002: 4 functions\n" + NOTES,
                      "Facts:\nmod_001: 99 functions\nmod_003: 5 functions\n" + NOTES)
    ctx, comp = make(tmp_path, writer)
    out = ctx.prepare(single_turn())
    assert note_of(out).count("mod_001: 3 functions") == 1
    out2 = ctx.prepare(grow(out))
    note = note_of(out2)
    assert len(writer.calls) == 2 and comp.reports[-1]["from_originals"] is True
    lines = note.split("Facts:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert lines[:3] == ["mod_001: 3 functions", "mod_002: 4 functions", "mod_003: 5 functions"]
    assert "mod_001: 99" not in note and comp.reports[-1]["facts"] == 3
    given = writer.calls[1][1]
    assert "Facts so far" in given and given.count("mod_002: 4 functions") == 1       # shown once, not again inside the earlier notes


def test_facts_survive_many_rounds_even_when_the_call_names_none(tmp_path):
    writer = Scripted("Facts:\nmod_001: 3 functions\n" + NOTES, NOTES)
    ctx, comp = make(tmp_path, writer)
    out = ctx.prepare(single_turn())
    for r in range(4):
        out = ctx.prepare(grow(out, start=200 + 100 * r))
    assert comp.count >= 3 and "mod_001: 3 functions" in note_of(out)


def test_the_agents_own_ledger_lines_are_read_from_its_writes(tmp_path):
    msgs = single_turn()
    def write(k, name, args):
        msgs[2 + 2 * k]["tool_calls"][0]["function"] = {"name": name, "arguments": json.dumps(args)}
    write(3, "edit_file", {"path": "LEDGER.md", "edits": [{"target": "# Ledger", "replacement": "# Ledger\nmod_005: 4 functions\nmod_006: 3 functions"}]})
    write(5, "write_file", {"path": "notes.md", "content": "Note: remember this\ndef f(x: int):\n    return {\"a\": 1}\nhttp://example.com/x: y\nmod_006: 7 functions\n"})
    ctx, comp = make(tmp_path, Writer())
    note = note_of(ctx.prepare(msgs))
    section = note.split("Facts:\n", 1)[1].split("\n\n", 1)[0]
    assert section.splitlines() == ["mod_005: 4 functions", "mod_006: 7 functions"]       # prose, code and a URL are not facts; the later write wins


def test_facts_are_kept_when_the_notes_are_made_without_the_model(tmp_path):
    writer = Scripted("Facts:\nmod_001: 3 functions\n" + NOTES, TimeoutError("slow"))
    ctx, comp = make(tmp_path, writer)
    out = ctx.prepare(single_turn())
    out2 = ctx.prepare(grow(out))
    note = note_of(out2)
    assert ctx.take_compaction()["summary_by"] == "extract" and note.split("Facts:\n", 1)[1].startswith("mod_001: 3 functions")


def test_a_long_facts_section_drops_its_oldest_lines_and_says_so():
    lines = [f"mod_{i:03d}: {i} functions" for i in range(100)]
    text = cf.render(lines, 400)
    kept = text.splitlines()
    assert kept[1].startswith("(") and "older fact lines left out" in kept[1] and kept[-1] == lines[-1] and len(text) <= 460
    assert cf.split(text)[0][0].startswith("mod_") and not any("left out" in x for x in cf.split(text)[0])


# -- earlier notes and pointers -----------------------------------------------------------------------------------------


def test_the_added_lists_of_an_earlier_record_are_not_given_back_as_notes(tmp_path):
    writer = Writer()
    ctx, comp = make(tmp_path, writer)
    out = ctx.prepare(single_turn())
    assert "Where the full results are saved" in note_of(out)
    ctx.prepare(grow(out))
    notes = writer.calls[1][1].split("Notes so far, from an earlier compaction:", 1)[1].split("Taken from the calls made", 1)[0]
    assert "run the tests" in notes and "Where the full results" not in notes


def test_the_note_points_at_where_each_folded_result_is_saved(tmp_path):
    ctx, comp = make(tmp_path, Writer(), budget=6000)
    msgs = single_turn(steps=60, size=500)
    msgs[2 + 2 * 10]["tool_calls"][0]["function"]["name"] = "write_file"          # a write is not a result to look up
    out = ctx.prepare(msgs)
    note = note_of(out)
    block = note.split("Where the full results are saved (read_file on that path):\n", 1)[1].split("\n")
    lines = [x for x in block if x.startswith("- src/")]
    assert 3 <= len(lines) <= 40 and not any("src/f10.py" in x for x in lines)
    for x in lines:
        path = x.split(": ", 1)[1].split("\n")[0].strip()
        assert (tmp_path / "art" / Path(path).name).exists()
    assert "originals:" in note                                                     # the pointer to the whole record stays


def test_a_small_result_that_was_not_masked_gets_no_pointer(tmp_path):
    from harness import compaction as cm
    ctx, comp = make(tmp_path, Writer(), budget=3000)
    plan = cm.plan_cut(single_turn(steps=10, size=20), 100, 3.5)
    assert plan.region and comp._pointers(ctx, plan) == ""


# -- the summary client -------------------------------------------------------------------------------------------------


class Finishing:
    """A client whose answer ends with the given finish reason."""

    def __init__(self, finish):
        self.finish = finish

    def chat_completion(self, **kw):
        return {"choices": [{"message": {"content": "Goal: carry on " * 5}, "finish_reason": self.finish}]}


def test_a_summary_that_ended_on_the_length_limit_raises_only_when_asked_to():
    import pytest
    from harness.stack import _summarizer
    with pytest.raises(SummaryCut):
        _summarizer(Finishing("length"), reject_cut=True)("instructions", "text")
    assert _summarizer(Finishing("length"))("instructions", "text").startswith("Goal")          # the turn-based summary takes what it gets
    assert _summarizer(Finishing("stop"), reject_cut=True)("instructions", "text").startswith("Goal")
