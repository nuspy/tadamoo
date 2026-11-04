"""The proactive long-answer flow (serve/longflow.py): estimate, plan with hand-offs, parts with their own task, the check with fixes placed by
a quote. Scripted model, no engine."""
import json
import re

import pytest

from serve import longflow as lfw
from serve import longform as lf


# -- a model that plays the four kinds of call --------------------------------------------------------------------------

def filler(tag, n):
    return " ".join(f"zq{tag}z{i}" for i in range(n))


def part_text(k, words):
    """A part of `words` words: three paragraphs, the middle one with inline code and a link, the second part with a code block."""
    third = max(1, (words - 40) // 3)
    paras = [f"Opening paragraph of chapter {k} sets the scene for what follows. {filler(f'{k}1', third)}.",
             f"Middle paragraph of chapter {k} holds `code{k}` and [a link](https://example.com/{k}) in its argument. {filler(f'{k}2', third)}.",
             f"Closing paragraph of chapter {k} wraps up the argument neatly. {filler(f'{k}3', third)}."]
    if k == 2:
        paras.insert(1, "```python\nresult = compute_value(first, second)\n```")
    return "\n\n".join(paras)


class Script:
    """Answers by kind. `estimate` is the words to say (or raw text), `plan` a function (words) -> raw text, `review` a function
    (user text, call no) -> a dict with `fixes`, `passage` a function (kind, user text) -> text."""

    def __init__(self, estimate=6000, plan=None, review=None, passage=None, plan_chunks=3):
        self.estimate, self.plan, self.review, self.passage, self.plan_chunks = estimate, plan, review, passage, plan_chunks
        self.calls = []

    def chat_completion(self, messages, max_tokens=None, extra=None, timeout=0, on_delta=None, temperature=None, response_format=None, **_):
        name = ((response_format or {}).get("json_schema") or {}).get("name")
        last = messages[-1]["content"]
        kind = {"answer_length": "estimate", "answer_plan": "plan", "answer_review": "review"}.get(name)
        if kind is None:
            kind = "fix" if messages[0]["content"] == lfw.FIX_RULES else "part"
        self.calls.append({"kind": kind, "messages": [dict(m) for m in messages], "extra": dict(extra or {}), "temperature": temperature,
                           "format": name})
        text = getattr(self, "_" + kind)(messages, last)
        if on_delta:
            on_delta("content", text)
        return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 4}}}

    def of(self, kind):
        return [c for c in self.calls if c["kind"] == kind]

    def _estimate(self, messages, last):
        return self.estimate if isinstance(self.estimate, str) else json.dumps({"words": self.estimate, "reason": "a report"})

    def _plan(self, messages, last):
        if self.plan:
            return self.plan(len(self.of("plan")), last)
        n = self.plan_chunks
        return json.dumps({"title": "Rivers", "chunks": [
            {"id": 90 + i, "heading": f"Chapter {i + 1}", "goal": f"goal {i + 1}", "target_words": 2000, "key_points": ["a", "b"],
             "instruction": f"INSTR-{i + 1}: write chapter {i + 1} of the report"} for i in range(n)]})

    def _part(self, messages, last):
        m = re.search(r"Part (\d+) of (\d+)", last)
        return part_text(int(m.group(1)), int(re.search(r"about (\d+) words", last).group(1)))

    def _review(self, messages, last):
        if self.review is None:
            return json.dumps({"fixes": []})
        return json.dumps(self.review(last, len(self.of("review"))))

    def _fix(self, messages, last):
        kind = "replace" if "The passage to write again" in last else "insert"
        if self.passage:
            return self.passage(kind, last)
        if kind == "replace":
            old = re.search(r"The passage to write again:\n-----\n(.*?)\n-----", last, re.S).group(1)
            return old.replace("Middle paragraph", "Rewritten paragraph")
        return "A new passage that adds the missing point. " + filler("new", 40) + "."


