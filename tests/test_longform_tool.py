"""plan_long_answer, and the three ways a long answer starts from the harness: the model's own call, the deterministic check of a request
(a card where the person is, the offer taken where nobody watches), and the tool's place in the tool list. Scripted model, no engine."""
import copy
import json
import re
import threading
import time

import pytest

from harness import agent_loop as al
from harness import questions as qs
from harness.session import Session, SessionStore
from harness.stack import AgentConfig, build_agent
from harness.tools import create_standard_tool_registry
from harness.tools.registry import STABLE, EffectClass

SECTIONS = [{"heading": f"Section {i}", "goal": f"goal {i}", "target_words": 2000, "key_points": ["a", "b"]} for i in (1, 2, 3)]


class Model:
    """Plays the model: a call that asks for a part gets a part of the right length; any other call gets the next scripted reply."""

    def __init__(self, *steps):
        self.steps, self.sent, self.parts = list(steps), [], []

    def chat_completion(self, messages, tools=None, on_delta=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        last = messages[-1]["content"] if isinstance(messages[-1].get("content"), str) else ""
        part = re.search(r"Part (\d+) of (\d+)", last)
        if part and not tools:
            words = int(re.search(r"about (\d+) words", last).group(1))
            text = f"Body of part {part.group(1)}. " + " ".join(f"w{part.group(1)}x{i}" for i in range(words))
            self.parts.append(text)
            if on_delta:
                on_delta("content", text)
            return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": {}}
        step = self.steps.pop(0)
        return step() if callable(step) else step

    def context_window(self):
        return None


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def call(name, **args):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "finish_reason": "tool_calls"}]}


class Events:
    def __init__(self):
        self.items, self.cond = [], threading.Condition()

    def __call__(self, event):
        with self.cond:
            self.items.append(event)
            self.cond.notify_all()

    def wait(self, kind, timeout=20.0, after=0):
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

    def kinds(self):
        return [e["event"] for e in self.items]


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr("harness.stack.local_now", None)


def session_with(tmp_path, client, unattended=False):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode="default", max_turns=6), client=client)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None,
                      unattended=unattended)
    return session, events, built


# -- the tool itself ----------------------------------------------------------------------------------------------------

def test_the_tool_is_pure_and_leads_the_tool_list(tmp_path):
    registry, _ = create_standard_tool_registry(tmp_path)
    tool = registry.get("plan_long_answer")
    assert tool.effect == EffectClass.PURE and tool.tier == STABLE and registry.timeout_for("plan_long_answer") is None
    assert "plan_long_answer" in [s["function"]["name"] for s in registry.schemas()][:60]


def test_the_model_hands_over_a_plan_and_the_harness_writes_it_in_parts(tmp_path):
    registry, _ = create_standard_tool_registry(tmp_path)
    model = Model()
    shown, events = [], []
    binding = registry.longform
    binding.client, binding.deliver, binding.progress = model, shown.append, events.append
    out = registry.execute("plan_long_answer", {"task": "A report on rivers", "sections": SECTIONS, "total_words": 6000, "language": "English"})
    assert len(model.parts) == 3                                              # 6000 words: three parts of 2000
    assert out.startswith("Written in 3 of 3 parts") and "do not write it again" in out and "Start: Body of part 1." in out
    text = "".join(shown)
    assert text.count("Body of part") == 3 and text.endswith("\n\n")
    assert [e["event"] for e in events][0] == "plan" and events[-1]["event"] == "done" and events[-1]["segments"] == 3
    first = model.sent[0]
    assert "Plan of the whole text" in first[0]["content"] and "1. Section 1: goal 1 (about 2000 words)" in first[0]["content"]


def test_without_a_place_to_show_it_the_text_is_returned(tmp_path):
    registry, _ = create_standard_tool_registry(tmp_path)
    registry.longform.client = Model()
    out = registry.execute("plan_long_answer", {"task": "t", "sections": SECTIONS[:1], "total_words": 900})
    assert "The text:\nBody of part 1." in out


def test_a_plan_that_cannot_be_used_is_told_back_to_the_model(tmp_path):
    registry, _ = create_standard_tool_registry(tmp_path)
    assert "No model is connected" in registry.execute("plan_long_answer", {"task": "t"})
    registry.longform.client = Model()
    (tmp_path / "a.txt").write_text("Some text to translate. " * 40, encoding="utf-8")
    assert "translate needs" in registry.execute("plan_long_answer", {"task": "t", "mode": "translate", "source_path": "a.txt"})
    assert "Cannot read" in registry.execute("plan_long_answer", {"task": "t", "mode": "translate", "language": "it", "source_path": "missing.txt"})
    assert "no message text" in registry.execute("plan_long_answer", {"task": "t", "mode": "translate", "language": "it", "use_message": True})


