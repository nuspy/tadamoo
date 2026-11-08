"""Text that has fallen into a loop: the test for a finished fragment, the watch on a stream, and what the agent loop does about it."""
import copy
import json

import pytest

from tadamoo.agent_loop import AgentLoop, DEGENERATE, FINAL
from tadamoo.output_guard import RepetitionAbort, RepetitionWatch, raised_penalty, repetition_dominated, trailing_period
from tadamoo.tools import create_standard_tool_registry

LOOP = "I think the answer is that we should consider the following option again. "


def prose(n):
    return " ".join(f"Sentence number {i} talks about topic {i * 7} and gives the result {i ** 2}." for i in range(n))


def test_a_phrase_repeated_over_most_of_a_fragment_is_a_loop():
    assert repetition_dominated(LOOP * 12)
    assert repetition_dominated("Okay. " + LOOP * 12 + "The end.")


def test_ordinary_text_is_not():
    assert not repetition_dominated(prose(40))
    assert not repetition_dominated(LOOP * 4)                       # four times is not five
    assert not repetition_dominated("")
    assert not repetition_dominated("word " * 50)                   # under 400 characters is never judged


def test_a_loop_must_cover_at_least_half_of_the_fragment():
    assert not repetition_dominated(prose(80) + LOOP * 6)
    assert repetition_dominated(prose(2) + LOOP * 12)


def test_tables_and_logs_with_different_lines_are_not_flagged():
    table = "\n".join(f"| row {i} | value {i * 3} | status ok | notes about thing {i} |\n|---|---|---|---|" for i in range(30))
    log = "\n".join(f"2026-10-29 12:00:{i:02d} INFO worker started with id {i} and is ready to serve" for i in range(50))
    assert not repetition_dominated(table) and not repetition_dominated(log)


def test_the_same_line_over_and_over_is_a_loop():
    assert repetition_dominated("\n".join(["ERROR connection refused to the database host at port 5432, retrying"] * 30))


def test_a_line_of_rules_or_blanks_is_not_a_repeat():
    assert not repetition_dominated(prose(10) + "\n" + "-" * 300 + "\n" + " " * 300)


def test_an_exact_loop_is_seen_within_a_few_thousand_characters_not_sixteen():
    watch = RepetitionWatch()
    for _ in range(200):
        if watch.feed("content", LOOP * 3):                       # 228 characters a piece
            break
    assert watch.kind == "content" and watch.found["by"] == "exact" and watch.found["unit_chars"] > 0
    assert 3_000 <= len(watch.text["content"]) < 3_000 + 1_500 + 228


def test_a_loose_loop_that_is_not_an_exact_copy_is_still_seen_at_16k_and_each_doubling():
    watch = RepetitionWatch(period_first=10 ** 9)                 # only the loose look
    for i in range(400):
        if watch.feed("content", f"{LOOP} variant {i % 3} of the same long sentence again. " * 2):
            break
    assert watch.kind == "content" and watch.found["by"] == "loose"
    assert len(watch.text["content"]) >= 16_384


def test_trailing_period_finds_the_unit_the_end_repeats_and_nothing_in_prose_or_rules():
    unit = "the river turned back to the same old bend and the same old mill. "
    assert trailing_period(prose(40) + unit * 3) == len(unit)
    assert trailing_period(prose(40) + unit * 2) == 0                                       # two copies are not enough for the reasoning
    assert trailing_period(prose(80)) == 0
    assert trailing_period("-" * 400 + " " * 400) == 0
    assert trailing_period(prose(40) + unit * 3, repeats=4) == 0


def test_the_answer_needs_one_copy_more_than_the_reasoning():
    unit = "the river turned back to the same old bend and the same old mill. "
    for kind, copies in (("reasoning", 3), ("content", 4)):
        watch = RepetitionWatch(period_first=500, step=100)
        assert not watch.feed(kind, prose(30))
        assert not watch.feed(kind, unit * (copies - 1) + "and so it ended differently, in a new place.")
        assert watch.feed(kind, unit * (copies + 1)), kind


def test_the_watch_can_be_turned_off_and_its_numbers_set():
    off = RepetitionWatch(enabled=False)
    assert not any(off.feed("content", LOOP * 3) for _ in range(200))
    late = RepetitionWatch(period_first=50_000, first=10 ** 9)
    assert not any(late.feed("content", LOOP * 3) for _ in range(150))


