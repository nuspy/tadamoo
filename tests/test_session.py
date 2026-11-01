"""The conversation process: history, retries, blocks, cancel, and the action that cannot be undone."""
import copy
import io
import json
import queue
import threading
import time

import pytest

from harness import agent_loop as al
from harness.client import BasaltError
from harness.session import UNAVAILABLE_NOTE, Session, SessionStore, approving_value, main, question_source, unattended_confirm
from harness.stack import AgentConfig, build_agent
from harness.tools.registry import EffectClass

@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    """These tests count the messages the model is sent; the time note has its own tests (test_clock.py)."""
    monkeypatch.setattr("harness.stack.local_now", None)



class Scripted:
    def __init__(self, *steps):
        self.steps, self.sent = list(steps), []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        step = self.steps.pop(0)
        if callable(step):
            step = step()
        if isinstance(step, BaseException):
            raise step
        return step


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def call(name, **args):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


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

    def kinds(self):
        return [e["event"] for e in self.items]


def make(tmp_path, client, **kw):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode="auto", max_turns=6), client=client)
    events = Events()
    store = SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets)
    session = Session(built, store, events, session_id="s1", waits=kw.pop("waits", (0, 0, 0)), sleep=lambda s: None, **kw)
    return session, events, store, built


def test_two_messages_in_a_row_the_second_sees_the_first(tmp_path):
    client = Scripted(say("Mi chiamo Basalt."), say("Hai detto di chiederti il nome."))
    session, ev, store, built = make(tmp_path, client)
    session.send("m1", "Come ti chiami?")
    ev.wait("done")
    session.send("m2", "Che cosa ti ho chiesto?")
    done = ev.wait("done", after=ev.kinds().index("done") + 1)
    assert done["final"] == "Hai detto di chiederti il nome."
    roles = [m["role"] for m in client.sent[1]]
    assert roles == ["system", "user", "assistant", "user"]
    assert [e["event"] for e in ev.items[:3]] == ["accepted", "started", "agent"]


def test_the_history_survives_the_process_and_the_prefix_does_not_change(tmp_path):
    client = Scripted(say("Uno."), say("Due."))
    session, ev, store, built = make(tmp_path, client)
    session.send("m1", "primo")
    ev.wait("done")
    saved = [m for m in built.agent.messages]
    client2 = Scripted(say("Tre."))
    session2, ev2, store2, built2 = make(tmp_path, client2)
    assert session2.restore() == 2
    assert [{k: v for k, v in m.items() if k != "code"} for m in built2.agent.messages[:3]] == [{k: v for k, v in m.items() if k != "code"} for m in saved[:3]]      # the same system prompt and the same first turn (the code mark is bookkeeping, made again on demand)
    session2.send("m2", "secondo")
    ev2.wait("done")
    assert client2.sent[0][:3] == [{k: v for k, v in m.items() if k not in ("pinned", "pin_category", "code")} for m in saved[:3]]


class Streamer:
    """A client that writes its answer in pieces, as the server does."""

    def __init__(self, pieces, final):
        self.pieces, self.final = pieces, final

    def chat_completion(self, messages, tools=None, on_delta=None, **kwargs):
        for kind, text in self.pieces:
            on_delta(kind, text)
        return say(self.final)


def test_the_text_is_handed_on_in_pieces_before_the_whole_message(tmp_path):
    client = Streamer([("reasoning", "penso "), ("reasoning", "un po'"), ("content", "Ciao "), ("content", "mondo")], "Ciao mondo")
    session, ev, *_ = make(tmp_path, client)
    session.send("m1", "saluta")
    ev.wait("done")
    deltas = [e for e in ev.items if e["event"] == "delta"]
    assert [(d["kind"], d["text"]) for d in deltas] == [("reasoning", "penso un po'"), ("content", "Ciao mondo")]
    order = ev.kinds()
    assert order.index("delta") < order.index("agent") < order.index("done")


def test_a_cancel_stops_the_answer_while_it_is_being_written(tmp_path):
    written, gate = [], threading.Event()

    class Slow:
        def chat_completion(self, messages, tools=None, on_delta=None, **kw):
            for i in range(1000):
                on_delta("content", f"parola{i} ")        # raises once the person has cancelled
                written.append(i)
                if i == 2:
                    gate.set()
                time.sleep(0.01)
            return say("mai arrivato")

    session, ev, store, built = make(tmp_path, Slow())
    session.send("m1", "racconta")
    gate.wait(5)
    session.cancel()
    done = ev.wait("done", 5)
    assert done["stop_reason"] == al.CANCELLED and len(written) < 100
    assert not any(m["role"] == "assistant" for m in built.agent.messages)        # the half-written answer is not kept


def test_a_second_message_while_one_runs_waits_for_the_safe_point_instead_of_being_refused(tmp_path):
    gate = threading.Event()
    client = Scripted(lambda: (gate.wait(5), say("ok"))[1], say("visto anche il secondo"))
    session, ev, *_ = make(tmp_path, client)
    session.send("m1", "lento")
    ev.wait("accepted")
    assert session.send("m2", "ancora") is True
    assert ev.wait("queued")["turn"] == "m2"
    gate.set()
    ev.wait("done")


def test_cancel_in_the_middle_of_a_turn_ends_it(tmp_path):
    started, gate = threading.Event(), threading.Event()

    def slow():
        started.set()
        gate.wait(5)
        return call("list_files", path=".")

    client = Scripted(slow)
    session, ev, *_ = make(tmp_path, client)
    session.send("m1", "lavora")
    started.wait(5)
    session.cancel()
    gate.set()
    done = ev.wait("done")
    assert done["stop_reason"] == al.CANCELLED


def test_the_message_is_on_disk_before_the_model_sees_it(tmp_path):
    seen = []

    def look():
        data = json.loads((tmp_path / "hist" / "history.json").read_text(encoding="utf-8"))
        seen.append([m["content"] for m in data["messages"] if m["role"] == "user"])
        return say("ok")

    session, ev, *_ = make(tmp_path, Scripted(look))
    session.send("m1", "non perderlo")
    ev.wait("done")
    assert seen == [["non perderlo"]]


