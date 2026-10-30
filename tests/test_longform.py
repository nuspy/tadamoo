"""Long answers in segments: protected content, splitting, the checks, the plan, the run (scripted client, no model)."""
import json
import re
import threading

import pytest

from serve import longform as lf


# -- a client that plays a model --------------------------------------------------------------------------------------

class Model:
    """Answers by what the last user message asks; `script(call_no, messages)` may override with (text, finish)."""

    def __init__(self, script=None):
        self.calls = []
        self.script = script

    def chat_completion(self, messages, max_tokens=None, extra=None, timeout=0, on_delta=None, **_):
        self.calls.append({"messages": [dict(m) for m in messages], "max_tokens": max_tokens, "extra": extra})
        text, finish = self.script(len(self.calls), messages) if self.script else (self.reply(messages), "stop")
        if on_delta:
            on_delta("content", text)
        return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 40}}}

    @staticmethod
    def reply(messages):
        last = messages[-1]["content"]
        part = re.search(r"Part (\d+) of (\d+)", last)
        src = re.search(r"-----\n(.*?)\n-----", last, re.S)
        if src and "Source of this part" in last:
            return src.group(1).replace("the", "il")        # a "translation" that keeps every marker
        if not part:
            return "{}"
        words = int(re.search(r"about (\d+) words", last).group(1))
        return f"Part {part.group(1)} body. " + " ".join(f"w{part.group(1)}x{i}" for i in range(words))


def run(model, spec, **kw):
    la = lf.LongAnswer(model, spec, clock=iter(range(10 ** 6)).__next__, **kw)
    events = [e for e in la.run() if e is not None]
    return la, events, [e for e in events if e["event"] == "done"][0]


SOURCE = """# Title

Intro with an image ![chart](img/chart.png) and a [link](https://example.com/a?b=1) and `code`.
See https://example.org/page. Cited [S1, S2] and a note[^1].

```mermaid
graph TD
  A[Start] --> B[End]
```

| a | b |
|---|---|
| 1 | 2 |

$$
x = y^2
$$

<div class="box">raw html</div>

[ref]: https://example.net/ "t"

[^1]: The footnote text.
"""


# -- protected content ------------------------------------------------------------------------------------------------

def test_protect_and_restore_are_exact():
    masked, prot = lf.protect(SOURCE)
    for original in ("img/chart.png", "https://example.com/a?b=1", "graph TD", "x = y^2", "raw html", "https://example.org/page"):
        assert original not in masked
    assert "[link](⟦URL" in masked and "|---|---|" in masked and "[S1, S2]" not in masked
    assert prot.restore(masked) == SOURCE.replace("\r\n", "\n")
    kinds = {re.sub(r"\d+$", "", k) for k in prot.items}
    assert {"IMG", "URL", "CODE", "IC", "MATH", "HTML", "REF", "CITE", "FN"} <= kinds


def test_front_matter_and_unclosed_fence_and_tilde_fence():
    masked, prot = lf.protect("---\ntitle: x\n---\nBody\n\n~~~py\nprint(1)\n~~~\n\n```\nnever closed\nmore")
    assert masked.startswith("⟦FM1⟧") and "print" not in masked and "never closed" not in masked
    assert prot.restore(masked) == "---\ntitle: x\n---\nBody\n\n~~~py\nprint(1)\n~~~\n\n```\nnever closed\nmore"


def test_a_link_around_code_restores_in_one_go():
    masked, prot = lf.protect("[the `x` page](http://a.b/`y`)")
    assert prot.restore(masked) == "[the `x` page](http://a.b/`y`)"


def test_marks_with_stray_spaces_are_normalised_and_unknown_ones_dropped():
    masked, prot = lf.protect("see ![a](b.png) now")
    key = lf.marks_in(masked)[0]
    assert lf.normalize_marks(f"x ⟦ {key} ⟧ y") == f"x ⟦{key}⟧ y"
    assert prot.restore("a ⟦ZZ9⟧ b") == "a  b"