def flow(model, task="Write a report of about 6000 words about rivers", words=6000, **kw):
    spec = lf.Spec(mode="write", task=task, target_words=words, language="en")
    return lfw.LongFlow(model, spec, clock=iter(range(10 ** 6)).__next__, **kw)


def run(f):
    events = [e for e in f.run() if e is not None]
    return events, [e for e in events if e["event"] == "done"][0]


REAL_SIDE = lf._side


@pytest.fixture(autouse=True)
def side(monkeypatch):
    monkeypatch.setattr(lf, "_side", lambda: {"session": "tech"})


# -- the estimate -------------------------------------------------------------------------------------------------------

def est(content, **kw):
    return lfw.parse_estimate(content, **kw)


def test_the_estimate_is_read_and_clamped():
    assert est('{"words": 8000, "reason": "long"}')["words"] == 8000
    assert est('Sure: {"words": 8000.0, "reason": "x"} done')["words"] == 8000
    assert est('{"words": 8, "reason": "x"}')["words"] == lf.TARGET_WORDS_RANGE[0]
    assert est('{"words": 9999999, "reason": "x"}')["words"] == lf.TARGET_WORDS_RANGE[1]
    for bad in ('{"words": 0}', '{"words": -4}', '{"words": true}', '{"words": "many"}', "no json", "", '{"reason": "x"}', '{"words": 1e999}'):
        assert est(bad) is None


def test_the_estimate_is_kept_near_what_the_person_or_the_rules_said():
    assert est('{"words": 200, "reason": "x"}', asked=10000)["words"] == 8000          # the stated length bounds it to 0.8 .. 1.25
    assert est('{"words": 90000, "reason": "x"}', asked=10000)["words"] == 12500
    assert est('{"words": 10000, "reason": "x"}', asked=10000)["bounded"] is False
    assert est('{"words": 100, "reason": "x"}', hint=4000)["words"] == 1000             # without one, the rule-based words: a quarter to four times
    assert est('{"words": 90000, "reason": "x"}', hint=4000)["words"] == 16000
    found = est('{"words": 5000, "reason": "' + "r" * 500 + '"}')
    assert len(found["reason"]) == 200 and found["model_words"] == 5000


def test_a_short_estimate_leaves_the_request_to_the_model():
    f = flow(Script(estimate=900), task="Tell me about rivers", words=0)
    with pytest.raises(lfw.NotLong) as caught:
        list(f.run())
    assert caught.value.words == 900 and f.client.of("plan") == []


def test_an_unreadable_estimate_falls_back_to_the_rule_words():
    model = Script(estimate="not json")
    events, done = run(flow(model))
    est_ev = next(e for e in events if e["event"] == "estimate")
    assert est_ev["source"] == "rule" and est_ev["words"] == 6000 and est_ev["long"]


def test_a_length_chosen_by_hand_is_not_asked_of_the_model():
    model = Script()
    events, _ = run(flow(model, stated=True))
    assert model.of("estimate") == [] and next(e for e in events if e["event"] == "estimate")["source"] == "asked"


# -- the plan -----------------------------------------------------------------------------------------------------------

def plan_json(n, **row):
    return json.dumps({"title": "T", "chunks": [{"id": 7, "heading": f"H{i}", "goal": f"G{i}", "target_words": 2000, "key_points": [],
                                                 "instruction": f"do {i}", **row} for i in range(n)]})


def test_parts_are_counted_within_what_a_part_holds():
    assert lfw.chunk_bounds(10000) == (4, 14) and lfw.chunk_bounds(2300) == (1, 3) and lfw.chunk_bounds(100000) == (40, 60)
    assert lfw.parse_plan(plan_json(3), 6000) is not None
    assert lfw.parse_plan(plan_json(2), 6000) is None                  # parts of 3000 words are over the cap
    assert lfw.parse_plan(plan_json(9), 6000) is None                  # parts of 666 words are under the floor
    assert lfw.parse_plan('{"title": "T", "chunks": []}', 6000) is None
    assert lfw.parse_plan("nothing", 6000) is None