def test_after_a_cancel_the_next_message_runs(tmp_path):
    started, gate = threading.Event(), threading.Event()

    def slow():
        started.set()
        gate.wait(5)
        return call("list_files", path=".")

    session, ev, *_ = make(tmp_path, Scripted(slow, say("Eccomi.")))
    session.send("m1", "lavora")
    started.wait(5)
    session.cancel()
    gate.set()
    assert ev.wait("done")["stop_reason"] == al.CANCELLED
    n = len(ev.items)
    session.send("m2", "ci sei?")
    assert ev.wait("done", after=n)["final"] == "Eccomi."


def test_a_busy_server_is_retried_with_waits_and_the_turn_ends_well(tmp_path):
    client = Scripted(BasaltError(503, "busy"), BasaltError(0, "cannot reach"), say("Fatto."))
    session, ev, *_ = make(tmp_path, client)
    session.send("m1", "ciao")
    done = ev.wait("done")
    assert done["final"] == "Fatto."
    retries = [e for e in ev.items if e["event"] == "retrying"]
    assert [r["attempt"] for r in retries] == [1, 2] and retries[0]["status"] == 503
    assert [m["role"] for m in client.sent[2]] == ["system", "user"]       # the message was not added again


def test_a_model_error_of_any_kind_never_blocks_it_is_retried_and_the_event_says_what_is_waited_for(tmp_path):
    client = Scripted(BasaltError(400, "the prompt is over the context"), say("Ora sì."))
    session, ev, *_ = make(tmp_path, client)
    session.send("m1", "enorme")
    assert ev.wait("done")["final"] == "Ora sì."
    assert "blocked" not in ev.kinds() and len(client.sent) == 2
    retry = next(e for e in ev.items if e["event"] == "retrying")
    assert retry["status"] == 400 and retry["waiting_for"]


def test_a_model_that_is_not_loaded_is_loaded_by_the_session_and_attended_auto_does_not_block(tmp_path):
    client = Scripted(BasaltError(503, "no model is loaded yet"), say("Carico."))
    session, ev, *_ = make(tmp_path, client, health=lambda: {"reachable": True, "loaded": False, "loading": False, "auto_load": False})
    session.endpoint = "http://127.0.0.1:1"
    loads = []
    session.load_model = lambda wait_s: loads.append(wait_s) or "started"
    session.send("m1", "ciao")
    assert ev.wait("done")["final"] == "Carico." and len(loads) == 1 and "blocked" not in ev.kinds()
    retry = next(e for e in ev.items if e["event"] == "retrying")
    assert "loading" in retry["waiting_for"] and "load_error" not in retry


def test_a_load_the_loader_refuses_is_said_in_the_event_and_the_retries_go_on(tmp_path):
    client = Scripted(BasaltError(503, "no model is loaded yet"), BasaltError(503, "no model is loaded yet"), say("Eccomi."))
    session, ev, *_ = make(tmp_path, client, health=lambda: {"reachable": True, "loaded": False, "loading": False, "auto_load": False})
    session.endpoint = "http://127.0.0.1:1"

    def refuse(wait_s):
        raise RuntimeError("no model is chosen: pick one in the Loader's model list first")
    session.load_model = refuse
    session.send("m1", "ciao")
    assert ev.wait("done")["final"] == "Eccomi." and "blocked" not in ev.kinds()
    retries = [e for e in ev.items if e["event"] == "retrying"]
    assert len(retries) == 2 and "no model is chosen" in retries[0]["load_error"]


def test_a_server_that_loads_by_itself_is_not_asked_to_load(tmp_path):
    client = Scripted(BasaltError(503, "no model is loaded yet"), say("Ok."))
    session, ev, *_ = make(tmp_path, client, health=lambda: {"reachable": True, "loaded": False, "loading": False, "auto_load": True})
    session.endpoint = "http://127.0.0.1:1"
    session.load_model = lambda wait_s: pytest.fail("loaded by the session")
    session.send("m1", "ciao")
    assert ev.wait("done")["final"] == "Ok."


def test_retries_never_run_out_and_the_last_wait_is_repeated(tmp_path):
    client = Scripted(*[BasaltError(503, "busy")] * 7, say("Finalmente."))
    session, ev, *_ = make(tmp_path, client, waits=(0, 0, 0))
    session.send("m1", "ciao")
    assert ev.wait("done")["final"] == "Finalmente."
    assert "blocked" not in ev.kinds() and len(client.sent) == 8
    retries = [e for e in ev.items if e["event"] == "retrying"]
    assert [r["attempt"] for r in retries] == list(range(1, 8)) and all("of" not in r for r in retries)


def test_the_waits_grow_up_to_a_few_minutes_and_stay_there():
    from harness.session import BACKOFF_S
    assert list(BACKOFF_S) == sorted(BACKOFF_S) and 120 <= BACKOFF_S[-1] <= 600 and BACKOFF_S[0] <= 5


def test_a_model_the_server_will_not_load_is_retried_and_the_facts_are_in_the_event(tmp_path):
    client = Scripted(BasaltError(503, "no model loaded"), say("Ora è carico."))
    session, ev, *_ = make(tmp_path, client, health=lambda: {"reachable": True, "loaded": False, "loading": False, "auto_load": False})
    session.send("m1", "ciao")
    assert ev.wait("done")["final"] == "Ora è carico."
    retry = next(e for e in ev.items if e["event"] == "retrying")
    assert retry["reachable"] is True and retry["loaded"] is False and retry["auto_load"] is False


def test_a_tool_that_fails_is_seen_by_the_model_and_not_retried_by_the_session(tmp_path):
    client = Scripted(call("read_file", path="manca.txt"), say("Il file non c'è."))
    session, ev, *_ = make(tmp_path, client)
    session.send("m1", "leggi manca.txt")
    assert ev.wait("done")["final"] == "Il file non c'è."
    assert "retrying" not in ev.kinds() and len(client.sent) == 2


class Died(BaseException):
    """What a process dying in the middle of a tool looks like to the code."""


