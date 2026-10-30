"""The long answer from the harness's side: the deterministic check of a request, the card (or the offer taken where nobody watches), the
control line of the Agent tab, and the harness writing the text itself (estimate, plan, parts, check). The model-called tool `plan_long_answer`
is gone. Scripted model, no engine."""
import copy
import re
import threading
import time

import pytest

from harness import agent_loop as al
from harness import questions as qs
from harness.session import Session, SessionStore
from harness.stack import AgentConfig, build_agent
from harness.tools import create_standard_tool_registry
from serve import longform as lf
from tests.test_longform_flow import Script, filler, fix, reviewing


class Model(Script):
    """The flow's scripted calls, and the agent's own calls (those that carry tools) from `steps`."""

    def __init__(self, *steps, **kw):
        super().__init__(**kw)
        self.steps, self.agent_calls = list(steps), []

    def chat_completion(self, messages, tools=None, on_delta=None, **kwargs):
        if tools:
            self.agent_calls.append(copy.deepcopy(messages))
            return self.steps.pop(0)
        return super().chat_completion(messages, on_delta=on_delta, **kwargs)

    def _part(self, messages, last):
        src = re.search(r"-----\n(.*?)\n-----", last, re.S)
        if "Source of this part" in last and src:
            return "Il fiume corre verso il mare e la gente che vive sulle sue rive ha costruito città da molto tempo. " + filler("t", 40) + "."
        return super()._part(messages, last)

    def context_window(self):
        return None


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


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

    def progress(self):
        return [e["data"] for e in self.items if e["event"] == "agent" and e["data"].get("type") == "long_answer"]

    def shown(self):
        return "".join(e["text"] for e in self.items if e["event"] == "delta" and e.get("kind") == "content")


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("harness.stack.local_now", None)
    monkeypatch.setattr(lf, "_side", lambda: {"session": "tech"})


def session_with(tmp_path, client, unattended=False):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode="default", max_turns=6), client=client)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None,
                      unattended=unattended)
    return session, events, built


ASK = "Scrivi un report di 10.000 parole sui fiumi d'Europa"
ASK_EN = "Write a report of 6000 words about the rivers of Europe"


def accept(session, ev, q, option=0, after=0):
    answer = qs.ANSWER_HEADER + "\n1. " + q["text"][:60] + " -> " + q["options"][option]["label"] + " (your recommended option)\n" + qs.GO_ON
    session.send("m2", answer)
    return ev.wait("done", after=after)


# -- the tool is gone ---------------------------------------------------------------------------------------------------

def test_the_model_called_tool_no_longer_exists(tmp_path):
    registry, _ = create_standard_tool_registry(tmp_path)
    assert registry.get("plan_long_answer") is None
    assert "plan_long_answer" not in [s["function"]["name"] for s in registry.schemas()]
    assert not hasattr(registry, "longform")
    with pytest.raises(ImportError):
        __import__("harness.longform_tool")


def test_the_agent_is_never_asked_for_the_text_by_a_tool_call(tmp_path):
    model = Model(say("Fatto."))
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK_EN)
    q = ev.wait("ask")["questions"][0]
    ev.wait("done")
    done = accept(session, ev, q, after=len(ev.items))
    assert done["stop_reason"] == al.FINAL and model.agent_calls == []
    assert "plan_long_answer" not in str(built.agent.messages)


# -- the card, then the harness writes ----------------------------------------------------------------------------------

def test_where_the_person_is_a_card_offers_the_parts_and_the_answer_starts_the_harness(tmp_path):
    model = Model(estimate=10000, plan_chunks=5)
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    q = ev.wait("ask")["questions"][0]
    assert q["kind"] == "single" and "10,000" in q["text"] and "5 parts" in q["text"] and "token" in q["text"] and q["options"][0]["recommended"] and not q["allow_other"]
    assert ev.wait("done")["stop_reason"] == al.ASKED and model.calls == []            # nothing was asked of the model yet
    n = len(ev.items)
    done = accept(session, ev, q, after=n)
    text = built.agent.messages[-1]["content"]
    assert done["stop_reason"] == al.FINAL and done["final"] == text and text.count("Opening paragraph of chapter") == 5
    assert ev.shown().count("Opening paragraph of chapter") == 5
    kinds = [p["event"] for p in ev.progress()]
    assert kinds[0] == "estimate" and "plan" in kinds and kinds.count("segment_done") == 5 and kinds[-1] == "done" and "review" in kinds
    asked = [c["kind"] for c in model.calls]
    assert asked[:2] == ["estimate", "plan"] and asked.count("part") == 5 and asked.count("review") >= 5
    assert model.agent_calls == [] and built.agent.turns[-1].assistant_content == text