# -- splitting --------------------------------------------------------------------------------------------------------

def test_units_keep_tables_lists_and_headings_whole():
    text = "# H\n\npara one\nline two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- item\n\n  continued\n- item 2\n\nlast"
    units = lf.split_units(text)
    assert units == ["# H", "para one\nline two", "| a | b |\n|---|---|\n| 1 | 2 |", "- item\n\n  continued\n- item 2", "last"]


def test_pack_glues_headings_and_never_cuts_a_table():
    table = "\n".join(["| a | b |", "|---|---|"] + [f"| {i} | {i} |" for i in range(300)])
    para = " ".join(f"word{i}." for i in range(900))
    pieces = lf.pack(lf.split_units(f"# One\n\n{para}\n\n{table}\n\n## Two\n\nshort"), 400)
    assert any(table in p for p in pieces)
    assert all(not p.rstrip().endswith("## Two") for p in pieces)
    assert sum(lf.count_words(p) for p in pieces) == lf.count_words(f"# One\n\n{para}\n\n{table}\n\n## Two\n\nshort")
    assert pieces[0].startswith("# One")


# -- the checks -------------------------------------------------------------------------------------------------------

def codes(issues):
    return [i.code for i in issues]


def test_checks_find_each_problem():
    ok = ("Una frase normale della traduzione che non finisce in modo strano. Il gatto è nella casa e il cane non è con l'uomo che era là. "
          "Ogni parola di questo testo è diversa dalle altre e per questo non può essere una ripetizione, come si vede.")
    assert lf.check_segment(ok, expected_marks=[], language="it") == []
    assert "truncated" in codes(lf.check_segment("testo", finish="length"))
    assert "markers_missing" in codes(lf.check_segment("a ⟦IMG1⟧ b.", expected_marks=["IMG1", "URL2"]))
    assert "markers_extra" in codes(lf.check_segment("a ⟦IMG1⟧ ⟦IMG1⟧ b.", expected_marks=["IMG1"]))
    assert "markers_order" in codes(lf.check_segment("⟦CODE2⟧\n\n⟦CODE1⟧", expected_marks=["CODE1", "CODE2"]))
    assert "markers_order" not in codes(lf.check_segment("b ⟦URL2⟧ and ⟦URL1⟧.", expected_marks=["URL1", "URL2"]))
    assert "too_short" in codes(lf.check_segment("short text.", lo=50))
    assert "language" in codes(lf.check_segment("The quick brown fox jumps over the lazy dog and the cat is in the house of the man. " * 3, language="it"))
    assert "repeated" in codes(lf.check_segment("the same six words repeat here again. " * 30))
    assert "fence" in codes(lf.check_segment("```\ncode never closed"))
    assert "table" in codes(lf.check_segment("| a | b |\n|---|---|\n| 1 |", source=""))
    assert "table" in codes(lf.check_segment("| a | b | c |\n|---|---|---|", source="| a | b |\n|---|---|"))
    assert "cut_midword" in codes(lf.check_segment("This is a complete looking paragraph that stops in the mid"))
    assert "cut_midword" not in codes(lf.check_segment("# A heading without a stop"))
    assert "leaked" in codes(lf.check_segment("Certainly! Here is the text. " + "word " * 40))
    assert "empty" in codes(lf.check_segment("   "))


def test_translation_checks_numbers_headings_glossary_are_soft():
    src = "# Title\n\nWe sold 1,200 units of the engine."
    issues = lf.check_segment("Titolo\n\nAbbiamo venduto unità del motore.", source=src, mode="translate", glossary={"engine": "propulsore"})
    assert {"headings", "numbers", "glossary"} <= set(codes(issues))
    assert all(not i.hard for i in issues if i.code in ("headings", "numbers", "glossary"))