def test_an_action_that_cannot_be_undone_is_not_run_again_by_the_harness_after_a_crash(tmp_path):
    ran = []

    def pay(**kw):
        ran.append(kw)
        raise Died()

    client = Scripted(call("pay", amount="10"))
    session, ev, store, built = make(tmp_path, client)
    built.agent.tools.register("pay", "pays", {"type": "object", "properties": {"amount": {"type": "string"}}}, EffectClass.IRREVERSIBLE, pay)
    session.send("m1", "paga")
    assert ev.wait("blocked")["reason"] == "crash" and len(ran) == 1
    assert store.inflight()["tool"] == "pay"

    client2 = Scripted(say("Controllo prima di fare altro."))
    session2, ev2, store2, built2 = make(tmp_path, client2)
    built2.agent.tools.register("pay", "pays", {"type": "object", "properties": {}}, EffectClass.IRREVERSIBLE, lambda **k: ran.append("again"))
    session2.restore()
    assert session2.blocked is None                                             # nothing stops: the model is told instead
    assert store2.inflight() is None
    session2.send("m2", "come è andata?")
    ev2.wait("done")
    note = next(m["content"] for m in client2.sent[0] if m["role"] == "user" and "may have run" in m["content"])
    assert "pay" in note and "Check what really happened before repeating it" in note
    assert ran == [{"amount": "10"}]                                          # the harness never runs it a second time


def test_no_secret_reaches_the_history_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_TEST_SECRET", "s3cr3t-value-123")
    client = Scripted(say("ok"))
    ws = tmp_path / "ws"
    ws.mkdir()
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    secret_names=["BASALT_TEST_SECRET"]), client=client)
    events = Events()
    store = SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets)
    session = Session(built, store, events, session_id="s1", sleep=lambda s: None)
    session.send("m1", "la chiave è s3cr3t-value-123, non scriverla")
    events.wait("done")
    text = (tmp_path / "hist" / "history.json").read_text(encoding="utf-8")
    assert "s3cr3t-value-123" not in text and "[REDACTED:BASALT_TEST_SECRET]" in text


class Feed:
    """A stdin that gives its lines and then waits, as a pipe does, until told to end."""

    def __init__(self):
        self.q = queue.Queue()

    def put(self, obj):
        self.q.put(json.dumps(obj) + "\n")

    def end(self):
        self.q.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.q.get(timeout=20)
        if line is None:
            raise StopIteration
        return line


def test_the_process_speaks_lines_of_json_and_closes_on_command(tmp_path, monkeypatch):
    import harness.session as hs
    client = Scripted(say("Ciao dal processo."))
    ws = tmp_path / "ws"
    ws.mkdir()
    real_build = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, technical=None, _c=client: real_build(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "p1",
                                              "--no-memory", "--no-checkpoints", "--heartbeat", "0.05"], stdin=feed, stdout=out), daemon=True)
    t.start()
    feed.put({"cmd": "send", "id": "m1", "text": "ciao"})
    deadline = time.monotonic() + 10
    while '"done"' not in out.getvalue() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.4)                                   # a few beats
    feed.put({"cmd": "bogus"})
    feed.put({"cmd": "close"})
    t.join(10)
    events = [json.loads(l) for l in out.getvalue().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "ready" and "accepted" in kinds and "done" in kinds and kinds[-1] == "closed"
    assert "heartbeat" in kinds and any(e.get("code") == "bad_command" for e in events)
    assert next(e for e in events if e["event"] == "done")["final"] == "Ciao dal processo."
    assert json.loads((tmp_path / "h" / "history.json").read_text(encoding="utf-8"))["turns"] == 1


def test_a_position_sent_with_a_message_brings_the_tool_for_that_turn(tmp_path, monkeypatch):
    import harness.session as hs
    offered = []

    class Spy(Scripted):
        def chat_completion(self, messages, tools=None, **kwargs):
            offered.append([t["function"]["name"] for t in tools or []])
            return super().chat_completion(messages, tools=tools, **kwargs)

    client = Spy(call("where_is_user"), say("Sei in Via Roma."))
    ws = tmp_path / "ws"
    ws.mkdir()
    real_build = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, technical=None, _c=client: real_build(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "pl",
                                              "--no-memory", "--no-checkpoints", "--heartbeat", "0.05"], stdin=feed, stdout=out), daemon=True)
    t.start()
    feed.put({"cmd": "send", "id": "m1", "text": "ristoranti qui vicino", "place": {
        "lat": 45.46427, "lon": 9.18951, "address": {"street": "Via Roma", "number": "12", "city": "Milano"}}})
    deadline = time.monotonic() + 10
    while '"done"' not in out.getvalue() and time.monotonic() < deadline:
        time.sleep(0.05)
    feed.put({"cmd": "close"})
    t.join(10)
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    ended = next(e["data"] for e in events if e["event"] == "agent" and e["data"].get("type") == "tool_end")
    assert "Via Roma 12" in ended["output"] and "Milano" in ended["output"]
    assert "where_is_user" in offered[0]


# ---- questions to the person: confirmations that wait, questions that end the turn, the plans note ----------------

class FakePlans:
    """Stands in for the planner client: the plans live in a list."""

    def __init__(self, plans=None):
        self.plans = plans if plans is not None else [{"plan_id": "p1", "title": "Viaggio a Cervinia", "nodes": 2, "completed": 0}]
        self.archived = []

    def list(self, archived=False):
        return list(self.plans)

    def snapshot(self, plan_id):
        p = next(x for x in self.plans if x["plan_id"] == plan_id)
        return {"plan": {"plan_id": plan_id, "title": p["title"], "target_type": "occasional", "nodes": {}}, "rev": 1}

    def archive(self, plan_id):
        self.archived.append(plan_id)
        self.plans = [p for p in self.plans if p["plan_id"] != plan_id]


def with_planner(tmp_path, client, plans=None, mode="default"):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode=mode, max_turns=6), client=client)
    events = Events()
    store = SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets)
    session = Session(built, store, events, session_id="s1", sleep=lambda s: None)
    built.permissions.confirm_callback = session.permission_confirm
    fake = plans or FakePlans()
    session.enable_planner(fake)
    return session, events, fake, built


