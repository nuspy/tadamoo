"""Prompt intake: cleaning that loses nothing, the gate, the validated triage form, the ask-or-act policy, the note, and the session around them."""
import copy
import json
import threading
import time
from datetime import datetime

import pytest

from tadamoo import agent_loop as al
from tadamoo import intake as it
from tadamoo import questions as qs
from tadamoo.session import Session, SessionStore
from tadamoo.stack import AgentConfig, build_agent

NBSP = chr(160)


# -- cleaning -----------------------------------------------------------------------------------------------------------

def test_cleaning_removes_only_what_is_invisible_or_noise_and_leaves_fenced_code_alone():
    code = "```py\r\nx = 1   \r\n\r\n\r\n\r\ny = '\u200b'\n```"
    raw = "Ciao\u200b\u202e  mondo   \r\n\r\n\r\n\r\nSecondo\x1b[31m rosso\x1b[0m\x00 \u00e0" + "\n" + code
    out = it.clean(raw)
    assert out.startswith("Ciao  mondo\n\nSecondo rosso \u00e0\n")
    assert "x = 1   \n\n\n\ny = '\u200b'" in out
    assert it.clean("e\u0301") == "\u00e9"                                              # NFC
    assert it.clean("") == "" and it.clean("plain") == "plain"


def test_cleaning_keeps_urls_paths_numbers_identifiers_and_typos():
    text = "Apri https://example.com/a_b?x=1&y=2 e C:\\dati\\file_2.csv poi fix getUserName() a 3.14 recieve teh"
    assert it.clean(text) == text


def test_the_folded_probe_is_a_separate_copy():
    assert it.probe("  Stra\u00dfe \uff21 ") == "strasse a"


# -- the gate ------------------------------------------------------------------------------------------------------------

CORPUS = [   # (message, expected triage in an agent conversation)
    ("ok", False), ("grazie", False), ("continua", False), ("go on", False), ("/status", False), ("ciao come stai", False),
    ("what is the capital of France", False), ("qual e la capitale d'Italia", False), ("thanks, that works", False),
    ("fix the failing test", True), ("elimina tutti i file temporanei", True), ("delete the build folder", True),
    ("send the report to Marco", True), ("prenota un tavolo per domani", True), ("rename this function", True),
    ("sistema questo", True), ("refactor it", True), ("pay the invoice", True), ("manda il file a Giulia", True),
    ("1. add a login page\n2. add logout\n3. add a profile page", True), ("- crea la tabella utenti\n- crea la tabella ordini", True),
    ("Please write a short script that reads a CSV file with columns name, email and country, removes the rows with an empty email, "
     "counts the people per country and writes the result as JSON next to the input file with the same base name", True),
    ("Vorrei che tu analizzassi il codice della cartella serve e mi dicessi quali funzioni non hanno test, poi scrivessi i test mancanti "
     "per le prime tre e li eseguissi per controllare che passino", True),
    ("explain how a hash map works", False), ("spiegami come funziona il garbage collector", False), ("hello", False), ("si", False),
    ("make it faster", True), ("cambia il colore", True), ("what does this do", False), ("run the tests", False), ("esegui i test", False),
    ("install the dependencies and then deploy it", True), ("scrivi una poesia sul mare", False), ("summarize the attached file", False),
    ("convert these to markdown", True), ("add a README", False), ("controlla che la build passi", False), ("buy the cheapest flight", True),
]


def test_the_gate_agrees_with_the_hand_labels_on_the_corpus():
    ctx = it.Context()
    hits = 0
    wrong = []
    for text, want in CORPUS:
        got, why = it.gate(text, it.signals(text), ctx)
        hits += got == want
        if got != want:
            wrong.append((text, why))
    assert hits / len(CORPUS) >= 0.85, wrong
    skipped = sum(1 for t, _ in CORPUS if not it.gate(t, it.signals(t), ctx)[0])
    assert skipped / len(CORPUS) >= 0.4                                              # most messages skip the call


def test_the_gate_skips_what_is_not_a_request_of_the_person():
    long = "please delete everything in the build folder and then rebuild all of it and send me the log by email when it is done " * 2
    sig = it.signals(long)
    assert it.gate(long, sig, it.Context())[0]
    for ctx in (it.Context(origin="planner"), it.Context(answers_card=True), it.Context(warm=True, history_tokens=50_000)):
        assert not it.gate(long, sig, ctx)[0]
    assert not it.gate(qs.ANSWER_HEADER + "\n1. x -> y", sig, it.Context())[0]
    assert not it.gate("Target Loop \u00b7 task 1 of 3: " + long, sig, it.Context())[0]
    assert not it.gate("[harness] " + long, sig, it.Context())[0]
    assert it.gate(long, sig, it.Context(warm=False, history_tokens=50_000))[0]        # a cold cache loses nothing
    chat = it.Context(ask_only=True)
    assert not it.gate("delete the build folder", it.signals("delete the build folder"), chat)[0] and it.gate(long * 2, it.signals(long * 2), chat)[0]