def test_the_text_is_kept_in_the_history_for_what_comes_next(tmp_path):
    model = Model(say("Fatto."))
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK_EN)
    q = ev.wait("ask")["questions"][0]
    ev.wait("done")
    accept(session, ev, q, after=len(ev.items))
    last = built.agent.messages[-1]
    assert last["role"] == "assistant" and last["content"].startswith("Opening paragraph of chapter 1")
    n = len(ev.items)
    session.send("m3", "Thanks, now give me a one line summary")
    ev.wait("done", after=n)
    assert any(m.get("role") == "assistant" and "Opening paragraph of chapter 1" in str(m.get("content")) for m in model.agent_calls[0])


def test_refusing_the_parts_leaves_the_request_to_the_model_alone(tmp_path):
    model = Model(say("Eccola, corta."))
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", ASK)
    q = ev.wait("ask")["questions"][0]
    ev.wait("done")
    n = len(ev.items)
    session.send("m2", qs.ANSWER_HEADER + "\n1. " + q["text"][:40] + " -> " + q["options"][1]["label"] + "\n" + qs.GO_ON)
    done = ev.wait("done", after=n)
    assert done["final"] == "Eccola, corta." and model.calls == [] and len(model.agent_calls) == 1


def test_where_nobody_watches_the_parts_are_taken_at_once(tmp_path):
    model = Model()
    session, ev, built = session_with(tmp_path, model, unattended=True)
    session.send("m1", ASK_EN)
    done = ev.wait("done")
    assert "ask" not in ev.kinds() and done["stop_reason"] == al.FINAL and done["final"].count("Opening paragraph of chapter") == 3
    assert ev.wait("auto_answered")["chosen"] == "recommended"
    assert any("chosen automatically" in str(m.get("content")) for m in built.agent.messages)


def test_ordinary_requests_are_left_alone(tmp_path):
    model = Model(say("Ciao."))
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", "Scrivi una poesia di 12 righe sul mare")
    done = ev.wait("done")
    assert "ask" not in ev.kinds() and done["final"] == "Ciao." and model.calls == []


def test_an_estimate_under_the_threshold_gives_the_request_back_to_the_model(tmp_path):
    model = Model(say("Eccola."), estimate=900)
    session, ev, built = session_with(tmp_path, model, unattended=True)
    session.send("m1", "Write a chapter about the rivers of Europe")
    done = ev.wait("done")
    assert done["final"] == "Eccola." and [c["kind"] for c in model.calls] == ["estimate"]
    assert [p["event"] for p in ev.progress()] == ["estimate"] and ev.progress()[0]["long"] is False


def test_the_side_session_reaches_the_short_calls_only(tmp_path):
    model = Model(review=reviewing([fix("insert_after", 2, "Closing paragraph of chapter 2 wraps up the argument")]))
    session, ev, built = session_with(tmp_path, model, unattended=True)
    session.send("m1", ASK_EN)
    ev.wait("done")
    by = {k: [c for c in model.calls if c["kind"] == k] for k in ("estimate", "plan", "review", "part", "fix")}
    assert by["fix"] and by["part"]
    for kind in ("estimate", "plan", "review"):
        assert by[kind] and all(c["extra"].get("session") == "tech" for c in by[kind])
    for kind in ("part", "fix"):
        assert all("session" not in c["extra"] for c in by[kind])


