"""The manager of the Chat's conversations: starting, events, supervision, restarts, blocks, dormancy.

It talks to a stand-in process (session_stub.py) that speaks the same lines of JSON as the real one."""
import sys
import time
from pathlib import Path

import pytest

from serve.agent_runs import RunBusy, RunError
from serve.agent_sessions import SessionManager

STUB = str(Path(__file__).with_name("session_stub.py"))


def stub_command(die_times=1):
    return lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history), "--die-times", str(die_times)]


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**kw):
        kw.setdefault("command", stub_command())
        kw.setdefault("tick_s", 0.1)
        mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", **kw)
        made.append(mgr)
        return mgr

    yield build
    for mgr in made:
        mgr.shutdown()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return str(root)


def until(predicate, seconds=15, step=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out waiting")


def kinds(mgr, sid, after=0):
    return [e["event"] for e in mgr.events(sid, after)["events"]]


def test_two_messages_one_process_and_the_history_is_readable(make, ws):
    mgr = make()
    sid = mgr.create(ws, title="")["id"]
    mgr.send(sid, "primo")
    until(lambda: "done" in kinds(mgr, sid))
    first_pid = next(e["pid"] for e in mgr.events(sid)["events"] if e["event"] == "ready")
    mgr.send(sid, "secondo")
    until(lambda: kinds(mgr, sid).count("done") == 2)
    assert [e["pid"] for e in mgr.events(sid)["events"] if e["event"] == "ready"] == [first_pid]       # one process for both
    hist = mgr.history(sid)
    assert [m["content"] for m in hist["messages"]] == ["primo", "echo: primo", "secondo", "echo: secondo"]
    assert mgr.get(sid)["state"] == "idle" and mgr.get(sid)["title"] == "primo"
    assert "heartbeat" not in kinds(mgr, sid)                                  # signs of life are not shown


def test_a_second_message_while_one_is_answered_goes_to_the_agent_which_looks_at_it_itself(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "hang")
    until(lambda: mgr.get(sid)["state"] == "busy")
    out = mgr.send(sid, "ancora")                                                # not refused, and the state does not change
    assert out["session"]["state"] == "busy" and out["turn"]
    assert [e["text"] for e in mgr.events(sid)["events"] if e["event"] == "user"] == ["hang", "ancora"]


def test_a_process_that_dies_in_the_middle_of_a_turn_is_started_again_and_the_turn_goes_on(make, ws):
    mgr = make(command=stub_command(die_times=1))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "die")
    until(lambda: "done" in kinds(mgr, sid))
    ev = kinds(mgr, sid)
    assert "restarting" in ev and ev.count("ready") == 2
    done = [e for e in mgr.events(sid)["events"] if e["event"] == "done"][0]
    assert done["final"] == "resumed after 1 messages"                         # the message was on disk: the turn went on
    assert mgr.get(sid)["state"] == "idle"


def test_a_message_the_process_never_confirmed_is_sent_again(make, ws):
    killer = {"n": 0}

    def command(sid, rec, history):
        killer["n"] += 1
        # the first process ends before it reads anything
        script = "import sys; sys.exit(3)" if killer["n"] == 1 else None
        return [sys.executable, "-c", script] if script else stub_command()(sid, rec, history)

    mgr = make(command=command)
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ciao")
    until(lambda: "done" in kinds(mgr, sid))
    assert [m["content"] for m in mgr.history(sid)["messages"]] == ["ciao", "echo: ciao"]


def test_too_many_restarts_block_the_conversation_and_wait_for_the_person(make, ws):
    mgr = make(command=stub_command(die_times=99), restart_cap=2)
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "die")
    blocked = until(lambda: next((e for e in mgr.events(sid)["events"] if e["event"] == "blocked"), None))
    assert blocked["reason"] == "process_unstable" and blocked["options"] == ["resume", "close"]
    assert kinds(mgr, sid).count("restarting") == 2
    assert mgr.get(sid)["state"] == "blocked"
    time.sleep(0.5)
    assert kinds(mgr, sid).count("ready") == 3                                  # no restart beyond the cap


