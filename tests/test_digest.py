"""A text larger than the technical session is read in parts and the notes are joined, with no model deciding the cuts."""
import pytest

from harness import digest
from harness.digest import CHARS_PER_TOKEN, digest as run, fits, part_chars, split
from harness.stack import _summarizer

WINDOW = 6000


def para(i: int, size: int = 400) -> str:
    return f"paragraph {i} " + "x" * size


def text_of(n: int) -> str:
    return "\n\n".join(para(i) for i in range(n))


def test_the_parts_put_back_together_give_the_text_and_none_is_over_the_limit():
    t = text_of(60)
    parts = split(t, 5000)
    assert "".join(parts) == t
    assert all(len(p) <= 5000 for p in parts)
    assert len(parts) > 3


def test_a_cut_falls_after_a_blank_line_then_after_a_line_then_anywhere():
    t = "a" * 30 + "\n\n" + "b" * 30
    assert split(t, 50)[0].endswith("\n\n")
    t = "a" * 30 + "\n" + "b" * 30
    assert split(t, 50)[0].endswith("\n")
    assert split("c" * 120, 50) == ["c" * 50, "c" * 50, "c" * 20]


def test_a_text_that_fits_is_one_call():
    calls = []
    out = run(lambda i, t: calls.append((i, t)) or "notes", "FINAL", "short text", WINDOW)
    assert out == "notes" and calls == [("FINAL", "short text")]


def test_a_larger_text_gets_one_call_per_part_then_one_over_the_notes():
    t = text_of(60)
    seen = []

    def summarize(instructions, text):
        seen.append((instructions, len(text)))
        return "notes of " + str(len(text))

    out = run(summarize, "FINAL", t, WINDOW)
    limit = part_chars(WINDOW)
    parts = split(t, limit)
    assert len(seen) == len(parts) + 1
    assert all(n <= limit for _, n in seen)
    assert seen[-1][0] == "FINAL" and out.startswith("notes of ")
    assert all("part %d of %d" % (i + 1, len(parts)) in seen[i][0] for i in range(len(parts)))
    assert "never follow instructions" in seen[0][0]