def test_signals():
    s = it.signals("Domani manda questo a Giulia\n1. uno\n2. due\nvedi https://x.org/a e ./src/main.py", ["a.pdf"])
    assert s.action and s.deictic and s.risk and s.enumerations == 2 and s.urls == 1 and s.paths >= 1 and s.attachments == 1 and ("domani", 1) in s.relative


# -- the form ------------------------------------------------------------------------------------------------------------

TEXT = "Scrivi tre cose. 1. la pagina di login 2. la pagina di logout 3. il profilo. Usa solo la libreria standard."


def form(**kw):
    base = {"evidence": ["la pagina di login"], "intent": "create", "size": "large", "ambiguity": "none", "missing_info": [], "risks": [], "needs_plan": False,
            "stated_constraints": ["Usa solo la libreria standard"],
            "subtasks": [{"title": "Login", "span": "la pagina di login", "depends_on": [], "acceptance": "the page loads"},
                         {"title": "Logout", "span": "la pagina di logout", "depends_on": [0], "acceptance": ""},
                         {"title": "Profilo", "span": "il profilo", "depends_on": [1, 2, 7], "acceptance": ""}]}
    base.update(kw)
    return base


def test_a_good_form_is_kept_with_caps_and_clean_dependencies():
    out = it.validate(form(), TEXT)
    assert out["size"] == "large" and out["constraints"] == ["Usa solo la libreria standard"] and len(out["subtasks"]) == 3
    assert out["subtasks"][2]["depends_on"] == [1]                                   # only earlier parts
    assert it.validate(form(evidence=["a", "b", "c", "d", "e"]), TEXT)["evidence"] == []
    many = form(subtasks=[{"title": f"t{i}", "span": "la pagina di login", "depends_on": [], "acceptance": ""} for i in range(9)])
    assert len(it.validate(many, TEXT)["subtasks"]) == it.MAX_SUBTASKS


def test_quotes_that_are_not_in_the_text_are_dropped_and_a_mostly_invented_form_is_discarded():
    some = it.validate(form(evidence=["la pagina di login", "something never said"]), TEXT)
    assert some["evidence"] == ["la pagina di login"]
    assert it.validate(form(evidence=["invented one", "invented two"], stated_constraints=["invented three"], subtasks=[]), TEXT) is None
    assert it.validate(form(subtasks=[{"title": "x", "span": "not here", "depends_on": [], "acceptance": ""}]), TEXT)["subtasks"] == []
    assert it.validate(form(evidence=["LA   pagina\ndi LOGIN"]), TEXT)["evidence"] == ["LA pagina di LOGIN"]    # whitespace and case do not matter


def test_an_unusable_form_is_none():
    for bad in (None, [], "x", {}, form(size="huge"), form(ambiguity="very")):
        assert it.validate(bad, TEXT) is None


# -- ask or act ----------------------------------------------------------------------------------------------------------

def item(what="which database", cost="high", reversible=True, owner="none", findable="no", default="PostgreSQL"):
    return {"what": what, "why": "", "default": default, "findable": findable, "cost": cost, "reversible": reversible, "owner": owner}


def decide(items, ambiguity="material", **ctx):
    return it.policy({"missing": items, "ambiguity": ambiguity}, it.Context(**ctx))


def test_the_policy_asks_only_what_is_worth_a_question_and_never_what_can_be_found():
    assert [m["what"] for m in decide([item()])["ask"]] == ["which database"]
    assert decide([item(findable="files")])["ask"] == [] and len(decide([item(findable="files")])["look"]) == 1
    assert decide([item()], ambiguity="none")["ask"] == []                            # nothing is material
    assert decide([item(cost="low")])["ask"] == []
    assert decide([item(cost="medium")])["ask"] == []                                 # 0.5 x 4 = 2, not above 2
    assert [m["what"] for m in decide([item(cost="medium", reversible=False)])["ask"]] == ["which database"]
    assert decide([item(cost="medium", reversible=False)], started=True)["ask"] == []  # after work started the bar is higher
    assert decide([item(cost="medium", reversible=False)], ask_only=True)["ask"] != [] and decide([item(cost="medium")], ask_only=True)["ask"] == []