def test_a_file_in_the_workspace_is_translated_in_parts_with_its_links_kept(tmp_path):
    registry, _ = create_standard_tool_registry(tmp_path)
    (tmp_path / "doc.md").write_text("# Title\n\nSee ![fig](fig.png) and [site](https://example.com/x).\n\n" + "A plain paragraph of the document. " * 60, encoding="utf-8")
    model = Model()
    seen = []
    registry.longform.client, registry.longform.deliver = model, seen.append
    out = registry.execute("plan_long_answer", {"task": "t", "mode": "translate", "language": "it", "source_path": "doc.md"})
    assert out.startswith("Written in 1 of 1 parts")
    assert "fig.png" not in model.sent[0][-1]["content"] and "example.com" not in model.sent[0][-1]["content"]


# -- the harness checks the request ---------------------------------------------------------------------------------------

ASK = "Scrivi un report di 10.000 parole sui fiumi d'Europa"


def plan_call():
    return call("plan_long_answer", task="Report sui fiumi", sections=SECTIONS, total_words=6000, language="Italian")


def test_where_the_person_is_a_card_offers_the_parts_and_the_answer_starts_the_model(tmp_path):
    model = Model(plan_call(), say("Fatto."))
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    ask = ev.wait("ask")
    q = ask["questions"][0]
    assert ask["source"] if "source" in ask else True
    assert q["kind"] == "single" and "10,000" in q["text"] and "5 parts" in q["text"] and "tokens" in q["text"] and q["options"][0]["recommended"] and not q["allow_other"]
    assert ev.wait("done")["stop_reason"] == al.ASKED and model.sent == []         # the model was not called
    n = len(ev.items)
    answer = qs.ANSWER_HEADER + "\n1. " + q["text"][:60] + " -> " + q["options"][0]["label"] + " (your recommended option)\n" + qs.GO_ON
    session.send("m2", answer)
    done = ev.wait("done", after=n)
    assert done["final"] == "Fatto."
    notes = [m["content"] for m in model.sent[0] if isinstance(m.get("content"), str) and m["content"].startswith("[harness] This request needs")]
    assert len(notes) == 1 and "plan_long_answer" in notes[0] and "10000 words" in notes[0] and "about 5 parts" in notes[0]
    shown = "".join(e["text"] for e in ev.items if e["event"] == "delta" and e.get("kind") == "content")
    assert shown.count("Body of part") == 3
    progress = [e["data"] for e in ev.items if e["event"] == "agent" and e["data"].get("type") == "long_answer"]
    assert progress[0]["event"] == "plan" and progress[-1]["event"] == "done"


def test_refusing_the_parts_leaves_the_request_to_the_model_alone(tmp_path):
    model = Model(say("Eccola, corta."))
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    q = ev.wait("ask")["questions"][0]
    ev.wait("done")
    n = len(ev.items)
    session.send("m2", qs.ANSWER_HEADER + "\n1. " + q["text"][:40] + " -> " + q["options"][1]["label"] + "\n" + qs.GO_ON)
    ev.wait("done", after=n)
    assert not any("plan_long_answer" in str(m.get("content")) and str(m.get("content")).startswith("[harness]") for m in model.sent[0])


def test_where_nobody_watches_the_parts_are_taken_at_once(tmp_path):
    model = Model(plan_call(), say("Fatto."))
    session, ev, built = session_with(tmp_path, model, unattended=True)
    session.send("m1", ASK)
    done = ev.wait("done")
    assert "ask" not in ev.kinds() and done["final"] == "Fatto." and done["stop_reason"] != al.ASKED
    auto = ev.wait("auto_answered")
    assert auto["chosen"] == "recommended"
    told = json.dumps(model.sent[0])
    assert "plan_long_answer" in told and "chosen automatically" in told
    assert len(model.parts) == 3


def test_ordinary_requests_are_left_alone(tmp_path):
    model = Model(say("Ciao."))
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", "Scrivi una poesia di 12 righe sul mare")
    done = ev.wait("done")
    assert "ask" not in ev.kinds() and done["final"] == "Ciao."
    assert not any(str(m.get("content")).startswith("[harness] This request needs") for m in model.sent[0])


def test_the_control_line_of_the_agent_tab_starts_it_without_a_card(tmp_path):
    source = "The river runs to the sea and the people on its banks have lived there for a very long time, building towns. " * 3
    model = Model(call("plan_long_answer", task="Translate", mode="translate", language="Italian", use_message=True), say("Fatto."))
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", "Translate this\n\n" + source + "\n\n[long answer: mode=translate; language=Italian]")
    done = ev.wait("done")
    assert "ask" not in ev.kinds() and done["final"] == "Fatto."
    note = next(m["content"] for m in model.sent[0] if str(m.get("content")).startswith("[harness] This request needs"))
    assert "use_message true" in note and "The person chose: language Italian" in note
    asked = [m for m in model.sent if m[-1]["role"] == "user" and "Source of this part" in str(m[-1]["content"])]
    assert len(asked) == 1 and "long answer" not in asked[0][-1]["content"] and "Translate this" in asked[0][-1]["content"]
