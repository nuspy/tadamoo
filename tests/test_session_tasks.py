"""A message written while the agent works: steered into the task, queued, or urgent (the task is put aside and goes on after)."""
import copy
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import agent_loop as al
from harness.router import Router
from harness.session import Session, SessionStore
from harness.stack import AgentConfig, build_agent
from test_session import Events, call, say

@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    """These tests count the messages the model is sent; the time note has its own tests (test_clock.py)."""
    monkeypatch.setattr("harness.stack.local_now", None)



class Model:
    """Answers by what the conversation says: the first matching rule wins. Records every request."""

    def __init__(self, *rules):
        self.rules, self.sent, self.lock = list(rules), [], threading.Lock()

    def chat_completion(self, messages, tools=None, on_delta=None, **kw):
        with self.lock:
            self.sent.append(copy.deepcopy(messages))
        for pred, fn in self.rules:
            if pred(messages):
                return fn(messages, on_delta) if fn.__code__.co_argcount == 2 else fn()
        return say("niente da fare")

    def users(self, i):
        return [m["content"] for m in self.sent[i] if m["role"] == "user"]


def last_user(messages):
    return next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")


def asked(text):
    return lambda messages: any(m["role"] == "user" and text in m["content"] for m in messages) and messages[-1]["role"] != "tool"


def said_in(text):
    return lambda messages: text in last_user(messages)


def make(tmp_path, model, judge=None, parallel=1):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode="auto", max_turns=6), client=model)
    events = Events()
    store = SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets)
    health = lambda: {"reachable": True, "loaded": True, "loading": False, "auto_load": True, "parallel": parallel}
    session = Session(built, store, events, session_id="s1", sleep=lambda s: None, health=health, router=Router(judge))
    return session, events, store, built


def gated(gate, started=None, then=None):
    """A model call that waits for the gate, then answers with `then` (a tool call by default)."""
    def fn():
        if started:
            started.set()
        gate.wait(10)
        return then or call("list_files", path=".")
    return fn


def test_a_message_that_changes_the_task_is_added_to_it_before_the_next_model_call(tmp_path):
    gate, started = threading.Event(), threading.Event()
    model = Model((said_in("caffè"), lambda: say("Aggiunto il caffè.")),
                  (lambda m: m[-1]["role"] == "tool", lambda: say("Finito, senza novità.")),
                  (said_in("organizza"), gated(gate, started)))
    session, ev, *_ = make(tmp_path, model)
    session.send("m1", "organizza la cena")
    started.wait(5)
    assert session.send("m2", "aggiungi anche il caffè")                       # it is not refused: it waits for the safe point
    queued = ev.wait("queued")
    assert queued["turn"] == "m2" and queued["text"] == "aggiungi anche il caffè"
    gate.set()
    done = ev.wait("done")
    assert done["final"] == "Aggiunto il caffè." and done["more"] is False
    routed = ev.wait("routed")
    assert (routed["turn"], routed["how"], routed["why"]) == ("m2", "steer", "default")
    assert model.users(1)[-1] == "aggiungi anche il caffè"                     # the second model call already saw it
    assert len(model.sent) == 2


def test_a_request_that_can_wait_starts_when_the_task_is_done_on_the_same_conversation(tmp_path):
    gate, started = threading.Event(), threading.Event()
    model = Model((lambda m: m[-1]["role"] == "tool", lambda: say("Cena organizzata.")),
                  (said_in("controlla le mail"), lambda: say("Nessuna mail nuova.")),
                  (said_in("organizza"), gated(gate, started)))
    session, ev, *_ = make(tmp_path, model, judge=lambda text, ctx: "queue")
    session.send("m1", "organizza la cena")
    started.wait(5)
    session.send("m2", "controlla le mail")
    gate.set()
    first = ev.wait("done")
    assert first["turn"] == "m1" and first["more"] is True                      # something is still waiting
    routed = ev.wait("routed")
    assert (routed["how"], routed["position"]) == ("queue", 1)
    second = ev.wait("done", after=ev.items.index(first) + 1)
    assert second["turn"] == "m2" and second["final"] == "Nessuna mail nuova." and second["more"] is False
    assert model.users(2)[:2] == ["organizza la cena", "controlla le mail"]     # the same conversation went on
    assert ev.kinds().count("accepted") == 2 and session.busy is False