def test_a_confirmation_is_a_question_with_buttons_the_turn_waits_for(tmp_path):
    client = Scripted(call("plan_archive", plan_id="p1"), say("Archiviato."))
    session, ev, plans, built = with_planner(tmp_path, client)
    session.send("m1", "metti via quel piano")
    ask = ev.wait("ask")
    assert ask["mode"] == "confirm" and "Viaggio a Cervinia" in ask["question"] and ask["facts"]["kind"] == "plan_archive"
    assert [o["value"] for o in ask["options"]] == ["confirm", "cancel"]
    time.sleep(0.3)
    assert session.waiting and session.busy and plans.archived == []                  # waiting, and nothing done yet
    assert session.answer(ask["id"], "confirm")
    assert ev.wait("done")["final"] == "Archiviato." and plans.archived == ["p1"]
    assert session.waiting is False


def test_an_answer_that_is_not_one_of_the_buttons_is_ignored_and_a_cancel_ends_the_wait(tmp_path):
    client = Scripted(call("plan_archive", plan_id="p1"))
    session, ev, plans, built = with_planner(tmp_path, client)
    session.send("m1", "metti via quel piano")
    ask = ev.wait("ask")
    assert session.answer(ask["id"], "forse") is False and ev.wait("error")["code"] == "bad_answer"
    assert session.answer("nope", "confirm") is False
    session.cancel()
    assert ev.wait("done")["stop_reason"] == al.CANCELLED and plans.archived == []


def test_a_question_tool_ends_the_turn_and_the_answer_is_the_next_message(tmp_path):
    client = Scripted(call("ask_user", questions=[{"text": "Quale giorno?", "kind": "single", "options": [{"label": "Sabato", "recommended": True}, {"label": "Domenica"}]}]),
                      say("Va bene sabato."))
    session, ev, plans, built = with_planner(tmp_path, client)
    session.send("m1", "organizza il weekend")
    ask = ev.wait("ask")
    assert ask["mode"] == "question" and ask["kind"] == "questions" and ask["questions"][0]["options"][0]["recommended"] and ask["question"] == "Quale giorno?"
    done = ev.wait("done")
    assert done["stop_reason"] == al.ASKED and len(client.sent) == 1                  # the model was not called again
    n = len(ev.items)
    text = "The user answered your questions:\n1. Quale giorno? -> Sabato (your recommended option)\nGo on with these answers. Do not ask them again."
    session.send("m2", text)                                                           # the server puts the card's answer in words
    assert ev.wait("done", after=n)["final"] == "Va bene sabato."
    assert [m["content"] for m in client.sent[1] if m["role"] == "user"][-1] == text
    assert "Asking the user (ask_user)" in client.sent[0][0]["content"]               # the rules are in the prompt when the tool is


def test_the_permission_question_is_put_in_the_conversation(tmp_path):
    ran = []
    client = Scripted(call("pay", amount="5"), say("Fatto."))
    session, ev, plans, built = with_planner(tmp_path, client, mode="default")
    built.agent.tools.register("pay", "pays", {"type": "object", "properties": {"amount": {"type": "string"}}}, EffectClass.IRREVERSIBLE,
                               lambda **kw: ran.append(kw) or "paid")
    session.send("m1", "paga")
    ask = ev.wait("ask")
    assert ask["kind"] == "permission" and ask["tool"] == "pay" and ask["arguments"] == {"amount": "5"}
    session.answer(ask["id"], "confirm")
    ev.wait("done")
    assert ran == [{"amount": "5"}]


def test_a_no_to_the_permission_question_runs_nothing(tmp_path):
    ran = []
    client = Scripted(call("pay", amount="5"), say("Non l'ho fatto."))
    session, ev, plans, built = with_planner(tmp_path, client, mode="default")
    built.agent.tools.register("pay", "pays", {"type": "object", "properties": {"amount": {"type": "string"}}}, EffectClass.IRREVERSIBLE,
                               lambda **kw: ran.append(kw) or "paid")
    session.send("m1", "paga")
    session.answer(ev.wait("ask")["id"], "cancel")
    ev.wait("done")
    assert ran == []


def test_the_plans_note_comes_before_the_message_and_only_when_the_plans_changed(tmp_path):
    client = Scripted(say("Uno."), say("Due."), say("Tre."))
    session, ev, plans, built = with_planner(tmp_path, client)
    session.send("m1", "ciao")
    ev.wait("done")
    first = [m["content"] for m in client.sent[0] if m["role"] == "user"]
    assert first[0].startswith("[harness] The user's plans now:") and "Viaggio a Cervinia" in first[0] and first[1] == "ciao"
    n = len(ev.items)
    session.send("m2", "ancora")
    ev.wait("done", after=n)
    second = [m["content"] for m in client.sent[1] if m["role"] == "user"]
    assert second == first + ["ancora"]                                                 # nothing new while nothing changed
    plans.plans = []
    n = len(ev.items)
    session.send("m3", "e adesso?")
    ev.wait("done", after=n)
    third = [m["content"] for m in client.sent[2] if m["role"] == "user"]
    assert third[-2] == "[harness] The user has no active plans now." and third[-1] == "e adesso?"
    assert client.sent[1][:len(client.sent[0])] == client.sent[0]                      # the prefix did not change between two turns


def test_a_turn_waiting_for_the_person_is_never_reported_as_stuck(tmp_path):
    session, ev, plans, built = with_planner(tmp_path, Scripted(call("plan_archive", plan_id="p1"), say("Ok, niente.")))
    session.send("m1", "metti via quel piano")
    ask = ev.wait("ask")
    session.last_progress -= 3600                          # an hour of silence from the model
    assert session.waiting and session.idle_seconds() == 0.0
    session.answer(ask["id"], "cancel")
    ev.wait("done")
    session.busy, session.last_progress = True, time.monotonic() - 100
    assert 99 <= session.idle_seconds() <= 102             # a turn that is only silent does count


def with_vault(tmp_path, client, entries):
    from harness.vault import Vault
    vault = Vault(tmp_path / "vault")
    for entry_id, kw in entries.items():
        vault.put(entry_id, **kw)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode="auto", max_turns=6, vault_dir=str(tmp_path / "vault")), client=client)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None)
    built.agent.secrets.ask_code = session.ask_code
    return session, events, built