def test_seam_overlap_is_trimmed():
    prev = "one two three four five six seven eight nine ten eleven twelve"
    new = "five six seven eight nine ten eleven twelve and then more words"
    assert lf.trim_overlap(prev, new) == "and then more words"
    assert lf.trim_overlap(prev, "five six seven and fresh") == "five six seven and fresh"


def test_language_detection_and_names():
    en = "The cat is in the house and the dog is not with the man that was there for the day. " * 2
    it = "Il gatto è nella casa e il cane non è con l'uomo che era lì per tutto il giorno di oggi. " * 2
    assert lf.detect_language(en) == "en" and lf.detect_language(it) == "it" and lf.detect_language("short") == ""
    assert lf.language_code("Italiano") == "it" and lf.language_code("english") == "en" and lf.language_code("klingon") == ""


# -- the request ------------------------------------------------------------------------------------------------------

def test_spec_defaults_and_errors():
    spec = lf.Spec.from_request(True, "Write about rivers")
    assert (spec.mode, spec.task, spec.target_words, spec.retries, spec.protect) == ("write", "Write about rivers", 2000, 1, True)
    spec = lf.Spec.from_request({"mode": "translate", "target_language": "Italian"}, "Hello world")
    assert (spec.mode, spec.language, spec.source) == ("translate", "it", "Hello world")
    for bad, why in [({"mode": "poem"}, "mode"), ({"mode": "translate"}, "target_language"), ({"target_words": 5}, "target_words"),
                     ({"nope": 1}, "unknown field"), ({"retries": 9}, "retries"), ({"glossary": {"a": 1}}, "glossary"),
                     ({"mode": "rewrite"}, "instruction"), ({"outline": "x"}, "outline"), ({"mode": "translate", "target_language": "it", "outline": [{"heading": "a"}]}, "only for write")]:
        with pytest.raises(lf.LongformError, match=why):
            lf.Spec.from_request(bad, "text")
    with pytest.raises(lf.LongformError):
        lf.Spec.from_request({}, "")
    with pytest.raises(lf.LongformError):
        lf.Spec.from_request("yes", "x")


# -- when a request needs parts ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Write a report of 10,000 words on rivers", ("write", 10000)),
    ("Scrivi un racconto di 10.000 parole", ("write", 10000)),
    ("write an essay of 20 pages", ("write", 9000)),
    ("write 5k words about it", ("write", 5000)),
    ("Scrivimi il capitolo 3 del libro", ("write", 2500)),
    ("write a 500 word summary", None),
    ("Write a poem", None),
    ("what is a chapter in a book", None),
    ("Traduci in inglese: " + "parola " * 2500, ("translate", 2503)),
    ("Translate this: short text", None),
    ("riscrivi questo " + "testo " * 2000, ("rewrite", 2002)),
    ("Analizza questo documento: " + "testo " * 4000, ("write", 2001)),
    ("Scrivi un testo di 2100 parole sui fiumi d'Europa", ("write", 2100)),                  # Italian: 1.5 tokens a word, 3150 tokens
    ("Write a text of 2100 words about the rivers of Europe", None),                           # English: 1.35 tokens a word, 2835 tokens
    ("Write a text of 2300 words about the rivers of Europe", ("write", 2300)),
    ("Scrivi un testo di 1900 parole sui fiumi d'Europa", None),
    ("Write a chapter about the rivers of Europe", ("write", 2500)),
    ("Translate this into Italian: " + "the " * 2100, None),
    ("Translate this into Italian: " + "the " * 2300, ("translate", 2304)),
])
def test_detect_long_request(text, expected):
    found = lf.detect_long_request(text)
    assert (found and (found["mode"], found["words"])) == expected
    if found:
        assert found["parts"] == lf.chunk_count(found["words"])