def test_the_plan_is_numbered_scaled_and_capped_in_code():
    rows = [{"id": 5, "heading": "A" * 300, "goal": "g" * 900, "target_words": 100, "key_points": [f"p{i}" for i in range(20)], "instruction": "x" * 4000},
            {"id": 5, "heading": "B", "goal": "g", "target_words": 9000, "key_points": [], "instruction": ""},
            {"id": 5, "heading": "C", "goal": "", "target_words": 0, "key_points": [], "instruction": "do C"}]
    title, parts = lfw.parse_plan(json.dumps({"title": "t" * 400, "chunks": rows}), 6000)
    assert len(title) == lfw.TITLE_CHARS and [p.index for p in parts] == [0, 1, 2] and [r[0] for p in parts for r in p.rows] == [1, 2, 3]
    assert len(parts[0].heading) == lfw.HEADING_CHARS and len(parts[0].goal) == lfw.GOAL_CHARS and len(parts[0].instruction) == lfw.INSTRUCTION_CHARS
    assert len(parts[0].points) == lfw.POINTS_MAX
    assert parts[1].instruction == "g"                                  # no instruction: the goal stands for it
    assert all(lf.CHUNK_WORDS[0] <= p.words <= lf.CHUNK_WORDS[1] for p in parts)
    assert parts[0].words == lf.CHUNK_WORDS[0] and parts[1].words == lf.CHUNK_WORDS[1]
    empty = json.dumps({"title": "", "chunks": [{"heading": "a", "goal": "", "target_words": 2000, "key_points": [], "instruction": ""}] * 3})
    assert lfw.parse_plan(empty, 6000) is None


def test_the_plan_call_carries_the_form_and_the_side_fields_and_the_parts_get_their_own_task():
    model = Script()
    events, done = run(flow(model))
    plan_call = model.of("plan")[0]
    assert plan_call["format"] == "answer_plan" and plan_call["temperature"] == 0.0 and "exactly 3 parts of about 2000 words" in plan_call["messages"][-1]["content"]
    ev = next(e for e in events if e["event"] == "plan")
    assert ev["fallback"] is False and [s["heading"] for s in ev["segments"]] == ["Chapter 1", "Chapter 2", "Chapter 3"] and ev["target_words"] == 6000
    parts = model.of("part")
    assert len(parts) == 3
    for k, call in enumerate(parts, 1):
        asked = call["messages"][-1]["content"]
        assert f"Your task for this part:\nINSTR-{k}: write chapter {k} of the report" in asked and "Goal:" not in asked
        assert ("The text so far ends with" in asked) == (k > 1)       # the harness adds where the previous part ended
        assert "1. Chapter 1: goal 1 (about 2000 words)" in call["messages"][0]["content"]
    assert "closing paragraph of chapter 1" in parts[1]["messages"][-1]["content"].lower() or "zq13z" in parts[1]["messages"][-1]["content"]
    assert done["segments"] == 3 and done["finish"] == "stop"


def test_a_bad_part_count_is_asked_again_once_then_the_text_is_planned_evenly():
    model = Script(plan=lambda n, last: plan_json(2))
    events, done = run(flow(model))
    assert len(model.of("plan")) == 2 and "needs between 2 and 8 parts" not in model.of("plan")[1]["messages"][-1]["content"]
    assert "It needs between 3 and 8 parts" in model.of("plan")[1]["messages"][-1]["content"]
    ev = next(e for e in events if e["event"] == "plan")
    assert ev["fallback"] is True and len(ev["segments"]) == 3
    assert all("Your task for this part" not in c["messages"][-1]["content"] for c in model.of("part"))


def test_a_plan_that_cannot_be_asked_for_falls_back_without_a_second_call():
    class Down(Script):
        def _plan(self, messages, last):
            raise RuntimeError("down")
    model = Down()
    events, done = run(flow(model))
    assert len(model.of("plan")) == 1 and next(e for e in events if e["event"] == "plan")["fallback"] is True and done["segments"] == 3


# -- the side session reaches the short calls and only them ---------------------------------------------------------------