def test_a_text_that_is_fine_is_passed_and_looked_at_again_at_the_next_doubling():
    watch = RepetitionWatch(first=1000, tail=2000)
    seen = [watch.feed("content", f"Sentence number {i} says something new about topic {i * 7}, result {i ** 2}. ") for i in range(150)]
    assert not any(seen) and watch._next["content"] >= 8000


def test_the_answer_and_the_reasoning_are_watched_apart():
    watch = RepetitionWatch(first=1000, tail=2000)
    for _ in range(14):
        assert not watch.feed("content", prose(1) + " ")
    stopped = [watch.feed("reasoning", LOOP * 3) for _ in range(10)]
    assert any(stopped) and watch.kind == "reasoning"


def test_the_penalty_for_the_retry_is_a_step_above_the_sessions_own():
    assert raised_penalty(None) == 1.1 and raised_penalty(1.2) == 1.3 and raised_penalty(1.49) == 1.5 and raised_penalty(0.5) == 1.1


# -- in the agent loop -------------------------------------------------------------------------------------------------

class Streaming:
    """A model double: each step is (pieces to stream, the whole reply); the pieces go to the loop's `on_delta` first."""

    def __init__(self, *steps):
        self.steps, self.options, self.sent = list(steps), [], []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        self.options.append(kwargs)
        pieces, whole = self.steps.pop(0)
        for kind, piece in pieces:
            kwargs["on_delta"](kind, piece)
        return whole


def reply(text, calls=None):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


def looping(n=700):
    return [("content", LOOP)] * n


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def make(ws, client, shown, **kw):
    registry, _ = create_standard_tool_registry(ws)
    events = []
    return AgentLoop(client=client, tool_registry=registry, on_delta=lambda k, t: shown.append((k, t)), on_event=events.append, **kw), events


def test_a_stream_that_loops_is_cut_and_asked_again_once_with_a_higher_penalty(ws):
    shown = []
    client = Streaming((looping(), None), ([("content", "A calm answer.")], reply("A calm answer.")))
    agent, events = make(ws, client, shown)
    agent.run("go")
    assert agent.stop_reason == FINAL and client.options[1]["extra"]["repetition_penalty"] == 1.1 and "extra" not in client.options[0]
    assert [m["role"] for m in client.sent[1]] == ["system", "user"]                 # nothing of the loop is in the conversation
    assert any(e["type"] == "stream_restart" for e in events)
    assert sum(len(t) for k, t in shown if k == "content") < 16_384 + 400 + 14        # the stream was closed at the first look, not read to the end


def test_the_penalty_starts_from_the_sessions_own(ws):
    client = Streaming((looping(), None), ([], reply("ok")))
    agent, _ = make(ws, client, [], sampling={"repetition_penalty": 1.2})
    agent.run("go")
    assert client.options[1]["extra"]["repetition_penalty"] == 1.3


def test_a_second_loop_ends_the_run_with_a_notice(ws):
    client = Streaming((looping(), None), (looping(), None), ([], reply("never")))
    agent, events = make(ws, client, [])
    agent.run("go")
    assert agent.stop_reason == DEGENERATE and "repetition loop" in agent.error and len(client.sent) == 2
    assert [m["role"] for m in agent.messages] == ["system", "user"]
    assert events[-1] == {"type": "answer_degenerate"} or any(e["type"] == "answer_degenerate" for e in events)


def test_a_tool_call_that_was_being_written_when_the_loop_was_cut_never_runs(ws):
    call = [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "loop.txt", "content": "x"})}}]
    client = Streaming((looping(), reply("", call)), ([], reply("fine")))
    agent, _ = make(ws, client, [])
    agent.run("go")
    assert not (ws / "loop.txt").exists() and agent.stop_reason == FINAL


def test_without_a_stream_nothing_is_watched_and_the_loop_in_a_whole_reply_is_judged_when_the_limit_ended_it(ws):
    class Whole:
        def __init__(self):
            self.n = 0

        def chat_completion(self, messages, tools=None, **kwargs):
            self.n += 1
            assert "on_delta" not in kwargs
            return {"choices": [{"message": {"role": "assistant", "content": LOOP * 12}, "finish_reason": "length"}]}

    registry, _ = create_standard_tool_registry(ws)
    agent = AgentLoop(client=Whole(), tool_registry=registry)
    agent.run("go")
    assert agent.stop_reason == DEGENERATE and agent.client.n == 2


def test_the_abort_carries_what_was_looping():
    with pytest.raises(RepetitionAbort, match="reasoning"):
        raise RepetitionAbort("reasoning")
