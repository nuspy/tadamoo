"""Next-prompt chips: the parsing and repair of the reply, when they are skipped, that the instruction never enters the history, and that a new send cancels."""
import copy
import json
import threading
import time

import pytest

from harness import agent_loop as al
from harness import suggestions as sg
from harness.session import Session, SessionStore
from harness.stack import AgentConfig, build_agent


def chips(*pairs):
    return json.dumps({"suggestions": [{"label": a, "prompt": b} for a, b in pairs]})


def test_parse_drops_duplicates_and_keeps_the_whole_message():
    long_one = "Compare the validation step with the parsing step, because the three steps are independent and a table shows where they overlap. " * 3
    out = sg.parse(chips(("", "Compare the two options in a table"), ("Same", "compare the two options in a table!"), ("Apply", long_one),
                         ("L" * 90, "Check the edge cases"), ("", "ok"), ("Fifth", "Another one here"), ("Sixth", "And one more here")))
    assert [c["prompt"] for c in out][:2] == ["Compare the two options in a table", long_one.strip()]          # 380 characters, kept whole
    assert all(set(c) == {"prompt"} for c in out)
    assert len(out) == sg.MAX_ITEMS and all(len(c["prompt"]) <= sg.PROMPT_CHARS for c in out)
    for bad in ("", "nope", "[]", json.dumps({"suggestions": "x"}), json.dumps({"other": []}), None, json.dumps({"suggestions": [5, None]})):
        assert sg.parse(bad) == []


def test_a_message_over_the_limit_keeps_its_whole_sentences_and_is_never_cut_in_a_word():
    first = "Explain how the validation step rejects a row, and which error it reports for each kind of fault."
    second = "Then show the same check on a file with a thousand rows, to see whether the cost grows with the size."
    text = first + " " + second + " " + "Finally, " + "and also " * 60
    assert len(text) > sg.PROMPT_CHARS
    out = sg.parse(chips(("", text)))
    assert out == [{"prompt": first + " " + second}]
    assert sg.parse(chips(("", "word " * 200))) == []                                      # no sentence end to cut at: dropped, not truncated


def test_the_instruction_asks_for_complete_self_explanatory_messages_in_the_users_language():
    text = sg.INSTRUCTION
    for needle in ("language of the user's last message", "complete and self-explanatory", "as they are", "real names", "reason", "never cut off", "Output only JSON"):
        assert needle in text, needle
    assert sg.PROMPT_CHARS >= 400 and sg.MAX_TOKENS >= 400 and sg.MAX_ITEMS == 4
    assert sg.SCHEMA["properties"]["suggestions"]["items"]["required"] == ["prompt"]


def skip(s, **kw):
    base = dict(answer="x" * 300, stop_reason="final", cut=False, error="", attended=True, plan_mode=False, idle=True, prompt_tokens=1000, cached_tokens=900)
    return s.skip_reason(**{**base, **kw})


def test_the_skip_rules():
    s = sg.Suggester(object())
    assert skip(s) == ""
    for kw, why in (({"attended": False}, "unattended"), ({"stop_reason": "asked"}, "ending"), ({"cut": True}, "ending"), ({"error": "x"}, "ending"),
                    ({"answer": "Done."}, "short"), ({"plan_mode": True}, "plan"), ({"idle": False}, "busy"), ({"prompt_tokens": 90_000}, "long"),
                    ({"prompt_tokens": 5000, "cached_tokens": 100}, "cold")):
        assert skip(s, **kw) == why, why
    assert skip(sg.Suggester(object(), enabled=False)) == "off" and skip(sg.Suggester(None)) == "off"
    assert skip(sg.Suggester(object(), side_call_tokens=500)) == "long"


def test_after_three_unused_sets_it_runs_every_second_turn_and_a_used_one_resets():
    s = sg.Suggester(object())
    for _ in range(3):
        s.offered = ["a", "b"]
        s.sent("something else")
    assert s.unused == 3
    assert skip(s) == "backoff" and skip(s) == "" and skip(s) == "backoff"
    s.offered = ["a", "b"]
    s.sent(" b ")
    assert s.unused == 0 and skip(s) == ""
    s.sent("nothing offered")
    assert s.unused == 0


class Client:
    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def chat_completion(self, messages, tools=None, **kw):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": tools, **kw})
        step = self.steps.pop(0)
        if callable(step):
            step = step(kw)
        if isinstance(step, BaseException):
            raise step
        return step


def say(text, usage=None):
    return {"choices": [{"message": {"role": "assistant", "content": text}}], **({"usage": usage} if usage else {})}