def test_words_and_tokens_are_converted_in_one_place_by_language():
    assert lf.LONG_MIN_TOKENS == 3000
    assert lf.tokens_for_words(2000, "it") == 3000 and lf.tokens_for_words(2000, "en") == 2700 and lf.words_for_tokens(3000, "it") == 2000
    assert lf.tokens_for_words(1000, "") == lf.tokens_for_words(1000, "it") and lf.tokens_for_words(1000, "xx") == 1500
    assert lf.request_language("Write a text of words about the rivers of Europe") == "en"
    assert lf.request_language("Scrivi un testo di mille parole sui fiumi che attraversano l'Europa") == "it"
    assert lf.detect_long_request("Write a text of 2300 words about the rivers of Europe")["tokens"] == 3105
    assert lf.parse_hint("Write about rivers\n\n[long answer: mode=write]")["words"] == 2000


# -- the plan ---------------------------------------------------------------------------------------------------------

def test_chunk_count_keeps_parts_within_what_is_reliable():
    assert lf.SEGMENT_WORDS == 2000 and lf.CHUNK_WORDS == (700, 2500) and lf.CHUNK_MAX == 2500 and lf.SEGMENT_WORDS_RANGE[1] == 2500
    assert lf.chunk_count(10000) == 5 and lf.chunk_count(10000, 1500) == 7 and lf.chunk_count(300) == 1
    assert lf.chunk_count(10000, 100) == 14                      # parts are never smaller than 700 words
    assert lf.chunk_count(40000, 3000) == 16                     # nor above 2500
    for words in (2000, 5000, 12345, 90000):
        for size in (700, 1000, 2000, 2500, 3000):
            assert words / lf.chunk_count(words, size) <= lf.CHUNK_MAX


def test_a_plan_made_by_the_model_follows_the_same_part_size():
    segs = lf.chunks_from_outline(outline(*[1000] * 10), 10000)
    assert len(segs) == 5 and all(s.words == 2000 for s in segs)
    assert lf.Spec.from_request({"mode": "write", "task": "t", "target_words": 9000}, "").segment_words == 2000
    with pytest.raises(lf.LongformError, match="segment_words"):
        lf.Spec.from_request({"mode": "write", "task": "t", "segment_words": 2600}, "")


def outline(*sizes):
    return [{"heading": f"S{i}", "goal": f"goal {i}", "target_words": w, "key_points": [f"p{i}a", f"p{i}b"]} for i, w in enumerate(sizes, 1)]


def test_chunks_follow_the_words_of_the_sections():
    segs = lf.chunks_from_outline(outline(3000, 500, 500, 6000), 10000, 1500)
    assert len(segs) == 7 and sum(s.words for s in segs) in range(9995, 10006)
    assert [r[0] for r in segs[0].rows] == [1] and segs[0].rows[0][2] is True
    assert [r[0] for r in segs[2].rows] == [1, 2, 3, 4] and segs[2].rows[0][2] is False and segs[2].rows[1][2] is True
    seen = {r[0] for s in segs for r in s.rows}
    assert seen == {1, 2, 3, 4}
    assert sum(1 for s in segs for r in s.rows if r[2]) == 4                    # each section begins exactly once
    assert segs[1].goal.startswith("Continue: goal 1") and "p1a" in segs[1].points


def test_a_single_short_section_is_one_part():
    segs = lf.chunks_from_outline(outline(900), 900)
    assert len(segs) == 1 and segs[0].rows == [(1, "S1", True)]


def test_outline_parse_and_fallbacks():
    reply = 'Sure: {"title": "T", "sections": [{"heading": "A", "goal": "g", "target_words": 100, "key_points": ["x"]}, {"heading": "B", "goal": "h", "target_words": 300}]}'
    title, segs = lf.parse_outline(reply, 2, 4000)
    assert title == "T" and [s.words for s in segs] == [1000, 3000]
    assert lf.parse_outline("no json here", 2, 100) is None
    assert lf.parse_outline('{"sections": []}', 2, 100) is None


# -- the run: write ---------------------------------------------------------------------------------------------------