def test_the_model_is_told_the_names_in_the_vault_and_never_a_value(tmp_path):
    client = Scripted(say("ok"))
    session, ev, built = with_vault(tmp_path, client, {"name": {"kind": "identity", "fields": {"full_name": "Mario Rossi"}}})
    session.send("m1", "ciao")
    ev.wait("done")
    system = client.sent[0][0]["content"]
    assert "{{vault:ENTRY.FIELD}}" in system and "name (identity" in system
    assert "Mario Rossi" not in json.dumps(client.sent[0])


def test_a_second_factor_code_is_asked_to_the_person_and_reaches_only_the_command(tmp_path):
    client = Scripted(call("shell_command", command="echo code={{vault:bank.totp}}"), say("fatto"))
    session, ev, built = with_vault(tmp_path, client, {"bank": {"kind": "login", "fields": {"username": "mr"}, "second_factor": "manual"}})
    session.send("m1", "entra in banca")
    ask = ev.wait("ask")
    assert ask["kind"] == "code" and ask["free"] is True and ask["facts"] == {"who": "bank"} and ask["options"] == []
    assert session.answer(ask["id"], "not a code!") is False and ev.wait("error")["code"] == "bad_answer"
    assert session.answer(ask["id"], "554 433")
    assert ev.wait("done")["final"] == "fatto"
    seen = json.dumps(client.sent[1])
    assert "554" not in seen and "{{vault:bank.totp}}" not in seen.replace("echo code={{vault:bank.totp}}", "")
    kept = "".join(f.read_text(encoding="utf-8", errors="ignore") for f in (tmp_path / "hist").rglob("*") if f.is_file())
    assert "554" not in kept


def test_a_cancelled_code_question_stops_the_command(tmp_path):
    client = Scripted(call("shell_command", command="echo code={{vault:bank.totp}}"), say("annullato"))
    session, ev, built = with_vault(tmp_path, client, {"bank": {"kind": "login", "fields": {"username": "mr"}, "second_factor": "manual"}})
    session.send("m1", "entra in banca")
    ev.wait("ask")
    session.cancel()
    assert ev.wait("done")["stop_reason"] == al.CANCELLED


def test_the_time_a_tool_waits_for_the_person_does_not_count_against_its_limit(tmp_path):
    client = Scripted(call("needs_yes"), say("fatto"))
    session, ev, store, built = make(tmp_path, client)
    built.agent.tools.default_timeout = 0.5
    answers = []
    built.agent.tools.register("needs_yes", "asks", {"type": "object", "properties": {}}, EffectClass.PURE,
                               lambda: answers.append(session.permission_confirm("x", {})) or "asked")
    session.send("m1", "go")
    ask = ev.wait("ask")
    time.sleep(1.3)                                               # the person thinks for longer than the tool's limit
    assert session.answer(ask["id"], "confirm")
    done = ev.wait("done")
    assert answers == [True] and done["stop_reason"] == al.FINAL


def _plan_session(tmp_path, client, **cfg):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode="plan", max_turns=8, **cfg), client=client)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist"), events, session_id="s1", waits=(0,), sleep=lambda s: None)
    return session, events, built, ws


def test_a_plan_the_person_approves_ends_plan_mode_and_the_work_goes_on(tmp_path):
    client = Scripted(call("write_file", path="a.txt", content="early"), call("present_plan", plan="1. write a.txt\n2. run the tests"),
                      call("write_file", path="a.txt", content="after approval"), say("done"))
    session, ev, built, ws = _plan_session(tmp_path, client)
    assert "present_plan" in built.agent.tools.names() and built.permissions.plan_mode
    session.send("m1", "do it")
    ask = ev.wait("ask")
    assert ask["kind"] == "plan_approval" and "1. write a.txt" in ask["question"] and not (ws / "a.txt").exists()      # plan mode refused the early write
    assert session.answer(ask["id"], "confirm")
    done = ev.wait("done")
    assert done["stop_reason"] == al.FINAL and (ws / "a.txt").read_text(encoding="utf-8") == "after approval"
    assert not built.permissions.plan_mode and built.permissions.level.value == "default"
    assert "present_plan" in built.agent.tools.names()                       # the tool list does not change with the mode
    assert [r["action"] for r in built.audit.records if r["action"].startswith("plan_")] == ["plan_approved"]
    assert any("Plan mode active" in m["content"] for m in built.agent.messages if m["role"] == "tool")


def test_a_plan_the_person_sends_back_keeps_plan_mode_and_tells_the_model(tmp_path):
    client = Scripted(call("present_plan", plan="rewrite everything"), call("write_file", path="a.txt", content="x"), say("I will revise"))
    session, ev, built, ws = _plan_session(tmp_path, client)
    session.send("m1", "do it")
    session.answer(ev.wait("ask")["id"], "cancel")
    ev.wait("done")
    assert built.permissions.plan_mode and "present_plan" in built.agent.tools.names() and not (ws / "a.txt").exists()
    told = [m["content"] for m in built.agent.messages if m["role"] == "tool"]
    assert "did not approve" in told[0] and "Plan mode active" in told[1]
    assert [r["action"] for r in built.audit.records if r["action"].startswith("plan_")] == ["plan_rejected"]


def test_the_mode_a_plan_goes_on_in_is_the_persons_choice(tmp_path):
    client = Scripted(call("present_plan", plan="p"), say("ok"))
    session, ev, built, ws = _plan_session(tmp_path, client, after_plan="auto")
    session.send("m1", "go")
    session.answer(ev.wait("ask")["id"], "confirm")
    ev.wait("done")
    assert built.permissions.level.value == "auto" and not built.permissions.plan_mode


def test_a_run_with_nobody_to_ask_stays_a_plan_and_the_mode_can_be_set_back_to_plan(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, permission_mode="plan"), client=object())
    try:
        tool = built.agent.tools
        assert "Nobody can approve" in tool.execute("present_plan", {"plan": "do x"}) and built.permissions.plan_mode
        assert "plan is empty" in tool.execute("present_plan", {"plan": "  "})
        built.set_mode("auto")
        assert "present_plan" in tool.names() and not built.permissions.plan_mode
        assert "Plan mode is not on" in tool.execute("present_plan", {"plan": "do x"})            # there is nothing to approve outside plan mode
        built.set_mode("plan")
        assert "present_plan" in tool.names() and built.permissions.plan_mode
        with pytest.raises(ValueError):
            built.set_mode("anything")
        with pytest.raises(ValueError):
            build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d2"), use_memory=False, after_plan="read_only"), client=object())
    finally:
        built.close()