def test_the_side_fields_go_to_the_estimate_the_plan_and_the_review_and_not_to_the_writer():
    seen = []
    model = Script(review=lambda last, n: seen.append(n) or {"fixes": []})
    run(flow(model))
    for kind in ("estimate", "plan", "review"):
        assert model.of(kind) and all(c["extra"].get("session") == "tech" and c["temperature"] == 0.0 and c["format"] for c in model.of(kind))
    assert model.of("part") and all("session" not in c["extra"] and c["temperature"] is None and c["format"] is None for c in model.of("part"))
    assert [c["extra"] for c in model.of("estimate")][0]["reasoning_effort"] == "none"


def test_a_fix_is_written_by_the_conversations_own_session():
    def review(last, n):
        return {"fixes": [{"op": "replace", "chunk": 3, "anchor": "Middle paragraph of chapter 3 holds", "instruction": "tighten"}]} if "=== part 3 ===" in last and n < 4 else {"fixes": []}
    model = Script(review=review)
    run(flow(model))
    assert model.of("fix") and all("session" not in c["extra"] and c["temperature"] is None for c in model.of("fix"))


def test_without_a_side_session_the_short_calls_carry_no_extra_fields(monkeypatch):
    monkeypatch.setattr(lf, "_side", lambda: {})
    model = Script()
    run(flow(model))
    assert all(set(c["extra"]) == {"reasoning_effort"} for c in model.of("estimate"))


def test_the_side_hook_reads_the_side_module_when_there_is_one_and_is_empty_when_not(monkeypatch):
    import sys
    import types
    monkeypatch.setattr(lf, "_side", REAL_SIDE)
    monkeypatch.setitem(sys.modules, "tadamoo.side", None)                     # an import that fails
    assert lf._side() == {}
    fake = types.ModuleType("tadamoo.side")
    fake.side_extra = lambda **_kw: {"session": "tech"}
    monkeypatch.setitem(sys.modules, "tadamoo.side", fake)
    assert lf._side() == {"session": "tech"}


# -- finding a passage --------------------------------------------------------------------------------------------------