def test_an_urgent_request_puts_the_task_aside_where_it_stands_and_it_goes_on_afterwards(tmp_path):
    gate, started = threading.Event(), threading.Event()
    model = Model((said_in("[harness] You were set aside"), lambda: say("Ripreso e finito: cena organizzata.")),
                  (said_in("telefona al medico"), lambda: say("Medico chiamato.")),
                  (lambda m: m[-1]["role"] == "tool", lambda: say("non dovrebbe arrivare")),
                  (said_in("organizza"), gated(gate, started)))
    session, ev, *_ = make(tmp_path, model, judge=lambda text, ctx: "urgent")
    session.send("m1", "organizza la cena")
    started.wait(5)
    session.send("m2", "telefona al medico")
    gate.set()
    ev.wait("resumed")
    kinds = ev.kinds()
    assert kinds.index("suspended") < kinds.index("resumed")
    suspended = ev.wait("suspended")
    assert (suspended["turn"], suspended["by"]) == ("m1", "m2")
    dones = [e for e in ev.items if e["event"] == "done"]
    until = time.monotonic() + 10
    while len([e for e in ev.items if e["event"] == "done"]) < 2 and time.monotonic() < until:
        time.sleep(0.02)
    dones = [e for e in ev.items if e["event"] == "done"]
    assert [(d["turn"], d["final"], d["more"]) for d in dones] == [("m2", "Medico chiamato.", True), ("m1", "Ripreso e finito: cena organizzata.", False)]
    side = next(m for m in model.sent if "telefona al medico" in last_user(m))
    assert [m["role"] for m in side] == ["system", "user"]                       # the urgent request has a conversation of its own
    assert last_user(model.sent[-1]).startswith("[harness] You were set aside")  # and the first one is told it was
    assert any(m["role"] == "tool" for m in model.sent[-1])                      # with its tool's result still there: nothing was lost


def test_a_strong_urgency_word_cuts_the_model_call_that_is_being_written(tmp_path):
    written, started = [], threading.Event()

    def slow(messages, on_delta):
        started.set()
        for i in range(2000):
            on_delta("content", f"parola{i} ")
            written.append(i)
            time.sleep(0.005)
        return say("lunghissimo")

    model = Model((said_in("urgente"), lambda: say("Fatto subito.")),
                  (lambda m: "[harness] You were set aside" in last_user(m), lambda: say("Ripresa.")),
                  (said_in("scrivi"), slow))
    session, ev, *_ = make(tmp_path, model)                                     # no judge: the words alone decide
    session.send("m1", "scrivi una storia")
    started.wait(5)
    time.sleep(0.1)
    session.send("m2", "È urgente: chiama Gianni")
    ev.wait("suspended", 10)
    assert len(written) < 1500                                                  # the long answer was cut, not waited for
    until = time.monotonic() + 10
    while len([e for e in ev.items if e["event"] == "done"]) < 2 and time.monotonic() < until:
        time.sleep(0.02)
    assert [e["turn"] for e in ev.items if e["event"] == "done"] == ["m2", "m1"]
    assert not any(m["role"] == "assistant" and "parola" in (m.get("content") or "") for m in session.agent.messages)    # no half answer kept


def test_when_the_model_answers_several_requests_at_once_a_waiting_request_runs_beside_the_task(tmp_path):
    gate, started, side_seen = threading.Event(), threading.Event(), threading.Event()
    model = Model((lambda m: m[-1]["role"] == "tool", lambda: say("Cena fatta.")),
                  (said_in("controlla le mail"), lambda: (side_seen.set(), say("Mail controllate."))[1]),
                  (said_in("organizza"), gated(gate, started)))
    session, ev, *_ = make(tmp_path, model, judge=lambda text, ctx: "queue", parallel=2)
    session.send("m1", "organizza la cena")
    started.wait(5)
    session.send("m2", "controlla le mail")
    assert side_seen.wait(10)                                                    # it ran while the first was still waiting
    assert not any(e["event"] == "done" and e["turn"] == "m1" for e in ev.items)
    assert ev.wait("routed")["how"] == "parallel"
    gate.set()
    until = time.monotonic() + 10
    while len([e for e in ev.items if e["event"] == "done"]) < 2 and time.monotonic() < until:
        time.sleep(0.02)
    assert {e["turn"] for e in ev.items if e["event"] == "done"} == {"m1", "m2"}
    assert session.busy is False


def test_stop_means_stop_for_the_task_and_for_what_waited(tmp_path):
    gate, started = threading.Event(), threading.Event()
    model = Model((said_in("organizza"), gated(gate, started)), (said_in("mail"), lambda: say("non deve partire")))
    session, ev, *_ = make(tmp_path, model, judge=lambda text, ctx: "queue")
    session.send("m1", "organizza la cena")
    started.wait(5)
    session.send("m2", "controlla le mail")
    session.send("m3", "e anche le mail di lavoro")
    gate.set()
    session.cancel()
    done = ev.wait("done")
    assert done["stop_reason"] == al.CANCELLED and done["more"] is False
    assert ev.wait("cleared")["count"] >= 1
    time.sleep(0.4)
    assert session.busy is False and not any("non deve partire" in str(e.get("final")) for e in ev.items)
    assert session.queue == [] and session.inbox == [] and session.stack == []