def test_plan_mode_is_not_left_by_a_model_that_only_says_it_was_approved(tmp_path):
    client = Scripted(call("present_plan", plan="p"), call("write_file", path="b.txt", content="sneaky"), say("done"))
    session, ev, built, ws = _plan_session(tmp_path, client)
    session.send("m1", "go")
    ev.wait("ask")
    session.cancel()                                          # Stop while the question is open counts as no
    ev.wait("done")
    assert built.permissions.plan_mode and not (ws / "b.txt").exists()


def test_refused_calls_are_counted_in_the_conversation_state_and_in_its_closing_report(tmp_path, monkeypatch):
    import harness.session as hs
    client = Scripted(call("shell_command", command="echo hi"), call("shell_command", command="echo again"), say("I could not run them."))
    ws = tmp_path / "ws"
    ws.mkdir()
    real_build = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, technical=None, _c=client: real_build(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "d1", "--deny", "shell_command:*",
                                              "--no-memory", "--no-checkpoints", "--no-planner", "--heartbeat", "0.05"], stdin=feed, stdout=out), daemon=True)
    t.start()
    feed.put({"cmd": "send", "id": "m1", "text": "run things"})
    deadline = time.monotonic() + 10
    while '"done"' not in out.getvalue() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    feed.put({"cmd": "close"})
    t.join(10)
    events = [json.loads(l) for l in out.getvalue().splitlines()]
    assert next(e for e in events if e["event"] == "done")["denied"] == 2
    assert [e["denied"] for e in events if e["event"] == "heartbeat"][-1] == 2
    assert events[-1]["event"] == "closed" and events[-1]["permission_denials"] == {"total": 2, "by_tool": {"shell_command": 2}}


def test_the_session_end_hook_and_the_headless_result_carry_the_count(tmp_path):
    from harness.headless import run_headless
    from harness.hooks import HookManager
    from harness.agent_loop import AgentLoop
    from harness.permissions import PermissionLevel, PermissionManager
    from harness.tools import create_standard_tool_registry
    ws = tmp_path / "ws"
    ws.mkdir()
    seen = []
    hooks = HookManager()
    hooks.register_event_hook("session_end", seen.append)
    registry, _ = create_standard_tool_registry(ws)
    agent = AgentLoop(Scripted(call("shell_command", command="echo hi"), say("no")), registry, permissions=PermissionManager(level=PermissionLevel.READ_ONLY), hooks=hooks)
    agent.run("go")
    agent.close()
    assert seen == [{"turns": 2, "stop_reason": "final", "permission_denials": 1}]
    cfg = AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, report=False, permission_mode="read_only")
    result = run_headless(cfg, "go", client=Scripted(call("shell_command", command="echo hi"), say("blocked")))
    assert result["permission_denials"] == {"total": 1, "by_tool": {"shell_command": 1}} and result["status"] == "success"


def test_always_allow_answers_the_question_and_the_same_kind_of_command_then_runs_without_asking(tmp_path):
    ran = []
    client = Scripted(call("shell_command", command="pytest -q"), call("shell_command", command="pytest tests/a.py"), say("Fatto."))
    session, ev, plans, built = with_planner(tmp_path, client, mode="default")
    built.agent.tools.register("shell_command", "runs", {"type": "object", "properties": {"command": {"type": "string"}}}, EffectClass.IRREVERSIBLE,
                               lambda **kw: ran.append(kw) or "ok")
    session.send("m1", "prova")
    ask = ev.wait("ask")
    assert [o["value"] for o in ask["options"]] == ["confirm", "chat", "always", "cancel"]       # this call, this conversation, for good
    always = next(o for o in ask["options"] if o["value"] == "always")
    assert always["rule"] == "shell_command:pytest *"
    session.answer(ask["id"], "always")
    added = ev.wait("rule_added")
    assert added["rule"] == "shell_command:pytest *" and added["scope"] == "global"
    ev.wait("done")
    assert [r["command"] for r in ran] == ["pytest -q", "pytest tests/a.py"]
    assert len([e for e in ev.items if e["event"] == "ask"]) == 1                      # the second command did not ask


def test_allowing_for_this_conversation_is_not_for_good_and_a_program_that_runs_code_gets_the_same_choices(tmp_path):
    client = Scripted(call("shell_command", command="python -c 1"), say("Fatto."))
    session, ev, plans, built = with_planner(tmp_path, client, mode="default")
    built.agent.tools.register("shell_command", "runs", {"type": "object", "properties": {"command": {"type": "string"}}}, EffectClass.IRREVERSIBLE,
                               lambda **kw: "ok")
    session.send("m1", "prova")
    ask = ev.wait("ask")
    chat = next(o for o in ask["options"] if o["value"] == "chat")
    assert chat["rule"] == "shell_command:python *"
    session.answer(ask["id"], "chat")
    assert ev.wait("rule_added")["scope"] == "chat"


def test_a_command_that_chains_others_gets_the_same_three_choices_and_a_payment_only_its_own(tmp_path):
    client = Scripted(call("shell_command", command="pytest && rm -rf x"), say("no"))
    session, ev, plans, built = with_planner(tmp_path, client, mode="default")
    built.agent.tools.register("shell_command", "runs", {"type": "object", "properties": {"command": {"type": "string"}}}, EffectClass.IRREVERSIBLE,
                               lambda **kw: "ok")
    session.send("m1", "prova")
    ask = ev.wait("ask")
    assert [o["value"] for o in ask["options"]] == ["confirm", "chat", "always", "cancel"]
    assert ask["options"][2]["rules"] == ["shell_command:pytest *", "shell_command:rm *"]
    session.answer(ask["id"], "cancel")
    ev.wait("done")


def test_a_tool_with_a_colon_in_its_name_gets_the_three_levels_and_its_own_rule(tmp_path):
    session, ev, plans, built = with_planner(tmp_path, Scripted(), mode="default")
    answers = []
    t = threading.Thread(target=lambda: answers.append(session.permission_confirm("acp_permission:codex:run tests", {"title": "run tests"})))
    t.start()
    ask = ev.wait("ask")
    assert [o["value"] for o in ask["options"]] == ["confirm", "chat", "always", "cancel"]
    assert ask["options"][2]["rules"] == ["acp_permission:codex:run tests"]
    session.answer(ask["id"], "always")
    t.join(5)
    assert answers == [True] and ev.wait("rule_added")["rule"] == "acp_permission:codex:run tests"
    assert built.permissions.allowed_by("acp_permission:codex:run tests", {})              # the next one passes without asking