def spec_for(**kw):
    base = {"mode": "write", "task": "Write about rivers", "target_words": 3000, "segment_words": 1000, "outline": outline(1000, 1000, 1000)}
    base.update(kw)
    return lf.Spec.from_request(base, "")


def test_planned_text_is_written_part_by_part_with_the_plan_in_every_call():
    model = Model()
    la, events, done = run(model, spec_for(segment_words=1000))
    assert [e["event"] for e in events if e["event"] != "delta"][:2] == ["plan", "segment_start"]
    assert done["finish"] == "stop" and done["segments"] == 3 and done["of"] == 3 and done["stopped"] == ""
    assert len(model.calls) == 3                                           # no planning call: the model's plan was given
    for k, call in enumerate(model.calls, 1):
        system = call["messages"][0]
        assert system["role"] == "system" and "1. S1: goal 1 (about 1000 words)" in system["content"] and "3. S3" in system["content"]
        last = call["messages"][-1]["content"]
        assert f"Part {k} of 3." in last and f"goal {k}" in last and "Length: about 1000 words" in last
        assert call["extra"]["reasoning_effort"] == "none" and call["max_tokens"] >= 1000
    assert "Begin the text." in model.calls[0]["messages"][-1]["content"]
    assert "Do not conclude." in model.calls[1]["messages"][-1]["content"]
    assert "close the text properly" in model.calls[2]["messages"][-1]["content"]
    # the transcript layout: the next call holds the earlier parts as assistant turns
    assert [m["role"] for m in model.calls[2]["messages"]] == ["system", "user", "assistant", "user", "assistant", "user"]
    text = "".join(e["text"] for e in events if e["event"] == "delta")
    assert text == la.text() and text.count("Part 1 body.") == 1 and "Part 3 body." in text
    assert done["usage"]["calls"] == 3 and done["usage"]["cached_tokens"] == 120


def test_rolling_layout_carries_one_line_per_part_and_the_end_of_the_text():
    model = Model()
    la, events, done = run(model, spec_for(segment_words=1000, context_tokens=2000))
    last = model.calls[2]["messages"]
    assert [m["role"] for m in last] == ["system", "user"]
    body = last[-1]["content"]
    assert "Already written, one line per part:\n1. S1 (done)\n2. S2 (done)" in body
    assert "The text so far ends with" in body and "w2x" in body
    assert any(e["event"] == "segment_start" and e["layout"] == "rolling" for e in events)


def test_the_model_plans_when_no_outline_is_given():
    outline_reply = json.dumps({"title": "Rivers", "sections": [{"heading": h, "goal": "g", "target_words": 1000, "key_points": []} for h in "ABC"]})

    def script(n, messages):
        return (outline_reply, "stop") if n == 1 else (Model.reply(messages), "stop")
    model = Model(script)
    spec = lf.Spec.from_request({"mode": "write", "task": "rivers", "target_words": 3000, "segment_words": 1000}, "")
    la, events, done = run(model, spec)
    assert done["segments"] == 3 and len(model.calls) == 4
    assert "Title: Rivers" in model.calls[1]["messages"][0]["content"]
    plan = [e for e in events if e["event"] == "plan"][0]
    assert not plan["fallback"] and [s["heading"] for s in plan["segments"]] == ["A", "B", "C"]


def test_a_plan_that_cannot_be_read_falls_back_to_even_parts_and_says_so():
    def script(n, messages):
        return ("I cannot do JSON", "stop") if n <= 2 else (Model.reply(messages), "stop")
    la, events, done = run(Model(script), lf.Spec.from_request({"mode": "write", "task": "t", "target_words": 2000, "segment_words": 1000}, ""))
    assert [e for e in events if e["event"] == "plan"][0]["fallback"] is True and done["segments"] == 2