def test_a_waiting_request_can_be_made_urgent_or_dropped(tmp_path):
    gate, started = threading.Event(), threading.Event()
    model = Model((said_in("[harness] You were set aside"), lambda: say("Ripreso.")), (said_in("mail"), lambda: say("Mail fatte.")),
                  (lambda m: m[-1]["role"] == "tool", lambda: say("Cena.")), (said_in("organizza"), gated(gate, started)))
    session, ev, *_ = make(tmp_path, model, judge=lambda text, ctx: "queue")
    session.send("m1", "organizza la cena")
    started.wait(5)
    session.send("m2", "controlla le mail")
    session.send("m3", "annaffia le piante")
    # let them reach the queue: they are looked at at the task's next safe point
    session._safe_point(session.tasks["m1"])
    assert [m["turn"] for m in session.queue] == ["m2", "m3"]
    assert session.drop("m3") and [m["turn"] for m in session.queue] == ["m2"] and ev.wait("dropped")["turn"] == "m3"
    assert session.drop("zz") is False
    assert session.promote("m2") and session.queue == [] and [m["turn"] for m in session.urgent] == ["m2"]
    gate.set()
    ev.wait("resumed")
    assert [e["turn"] for e in ev.items if e["event"] == "done"][:1] == ["m2"]


def test_the_waiting_requests_survive_the_process_and_wait_for_the_person_to_release_them(tmp_path):
    gate, started = threading.Event(), threading.Event()
    model = Model((said_in("mail"), lambda: say("Mail fatte.")), (said_in("organizza"), gated(gate, started, then=say("Cena."))))
    session, ev, store, built = make(tmp_path, model, judge=lambda text, ctx: "queue")
    session.send("m1", "organizza la cena")
    started.wait(5)
    session.send("m2", "controlla le mail")
    pending = json.loads((tmp_path / "hist" / "history.json").read_text(encoding="utf-8"))["pending"]
    assert [m["text"] for m in pending] == ["controlla le mail"]                # it was on disk before anything looked at it
    gate.set()
    ev.wait("done")

    model2 = Model((said_in("mail"), lambda: say("Mail fatte dopo il riavvio.")))
    session2, ev2, store2, built2 = make(tmp_path, model2)
    session2.queue = []
    # the saved file as it was while m2 waited
    (tmp_path / "hist" / "history.json").write_text(json.dumps({"messages": [{"role": "user", "content": "organizza la cena"}, {"role": "assistant", "content": "Cena."}],
                                                                 "turns": 1, "pending": [{"turn": "m2", "text": "controlla le mail"}]}), encoding="utf-8")
    session2.restore()
    assert session2.blocked["reason"] == "unfinished_requests" and "1 request" in session2.blocked["detail"]
    time.sleep(0.2)
    assert "done" not in ev2.kinds()                                            # nothing went on by itself
    assert session2.resume()
    assert ev2.wait("done")["final"] == "Mail fatte dopo il riavvio."


def test_a_message_written_while_a_confirmation_waits_is_looked_at_after_the_answer(tmp_path):
    from test_session import FakePlans, with_planner
    model = Model((said_in("dimenticavo"), lambda: say("Fatto, e ho letto il resto.")),
                  (said_in("metti via"), lambda: call("plan_archive", plan_id="p1")))
    session, ev, plans, built = with_planner(tmp_path, model)
    session.send("m1", "metti via quel piano")
    ask = ev.wait("ask")
    assert session.send("m2", "ah, dimenticavo: il viaggio è a marzo")           # not refused while it asks
    assert ev.wait("queued")["turn"] == "m2"
    assert session.idle_seconds() == 0.0
    session.answer(ask["id"], "confirm")
    assert ev.wait("done")["final"] == "Fatto, e ho letto il resto."
    assert model.users(1)[-1] == "ah, dimenticavo: il viaggio è a marzo"


def test_an_urgent_request_is_not_put_aside_for_another_urgent_one_they_go_in_order(tmp_path):
    g1, s1, g2, s2 = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    model = Model((said_in("[harness] You were set aside"), lambda: say("Ripreso.")),
                  (said_in("primo urgente"), gated(g2, s2, then=say("Primo urgente fatto."))),
                  (said_in("secondo urgente"), lambda: say("Secondo urgente fatto.")),
                  (said_in("organizza"), gated(g1, s1)))
    session, ev, *_ = make(tmp_path, model, judge=lambda text, ctx: "urgent")
    session.send("m1", "organizza la cena")
    s1.wait(5)
    session.send("m2", "primo urgente")
    g1.set()
    s2.wait(10)                                                                 # the first urgent one is under way
    session.send("m3", "secondo urgente")
    g2.set()
    until = time.monotonic() + 15
    while len([e for e in ev.items if e["event"] == "done"]) < 3 and time.monotonic() < until:
        time.sleep(0.02)
    assert [e["turn"] for e in ev.items if e["event"] == "done"] == ["m2", "m3", "m1"]
    assert [e["turn"] for e in ev.items if e["event"] == "suspended"] == ["m1"]    # only the task in hand was put aside, once
