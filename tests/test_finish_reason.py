"""How the agent loop reads the way a model reply ended: cut by the output limit, empty after thinking, cut inside a tool call."""
import copy
import json

import pytest

from harness.agent_loop import (AgentLoop, CANCELLED, DEGENERATE, EMPTY, FINAL, LENGTH, MAX_CONTINUATIONS, answer_text)
from harness.audit import AuditLedger
from harness.client import BasaltClient, _collect_stream
from harness.eval.report import generate_eval_report, summarize
from harness.eval.runner import EvalResult, Trial
from harness.tools import create_standard_tool_registry


class Client:
    """Plays back canned replies and records what the loop sent, with the options of each request."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.sent, self.options = [], []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        self.options.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def reply(text="", finish="stop", reasoning="", calls=None, usage=None, **top):
    message = {"role": "assistant", "content": text}
    if reasoning:
        message["reasoning_content"] = reasoning
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": usage or {}, **top}


def tool_call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def make(ws, client, **kw):
    registry, _ = create_standard_tool_registry(ws)
    events = []
    agent = AgentLoop(client=client, tool_registry=registry, on_event=events.append, **kw)
    return agent, events


def wire_text(messages):
    return [m.get("content") for m in messages]


# -- a reply cut by the output limit ----------------------------------------------------------------------------------

def test_a_cut_answer_is_asked_to_go_on_and_the_pieces_join(ws, tmp_path):
    audit = AuditLedger(tmp_path / "audit.jsonl")
    client = Client(reply("The first half of the", "length"), reply("the answer, and the rest."))
    agent, events = make(ws, client, audit=audit)
    turns = agent.run("write it")
    assert agent.stop_reason == FINAL and not agent.cut and agent.length_stops == 1 and len(turns) == 2
    second = client.sent[1]
    assert second[-2] == {"role": "assistant", "content": "The first half of "}                # the unfinished last word is written again by the model
    assert "Continue exactly where you stopped" in second[-1]["content"] and second[-1]["role"] == "user"
    assert client.sent[0] == second[:len(client.sent[0])]                      # the cached prefix is untouched: the note is at the tail
    assert turns[0].cut and turns[1].continues and not turns[1].cut
    assert answer_text(turns) == "The first half of the answer, and the rest."
    cut = [e for e in events if e["type"] == "answer_truncated"]
    assert cut == [{"type": "answer_truncated", "final": False, "continuation": 1, "of": MAX_CONTINUATIONS, "degenerate": False}]
    assert [r["details"]["final"] for r in audit.records if r["action"] == "answer_truncated"] == [False]


def test_the_pieces_join_at_a_word_boundary_without_a_doubled_space_or_a_repeat(ws):
    cut = "Rome was not built in a day, and the people who built it knew that every wall needed patience, so they began with the foundations and went up"
    client = Client(reply(cut, "length"), reply("went up one course at a time.\n\nThe second paragraph.", "length"), reply("paragraph.\n\nThird."))
    agent, _ = make(ws, client)
    turns = agent.run("write it")
    assert client.sent[1][-2]["content"] == cut[:-len("up")]                       # kept up to the last whitespace: "up" is written again
    assert answer_text(turns) == cut[:-len("went up")] + "went up one course at a time.\n\nThe second paragraph.\n\nThird."
    assert "went up went up" not in answer_text(turns) and "\n\n\n" not in answer_text(turns)


def test_a_cut_in_the_middle_of_a_word_is_finished_by_the_word_written_again(ws):
    client = Client(reply("The international rel", "length"), reply("relations of states are old."))
    agent, _ = make(ws, client)
    turns = agent.run("go")
    assert client.sent[1][-2]["content"] == "The international "
    assert answer_text(turns) == "The international relations of states are old."


def test_the_note_is_marked_so_it_never_counts_as_a_request(ws):
    client = Client(reply("part", "length"), reply(" more"))
    agent, _ = make(ws, client)
    agent.run("go")
    notes = [m for m in agent.messages if m.get("note")]
    assert len(notes) == 1 and "[harness]" in notes[0]["content"]


def test_at_most_two_continuations_then_the_answer_is_delivered_flagged_cut(ws, tmp_path):
    audit = AuditLedger(tmp_path / "audit.jsonl")
    client = Client(reply("one ", "length"), reply("two ", "length"), reply("three ", "length"), reply("never asked"))
    agent, events = make(ws, client, audit=audit)
    turns = agent.run("go")
    assert MAX_CONTINUATIONS == 2 and len(client.sent) == 3 and len(client.steps) == 1
    assert agent.stop_reason == FINAL and agent.cut and agent.length_stops == 3
    assert answer_text(turns) == "one two three"
    finals = [e["final"] for e in events if e["type"] == "answer_truncated"]
    assert finals == [False, False, True]
    assert [r["details"]["final"] for r in audit.records if r["action"] == "answer_truncated"] == [False, False, True]


def test_the_allowance_is_per_request(ws):
    client = Client(reply("a", "length"), reply("b", "length"), reply("c", "length"), reply("d", "length"), reply("e", "length"), reply("f"))
    agent, _ = make(ws, client)
    agent.run("first")
    assert agent.cut
    client.steps[:] = [reply("x", "length"), reply("y")]
    agent.run("second")
    assert not agent.cut and agent.stop_reason == FINAL and len(client.sent) == 3 + 2


def test_a_cut_answer_that_is_a_repetition_loop_is_not_continued(ws):
    loop = "I think the answer is that we should consider the following option again. " * 12
    client = Client(reply(loop, "length"), reply("a calm answer"), reply("never"))
    agent, events = make(ws, client)
    agent.run("go")
    # the loop was asked again once with a higher repetition penalty, and the calm answer was delivered
    assert client.options[1]["extra"]["repetition_penalty"] == 1.1 and agent.stop_reason == FINAL and not agent.cut
    assert any(e["type"] == "stream_restart" for e in events)


def test_a_cut_loop_that_comes_again_ends_the_run_as_degenerate_and_keeps_nothing(ws):
    loop = "I think the answer is that we should consider the following option again. " * 12
    client = Client(reply(loop, "length"), reply(loop, "length"))
    agent, events = make(ws, client)
    agent.run("go")
    assert agent.stop_reason == DEGENERATE and "repetition loop" in agent.error and len(client.sent) == 2
    assert [m["role"] for m in agent.messages] == ["system", "user"]            # what was written is not kept


def test_a_second_loop_in_the_same_request_ends_the_run_as_degenerate(ws):
    loop = "I think the answer is that we should consider the following option again. " * 12
    client = Client(reply(loop, "length"), reply("calm part", "length"), reply(loop, "length"))
    agent, events = make(ws, client)
    agent.run("go")
    # the first loop used the one retry of the request; the calm cut reply is continued; the next loop has no retry left
    assert len(client.sent) == 3 and agent.stop_reason == DEGENERATE


def test_an_answer_that_ended_normally_is_not_flagged(ws):
    agent, events = make(ws, Client(reply("done")))
    agent.run("go")
    assert agent.stop_reason == FINAL and not agent.cut and agent.length_stops == 0
    assert not [e for e in events if e["type"] == "answer_truncated"]


# -- a tool call cut inside its arguments -----------------------------------------------------------------------------

def test_a_tool_call_cut_in_its_arguments_never_runs_and_is_asked_again_with_more_room(ws, tmp_path):
    audit = AuditLedger(tmp_path / "audit.jsonl")
    cut = reply("", "length", calls=[tool_call("write_file", '{"path": "a.txt", "content": "half of a lo')], usage={"completion_tokens": 4096})
    good = reply("", "tool_calls", calls=[tool_call("write_file", {"path": "a.txt", "content": "whole"}, "c2")])
    client = Client(cut, good, reply("done"))
    agent, events = make(ws, client, audit=audit)
    agent.run("write a")
    assert (ws / "a.txt").read_text(encoding="utf-8") == "whole"
    assert [o.get("max_tokens") for o in client.options] == [None, 8192, 8192]           # asked again with twice what the cut reply had written; kept for the request
    assert len(client.sent[0]) == len(client.sent[1])                                       # the cut reply was not added to the conversation
    assert agent.stop_reason == FINAL and agent.length_stops == 1
    assert [e["attempt"] for e in events if e["type"] == "tool_call_cut" and not e["final"]] == [1]
    assert [r["details"]["attempt"] for r in audit.records if r["action"] == "tool_call_cut" and not r["details"]["final"]] == [1]


def test_the_ceiling_setting_is_doubled_from_what_it_was(ws):
    cut = reply("", "length", calls=[tool_call("write_file", '{"path": "a.txt", "content": "ha')])
    client = Client(cut, cut, reply("", "tool_calls", calls=[tool_call("write_file", {"path": "a.txt", "content": "ok"})]), reply("done"))
    agent, _ = make(ws, client, max_tokens=1000)
    agent.run("go")
    assert [o.get("max_tokens") for o in client.options] == [1000, 4096 * 2, 4096 * 4, 4096 * 4]


def test_a_tool_call_that_stays_cut_after_three_retries_ends_the_run_and_nothing_runs(ws):
    cut = reply("", "length", calls=[tool_call("write_file", '{"path": "a.txt", "content": "ha')], usage={"completion_tokens": 100})
    client = Client(cut, cut, cut, cut, reply("never"))
    agent, events = make(ws, client)
    turns = agent.run("go")
    assert agent.stop_reason == LENGTH and "cut by the output limit" in agent.error and len(client.sent) == 4
    assert not (ws / "a.txt").exists() and agent.length_stops == 4
    assert [e["final"] for e in events if e["type"] == "tool_call_cut"] == [False, False, False, True]
    assert len(turns) == 1 and turns[0].tool_calls == []


def test_an_unclosed_tag_call_in_a_cut_reply_is_asked_again(ws):
    cut = reply('<tool_call>{"name": "write_file", "arguments": {"path": "a.txt", "content": "ha', "length")
    good = reply('<tool_call>{"name": "write_file", "arguments": {"path": "a.txt", "content": "ok"}}</tool_call>')
    client = Client(cut, good, reply("done"))
    agent, _ = make(ws, client)
    agent.run("go")
    assert (ws / "a.txt").read_text(encoding="utf-8") == "ok" and client.options[1]["max_tokens"] == 8192


def test_the_client_says_how_many_calls_it_left_out_of_a_cut_stream():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write_file", "arguments": '{"path": "a.txt", "co'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {"completion_tokens": 7}},
    ]
    lines = [f"data: {json.dumps(c)}\n".encode() for c in chunks] + [b"data: [DONE]\n"]
    done = _collect_stream(lines, lambda k, t: None)
    assert done["cut_calls"] == 1 and "tool_calls" not in done["choices"][0]["message"] and done["choices"][0]["finish_reason"] == "length"
    whole = [f"data: {json.dumps(c)}\n".encode() for c in (
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})] + [b"data: [DONE]\n"]
    assert _collect_stream(whole, lambda k, t: None)["cut_calls"] == 0


def test_a_cut_call_in_a_stream_is_asked_again(ws):
    client = Client(reply("", "length", cut_calls=1, usage={"completion_tokens": 300}),
                    reply("", "tool_calls", calls=[tool_call("write_file", {"path": "s.txt", "content": "x"})]), reply("done"))
    agent, _ = make(ws, client)
    agent.run("go")
    assert (ws / "s.txt").exists() and client.options[1]["max_tokens"] == 8192


# -- an empty reply after thinking ------------------------------------------------------------------------------------

def test_thinking_that_used_the_output_is_asked_again_once_with_less_thinking_and_a_short_answer_note(ws, tmp_path):
    audit = AuditLedger(tmp_path / "audit.jsonl")
    client = Client(reply("", "length", reasoning="a very long thought"), reply("Short answer."))
    agent, events = make(ws, client, audit=audit)
    turns = agent.run("question")
    assert agent.stop_reason == FINAL and answer_text(turns) == "Short answer." and agent.length_stops == 1
    assert client.options[1]["extra"]["reasoning_effort"] == "low" and "extra" not in client.options[0]
    assert "answer itself, in a few sentences" in client.sent[1][-1]["content"]
    assert [e["final"] for e in events if e["type"] == "answer_empty"] == [False]
    assert [r["details"]["final"] for r in audit.records if r["action"] == "answer_empty"] == [False]


def test_the_lower_level_follows_the_session_level(ws):
    client = Client(reply("", "stop", reasoning="thought"), reply("ok"))
    agent, _ = make(ws, client, sampling={"reasoning_effort": "low"})
    agent.run("go")
    assert client.options[0]["extra"] == {"reasoning_effort": "low"} and client.options[1]["extra"] == {"reasoning_effort": "none"}


def test_thinking_already_off_stays_off_on_the_second_try(ws):
    client = Client(reply("", "stop", reasoning="thought"), reply("ok"))
    agent, _ = make(ws, client, sampling={"reasoning_effort": "none"})
    agent.run("go")
    assert client.options[1]["extra"] == {"reasoning_effort": "none"}


def test_an_empty_answer_twice_ends_as_empty_with_a_notice_and_not_as_a_final(ws):
    client = Client(reply("", "length", reasoning="thought one"), reply("", "length", reasoning="thought two"), reply("never"))
    agent, events = make(ws, client)
    turns = agent.run("question")
    assert agent.stop_reason == EMPTY and "no answer" in agent.error and len(client.sent) == 2
    assert [e["final"] for e in events if e["type"] == "answer_empty"] == [False, True]
    assert answer_text(turns) == ""


def test_the_retry_is_once_per_request_and_the_effort_is_for_that_call_only(ws):
    client = Client(reply("", "length", reasoning="t"), reply("answer one"), reply("", "stop", reasoning="t"), reply("answer two"))
    agent, _ = make(ws, client)
    agent.run("one")
    agent.run("two")
    assert agent.stop_reason == FINAL and [o.get("extra") for o in client.options] == [None, {"reasoning_effort": "low"}, None, {"reasoning_effort": "low"}]


def test_a_reply_with_no_thinking_and_no_text_that_ended_normally_is_left_alone(ws):
    agent, _ = make(ws, Client(reply("")))
    agent.run("go")
    assert agent.stop_reason == FINAL


# -- the ceiling and the temperature ----------------------------------------------------------------------------------

def test_no_ceiling_and_no_temperature_are_sent_unless_set(ws):
    client = Client(reply("one"), reply("two"), reply("three"))
    agent, _ = make(ws, client)
    agent.run("a")
    assert client.options[0] == {}
    agent.max_tokens, agent.sampling["temperature"] = 777, 0.0
    agent.run("b")
    assert client.options[1] == {"max_tokens": 777, "temperature": 0.0}


def test_the_session_temperature_is_passed_and_not_part_of_the_extra_options(ws):
    client = Client(reply("x"))
    agent, _ = make(ws, client, sampling={"temperature": 0.6, "seed": 5})
    agent.run("a")
    assert client.options[0] == {"temperature": 0.6, "extra": {"seed": 5}}


def test_the_client_leaves_temperature_and_max_tokens_out_of_the_request_unless_given(monkeypatch):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}'

    def fake_open(req, timeout=0):
        seen.append(json.loads(req.data.decode("utf-8")))
        return Resp()

    monkeypatch.setattr("harness.client.open_url", lambda req, timeout=0: fake_open(req, timeout))
    client = BasaltClient("http://x")
    client.chat_completion([{"role": "user", "content": "hi"}])
    client.chat_completion([{"role": "user", "content": "hi"}], temperature=0.0, max_tokens=5)
    assert "temperature" not in seen[0] and "max_tokens" not in seen[0]
    assert seen[1]["temperature"] == 0.0 and seen[1]["max_tokens"] == 5


def test_a_cancelled_stream_is_still_a_cancel(ws):
    from harness.client import StreamCancelled
    agent, _ = make(ws, Client(StreamCancelled()))
    agent.run("go")
    assert agent.stop_reason == CANCELLED


# -- the figure the evaluation report shows -----------------------------------------------------------------------------

def test_the_report_counts_the_replies_the_limit_ended_and_the_calls_that_were_mended():
    trials = [Trial(repetition=1, passed=True, length_stops=2, repaired_calls=1), Trial(repetition=2, passed=False, length_stops=1)]
    result = EvalResult(task_id="t", category="c", k_trials=2, pass_at_k=1.0, pass_pow_k=False, trials=trials)
    assert summarize([result])["length_stops"] == 3 and summarize([result])["repaired_calls"] == 1
    report = generate_eval_report([result])
    assert "`length_stops`): 3" in report and "mended before they ran 1" in report