def test_a_payment_the_broker_does_not_decide_gets_the_three_levels_and_an_allow_covers_the_next(tmp_path):
    session, ev, plans, built = with_planner(tmp_path, Scripted(), mode="default")
    request = {"amount": "5", "recipient": "Bar", "reason": "caffe", "system": "card"}
    answers = []
    t = threading.Thread(target=lambda: answers.append(session.ask_payment(request)))
    t.start()
    ask = ev.wait("ask")
    assert [o["value"] for o in ask["options"]] == ["confirm", "chat", "always", "cancel"]
    assert ask["options"][1]["rules"] == ["request_payment:card"]
    session.answer(ask["id"], "chat")
    t.join(5)
    assert answers == [True] and ev.wait("rule_added")["scope"] == "chat"
    assert session.ask_payment(request) is True and len([e for e in ev.items if e["event"] == "ask"]) == 1


def test_a_phone_call_in_a_manual_chat_has_the_three_levels_and_the_always_rule_covers_the_next_call(tmp_path):
    ran = []
    client = Scripted(call("phone_call", x="Anna"), call("phone_call", x="Luca"), say("Fatto."))
    session, ev, plans, built = with_planner(tmp_path, client, mode="default")
    register(built, "phone_call", ran)
    session.send("m1", "chiama")
    ask = ev.wait("ask")
    assert ask["kind"] == "permission" and [o["value"] for o in ask["options"]] == ["confirm", "chat", "always", "cancel"]
    assert ask["options"][2]["rules"] == ["phone_call"]
    session.answer(ask["id"], "always")
    ev.wait("done")
    assert [r["x"] for r in ran] == ["Anna", "Luca"] and len([e for e in ev.items if e["event"] == "ask"]) == 1


def test_a_phone_call_at_the_auto_level_is_not_asked_even_with_the_person_there(tmp_path):
    ran = []
    client = Scripted(call("phone_call", x="Anna"), say("Fatto."))
    session, ev, plans, built = with_planner(tmp_path, client, mode="auto")
    register(built, "phone_call", ran)
    session.send("m1", "chiama")
    ev.wait("done")
    assert ran == [{"x": "Anna"}] and "ask" not in ev.kinds()


def test_the_rule_of_two_question_has_the_three_levels_and_an_always_answer_stops_it_asking_again(tmp_path):
    ran = []
    client = Scripted(call("browse_url", x="a"), call("browse_url", x="b"), say("Fatto."))
    session, ev, plans, built = with_planner(tmp_path, client, mode="default")
    built.agent.tools.register("browse_url", "opens", {"type": "object", "properties": {"x": {"type": "string"}}}, EffectClass.PURE,
                               lambda **kw: ran.append(kw) or "page", outward=True)
    built.permissions.two.set_level("ask")
    built.permissions.note_untrusted_input("a page")
    built.permissions.note_private_data(".env")
    session.send("m1", "apri")
    ask = ev.wait("ask")
    assert [o["value"] for o in ask["options"]] == ["confirm", "chat", "always", "cancel"] and ask["options"][2]["rules"] == ["browse_url"]
    session.answer(ask["id"], "chat")
    ev.wait("done")
    assert len(ran) == 2 and len([e for e in ev.items if e["event"] == "ask"]) == 1


def test_an_unattended_session_answers_the_card_itself_parks_what_involves_people_and_does_not_end_the_turn(tmp_path):
    card = [{"text": "Quale giorno?", "kind": "single", "options": [{"label": "Sabato", "recommended": True}, {"label": "Domenica"}]},
            {"text": "Chiamo il veterinario?", "kind": "confirm", "recommended": True, "involves": ["people: il veterinario"]},
            {"text": "Il nome?", "kind": "free", "default": "Rex"}, {"text": "Quanti giorni?", "kind": "free"}]
    client = Scripted(call("ask_user", questions=card), say("Scelgo sabato."))
    session, ev, store, built = unattended_session(tmp_path, client)
    session.enable_planner(FakePlans())
    session.send("m1", "organizza")
    done = ev.wait("done")
    assert done["final"] == "Scelgo sabato." and "ask" not in ev.kinds() and done["stop_reason"] != al.ASKED
    autos = [e for e in ev.items if e["event"] == "auto_answered"]
    assert [e["chosen"] for e in autos] == ["recommended", "parked", "default", "none"] and all(e["kind"] == "question" for e in autos)
    assert autos[1]["source"]["named"] == "people"
    told = json.dumps(client.sent[1])
    assert "Sabato (chosen automatically: your recommended option)" in told and "Rex (default)" in told and "the person will answer later" in told
    assert "decide yourself, conservatively" in told
    pending = ev.wait("pending")
    assert pending["kind"] == "parked_question" and pending["question"] == "Chiamo il veterinario?" and pending["source"]["named"] == "people"
    assert session.answer(pending["id"], "yes") is True and ev.wait("pending_done")["value"] == "Yes"


def test_a_skill_run_asked_while_a_task_is_under_way_is_queued_and_started_when_it_is_free(tmp_path):
    gate, started = threading.Event(), threading.Event()

    def slow():
        started.set()
        gate.wait(5)
        return say("fatto")

    session, ev, store, built = make(tmp_path, Scripted(slow))
    ran = []
    session._run_skill_task = lambda task: (ran.append(task.skill["name"]), setattr(task.agent, "stop_reason", al.FINAL), "done")[2]
    session.send("m1", "lavora")
    started.wait(5)
    assert session.run_skill("k1", {"name": "daily"}) is True
    queued = ev.wait("queued")
    assert queued["kind"] == "skill" and queued["position"] == 1 and ran == [] and not session.idle()
    gate.set()
    ev.wait("accepted")
    deadline = time.monotonic() + 5
    while (ran != ["daily"] or not session.idle()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ran == ["daily"] and session.skill_queue == []


def test_a_change_to_a_plan_asks_nothing_at_the_auto_level(tmp_path):
    client = Scripted(call("plan_archive", plan_id="p1"), say("Archiviato."))
    session, ev, plans, built = with_planner(tmp_path, client, mode="auto")
    session.send("m1", "metti via quel piano")
    ev.wait("done")
    assert "ask" not in ev.kinds()


def unattended_session(tmp_path, client, **kw):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode="default", max_turns=6), client=client)
    events = Events()
    store = SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets)
    session = Session(built, store, events, session_id="s1", sleep=lambda s: None, unattended=True, **kw)
    built.permissions.confirm_callback = session.permission_confirm
    return session, events, store, built