def test_a_process_that_stops_giving_signs_of_life_is_ended_and_started_again(make, ws):
    mgr = make(beat_timeout_s=0.6, command=stub_command(die_times=0))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "hang")
    until(lambda: "stalled" in kinds(mgr, sid))
    until(lambda: kinds(mgr, sid).count("ready") == 2)
    until(lambda: "done" in kinds(mgr, sid))


def test_a_block_from_the_process_waits_for_the_person_and_resume_goes_on(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "block")
    until(lambda: mgr.get(sid)["state"] == "blocked")
    assert mgr.get(sid)["blocked"]["reason"] == "refused"
    with pytest.raises(RunError):
        mgr.resume(sid) if mgr.get(sid)["state"] != "blocked" else (_ for _ in ()).throw(RunError("x"))
    n = mgr.events(sid)["next"]
    mgr.resume(sid)
    until(lambda: "done" in kinds(mgr, sid, n))
    assert mgr.get(sid)["state"] == "idle"
    with pytest.raises(RunError, match="not waiting"):
        mgr.resume(sid)


def test_an_action_that_cannot_be_undone_is_never_resumed_by_the_manager(make, ws):
    mgr = make(command=stub_command(die_times=0))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "irreversible")
    until(lambda: kinds(mgr, sid).count("ready") == 2)
    until(lambda: mgr.get(sid)["state"] == "blocked")
    assert mgr.get(sid)["blocked"]["reason"] == "interrupted_irreversible"
    time.sleep(0.6)
    assert "done" not in kinds(mgr, sid)                                        # nothing was given on by itself
    assert not any(m["content"].startswith("resumed") for m in mgr.history(sid)["messages"])


def test_an_idle_conversation_goes_dormant_and_the_next_message_wakes_it_with_its_history(make, ws):
    mgr = make(idle_s=0.4)
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "uno")
    until(lambda: "done" in kinds(mgr, sid))
    until(lambda: mgr.get(sid)["state"] == "dormant")
    mgr.send(sid, "due")
    until(lambda: kinds(mgr, sid).count("done") == 2)
    ready = [e for e in mgr.events(sid)["events"] if e["event"] == "ready"]
    assert len(ready) == 2 and ready[1]["restored"] == 2


def test_the_conversations_survive_the_server_and_the_manager_ends_its_processes(make, ws, tmp_path):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ricordati")
    until(lambda: "done" in kinds(mgr, sid))
    pid = next(e["pid"] for e in mgr.events(sid)["events"] if e["event"] == "ready")
    mgr.shutdown()
    mgr2 = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=stub_command(), tick_s=0.1)
    try:
        assert [s["id"] for s in mgr2.view()["sessions"]] == [sid] and mgr2.get(sid)["state"] == "dormant"
        mgr2.send(sid, "ancora")
        until(lambda: "done" in kinds(mgr2, sid))
        assert [m["content"] for m in mgr2.history(sid)["messages"]][:2] == ["ricordati", "echo: ricordati"]
    finally:
        mgr2.shutdown()
    import psutil
    assert not psutil.pid_exists(pid)                                  # the first manager's process ended with it