def test_the_policy_limits_the_number_of_questions_and_orders_by_risk():
    items = [item(f"q{i}", cost="high") for i in range(4)]
    assert len(decide(items)["ask"]) == 3 and len(decide(items, ask_only=True)["ask"]) == 2        # without tools: two only when both are costly
    one_costly = [item("big", cost="high"), item("small", cost="medium", reversible=False)]
    assert [m["what"] for m in decide(one_costly, ask_only=True)["ask"]] == ["big"]
    mixed = [item("small", cost="medium", reversible=False), item("big", cost="high")]
    assert [m["what"] for m in decide(mixed)["ask"]] == ["big", "small"]


def test_unattended_never_asks_and_parks_only_the_named_owners_and_auto_asks_only_them():
    items = [item("pick", cost="high"), item("who", cost="high", owner="other_person"), item("room", cost="high", owner="booking")]
    un = decide(items, attended=False)
    assert un["ask"] == [] and [m["what"] for m in un["park"]] == ["who", "room"] and [m["what"] for m in un["assume"]] == ["pick"]
    auto = decide(items, auto=True)
    assert [m["what"] for m in auto["ask"]] == ["who", "room"] and [m["what"] for m in auto["assume"]] == ["pick"]


def test_the_card_is_made_by_the_harness_from_the_forms_fields():
    card = it.card_from([item("which database", default="PostgreSQL"), item("who", default="", owner="other_person")])
    q1, q2 = card["questions"]
    assert q1["kind"] == "single" and q1["options"][0]["recommended"] and q1["allow_other"] and q1["text"] == "which database?"
    assert q2["kind"] == "free" and q2["involves"] == ["people: who"]
    assert qs.auto_pick(q1)[1] == "recommended" and qs.auto_pick(q2)[1] == "parked"


# -- the note --------------------------------------------------------------------------------------------------------------

def test_the_note_is_deterministic_labelled_and_empty_when_there_is_nothing_to_say():
    assert it.note_text(assumptions=[], look=[], constraints=[], dates=[], checklist=0, subtasks=[]) == ""
    args = dict(assumptions=["db: PostgreSQL"], look=["the port"], constraints=["only stdlib"], dates=['"domani" = Saturday 2026-10-31'], checklist=3, subtasks=[])
    a, b = it.note_text(**args), it.note_text(**args)
    assert a == b and a.startswith(it.NOTE_HEAD) and "not the user's words" in a and '"only stdlib"' in a and "lists 3 items" in a
    sub = [{"title": "Login", "span": "", "depends_on": [], "acceptance": "loads"}, {"title": "Logout", "span": "", "depends_on": [0], "acceptance": ""}]
    assert "1. Login - done when: loads" in it.note_text(**{**args, "subtasks": sub}) and "2. Logout (after 1)" in it.note_text(**{**args, "subtasks": sub})


def test_relative_dates_come_from_the_clock():
    sig = it.signals("domani e dopodomani, ieri no, today")
    out = it.relative_dates(sig, datetime(2026, 10, 30, 9, 0))
    assert '"domani" = Saturday 2026-10-31' in out and '"dopodomani" = Sunday 2026-11-01' in out and '"today" = Friday 2026-10-30' in out
    assert it.relative_dates(sig, None) == []


# -- the triage call ---------------------------------------------------------------------------------------------------------

class Client:
    """Plays back canned replies and keeps every request it got."""

    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    def chat_completion(self, messages, tools=None, **kw):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": tools, **kw})
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def reply(obj):
    return {"choices": [{"message": {"role": "assistant", "content": obj if isinstance(obj, str) else json.dumps(obj)}}]}


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


LONG = "Cancella tutti i file della cartella build e poi rigenera tutto e mandami il log per email quando hai finito, per favore. " * 2