def register(built, name, ran):
    built.agent.tools.register(name, "does it", {"type": "object", "properties": {"x": {"type": "string"}}}, EffectClass.IRREVERSIBLE,
                               lambda **kw: ran.append(kw) or "done it")


def test_an_unattended_session_approves_what_is_not_a_named_exception_at_once(tmp_path):
    ran = []
    client = Scripted(call("pay", x="5"), say("Fatto."))
    session, ev, store, built = unattended_session(tmp_path, client)
    register(built, "pay", ran)
    session.send("m1", "paga")
    assert ev.wait("done")["final"] == "Fatto."
    assert ran == [{"x": "5"}] and "ask" not in ev.kinds()                              # nobody was asked, nothing waited
    auto = ev.wait("auto_answered")
    assert auto["kind"] == "permission" and auto["value"] == "confirm" and auto["available"] is True and auto["source"]["named"] is None


def test_an_unattended_session_tells_the_model_plainly_that_data_only_the_person_has_is_not_available(tmp_path):
    session, ev, store, built = unattended_session(tmp_path, Scripted())
    assert session.ask_code("bank") == ""
    auto = ev.wait("auto_answered")
    assert auto["kind"] == "code" and auto["available"] is False and auto["source"]["needs_data"] is True
    assert "not available" in UNAVAILABLE_NOTE and "Continue with what you have" in UNAVAILABLE_NOTE


def test_an_unattended_session_parks_a_call_to_another_person_and_the_model_goes_on(tmp_path):
    ran = []
    client = Scripted(call("phone_call", x="Anna"), say("Ho messo la chiamata in attesa e vado avanti."))
    session, ev, store, built = unattended_session(tmp_path, client)
    register(built, "phone_call", ran)
    session.send("m1", "chiama Anna")
    assert ev.wait("done")["final"] == "Ho messo la chiamata in attesa e vado avanti."
    pending = ev.wait("pending")
    assert pending["kind"] == "permission" and pending["tool"] == "phone_call" and pending["source"]["named"] == "people"
    assert ran == [] and "ask" not in ev.kinds()
    seen = json.dumps(client.sent[1])
    assert "The answer is pending" in seen and "go on with other work".lower() in seen.lower()
    assert [i["id"] for i in session.parked_questions()] == [pending["id"]]
    assert json.loads((tmp_path / "hist" / "history.json").read_text(encoding="utf-8"))["parked"][0]["id"] == pending["id"]


def test_the_answer_to_a_parked_question_is_applied_and_the_model_is_told(tmp_path):
    ran = []
    client = Scripted(call("phone_call", x="Anna"), say("In attesa."), call("phone_call", x="Anna"), say("Chiamata fatta."))
    session, ev, store, built = unattended_session(tmp_path, client)
    register(built, "phone_call", ran)
    session.send("m1", "chiama Anna")
    ev.wait("done")
    pending = ev.wait("pending")
    assert session.answer(pending["id"], "maybe") is False and ev.wait("error")["code"] == "bad_answer"
    assert session.answer(pending["id"], "confirm")
    assert ev.wait("pending_done")["id"] == pending["id"]
    ev.wait("done", after=ev.kinds().index("done") + 1)                                  # a turn was started for the note
    assert "The user answered a question that was pending" in json.dumps(client.sent[2]) and "run the call again" in json.dumps(client.sent[2])
    assert ran == [{"x": "Anna"}]                                                        # the re-run call passes without asking again
    assert session.parked_questions() == []


def test_a_parked_question_survives_the_process(tmp_path):
    client = Scripted(call("phone_call", x="Anna"), say("In attesa."))
    session, ev, store, built = unattended_session(tmp_path, client)
    register(built, "phone_call", [])
    session.send("m1", "chiama Anna")
    pending = ev.wait("pending")
    ev.wait("done")
    session2, ev2, store2, built2 = unattended_session(tmp_path, Scripted())
    session2.restore()
    assert [i["id"] for i in session2.parked_questions()] == [pending["id"]]


def test_a_session_with_the_person_present_still_asks(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted())
    result = []
    t = threading.Thread(target=lambda: result.append(session.ask_code("bank")))
    t.start()
    ask = ev.wait("ask")
    assert ask["kind"] == "code" and "auto_answered" not in ev.kinds()
    session.answer(ask["id"], "123456")
    t.join(5)
    assert result == ["123456"]


def test_the_source_of_a_question_is_told_with_facts():
    assert question_source({"kind": "payment"})["named"] == "resource"
    assert question_source({"kind": "permission", "tool": "phone_call"})["named"] == "people"
    assert question_source({"kind": "permission", "tool": "shell_command"})["named"] is None
    assert question_source({"facts": {"kind": "plan_x", "involves": ["a booking"]}})["named"] == "booking"
    assert question_source({"facts": {"involves": ["someone else"]}})["named"] == "people"
    assert question_source({"kind": "code"})["needs_data"] is True
    assert approving_value({"options": [{"value": "cancel"}, {"value": "confirm"}]}) == "confirm"
    assert approving_value({"options": [{"value": "no"}, {"value": "yes"}]}) == "yes"
    assert approving_value({"options": [{"value": "Saturday"}]}) == "Saturday"


def test_the_approvals_board_of_an_unattended_run_only_gets_the_named_exceptions():
    board = []
    confirm = unattended_confirm(lambda tool, arguments: board.append(tool) or False)
    assert confirm("shell_command", {}) is True and board == []
    assert confirm("phone_call", {}) is False and board == ["phone_call"]