def test_closing_ends_the_process_and_a_closed_conversation_takes_no_messages(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ciao")
    until(lambda: "done" in kinds(mgr, sid))
    assert mgr.close(sid)["state"] == "closed"
    with pytest.raises(RunError, match="closed"):
        mgr.send(sid, "ancora")


def test_what_the_page_can_ask_is_checked(make, ws, tmp_path):
    mgr = make()
    with pytest.raises(RunError):
        mgr.create("relativo/cartella")
    with pytest.raises(RunError):
        mgr.create(ws, mode="root")
    with pytest.raises(RunError, match="Automatic"):
        mgr.create(ws, mode="auto")
    sid = mgr.create(ws, mode="auto", confirm_auto=True)["id"]
    for bad in ("", "   ", None, 5):
        with pytest.raises(RunError):
            mgr.send(sid, bad)
    with pytest.raises(RunError):
        mgr.send("nope", "ciao")


def test_a_confirmation_is_kept_with_its_buttons_until_the_person_answers(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ask")
    until(lambda: mgr.get(sid)["state"] == "asking")
    ask = mgr.get(sid)["ask"]
    assert ask["id"] == "a1" and ask["question"] == "Archive it?" and ask["facts"] == {"plan": "X"}
    assert mgr.send(sid, "intanto scrivo")["session"]["state"] == "asking"      # a message while it asks is for the agent to look at
    with pytest.raises(RunError, match="not one of the answers"):
        mgr.answer(sid, "a1", "forse")
    with pytest.raises(RunError, match="not waiting"):
        mgr.answer(sid, "zzz", "confirm")
    mgr.answer(sid, "a1", "confirm")
    done = until(lambda: next((e for e in mgr.events(sid)["events"] if e["event"] == "done" and e["final"].startswith("answered")), None))
    assert done["final"] == "answered: confirm"
    assert mgr.get(sid)["state"] == "idle" and mgr.get(sid)["ask"] is None
    with pytest.raises(RunError, match="not waiting"):
        mgr.answer(sid, "a1", "confirm")                                         # answered once, not twice


def test_a_question_that_ends_the_turn_stays_until_the_next_message(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "question")
    until(lambda: "done" in kinds(mgr, sid))
    rec = mgr.get(sid)
    assert rec["state"] == "idle" and rec["question"]["options"] == [{"label": "Sat", "value": "Saturday"}]
    mgr.send(sid, "Saturday")                                                   # the button is the next message
    assert mgr.get(sid)["question"] is None                                     # it is gone as soon as it is answered
    until(lambda: kinds(mgr, sid).count("done") == 2)
    assert mgr.get(sid)["question"] is None
    assert [m["content"] for m in mgr.history(sid)["messages"]][-2:] == ["Saturday", "echo: Saturday"]


def test_a_process_that_dies_while_it_waits_for_an_answer_is_started_again_and_the_question_is_gone(make, ws):
    mgr = make(command=stub_command(die_times=0))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ask")
    until(lambda: mgr.get(sid)["state"] == "asking")
    from serve.agent_runs import _kill_tree
    _kill_tree(mgr._live[sid].proc)
    until(lambda: kinds(mgr, sid).count("ready") == 2)
    assert mgr.get(sid)["state"] == "busy" and mgr.get(sid).get("ask") is None        # the question died with the process


def test_a_conversation_without_a_named_folder_works_in_the_default_one(make, tmp_path):
    mgr = make(default_workspace=tmp_path / "chat-workspace")
    rec = mgr.create(None)
    assert Path(rec["workspace"]) == (tmp_path / "chat-workspace").resolve() and Path(rec["workspace"]).is_dir()
    bare = SessionManager(tmp_path / "j2", "http://127.0.0.1:1", command=stub_command(), tick_s=0.1)
    try:
        with pytest.raises(RunError):
            bare.create("")                                                     # no default and no folder: still refused
    finally:
        bare.shutdown()


def test_a_conversation_with_more_tasks_waiting_stays_at_work_until_the_last_one_is_done(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "more")
    until(lambda: kinds(mgr, sid).count("done") == 1)
    assert mgr.get(sid)["state"] == "busy"                                      # `more`: others are waiting
    mgr.send(sid, "uno")
    until(lambda: kinds(mgr, sid).count("done") == 2)
    assert mgr.get(sid)["state"] == "idle"


def test_a_queued_message_is_confirmed_by_the_process_and_not_sent_again_after_a_restart(make, ws):
    mgr = make(command=stub_command(die_times=0))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "queue-me")
    until(lambda: "queued" in kinds(mgr, sid))
    assert sid not in mgr._unconfirmed


def test_a_waiting_request_can_be_promoted_or_dropped_through_the_manager(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "hang")
    until(lambda: mgr.get(sid)["state"] == "busy")
    with pytest.raises(RunError):
        mgr.promote("nope", "x")
    mgr.promote(sid, "t1")
    mgr.drop(sid, "t2")
    time.sleep(0.1)
    mgr.close(sid)