def test_a_cut_part_is_continued_and_a_part_that_stays_cut_is_reported():
    def script(n, messages):
        if n == 1:
            return ("First half of the text ", "length")
        if n == 2:
            assert messages[-2] == {"role": "assistant", "content": "First half of the text "} and "Continue exactly" in messages[-1]["content"]
            return ("and the second half of the text, whole and finished properly.", "stop")
        return ("Cut again ", "length")
    la, events, done = run(Model(script), spec_for(outline=outline(1000, 1000), target_words=2000, retries=0))
    assert la.parts[0] == "First half of the text and the second half of the text, whole and finished properly."
    assert done["finish"] == "length"                                      # never delivered as complete
    assert [w["code"] for w in done["warnings"]].count("truncated") == 1 and "truncated" in {w["code"] for w in done["warnings"]}


def test_a_part_cut_inside_a_word_goes_on_from_the_word_before_it():
    sent = []

    def script(n, messages):
        if n == 1:
            return ("The international rel", "length")
        sent.append(messages[-2]["content"])
        return ("relations of states are old, and the rules that hold them together are older still, as every student of the subject learns.", "stop")
    la, events, done = run(Model(script), spec_for(outline=outline(1000), target_words=1000, retries=0))
    assert sent == ["The international "]
    assert la.parts[0].startswith("The international relations of states are old") and "relrelations" not in la.parts[0]


def test_a_short_part_is_asked_again_once_with_the_reason():
    def script(n, messages):
        if n == 1:
            return ("tiny.", "stop")
        assert "Correction" in messages[-1]["content"] and "too_short" in messages[-1]["content"]
        return (Model.reply(messages), "stop")
    model = Model(script)
    la, events, done = run(model, spec_for(outline=outline(1000), target_words=1000))
    assert len(model.calls) == 2 and done["warnings"] == [] and done["segments"] == 1
    assert [e for e in events if e["event"] == "segment_done"][0]["retries"] == 1


def test_a_text_that_keeps_coming_short_stops_visibly():
    model = Model(lambda n, m: ("Short part that is far too small to count.", "stop"))
    la, events, done = run(model, spec_for(retries=0, outline=outline(*[1000] * 5), target_words=5000))
    assert done["stopped"] == "short_segments" and done["finish"] == "length" and done["segments"] == 2 and done["of"] == 5
    assert "stopped_short" in {w["code"] for w in done["warnings"]}


def test_a_repeated_part_is_not_kept_silently():
    body = "This paragraph is long enough to count as a real paragraph of the first part of the text, and it says something. "
    def script(n, messages):
        return (body * 3 + "w " * 800, "stop")
    la, events, done = run(Model(script), spec_for(retries=0, outline=outline(1000, 1000), target_words=2000))
    assert "duplicate" in {w["code"] for w in done["warnings"]}


def test_cancel_between_parts_returns_what_was_written():
    cancel = threading.Event()
    model = Model()
    orig = model.chat_completion

    def once(*a, **k):
        r = orig(*a, **k)
        cancel.set()
        return r
    model.chat_completion = once
    la, events, done = run(model, spec_for(), cancel=cancel)
    assert done["stopped"] == "cancelled" and done["segments"] <= 1 and done["finish"] == "length"
    assert len(model.calls) == 1


def test_a_model_error_is_raised_after_the_delivered_parts():
    def script(n, messages):
        if n == 2:
            raise RuntimeError("engine died")
        return (Model.reply(messages), "stop")
    seen = []
    la = lf.LongAnswer(Model(script), spec_for(retries=0))
    with pytest.raises(RuntimeError, match="engine died"):
        for ev in la.run():
            seen.append(ev)
    assert any(e and e["event"] == "delta" for e in seen)


def test_system_and_earlier_turns_ride_ahead_of_the_parts():
    model = Model()
    prior = [{"role": "user", "content": "context question"}, {"role": "assistant", "content": "context answer"}]
    run(model, spec_for(outline=outline(1000), target_words=1000), prior=prior, system="Be terse.")
    msgs = model.calls[0]["messages"]
    assert msgs[0]["content"].startswith("Be terse.") and msgs[1:3] == prior


