"""The card worker: a card when a conversation has been quiet, the back-fill of old conversations (paced, only when free, stoppable, resumable), the use in the
settings, the conversations that tell it, and the Memories page's side of it."""
import json
import sys
import time
from pathlib import Path

import pytest

from tadamoo import side_uses
from tadamoo.past import PastIndex
from serve.agent_sessions import SessionManager
from serve.card_worker import CardWorker
from serve.memories_api import MemoriesApi
from tadamoo.personal_memory import PersonalMemory, PersonalMemoryError


def convo(*lines):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": text} for i, text in enumerate(lines)]


CARD = json.dumps({"title": "Eventi UE", "summary": ["Chiesti gli eventi."], "topics": ["eventi"], "also_called": ["UE", "EU"], "entities": [], "decisions": []})


class Clock:
    def __init__(self, now=10_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Model:
    """A client whose server has a technical session of `context` tokens (0: none) and answers every card with CARD."""

    def __init__(self, context=30000, reply=CARD):
        self.context, self.reply, self.calls = context, reply, []

    def side_context(self, timeout=3.0):
        return self.context

    def chat_completion(self, messages, **kw):
        self.calls.append((messages, kw))
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"choices": [{"message": {"content": self.reply}}]}


@pytest.fixture(autouse=True)
def _uses(tmp_path, monkeypatch):
    monkeypatch.setattr(side_uses, "_dir", lambda: tmp_path / "ud")          # the settings file of this test only
    side_uses._cache.update(key=None, values={})


def worker(tmp_path, model, free=lambda: True, clock=None):
    past = PastIndex(tmp_path / "past.db")
    return CardWorker(past, lambda: model, free, poll_s=0.05, clock=clock or Clock()), past


def test_a_conversation_gets_its_card_only_after_it_has_been_quiet(tmp_path):
    clock, model = Clock(), Model()
    w, past = worker(tmp_path, model, clock=clock)
    past.add_session("c1", convo("Quali eventi dell'Unione Europea?", "Il vertice."), title="UE", at=1.0)
    w.set_backfill(False)                                                    # only the conversation that ended, not the back-fill
    w.touch("c1")
    assert w.step() is False and model.calls == []
    clock.now += side_uses.param("conversation_card", "idle_s") + 1
    assert w.step() is True and past.card_of("c1")["source"] == "model" and past.search("EU")[0]["card"]
    assert model.calls[0][1]["extra"]["cache_slot"] == "side_only" and model.calls[0][1]["extra"]["side_use"] == "conversation_card"     # in the technical session
    assert "Quali eventi" in model.calls[0][0][1]["content"] and w.step() is False


def test_a_turn_that_ends_again_moves_the_time_back(tmp_path):
    clock = Clock()
    w, past = worker(tmp_path, Model(), clock=clock)
    w.set_backfill(False)
    past.add_session("c1", convo("hello there", "hi"), at=1.0)
    w.touch("c1")
    clock.now += side_uses.param("conversation_card", "idle_s") - 5
    w.touch("c1")
    clock.now += 10
    assert w.step() is False
    clock.now += side_uses.param("conversation_card", "idle_s")
    assert w.step() is True


def test_without_a_session_or_with_the_use_off_the_card_is_made_by_code_and_costs_no_call(tmp_path):
    for model, off in ((Model(context=0), False), (Model(), True)):
        w, past = worker(tmp_path / ("a" if off else "b"), model)
        if off:
            side_uses.save("conversation_card", on=False)
        past.add_session("c1", convo("Quali eventi dell'Unione Europea?", "Il vertice."), title="UE", at=1.0)
        assert w.step() is True and past.card_of("c1")["source"] == "fallback" and model.calls == []
        side_uses.reset()


def test_a_session_that_fails_leaves_the_card_by_code_and_the_back_fill_tries_again_later(tmp_path):
    clock, model = Clock(), Model(reply=RuntimeError("busy"))
    w, past = worker(tmp_path, model, clock=clock)
    past.add_session("c1", convo("Quali eventi?", "Il vertice."), at=1.0)
    assert w.step() and past.card_of("c1")["source"] == "fallback" and past.card_of("c1")["tries"] == 1
    model.reply = CARD
    clock.now += 1000
    assert w.step() and past.card_of("c1")["source"] == "model"              # the card by code was written again by the session


def test_the_back_fill_writes_one_card_per_gap_newest_first_and_only_while_the_server_is_free(tmp_path):
    clock, model, free = Clock(), Model(), [False]
    w, past = worker(tmp_path, model, free=lambda: free[0], clock=clock)
    for n, sid in enumerate(("old", "mid", "new")):
        past.add_session(sid, convo(f"topic {sid} here", "ok"), at=100.0 + n)
    assert w.step() is False and model.calls == []                           # something else is working
    free[0] = True
    assert w.step() is True and past.card_of("new") and not past.card_of("mid")
    assert w.last == clock.now                                               # the card was just written
    assert w.step() is False                                                 # the least time between two cards
    clock.now += side_uses.param("conversation_card", "gap_s") + 1
    assert w.step() is True and past.card_of("mid")
    assert w.status()["current"] == 2 and w.status()["total"] == 3 and w.status()["model"] == 2