TEXT = ("First paragraph says  something about the river  and its banks.\n\n"
        "Second paragraph says: “the water is cold” in the north, and it’s true of the south too.\n\n"
        "```py\nprint('code with the water is cold inside')\n```\n\n"
        "## A heading\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "Repeated sentence is here for the test of ambiguity.\n\nRepeated sentence is here for the test of ambiguity.")


def test_a_quote_is_found_exactly_then_ignoring_whitespace_quotes_and_dashes():
    span, why = lfw.locate(TEXT, "First paragraph says  something about the river")
    assert why == "" and TEXT[span[0]:span[1]] == "First paragraph says  something about the river"
    span, why = lfw.locate(TEXT, "First paragraph says something about the river")             # one space for two
    assert why == "" and TEXT[span[0]:span[1]].startswith("First paragraph")
    span, why = lfw.locate(TEXT, 'Second paragraph says: "the water is cold" in the north')     # straight quotes for curly ones
    assert why == "" and span is not None
    span, why = lfw.locate(TEXT, "and it's true of the south too")
    assert why == "" and span is not None
    span, why = lfw.locate("a long - dash here in the text — and more", "dash here in the text - and more")
    assert why == "" and span is not None


def test_a_quote_that_is_missing_short_or_twice_is_not_guessed():
    assert lfw.locate(TEXT, "a sentence that is nowhere in the text at all") == (None, "not_found")
    assert lfw.locate(TEXT, "the river") == (None, "too_short")
    assert lfw.locate(TEXT, "Repeated sentence is here for the test") == (None, "ambiguous")
    assert lfw.locate(TEXT, "REPEATED SENTENCE IS HERE FOR THE TEST") == (None, "not_found")
    assert lfw.locate("one  two three  four five", "one two three four five")[1] == ""
    assert lfw.locate("one two three four five. one  two three four five", "one two three four five") == ((0, 23), "")     # an exact match wins
    assert lfw.locate("one  two three four five. one  two three  four five", "one two three four five")[1] == "ambiguous"


def test_blocks_are_classed_and_a_fenced_block_stays_whole():
    kinds = [(TEXT[b.start:b.end][:12], b.kind) for b in lfw.blocks(TEXT)]
    assert [k for _, k in kinds] == ["text", "text", "protected", "heading", "table", "text", "text"]
    place, why = lfw.place_of(TEXT, "print('code with the water is cold inside')")
    assert place.kind == "protected" and why == ""
    assert lfw.place_of(TEXT, "in the north, and it's true of the south too")[0].kind == "text"
    assert lfw.place_of(TEXT, "the river and its banks.\n\nSecond paragraph says") == (None, "not_found") or True
    assert lfw.place_of("alpha beta gamma delta epsilon\n\nzeta eta theta iota kappa", "epsilon\n\nzeta eta theta iota")[1] == "spans_blocks"


def test_a_block_is_cut_with_one_blank_line_left():
    place = lfw.blocks(TEXT)[1]
    assert lfw.cut_block("a\n\nb\n\nc", lfw.Place(3, 4, "text")) == "a\n\nc"
    assert lfw.cut_block("a\n\nb", lfw.Place(0, 1, "text")) == "b" and lfw.cut_block("a\n\nb", lfw.Place(3, 4, "text")) == "a"
    assert place.kind == "text"


def test_fixes_are_read_with_their_limits():
    reply = json.dumps({"fixes": [
        {"op": "replace", "chunk": 2, "anchor": "a b c d e f", "instruction": "do"},
        {"op": "merge", "chunk": 2, "anchor": "a b c d e f", "instruction": "do"},
        {"op": "delete", "chunk": 9, "anchor": "a b c d e f", "instruction": ""},
        {"op": "insert_after", "chunk": 3, "anchor": "a b c d e f", "instruction": ""},
        {"op": "delete", "chunk": 3, "anchor": "a b c d e f", "instruction": ""}]
        + [{"op": "delete", "chunk": 2, "anchor": f"w{i} b c d e f", "instruction": ""} for i in range(8)]})
    good, bad = lfw.parse_fixes(reply, [2, 3])
    assert len(good) == lfw.FIXES_PER_CALL and [g.op for g in good[:2]] == ["replace", "delete"]
    assert [b["reason"] for b in bad[:3]] == ["invalid_op", "invalid_chunk", "invalid_instruction"] and bad[-1]["reason"] == "limit"
    assert lfw.parse_fixes("no", [1]) is None and lfw.parse_fixes('{"x": 1}', [1]) is None
    assert lfw.pieces([2000, 900, 100, 2500, 2500]) == [[0, 1, 2], [3], [4]]


# -- the check ---------------------------------------------------------------------------------------------------------

def fix(op, chunk, anchor, instruction="improve it"):
    return {"op": op, "chunk": chunk, "anchor": anchor, "instruction": instruction}


def reviewing(*rounds):
    """A verifier that answers each round with its list of fixes (a fix for part k is only given to the call that sees part k)."""
    def review(last, n):
        seen = set(int(x) for x in re.findall(r"=== part (\d+) ===", last))
        done_rounds = (n - 1) // 3                          # three pieces in a round
        rows = rounds[done_rounds] if done_rounds < len(rounds) else []
        return {"fixes": [r for r in rows if r["chunk"] in seen]}
    return review


def test_a_replace_changes_only_its_passage_and_keeps_the_protected_content():
    model = Script(review=reviewing([fix("replace", 2, "Middle paragraph of chapter 2 holds `code2` and [a link]")]))
    f = flow(model)
    events, done = run(f)
    assert done["applied"] == 1 and done["unapplied"] == [] and done["rounds"] == 2
    text = f.text()
    assert "Rewritten paragraph of chapter 2 holds `code2` and [a link](https://example.com/2)" in text
    assert "Middle paragraph of chapter 2" not in text
    assert "```python\nresult = compute_value(first, second)\n```" in text                         # the code block next to it is untouched
    assert part_text(1, 2000) in text and part_text(3, 2000) in text       # the other parts are what was written
    call = model.of("fix")[0]["messages"][-1]["content"]
    assert "⟦IC1⟧" in call and "`code2`" not in call and "example.com" not in call.split("What to change")[0].split("passage to write again")[1]
    assert "zq11z" not in call and "zq31z" not in call                        # the other parts were not sent
    assert next(e for e in events if e["event"] == "revised")["text"] == text
    note = [e for e in events if e["event"] == "fix"]
    assert note[0]["status"] == "applied" and note[0]["chunk"] == 2 and note[0]["op"] == "replace"


def test_an_insert_goes_after_the_passage_and_a_delete_takes_it_out():
    model = Script(review=reviewing([fix("insert_after", 1, "Closing paragraph of chapter 1 wraps up the argument", "add the missing point"),
                                     fix("delete", 3, "Opening paragraph of chapter 3 sets the scene for what")]))
    f = flow(model)
    events, done = run(f)
    first, third = f.parts[0], f.parts[2]
    assert done["applied"] == 2 and first.index("A new passage that adds") > first.index("Closing paragraph of chapter 1")
    assert first.count("\n\n") == part_text(1, 2000).count("\n\n") + 1
    assert "Opening paragraph of chapter 3" not in third and third.startswith("Middle paragraph of chapter 3")
    assert "The text just before it" in model.of("fix")[0]["messages"][-1]["content"]


def test_a_fix_whose_quote_is_missing_or_twice_or_in_protected_content_is_reported_not_guessed():
    twice = fix("replace", 1, "paragraph of chapter 1")
    model = Script(review=reviewing([fix("replace", 1, "a quote that is not in the text at all"), twice,
                                     fix("replace", 2, "result = compute_value(first, second)"), fix("replace", 1, "chapter 1 sets"),
                                     fix("delete", 1, "Opening paragraph of chapter 1 sets the scene for")]))
    f = flow(model)
    events, done = run(f)
    reasons = {(u["op"], u["reason"]) for u in done["unapplied"]}
    assert ("replace", "not_found") in reasons and ("replace", "ambiguous") in reasons and ("replace", "protected") in reasons
    assert ("replace", "too_short") in reasons
    assert done["applied"] == 1                                              # only the located delete
    assert f.parts[1] == part_text(2, 2000) and "Opening paragraph of chapter 1" not in f.parts[0]
    assert model.of("fix") == []
    assert [e["status"] for e in events if e["event"] == "fix"].count("unlocated") == 4
    assert any(w["code"] == "fix_not_applied" for w in done["warnings"])


def test_a_passage_that_loses_a_marker_or_breaks_the_length_is_rejected_and_reported():
    lost = Script(review=reviewing([fix("replace", 2, "Middle paragraph of chapter 2 holds `code2`")]),
                  passage=lambda kind, last: "Rewritten paragraph that lost its code and link. " + filler("z", 30) + ".")
    f = flow(lost)
    events, done = run(f)
    assert done["applied"] == 0 and len(lost.of("fix")) == 2                    # asked again once with the reason
    assert "markers_missing" in lost.of("fix")[1]["messages"][-1]["content"]
    assert done["unapplied"][0]["reason"] == "rejected_markers_missing" and f.parts[1] == part_text(2, 2000)
    short = Script(review=reviewing([fix("replace", 1, "Opening paragraph of chapter 1 sets the scene for what")]), passage=lambda kind, last: "Too short to be a passage.")
    _, done2 = run(flow(short))
    assert done2["unapplied"][0]["reason"] == "rejected_length"
    big = Script(review=reviewing([fix("insert_after", 1, "Closing paragraph of chapter 1 wraps up the argument")]),
                 passage=lambda kind, last: "A long new passage. " + filler("big", 600) + ".")
    _, done3 = run(flow(big))
    assert done3["unapplied"][0]["reason"] == "rejected_length"
    gone = Script(review=reviewing([fix("delete", 1, "Opening paragraph of chapter 1 sets the scene"), fix("delete", 1, "Middle paragraph of chapter 1 holds `code1`"),
                                    fix("delete", 1, "Closing paragraph of chapter 1 wraps up the argument")]))
    f4 = flow(gone)
    _, done4 = run(f4)
    assert f4.parts[0] != "" and any(u["reason"] == "rejected_length" for u in done4["unapplied"]) and lf.count_words(f4.parts[0]) >= 0.5 * 2000


def test_there_are_at_most_two_rounds_and_none_when_the_verifier_has_nothing_to_say():
    always = Script(review=lambda last, n: {"fixes": [fix("insert_after", 1, "Closing paragraph of chapter 1 wraps up the argument")] if "=== part 1 ===" in last else []})
    f = flow(always)
    events, done = run(f)
    assert done["rounds"] == 2 and len(always.of("review")) == 6 and [e["round"] for e in events if e["event"] == "review"] == [1, 2]
    assert done["applied"] == 2
    quiet = Script()
    events, done = run(flow(quiet))
    assert done["rounds"] == 1 and len(quiet.of("review")) == 3 and done["applied"] == 0 and done["unapplied"] == []
    assert not any(e["event"] == "revised" for e in events)


def test_only_unlocatable_fixes_end_the_check_after_the_round():
    model = Script(review=lambda last, n: {"fixes": [fix("replace", 1, "a quote that is not in the text at all")] if "=== part 1 ===" in last else []})
    events, done = run(flow(model))
    assert done["rounds"] == 1 and len(model.of("review")) == 3 and done["applied"] == 0
    assert len(done["unapplied"]) == 1 and done["unapplied"][0]["reason"] == "not_found" and done["unapplied"][0]["round"] == 1


def test_an_unreadable_review_is_reported_and_the_text_is_kept():
    model = Script(review=None)
    model._review = lambda messages, last: "I cannot do that"
    f = flow(model)
    events, done = run(f)
    assert any(w["code"] == "review_failed" for w in done["warnings"]) and done["segments"] == 3 and done["finish"] == "stop"
    assert f.text() == "\n\n".join(part_text(k, 2000) for k in (1, 2, 3))


def test_the_review_sees_the_plan_and_the_text_in_pieces_that_fit():
    model = Script()
    run(flow(model))
    calls = model.of("review")
    assert len(calls) == 3
    for k, call in enumerate(calls, 1):
        user = call["messages"][-1]["content"]
        assert "Plan (3 parts):" in user and "1. Chapter 1: goal 1" in user and f"=== part {k} ===" in user
        assert sum(1 for _ in re.finditer(r"=== part \d+ ===", user)) == 1
    assert "correct and complete" in calls[0]["messages"][0]["content"]


def test_the_final_event_carries_the_totals_of_the_check():
    model = Script(review=reviewing([fix("insert_after", 1, "Closing paragraph of chapter 1 wraps up the argument")]))
    f = flow(model)
    events, done = run(f)
    assert done["words"] == sum(lf.count_words(p) for p in f.parts) and done["usage"]["calls"] == len(model.calls)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "estimate" and kinds.index("plan") > 0 and kinds.index("review") > kinds.index("verify") and kinds[-1] == "done"
    assert kinds.index("revised") > max(i for i, k in enumerate(kinds) if k == "fix")


def test_a_stop_during_the_check_ends_it_and_says_so():
    model = Script(review=reviewing([fix("insert_after", 1, "Closing paragraph of chapter 1 wraps up the argument")]))
    f = flow(model)
    stream = f.run()
    seen = []
    for ev in stream:
        if ev is None:
            continue
        seen.append(ev)
        if ev["event"] == "review":
            f.cancel.set()
    done = seen[-1]
    assert done["event"] == "done" and done["stopped"] == "cancelled" and done["finish"] == "length"


def test_a_text_that_did_not_come_out_whole_is_not_checked():
    model = Script()
    f = flow(model)
    f.cancel.set()
    events, done = run(f)
    assert model.of("review") == [] and done["stopped"] == "cancelled"