def test_the_triage_payload_is_strict_has_no_tools_and_a_byte_stable_system_part():
    client = Client(reply(form(size="small", subtasks=[], stated_constraints=[], evidence=[])), reply(form(size="small", subtasks=[], stated_constraints=[], evidence=[])))
    intake = it.Intake("full", client)
    ctx = it.Context(attachments=["a.pdf"])
    intake.run(LONG, ctx)
    intake.run(LONG + "?", ctx)
    one, two = client.requests
    assert one["tools"] is None and one["temperature"] == 0.0 and one["extra"] == {"reasoning_effort": "none"} and one["max_tokens"] <= 600
    assert one["response_format"]["json_schema"]["strict"] is True and one["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert one["messages"][0] == two["messages"][0] and one["messages"][0]["content"] == it.TRIAGE_PROMPT
    assert "a.pdf" in one["messages"][1]["content"] and LONG[:50] in one["messages"][1]["content"]
    schema = it.TRIAGE_SCHEMA
    assert list(schema["properties"])[0] == "evidence" and set(schema["required"]) == set(schema["properties"])


def test_rules_mode_and_off_never_call_the_model_and_a_failed_call_is_a_skipped_call():
    client = Client()
    assert it.Intake("rules", client).run(LONG, it.Context()).triaged is False and client.requests == []
    for bad in ("not json", {"nope": 1}, BaseException):
        c = Client(reply(bad) if bad is not BaseException else RuntimeError("down"))
        out = it.Intake("full", c).run(LONG, it.Context())
        assert out.triaged is False and out.card is None and len(c.requests) == 1
    c = Client(reply(form(size="small", subtasks=[], evidence=[])))
    assert it.Intake("full", c).run("ok", it.Context()).triaged is False and c.requests == []        # the gate said no


def test_full_mode_turns_a_form_into_a_card_a_note_and_parts():
    c = Client(reply(form(ambiguity="material", missing_info=[
        {"what": "Quale cartella", "why": "two exist", "default": "build", "findable": "no", "wrong_guess_cost": "high", "reversible": False, "owner": "user"},
        {"what": "La porta", "why": "", "default": "8080", "findable": "files", "wrong_guess_cost": "low", "reversible": True, "owner": "none"}])))
    out = it.Intake("full", c).run(TEXT + " " + LONG, it.Context())
    assert out.triaged and [q["text"] for q in out.card["questions"]] == ["Quale cartella?"] and len(out.subtasks) == 3
    assert "La porta" in out.note and "1. Login" in out.note and "Usa solo la libreria standard" in out.note


# -- the session around it ------------------------------------------------------------------------------------------------

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


class Sink:
    def __init__(self):
        self.calls = []

    def record(self, task_id, status, result="", children=None, metadata=None):
        self.calls.append((task_id, status, children, metadata))
        return True


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr("tadamoo.stack.local_now", None)


def session_with(tmp_path, client, mode="full", unattended=False, permission_mode="default", clock=None):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    permission_mode=permission_mode, max_turns=6), client=client)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None,
                      unattended=unattended)
    session.intake = it.Intake(mode, client)
    if clock:
        built.agent.clock = clock
    return session, events, built


MATERIAL = form(size="small", subtasks=[], stated_constraints=[], evidence=[], ambiguity="material", missing_info=[
    {"what": "Quale cartella", "why": "ce ne sono due", "default": "build", "findable": "no", "wrong_guess_cost": "high", "reversible": False, "owner": "user"}])


def test_an_attended_card_ends_the_turn_before_the_model_works_and_the_answer_skips_triage(tmp_path):
    client = Client(reply(MATERIAL), say("Fatto."))
    session, ev, built = session_with(tmp_path, client)
    session.send("m1", LONG)
    ask = ev.wait("ask")
    assert ask["mode"] == "question" and ask["kind"] == "questions" and ask["source"] == "intake" and ask["questions"][0]["options"][0]["recommended"]
    done = ev.wait("done")
    assert done["stop_reason"] == al.ASKED and "Quale cartella" in done["final"] and len(client.requests) == 1       # only the triage call was made
    assert ev.wait("intake")["asked"] is True
    n = len(ev.items)
    answer = qs.ANSWER_HEADER + "\n1. Quale cartella? -> build (your recommended option)\n" + qs.GO_ON
    session.send("m2", answer)
    assert ev.wait("done", after=n)["final"] == "Fatto." and len(client.requests) == 2                                # no second triage
    roles = [m["role"] for m in client.requests[1]["messages"] if m["role"] != "system"]
    assert roles == ["user", "assistant", "user"] and "Quale cartella" in client.requests[1]["messages"][2]["content"]


def test_the_note_follows_the_message_is_a_note_and_the_message_stays_the_persons(tmp_path):
    client = Client(reply(form(size="small", subtasks=[], evidence=[], stated_constraints=[],
                               missing_info=[{"what": "La porta", "why": "", "default": "8080", "findable": "no", "wrong_guess_cost": "low", "reversible": True, "owner": "none"}])),
                    say("Fatto."))
    session, ev, built = session_with(tmp_path, client, clock=lambda: datetime(2026, 10, 30, 9, 0))
    session.send("m1", LONG + " domani   \r\n")
    ev.wait("done")
    sent = client.requests[1]["messages"]
    users = [m for m in sent if m["role"] == "user"]
    note = users[-1]["content"]
    assert note.startswith(it.NOTE_HEAD) and "La porta: 8080" in note and '"domani" = Saturday 2026-10-31' in note
    assert "domani" in users[-2]["content"] and "\r" not in users[-2]["content"]                                   # cleaned for the model
    messages = built.agent.messages
    assert messages[-3]["role"] == "user" and messages[-2].get("note") is True and messages[-3].get("raw", "").endswith("domani   \r\n")
    assert built.agent.last_user_text.startswith("Cancella") and not built.agent.last_user_text.startswith("[harness]")   # the note is not what a quote must come from