def test_a_fix_changes_the_text_the_page_shows_and_the_history_keeps(tmp_path):
    model = Model(review=reviewing([fix("replace", 3, "Middle paragraph of chapter 3 holds `code3`")]))
    session, ev, built = session_with(tmp_path, model, unattended=True)
    session.send("m1", ASK_EN)
    done = ev.wait("done")
    revised = next(p for p in ev.progress() if p["event"] == "revised")
    assert "Rewritten paragraph of chapter 3" in revised["text"] and revised["text"] == done["final"] == built.agent.messages[-1]["content"]
    assert "Middle paragraph of chapter 3" not in done["final"] and "Middle paragraph of chapter 2" in done["final"]
    last = ev.progress()[-1]
    assert last["event"] == "done" and last["applied"] == 1 and last["unapplied"] == []


def test_what_the_check_could_not_apply_is_in_the_final_event(tmp_path):
    model = Model(review=reviewing([fix("replace", 1, "a sentence that does not exist in this part")]))
    session, ev, built = session_with(tmp_path, model, unattended=True)
    session.send("m1", ASK_EN)
    ev.wait("done")
    last = ev.progress()[-1]
    assert last["applied"] == 0 and last["unapplied"][0]["reason"] == "not_found" and any(w["code"] == "fix_not_applied" for w in last["warnings"])


def test_stopping_during_the_text_keeps_what_was_written(tmp_path):
    model = Model()
    session, ev, built = session_with(tmp_path, model, unattended=True)
    original = model.chat_completion

    def stopping(messages, **kw):
        out = original(messages, **kw)
        if model.of("part"):
            built.agent.cancel.set()
        return out
    model.chat_completion = stopping
    session.send("m1", ASK_EN)
    done = ev.wait("done")
    assert done["error"] == "" and done["stop_reason"] == al.CANCELLED and len(model.of("part")) == 1 and "review" not in [p["event"] for p in ev.progress()]


# -- the control line of the Agent tab ----------------------------------------------------------------------------------

def test_a_length_chosen_by_hand_skips_the_estimate_and_the_card(tmp_path):
    model = Model()
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", "Write about the rivers of Europe, with care\n\n[long answer: mode=write; words=6000; language=English]")
    done = ev.wait("done")
    assert "ask" not in ev.kinds() and done["stop_reason"] == al.FINAL and model.of("estimate") == []
    assert [p["source"] for p in ev.progress() if p["event"] == "estimate"] == ["asked"]
    first = model.of("plan")[0]["messages"][-1]["content"]
    assert "Write about the rivers of Europe, with care" in first and "[long answer" not in first


def test_a_translation_by_hand_goes_part_by_part_from_the_message(tmp_path):
    source = "The river runs to the sea and the people on its banks have lived there for a very long time, building towns. " * 3
    model = Model()
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", "Translate this\n\n" + source + "\n\n[long answer: mode=translate; language=Italian]")
    done = ev.wait("done")
    assert "ask" not in ev.kinds() and done["stop_reason"] == al.FINAL and model.of("estimate") == [] and model.of("plan") == []
    asked = model.of("part")
    assert len(asked) == 1 and "long answer" not in asked[0]["messages"][-1]["content"] and "Translate this" in asked[0]["messages"][-1]["content"]
    assert "fiume" in done["final"]


def test_a_detected_translation_takes_the_language_and_the_source_from_the_request(tmp_path):
    body = ("The river runs to the sea and the people on its banks have lived there for a very long time, building towns and villages. " * 130).strip()
    model = Model()
    session, ev, built = session_with(tmp_path, model, unattended=True)
    session.send("m1", "Translate this text into Italian\n\n" + body)
    done = ev.wait("done")
    assert done["stop_reason"] == al.FINAL and model.of("estimate") == []
    first = model.of("part")[0]["messages"]
    assert "Italian" in first[0]["content"] and "Translate this text into Italian" not in first[-1]["content"]


def test_a_translation_that_names_no_language_is_left_to_the_model(tmp_path):
    body = ("The river runs to the sea and the people on its banks have lived there for a very long time, building towns and villages. " * 130).strip()
    model = Model(say("Which language?"))
    session, ev, built = session_with(tmp_path, model, unattended=True)
    session.send("m1", "Please translate\n\n" + body)
    done = ev.wait("done")
    assert done["final"] == "Which language?" and model.calls == []