# -- the run: translate and rewrite -----------------------------------------------------------------------------------

def test_translation_carries_protected_content_through_unchanged():
    src = SOURCE + "\n\n" + "\n\n".join(f"Paragraph {i} of the text is here, with the words and the rest of it." for i in range(150))
    model = Model()
    spec = lf.Spec.from_request({"mode": "translate", "target_language": "it", "segment_words": 400}, src)
    la, events, done = run(model, spec)
    out = la.text()
    assert done["segments"] > 2 and done["finish"] == "stop"
    for original in ("![chart](img/chart.png)", "https://example.com/a?b=1", "```mermaid\ngraph TD\n  A[Start] --> B[End]\n```",
                     "$$\nx = y^2\n$$", '<div class="box">raw html</div>', "[S1, S2]", "note[^1].", "`code`"):
        assert out.count(original) == 1, original
    assert "⟦" not in out and "il" in out
    assert not [w for w in done["warnings"] if w["code"] in ("markers_missing", "markers_lost")]
    first = model.calls[0]["messages"][-1]["content"]
    assert "⟦" in first and "img/chart.png" not in first and "example.com" not in first


def test_a_dropped_marker_is_asked_again_and_a_persistent_one_is_reported_and_put_back():
    src = "Before the image.\n\n![pic](p.png)\n\nAfter the image, with enough words to be a part of the text."
    calls = []

    def once(n, messages):
        calls.append(n)
        text = Model.reply(messages)
        return (re.sub(r"⟦IMG\d+⟧", "", text), "stop") if n == 1 else (text, "stop")
    spec = lf.Spec.from_request({"mode": "translate", "target_language": "it"}, src)
    la, events, done = run(Model(once), spec)
    assert len(calls) == 2 and la.text().count("![pic](p.png)") == 1 and done["warnings"] == []

    always = Model(lambda n, m: (re.sub(r"⟦IMG\d+⟧", "", Model.reply(m)), "stop"))
    la, events, done = run(always, spec)
    assert la.text().count("![pic](p.png)") == 1                           # put back at the end of its part, never lost
    assert "markers_lost" in {w["code"] for w in done["warnings"]}
    assert [e for e in events if e["event"] == "segment_done"][0]["retries"] == 1


def test_protection_can_be_turned_off():
    spec = lf.Spec.from_request({"mode": "rewrite", "instruction": "shorter", "protect": False}, "A [link](http://x.y) here with words enough.")
    model = Model()
    run(model, spec)
    assert "http://x.y" in model.calls[0]["messages"][-1]["content"]


def test_a_source_that_needs_too_many_parts_is_refused_up_front():
    spec = lf.Spec.from_request({"mode": "translate", "target_language": "it", "segment_words": 200, "max_segments": 2}, "word " * 2000)
    with pytest.raises(lf.LongformError, match="over the limit"):
        list(lf.LongAnswer(Model(), spec).run())


def test_the_control_line_is_read_and_removed():
    got = lf.parse_hint("Write about rivers\n\n[long answer: mode=write; words=4000; language=Italian]")
    assert got["text"] == "Write about rivers" and (got["mode"], got["words"], got["parts"]) == ("write", 4000, 2) and got["fields"]["language"] == "Italian"
    assert lf.parse_hint("Translate\n\n[long answer: mode=bogus]")["mode"] == "write"
    assert lf.parse_hint("a message about [long answer: x] in the middle\nand more") is None
    assert lf.parse_hint("no line") is None


def test_cancel_while_planning_ends_cleanly():
    cancel = threading.Event()
    cancel.set()
    la = lf.LongAnswer(Model(lambda n, m: ("{}", "stop")), lf.Spec.from_request({"mode": "write", "task": "t", "target_words": 3000}, ""), cancel=cancel)
    events = [e for e in la.run() if e]
    assert events[-1]["event"] == "done" and events[-1]["stopped"] == "cancelled" and events[-1]["segments"] == 0
