"""The four levels of summary, with a model that answers by rule (no real model): what each level asks of the model, that long texts go by parts,
that an answer that is too long, empty or looping is asked again, that a cut-off answer halves its part, and that the stop of the person ends the work."""
import re

import pytest

from serve import summarylevels as sl
from tadamoo import digest

NAMES = ("library", "school", "bridge", "clinic", "market", "harbour", "stadium")


def para(i):
    return " ".join(f"Item {i}-{j}: the committee met on day {j} and approved {i * 100 + j} euros for the {NAMES[(i + j) % 7]}." for j in range(12))


def text(n):
    return "\n\n".join(f"## Part {i}\n" + para(i) for i in range(n))


class Model:
    """Writes the first `share` of what it is given, and records what it was asked."""

    def __init__(self, share=0.4, fail=None):
        self.share, self.calls, self.fail = share, [], fail

    def __call__(self, instructions, body, max_tokens):
        self.calls.append((instructions, body, max_tokens))
        if self.fail:
            out = self.fail(len(self.calls), body)
            if out is not None:
                return out
        cut = body[: max(1, int(len(body) * self.share))]
        return cut[: cut.rfind(" ")] if " " in cut else cut                      # at a word: a heading is never cut in the middle


def test_the_levels_are_the_four_asked_for():
    assert sl.LEVELS == ("integral", "medium", "high", "executive")
    with pytest.raises(ValueError):
        sl.summarize(Model(), "x", "tiny", 8000)
    with pytest.raises(ValueError):
        sl.summarize(Model(), "  ", "medium", 8000)


def test_integral_and_medium_go_part_by_part_in_order_and_keep_each_part_shorter():
    body = text(30)
    model = Model(share=0.5)
    out = sl.summarize(model, body, "medium", 4000)
    assert out["parts"] > 1 and out["parts"] == len(model.calls)
    assert out["chars_in"] == len(body.strip()) and out["chars_out"] < out["chars_in"]
    assert all(call[0].startswith("You summarise a text part by part") for call in model.calls)
    heads = [int(m) for m in re.findall(r"## Part (\d+)$", out["summary"], re.M)]
    assert heads == sorted(heads) and heads[0] == 0


def test_a_short_text_is_one_call_at_every_level():
    for level in sl.LEVELS:
        model = Model()
        out = sl.summarize(model, text(1), level, 30000)
        assert len(model.calls) == 1 and out["parts"] == 1 and out["level"] == level and out["summary"]


def test_executive_and_high_ask_for_their_form_and_size():
    model = Model()
    sl.summarize(model, text(1), "executive", 30000)
    assert "at most 7 bullet points" in model.calls[0][0] and model.calls[0][2] == sl.EXEC_TOKENS
    model = Model()
    sl.summarize(model, text(20), "high", 30000)
    assert "one fifth of its length" in model.calls[0][0] and sl.HIGH_MIN_TOKENS <= model.calls[0][2] <= sl.HIGH_MAX_TOKENS


def test_a_text_larger_than_the_session_is_read_in_rounds_for_the_high_levels():
    body = text(80)
    model = Model(share=0.1)
    out = sl.summarize(model, body, "executive", 3000)
    assert out["parts"] > 1 and len(model.calls) > out["parts"]                  # the notes of the parts, then the final call
    assert any(digest.is_part(c[0]) for c in model.calls) and not digest.is_part(model.calls[-1][0])


def test_an_answer_that_is_too_long_empty_or_looping_is_asked_again_with_what_was_wrong():
    body = text(3)

    def bad_then_good(n, part):
        return {1: "", 2: "same line of words that repeats. " * 40, 3: part}.get(n)

    model = Model(share=0.3, fail=bad_then_good)
    out = sl.summarize(model, body, "medium", 30000)
    assert len(model.calls) == 3                                                   # the third answer is the longest, but the best there is
    assert "text is empty" in model.calls[1][0] and "repeats itself" in model.calls[2][0]
    assert out["summary"] == body.strip()


def test_a_part_nobody_could_write_is_kept_as_it_is():
    body = text(1)
    model = Model(fail=lambda n, part: "")
    assert sl.summarize(model, body, "integral", 30000)["summary"] == body.strip()


def test_a_cut_off_answer_halves_its_part():
    body = text(20)
    seen = {"first": True}

    def cut(n, part):
        if seen["first"] and len(part) > 5000:
            seen["first"] = False
            raise digest.Truncated()
        return None

    out = sl.summarize(Model(share=0.4, fail=cut), body, "medium", 30000)
    assert out["summary"] and out["parts"] == 1


def test_the_language_asked_for_goes_to_the_model_and_the_stop_ends_the_work():
    model = Model()
    sl.summarize(model, text(1), "medium", 30000, language="Hungarian")
    assert model.calls[0][0].endswith("Write the result in Hungarian.")

    class Job:
        def __init__(self):
            self.n = 0

        def progress(self, done, total):
            self.n = done

        def stopped(self):
            return self.n >= 1

    with pytest.raises(digest.Stopped):
        sl.summarize(Model(), text(40), "medium", 4000, job=Job())