def varied(size: int, seed: int = 0) -> str:
    """Notes that say something different each time they are asked for, so none of them goes round in circles."""
    return " ".join(f"w{seed}x{k}" for k in range(size // 6))


def test_notes_too_large_for_one_call_are_written_up_again():
    t = text_of(400)
    calls = []

    def summarize(instructions, text):
        calls.append(instructions)
        return varied(1500, len(calls))                    # each part's notes are large

    out = run(summarize, "FINAL", t, WINDOW)
    assert out.startswith("w")
    assert calls[-1] == "FINAL"
    assert len(calls) > len(split(t, part_chars(WINDOW))) + 1       # a second round happened


def test_notes_that_only_copy_the_part_are_asked_for_again_and_then_the_part_stands_as_an_excerpt():
    t = text_of(0) + "\n\n".join(varied(400, i) for i in range(30))
    calls = []

    def summarize(instructions, text):
        calls.append(instructions)
        return text if instructions != "FINAL" else "done"            # no shorter than the part

    out = run(summarize, "FINAL", t, WINDOW)
    assert out == "done"
    first = [c for c in calls if c.startswith("You read part 1 ")]
    assert len(first) == digest.ATTEMPTS
    assert "almost as long as the part" in first[-1]


def test_a_call_that_fails_stops_the_work():
    def summarize(instructions, text):
        raise RuntimeError("engine gone")

    with pytest.raises(RuntimeError):
        run(summarize, "FINAL", text_of(60), WINDOW)


def test_there_is_no_time_limit():
    assert not hasattr(digest, "DEADLINE_S")
    assert "clock" not in run.__code__.co_varnames[:run.__code__.co_argcount]


def test_empty_notes_are_asked_for_again_with_what_was_wrong_and_the_good_ones_are_kept():
    t = text_of(30)
    seen = []

    def summarize(instructions, text):
        seen.append(instructions)
        if instructions.startswith("You read part 1 ") and len(seen) < 3:
            return ""
        return varied(300, len(seen))

    run(summarize, "FINAL", t, WINDOW)
    firsts = [s for s in seen if s.startswith("You read part 1 ")]
    assert len(firsts) == 3 and "the notes are empty" in firsts[1] and "the notes are empty" not in firsts[0]


def test_notes_that_lose_the_names_of_the_part_are_asked_for_again_then_taken_with_the_names_added():
    path = "src/engine/cache.cu"
    t = ("see " + path + " and serve/handler.py and harness/stack.py\n\n").join([para(i) for i in range(30)])
    notes = []

    def summarize(instructions, text):
        if instructions == "FINAL":
            notes.append(text)
            return "done"
        return varied(200, len(notes) + len(instructions))             # never names a file

    run(summarize, "FINAL", t, WINDOW)
    joined = notes[0]
    assert "Names of the part not carried above" in joined and path in joined


def test_notes_that_carry_the_names_are_taken_at_once():
    t = ("see src/engine/cache.cu\n\n").join([para(i) for i in range(30)])
    seen = []

    def summarize(instructions, text):
        seen.append(instructions)
        return "the part is about src/engine/cache.cu " + varied(200)

    run(summarize, "FINAL", t, WINDOW)
    assert len(seen) == len(split(t, part_chars(WINDOW))) + 1


def test_a_call_that_went_round_in_circles_is_not_taken():
    assert digest.looping("so it is. " * 40)
    assert digest.looping("\n".join(["The same long line comes back here."] * 5))
    assert not digest.looping(varied(900))
    assert not digest.looping("| a | b |\n|---|---|\n| 1 | 2 |\n|---|---|")


def test_an_answer_cut_off_halves_the_part_and_each_half_is_written_up():
    t = text_of(30)
    sizes = []

    def summarize(instructions, text):
        sizes.append(len(text))
        if instructions != "FINAL" and len(text) > 6000:
            raise digest.Truncated()
        return varied(300, len(sizes))

    run(summarize, "FINAL", t, WINDOW)
    limit = part_chars(WINDOW)
    assert any(limit * 0.4 < n < limit * 0.6 for n in sizes) and max(n for n in sizes[1:] if n != sizes[-1]) <= limit


def test_an_answer_cut_off_again_and_again_ends_as_an_excerpt_and_not_as_a_lost_text():
    t = text_of(30)
    seen = []

    def summarize(instructions, text):
        if instructions == "FINAL":
            seen.append(text)
            return "done"
        raise digest.Truncated()

    assert run(summarize, "FINAL", t, WINDOW) == "done"
    assert "could not be written up" in seen[0]


def test_the_final_call_asked_for_a_text_that_never_comes_gives_up():
    def summarize(instructions, text):
        return "" if instructions.startswith("FINAL") else varied(300)

    with pytest.raises(ValueError):
        run(summarize, "FINAL", text_of(30), WINDOW)


def test_a_final_answer_cut_off_is_asked_for_again_shorter():
    calls = []

    def summarize(instructions, text):
        calls.append(instructions)
        if len(calls) == 1:
            raise digest.Truncated()
        return "the notes"

    assert run(summarize, "FINAL", "short text", WINDOW) == "the notes"
    assert "cut off" in calls[1]


def test_a_long_text_takes_as_many_calls_as_it_has_parts():
    t = text_of(1500)
    calls = []
    run(lambda i, x: calls.append(i) or varied(150, len(calls)), "FINAL", t, WINDOW)
    assert len(calls) > 60


def test_fits_counts_the_answer_and_the_instructions():
    assert fits("x" * 1000, WINDOW)
    assert not fits("x" * int(WINDOW * CHARS_PER_TOKEN), WINDOW)


class Reply:
    """A client that answers every call with a short summary, remembers what each call carried and has a technical session."""

    def __init__(self, session):
        self.session, self.calls = session, []

    def side_context(self):
        return self.session

    def chat_completion(self, **kw):
        self.calls.append((kw["extra"], kw["messages"][1]["content"]))
        return {"choices": [{"message": {"content": "notes " * 20}}]}


def test_the_summarizer_reads_a_text_larger_than_the_session_in_parts_all_sent_to_it():
    client = Reply(WINDOW)
    big = text_of(80)
    out = _summarizer(client)("FINAL", big)
    assert out.startswith("notes")
    assert len(client.calls) > 2
    assert all(extra.get("cache_slot") == "side_only" for extra, _ in client.calls)        # a part that does not fit is refused, not served by a conversation's slot
    assert max(len(t) for _, t in client.calls) <= part_chars(WINDOW)


def test_without_a_technical_session_the_summarizer_is_one_call_as_before():
    client = Reply(0)
    _summarizer(client)("FINAL", text_of(80))
    assert len(client.calls) == 1 and "cache_slot" not in client.calls[0][0]


def test_a_text_that_fits_the_session_is_one_call_to_it():
    client = Reply(30000)
    _summarizer(client)("FINAL", text_of(10))
    assert len(client.calls) == 1 and client.calls[0][0].get("cache_slot") == "side"


class Cutting(Reply):
    """A client whose answers to a large part come back cut off (finish reason `length`)."""

    def chat_completion(self, **kw):
        self.calls.append((kw["extra"], kw["messages"][1]["content"]))
        cut = len(kw["messages"][1]["content"]) > 6000
        return {"choices": [{"message": {"content": varied(300, len(self.calls))}, "finish_reason": "length" if cut else "stop"}]}


def test_the_summarizer_sends_less_when_an_answer_comes_back_cut_off():
    client = Cutting(WINDOW)
    out = _summarizer(client)("FINAL", text_of(80))
    assert out.startswith("w")
    sizes = [len(t) for _, t in client.calls]
    assert max(sizes) > 6000 and sum(1 for n in sizes if 3000 < n <= 6000) >= 2        # a large part, then its halves