def test_the_back_fill_by_code_does_not_wait_for_a_free_server(tmp_path):
    w, past = worker(tmp_path, Model(context=0), free=lambda: False)
    for sid in ("a", "b"):
        past.add_session(sid, convo(f"talk {sid} about owls", "yes"), at=1.0)
    assert w.step() and w.step() and w.step() is False
    assert w.status()["current"] == 2 and w.status()["fallback"] == 2


def test_the_back_fill_can_be_stopped_and_started_and_resumes_after_a_restart(tmp_path):
    model = Model(context=0)
    w, past = worker(tmp_path, model)
    for sid in ("a", "b", "c"):
        past.add_session(sid, convo(f"talk {sid} about owls", "yes"), at=1.0)
    assert w.step() and past.card_counts()["current"] == 1
    w.set_backfill(False)
    assert w.status()["backfill"] is False and w.step() is False
    again = CardWorker(past, lambda: model, lambda: True, poll_s=0.05)       # a new process, the same index
    assert again.backfill_on() is False and again.step() is False and past.card_counts()["current"] == 1
    again.set_backfill(True)
    assert again.step() and again.step() and again.step() is False and past.card_counts()["current"] == 3


def test_a_conversation_dropped_or_made_private_before_its_time_gets_no_card(tmp_path):
    clock = Clock()
    w, past = worker(tmp_path, Model(), clock=clock)
    w.set_backfill(False)
    past.add_session("c1", convo("secret plans", "ok"), at=1.0)
    w.touch("c1")
    past.drop("c1")
    clock.now += 10_000
    assert w.step() is True and past.card_of("c1") is None and past.search("secret") == [] and past.card_counts()["total"] == 0


def test_the_worker_thread_writes_and_stops(tmp_path):
    w, past = worker(tmp_path, Model(context=0))
    past.add_session("c1", convo("owls at night", "yes"), at=1.0)
    w.start()
    try:
        end = time.time() + 5
        while not past.card_of("c1") and time.time() < end:
            time.sleep(0.05)
    finally:
        w.stop()
    assert past.card_of("c1")["source"] == "fallback" and w.error == ""


def test_the_use_is_in_the_settings_on_by_default_and_needs_the_session():
    use = side_uses.BY_ID["conversation_card"]
    assert use.needs_session and use.default_on and side_uses.enabled("conversation_card") and not use.editable
    assert {p.key for p in use.params} == {"max_tokens", "timeout_s", "idle_s", "gap_s"} and side_uses.prompt_text("conversation_card").startswith("You write the card")
    assert [u["id"] for u in side_uses.view() if u["id"] == "conversation_card"] == ["conversation_card"]


def test_the_memories_page_reads_the_progress_and_switches_the_back_fill(tmp_path):
    w, past = worker(tmp_path, Model(context=0))
    past.add_session("c1", convo("owls at night", "yes"), at=1.0)
    api = MemoriesApi(PersonalMemory(tmp_path / "pm", key=bytes(range(32))), None, None, w)
    assert api.overview()["cards"] == {**past.card_counts(), "backfill": True, "waiting": 0, "error": "", "last": None, "last_source": ""}
    assert api.overview()["cards"]["total"] == 1 and api.overview()["cards"]["current"] == 0
    assert api.post("cards", {"backfill": False})["cards"]["backfill"] is False and past.meta_get("cards_backfill") == "off"
    for bad in ({}, {"backfill": "no"}):
        with pytest.raises(PersonalMemoryError):
            api.post("cards", bad)
    assert MemoriesApi(PersonalMemory(tmp_path / "pm2", key=bytes(range(32)))).overview()["cards"] is None


class Cards:
    def __init__(self):
        self.touched = []

    def touch(self, sid):
        self.touched.append(sid)


def test_a_turn_that_ends_tells_the_worker_unless_the_conversation_is_private(tmp_path):
    stub = str(Path(__file__).with_name("session_stub.py"))
    cards, past = Cards(), PastIndex(tmp_path / "past.db")
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", tick_s=0.1, default_workspace=tmp_path / "chat", past=past, cards=cards,
                         command=lambda sid, rec, history: [sys.executable, stub, "--session-id", sid, "--history-dir", str(history), "--die-times", "0"])
    (tmp_path / "chat").mkdir()

    def said(sid, text):
        before = sum(1 for e in mgr.events(sid)["events"] if e["event"] == "done")
        mgr.send(sid, text)
        end = time.monotonic() + 15
        while sum(1 for e in mgr.events(sid)["events"] if e["event"] == "done") <= before and time.monotonic() < end:
            time.sleep(0.05)
    try:
        open_one = mgr.create(None, title="")["id"]
        said(open_one, "a talk about otters")
        end = time.monotonic() + 5
        while cards.touched != [open_one] and time.monotonic() < end:
            time.sleep(0.05)
        assert cards.touched == [open_one]
        private = mgr.create(None, title="", private=True)["id"]
        said(private, "a private talk about badgers")
        time.sleep(0.5)
        assert cards.touched == [open_one] and past.search("badgers") == []
    finally:
        mgr.shutdown()
        past.close()