class Events:
    def __init__(self):
        self.items, self.cond = [], threading.Condition()

    def __call__(self, event):
        with self.cond:
            self.items.append(event)
            self.cond.notify_all()

    def wait(self, kind, timeout=10.0, after=0):
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                for e in self.items[after:]:
                    if e["event"] == kind:
                        return e
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no {kind} event; got {[e['event'] for e in self.items]}")
                self.cond.wait(left)

    def count(self, kind):
        return sum(1 for e in self.items if e["event"] == kind)


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr("harness.stack.local_now", None)


ANSWER = "The service reads the file, validates each row and writes the result next to the input; the three steps are independent and each can be tested alone. " * 2


def make(tmp_path, client, enabled=True, unattended=False, **kw):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="default", max_turns=6),
                        client=client)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None, unattended=unattended)
    session.suggester = sg.Suggester(client, enabled, **kw)
    return session, events, built


def test_chips_come_after_the_answer_and_the_instruction_never_enters_the_history(tmp_path):
    client = Client(say(ANSWER), say(chips(("Deeper", "Explain the validation step"), ("Apply", "Apply it to my CSV")), {"total_tokens": 321}))
    session, ev, built = make(tmp_path, client)
    session.send("m1", "describe the service")
    done = ev.wait("done")
    got = ev.wait("suggestions")
    assert got["turn"] == "m1" and got["items"][0] == {"prompt": "Explain the validation step"} and got["tokens"] == 321
    assert done["final"].startswith("The service")
    call = client.requests[1]
    assert call["messages"][-1] == {"role": "user", "content": sg.INSTRUCTION} and call["messages"][-2]["role"] == "assistant"
    assert call["response_format"]["json_schema"]["strict"] is True and call["max_tokens"] == sg.MAX_TOKENS and call["extra"] == {"reasoning_effort": "none"}
    assert call["tools"] and call["tool_choice"] == "none" and call["timeout"] == sg.TIMEOUT_S
    assert all(sg.INSTRUCTION not in json.dumps(m) for m in built.agent.messages)                                   # never stored
    assert session.suggester.offered == ["Explain the validation step", "Apply it to my CSV"]


def test_no_chips_unattended_off_short_or_after_a_card(tmp_path):
    for label, kwargs, answer in (("unattended", {"unattended": True}, ANSWER), ("off", {"enabled": False}, ANSWER), ("short", {}, "Done.")):
        client = Client(say(answer))
        session, ev, built = make(tmp_path / label, client, **kwargs)
        session.send("m1", "go")
        ev.wait("done")
        time.sleep(0.3)
        assert ev.count("suggestions") == 0 and len(client.requests) == 1, label


def test_a_failed_slow_or_empty_call_shows_nothing(tmp_path):
    for label, step in (("fail", RuntimeError("down")), ("empty", say(chips())), ("junk", say("sorry, no"))):
        client = Client(say(ANSWER), step)
        session, ev, built = make(tmp_path / label, client)
        session.send("m1", "go")
        ev.wait("done")
        deadline = time.monotonic() + 3
        while len(client.requests) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        assert ev.count("suggestions") == 0, label


def test_a_message_sent_meanwhile_cancels_the_call_and_its_result_is_dropped(tmp_path):
    started, release = threading.Event(), threading.Event()

    def slow(kw):
        started.set()
        release.wait(5)
        with pytest.raises(Exception):
            kw["on_delta"]("content", "x")                       # the stream is cut because the person sent a message
        return say(chips(("A", "A late suggestion")))
    client = Client(say(ANSWER), slow, say("Second answer."))
    session, ev, built = make(tmp_path, client)
    session.send("m1", "first")
    ev.wait("done")
    assert started.wait(5)
    n = len(ev.items)
    session.send("m2", "second")
    release.set()
    ev.wait("done", after=n)
    time.sleep(0.3)
    assert ev.count("suggestions") == 0


def test_a_cold_or_long_conversation_gets_no_call(tmp_path):
    usage = {"prompt_tokens": 20_000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 19_000}}
    client = Client(say(ANSWER, usage))
    session, ev, built = make(tmp_path, client)
    session.send("m1", "go")
    ev.wait("done")
    time.sleep(0.3)
    assert len(client.requests) == 1
    usage = {"prompt_tokens": 4_000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 100}}
    client2 = Client(say(ANSWER, usage))
    session2, ev2, built2 = make(tmp_path / "b", client2)
    session2.send("m1", "go")
    ev2.wait("done")
    time.sleep(0.3)
    assert len(client2.requests) == 1                                                  # cold: the call would read the whole history itself
    assert al.FINAL == "final"