def test_unattended_never_asks_assumes_and_parks_the_named_owner(tmp_path):
    items = [{"what": "Quale cartella", "why": "", "default": "build", "findable": "no", "wrong_guess_cost": "high", "reversible": False, "owner": "user"},
             {"what": "Il veterinario", "why": "", "default": "", "findable": "no", "wrong_guess_cost": "high", "reversible": False, "owner": "other_person"}]
    client = Client(reply({**MATERIAL, "missing_info": items}), say("Fatto."))
    session, ev, built = session_with(tmp_path, client, unattended=True)
    session.send("m1", LONG)
    done = ev.wait("done")
    assert done["final"] == "Fatto." and not [e for e in ev.items if e["event"] == "ask"]
    pending = ev.wait("pending")
    assert pending["kind"] == "parked_question" and pending["source"]["named"] == "people"
    note = [m["content"] for m in client.requests[1]["messages"] if m["role"] == "user"][-1]
    assert "Quale cartella: build" in note and "Left for the user to decide later" in note and "Il veterinario" in note


def test_the_auto_level_asks_only_the_named_owners(tmp_path):
    items = [{"what": "Quale cartella", "why": "", "default": "build", "findable": "no", "wrong_guess_cost": "high", "reversible": False, "owner": "user"},
             {"what": "Chi chiamo", "why": "", "default": "Marco", "findable": "no", "wrong_guess_cost": "high", "reversible": True, "owner": "other_person"}]
    client = Client(reply({**MATERIAL, "missing_info": items}), say("x"))
    session, ev, built = session_with(tmp_path, client, permission_mode="auto")
    session.send("m1", LONG)
    ask = ev.wait("ask")
    assert [q["text"] for q in ask["questions"]] == ["Chi chiamo?"] and ask["questions"][0]["involves"]


def test_a_large_request_is_recorded_in_the_registry_and_closed_when_it_ends(tmp_path):
    client = Client(reply(form()), say("Fatto."))
    session, ev, built = session_with(tmp_path, client)
    session.task_sink = sink = Sink()
    session.send("m1", TEXT + " " + LONG)
    ev.wait("done")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(c[0] == "req-m1" and c[1] == "done" for c in sink.calls):
        time.sleep(0.02)
    first = sink.calls[0]
    assert first[0] == "req-m1" and first[1] == "running" and first[2] == ["req-m1.1", "req-m1.2", "req-m1.3"] and first[3]["kind"] == "intake"
    parts = {c[0]: c for c in sink.calls if c[1] == "pending"}
    assert parts["req-m1.2"][3]["depends_on"] == ["req-m1.1"] and parts["req-m1.3"][3]["depends_on"] == ["req-m1.2"]
    assert ("req-m1", "done", None, None) in sink.calls and all(("req-m1.%d" % i, "closed", None, None) in sink.calls for i in (1, 2, 3))
    assert "1. Login" in [m["content"] for m in client.requests[1]["messages"] if m["role"] == "user"][-1]


def test_a_failed_triage_does_not_stop_the_request_and_off_does_nothing(tmp_path):
    client = Client(RuntimeError("down"), say("Fatto."))
    session, ev, built = session_with(tmp_path, client)
    session.send("m1", LONG)
    assert ev.wait("done")["final"] == "Fatto."
    client2 = Client(say("Fatto."))
    session2, ev2, built2 = session_with(tmp_path / "b", client2, mode="off")
    session2.send("m1", "caf\u00e9\u200b   \r\n")
    ev2.wait("done")
    assert [m for m in client2.requests[0]["messages"] if m["role"] == "user"][-1]["content"] == "caf\u00e9\u200b   \r\n"   # off: not even cleaned
    assert len(client2.requests) == 1


def test_a_target_loop_message_and_a_planner_note_are_not_looked_at(tmp_path):
    client = Client(say("Fatto."))
    session, ev, built = session_with(tmp_path, client)
    session.send("m1", "Target Loop \u00b7 task 1 of 2: " + LONG)
    ev.wait("done")
    assert len(client.requests) == 1                                                  # no triage call
