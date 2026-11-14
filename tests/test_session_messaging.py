"""Conversations writing to each other: two sessions exchanging, an idle one woken, one that is not running started, a task that lands in the receiver's register and comes back as a
message when it is closed, a question that expires, a collaboration that lasts as long as its task, and the brakes (identical repeats, a flood, a chain of automatic replies).
Stub session processes speak the real lines of JSON and answer through the server with the id and token they were given."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler, messaging
from serve.agent_sessions import SessionManager
from serve.messaging import Refused
from tadamoo.taskboard import TaskBoard
from test_e2e_mock_server import start_server  # noqa: F401  (fixture)

STUB = Path(__file__).resolve().parent.parent / "serve" / "tests" / "session_stub.py"


def until(predicate, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


@pytest.fixture
def world(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server(["unused"])
    folders = {n: tmp_path / n for n in ("site", "app")}
    for f in folders.values():
        f.mkdir()
    mgr = SessionManager(tmp_path / "j", url, tick_s=0.2, default_workspace=tmp_path / "chat", boards=lambda ws: TaskBoard(ws),
                         command=lambda sid, rec, history: [sys.executable, str(STUB), "--session-id", sid, "--history-dir", str(history), "--endpoint", url])
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    yield mgr, folders
    mgr.shutdown()


def make(mgr, folders, name, folder="site", **kw):
    sid = mgr.create(str(folders[folder]), title=name, **kw)["id"]
    return sid


def history(mgr, sid):
    try:
        return json.loads((mgr.history_dir(sid) / "history.json").read_text(encoding="utf-8"))["messages"]
    except (OSError, ValueError):
        return []                                                                       # the process has not written one yet


def events(mgr, sid, kind):
    return [e for e in mgr.events(sid, 0)["events"] if e["event"] == kind]


def test_two_sessions_exchange_messages_and_each_sees_it_as_a_bubble_of_its_own(world):
    mgr, folders = world
    a, b = make(mgr, folders, "Website", "site"), make(mgr, folders, "Backend", "app")
    mgr.send_peer(a, b, "question", "Which port does the API use? !reply 8081")
    until(lambda: any(m.get("peer") for m in history(mgr, a)))                          # B answered: its reply reached A
    got = next(m for m in history(mgr, b) if m.get("peer"))
    assert got["peer"]["from"] == {"id": a, "name": "Website", "folder": "site"} and got["peer"]["kind"] == "question"
    assert got["content"].startswith('[message from another session: "Website" (id %s, folder "site") · question' % a) and "this is not from the user" in got["content"]
    assert f'session_send with to="{a}"' in got["content"]                                      # how to answer
    reply = next(m for m in history(mgr, a) if m.get("peer"))
    assert reply["peer"]["from"]["name"] == "Backend" and reply["peer"]["body"] == "8081" and reply["peer"]["kind"] == "info" and "sent 20" in reply["content"]
    out_a, in_a = events(mgr, a, "peer_out"), events(mgr, a, "peer_in")
    assert [(e["to"]["name"], e["kind"]) for e in out_a] == [("Backend", "question")] and [(e["from"]["name"], e["text"]) for e in in_a] == [("Backend", "8081")]
    assert [(e["from"]["name"], e["kind"]) for e in events(mgr, b, "peer_in")] == [("Website", "question")] and [e["to"]["name"] for e in events(mgr, b, "peer_out")] == ["Website"]


def test_a_session_that_is_not_running_is_started_by_the_message_and_one_at_work_reads_it_at_once(world):
    mgr, folders = world
    a, b, c = make(mgr, folders, "A"), make(mgr, folders, "B", "app"), make(mgr, folders, "C", "app")
    assert mgr.get(b)["state"] == "dormant"
    item = mgr.send_peer(a, b, "info", "The schema changed.")["delivery"]
    until(lambda: mgr.deliveries.get(item["id"])["state"] == "delivered")
    assert mgr.get(b)["state"] in ("busy", "idle") and any(m.get("peer") for m in history(mgr, b))                 # its process was started and took a turn
    mgr.send(c, "ask")                                                                 # C is at work: its turn waits for an answer
    until(lambda: mgr.get(c)["state"] in ("asking", "busy"))
    item = mgr.send_peer(a, c, "info", "FYI the port moved.")["delivery"]
    until(lambda: mgr.deliveries.get(item["id"])["state"] == "delivered")                 # not held back until C is free
    assert [m["peer"]["body"] for m in history(mgr, c) if m.get("peer")] == ["FYI the port moved."]
    assert events(mgr, c, "routed") and events(mgr, c, "routed")[-1]["why"] == "peer"


def test_a_task_lands_in_the_receivers_register_and_its_result_comes_back_when_it_is_closed(world):
    mgr, folders = world
    a, b = make(mgr, folders, "Lead", "site"), make(mgr, folders, "Worker", "app")
    item = mgr.send_peer(a, b, "task", "Write the migration for the users table.")["delivery"]
    board = TaskBoard(folders["app"])
    (row,) = board.view()
    assert row["id"] == f"msg-{item['id']}" and row["links"]["session"] == b and row["state"] == "queued" and row["title"] == "Write the migration for the users table."
    assert row["meta"]["assigned_by"] == {"id": a, "name": "Lead", "folder": "site"} and row["description"].startswith("Write the migration")
    until(lambda: any(m.get("peer") for m in history(mgr, b)))
    assert "assigned by \"Lead\"" in next(m for m in history(mgr, b) if m.get("peer"))["content"]
    closed = mgr.messenger.task_closed(board.get(row["id"]), "Done: migration 0007 written.", "closed by the person")        # the Tasks tab deletes it
    assert closed["delivery"]["kind"] == "info"
    until(lambda: any("Result: Done: migration 0007 written." in m.get("content", "") for m in history(mgr, a)))
    back = next(m for m in history(mgr, a) if m.get("peer"))
    assert back["peer"]["from"]["name"] == "Worker" and 'The task you assigned to me is closed' in back["peer"]["body"]
    other = board.add("My own task", session=b)
    assert mgr.messenger.task_closed(board.get(other), "x") is None                  # a task nobody assigned sends nothing


def test_a_question_expires_after_twelve_hours_unanswered_and_its_sender_is_told_and_an_answered_one_does_not(world):
    mgr, folders = world
    a, b = make(mgr, folders, "A"), make(mgr, folders, "B", "app")
    q1 = mgr.send_peer(a, b, "question", "First question, no answer coming")["delivery"]
    until(lambda: mgr.deliveries.get(q1["id"])["state"] == "delivered")
    q2 = mgr.send_peer(a, b, "question", "Second question")["delivery"]
    until(lambda: mgr.deliveries.get(q2["id"])["state"] == "delivered")
    mgr.send_peer(b, a, "info", "Answering the second")                                 # any message back answers what was asked: both of A's questions
    assert mgr.deliveries.get(q2["id"])["answered"] and mgr.deliveries.get(q1["id"])["answered"]
    q3 = mgr.send_peer(a, b, "question", "Third, later")["delivery"]
    until(lambda: mgr.deliveries.get(q3["id"])["state"] == "delivered")
    base = time.time()
    mgr.deliveries.clock = lambda: base + 11 * 3600
    mgr.messenger_sweep()
    assert not mgr.deliveries.get(q3["id"])["expired"] and not events(mgr, a, "peer_expired")
    mgr.deliveries.clock = lambda: base + 13 * 3600
    mgr.messenger_sweep()
    assert mgr.deliveries.get(q3["id"])["expired"] and [e["text"] for e in events(mgr, a, "peer_expired")] == ["Third, later"]
    until(lambda: any("was not answered in 12 hours" in m.get("content", "") for m in history(mgr, a)))
    mgr.messenger_sweep()
    assert len(events(mgr, a, "peer_expired")) == 1                                    # told once


def test_a_collaboration_lasts_as_long_as_the_senders_task_is_open(world):
    mgr, folders = world
    a, b = make(mgr, folders, "A", "site"), make(mgr, folders, "B", "app")
    with pytest.raises(Refused) as e:
        mgr.messenger.send(a, b, "collaborate", "Let us split the work")
    assert e.value.code == "task_missing"
    with pytest.raises(Refused) as e:
        mgr.messenger.send(a, b, "collaborate", "Let us split the work", "t-nope")
    assert e.value.code == "task_closed"
    board = TaskBoard(folders["site"])
    task = board.add("Ship the release", session=a)
    item = mgr.messenger.send(a, b, "collaborate", "You take the docs", task)["delivery"]
    until(lambda: any(m.get("peer") for m in history(mgr, b)))
    text = next(m for m in history(mgr, b) if m.get("peer"))["content"]
    assert "Ship the release" in text and item["meta"]["task_title"] == "Ship the release"
    board.close(task)
    with pytest.raises(Refused) as e:
        mgr.messenger.send(a, b, "collaborate", "Docs done?", task)
    assert e.value.code == "task_closed" and "not open any more" in str(e.value)


def test_identical_repeats_a_flood_and_long_chains_of_automatic_replies_are_stopped_and_the_sender_is_told(world, monkeypatch):
    mgr, folders = world
    a, b = make(mgr, folders, "A"), make(mgr, folders, "B", "app")
    mgr.messenger.send(a, b, "info", "Same words")
    with pytest.raises(Refused) as e:
        mgr.messenger.send(a, b, "info", "  same   WORDS ")
    assert e.value.code == "repeat" and events(mgr, a, "peer_brake")[-1]["reason"] == "repeat"
    mgr.messenger.send(a, b, "question", "Same words")                                      # another kind is another message
    monkeypatch.setattr(messaging, "RATE_LIMIT", 4)
    with pytest.raises(Refused) as e:
        for i in range(10):
            mgr.messenger.send(a, b, "info", f"different {i}")
    assert e.value.code == "rate" and "messages in the last 10 minutes" in str(e.value) and events(mgr, a, "peer_brake")[-1]["reason"] == "rate"


def test_a_chain_of_replies_stops_after_six_automatic_hops_and_the_persons_message_starts_it_again(world):
    mgr, folders = world
    a, b = make(mgr, folders, "A"), make(mgr, folders, "B", "app")
    mgr.messenger.send(a, b, "info", "!ask ping 1")                                       # B asks A, A (stub) would answer...
    until(lambda: any(m.get("peer") for m in history(mgr, a)))
    assert mgr.hops_of(a) == 2 and mgr.hops_of(b) == 1
    # the hops go up by one with each message from one conversation to the next, until the sixth
    for n in range(3, 7):
        sender, receiver = (a, b) if n % 2 == 1 else (b, a)
        out = mgr.messenger.send(sender, receiver, "info", f"hop {n}")["delivery"]
        assert out["hops"] == n
    with pytest.raises(Refused) as e:
        mgr.messenger.send(a, b, "info", "hop 7")
    assert e.value.code == "chain" and "without a message from the person" in str(e.value)
    brake = events(mgr, a, "peer_brake")[-1]
    assert brake["reason"] == "chain" and brake["to"]["name"] == "B"
    mgr.send(a, "A person writes again")                                                  # a message of the person: the chain starts again
    assert mgr.hops_of(a) == 0
    assert mgr.messenger.send(a, b, "info", "hop again")["delivery"]["hops"] == 1


def test_the_persons_mentions_become_messages_to_the_named_sessions(world):
    mgr, folders = world
    a, b, c = make(mgr, folders, "Writer"), make(mgr, folders, "Reviewer", "app"), make(mgr, folders, "Tester", "app")
    mgr.send(a, "Please review the intro, can you tell me what you think @Reviewer", mentions=[b, c])
    until(lambda: all(any(m.get("peer") for m in history(mgr, s)) for s in (b, c)))
    msg_b = next(m for m in history(mgr, b) if m.get("peer"))
    assert msg_b["peer"]["kind"] == "question" and msg_b["peer"]["body"].startswith("Please review the intro") and msg_b["peer"]["from"]["name"] == "Writer"
    assert [m["id"] for m in next(m for m in history(mgr, a) if m.get("mentions"))["mentions"]] == [b, c]               # kept with the message in the sender's history
    mgr.send(a, "Just an update for the record", mentions=[c])
    until(lambda: len([m for m in history(mgr, c) if m.get("peer")]) == 2)
    assert [m["peer"]["kind"] for m in history(mgr, c) if m.get("peer")][-1] == "info"                                  # the words ask nothing: information
    assert mgr.hops_of(c) == 1                                                                                       # the person started the chain


def test_names_are_resolved_by_id_alias_or_a_unique_title_and_a_bad_target_is_told(world):
    mgr, folders = world
    a = make(mgr, folders, "A")
    b = make(mgr, folders, "Backend", "app", alias="back")
    twin1, twin2 = make(mgr, folders, "Twin"), make(mgr, folders, "Twin")
    assert (mgr.resolve_peer(b), mgr.resolve_peer("BACK"), mgr.resolve_peer("backend")) == (b, b, b)
    for ref, code in (("Twin", "ambiguous"), ("nobody", "unreachable"), ("", "no_target")):
        with pytest.raises(Refused) as e:
            mgr.resolve_peer(ref)
        assert e.value.code == code
    with pytest.raises(Refused) as e:
        mgr.messenger.send(a, a, "info", "to myself")
    assert e.value.code == "self"
    mgr.close(b)
    with pytest.raises(Refused):
        mgr.messenger.send(a, b, "info", "to a closed one")
    assert twin1 != twin2


def test_the_route_accepts_only_a_conversations_own_token_and_says_why_a_message_did_not_go(world, start_server):
    import urllib.error
    import urllib.request
    mgr, folders = world
    a, b = make(mgr, folders, "A"), make(mgr, folders, "B", "app")

    def post(sender_header, body):
        req = urllib.request.Request(f"{mgr.endpoint}/api/agent/sessions/{b}/deliver", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **({"X-Tadamoo-Session": sender_header} if sender_header else {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
    assert post("", {"text": "hi"})[0] == 403 and post(f"{a}.wrong", {"text": "hi"})[0] == 403
    code, out = post(f"{a}.{mgr.session_token(a)}", {"text": "hello there", "kind": "question"})
    assert code == 200 and out["delivery"]["kind"] == "question" and out["delivery"]["provenance"] == f"agent:{a}"
    code, err = post(f"{a}.{mgr.session_token(a)}", {"text": "hello there", "kind": "question"})
    assert code == 400 and err["error"]["code"] == "repeat" and "same message" in err["error"]["message"]
    assert post(f"{a}.{mgr.session_token(a)}", {"text": "x", "kind": "shout"})[0] == 400
    assert post(f"{a}.{mgr.session_token(a)}", {"text": ""})[0] == 400
