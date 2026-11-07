"""Names for conversations and messages between them, through the manager and a stand-in process."""
import json

import pytest

from serve.agent_runs import RunError
from serve.tests.test_agent_sessions import kinds, make, own_data_folder, until, ws  # noqa: F401  (fixtures and helpers)


def taken(mgr, sid):
    path = mgr.history_dir(sid) / "deliveries.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_a_conversation_can_be_given_a_name_that_is_unique_among_those_still_open(make, ws):
    mgr = make()
    a = mgr.create(ws, alias="Boss")["id"]
    b = mgr.create(ws)["id"]
    assert mgr.get(a)["alias"] == "boss" and mgr.resolve("BOSS") == a and mgr.resolve(a) == a and mgr.resolve("nobody") is None
    with pytest.raises(RunError) as e:
        mgr.set_alias(b, "boss")
    assert e.value.code == "alias_taken"
    mgr.close(a)
    assert mgr.resolve("boss") is None and mgr.resolve(a) == a                  # a closed one is found by id only
    assert mgr.set_alias(b, "boss")["alias"] == "boss"                          # and its name is free again
    assert mgr.set_alias(b, "")["alias"] == ""


@pytest.mark.parametrize("name", ["x", "has space", "UPPER_CASE", "-start", "a" * 33, "deadbeef", "12345678"])
def test_a_name_is_short_plain_and_never_an_id(make, ws, name):
    mgr = make()
    sid = mgr.create(ws)["id"]
    with pytest.raises(RunError) as e:
        mgr.set_alias(sid, name)
    assert e.value.code == "bad_alias"


def test_the_senders_that_may_give_orders_are_a_short_list_of_names_and_patterns(make, ws):
    mgr = make()
    sid = mgr.create(ws, cross_commands=["boss", "team-*"])["id"]
    assert mgr.get(sid)["cross_commands"] == ["boss", "team-*"]
    assert mgr.set_cross_commands(sid, ["log"])["cross_commands"] == ["log"]
    for bad in ("boss", ["bad name"], ["x"] * 11, [5]):
        with pytest.raises(RunError) as e:
            mgr.set_cross_commands(sid, bad)
        assert e.value.code == "bad_senders"


def test_the_command_line_carries_the_senders_and_the_environment_the_token(make, ws):
    mgr = make()
    sid = mgr.create(ws, cross_commands=["boss", "log"])["id"]
    cmd = mgr._command(sid, mgr._record(sid), mgr.history_dir(sid))
    assert [cmd[i + 1] for i, c in enumerate(cmd) if c == "--cross-commands"] == ["boss", "log"]
    token = mgr.session_token(sid)
    assert mgr.sender_of(f"{sid}.{token}") == sid
    for bad in (f"{sid}.0000", f"{sid}.", sid, "", None, f"nope.{token}", f"{sid}x.{token}"):
        assert mgr.sender_of(bad) is None


def test_a_message_sent_to_a_sleeping_conversation_wakes_it_and_arrives_with_its_provenance(make, ws):
    mgr = make()
    boss = mgr.create(ws, alias="boss")["id"]
    log = mgr.create(ws, alias="log")["id"]
    item = mgr.deliver("log", "append: 12:00 sold", sender=boss)["delivery"]
    assert item["provenance"] == "agent:boss" and item["session"] == log
    until(lambda: mgr.deliveries.get(item["id"])["state"] == "delivered")
    assert taken(mgr, log) == [{"id": item["id"], "provenance": "agent:boss", "text": "append: 12:00 sold"}]
    until(lambda: "done" in kinds(mgr, log))
    incoming = next(e for e in mgr.events(log)["events"] if e["event"] == "incoming")
    assert incoming["provenance"] == "agent:boss" and incoming["text"] == "append: 12:00 sold"
    assert mgr.status("log")["dead_letters"] == 0 and mgr.status("log")["pending_messages"] == 0


def test_a_message_from_a_schedule_names_where_it_comes_from(make, ws):
    mgr = make()
    sid = mgr.create(ws, alias="night")["id"]
    item = mgr.deliver("night", "Run the plan P1", provenance="plan:P1", run_id="fire-1")["delivery"]
    until(lambda: mgr.deliveries.get(item["id"])["state"] == "delivered")
    assert taken(mgr, sid)[0]["provenance"] == "plan:P1"
    again = mgr.deliver("night", "Run the plan P1", provenance="plan:P1", run_id="fire-1")["delivery"]
    assert again["id"] == item["id"] and len(taken(mgr, sid)) == 1              # one fire, one message


def test_a_message_for_a_conversation_at_work_waits_and_goes_when_it_is_free(make, ws):
    mgr = make()
    sid = mgr.create(ws, alias="busy")["id"]
    mgr.send(sid, "ask")                                                         # the stand-in waits for an answer: the conversation is asking
    until(lambda: mgr.get(sid)["state"] == "asking")
    item = mgr.deliver("busy", "when you can", provenance="cron")["delivery"]
    assert item["state"] == "pending" and taken(mgr, sid) == [] and mgr.status("busy")["pending_messages"] == 1
    mgr.answer(sid, "a1", "confirm")
    until(lambda: mgr.get(sid)["state"] == "idle")
    mgr.deliveries.pump()
    until(lambda: mgr.deliveries.get(item["id"])["state"] == "delivered")
    assert [t["text"] for t in taken(mgr, sid)] == ["when you can"]


def test_a_message_for_a_name_nobody_has_is_a_dead_letter_the_status_shows(make, ws):
    mgr = make()
    sid = mgr.create(ws, alias="here")["id"]
    item = mgr.deliver("nowhere", "lost?", provenance="cron")["delivery"]
    assert item["state"] == "dead" and item["reason"] == "unreachable" and item["message"] == "lost?"
    mgr.close(sid)
    closed = mgr.deliver(sid, "too late", provenance="cron")["delivery"]
    assert closed["state"] == "dead" and mgr.status(sid)["dead_letters"] == 1
    with pytest.raises(RunError) as e:
        mgr.status("nowhere")
    assert e.value.code == "gone"


def test_what_a_conversation_was_woken_by_is_the_line_its_own_messages_continue(make, ws):
    mgr = make()
    a = mgr.create(ws, alias="aa-one")["id"]
    b = mgr.create(ws, alias="bb-two")["id"]
    mgr.create(ws, alias="cc-three")
    first = mgr.deliver("bb-two", "from a", sender=a)["delivery"]
    until(lambda: mgr.deliveries.get(first["id"])["state"] == "delivered")
    until(lambda: mgr.get(b)["state"] == "idle")
    second = mgr.deliver("cc-three", "from b", sender=b)["delivery"]
    assert second["chain"] == [a, b]                                             # the message went a -> b -> c
    mgr.send(b, "hello")                                                         # the person speaks to b: its line starts again
    until(lambda: kinds(mgr, b).count("done") >= 2)
    assert mgr.deliver("cc-three", "again", sender=b)["delivery"]["chain"] == [b]


def test_what_is_wrong_with_a_message_is_said_by_the_manager(make, ws):
    mgr = make()
    mgr.create(ws, alias="here")
    for args, code in ((("here", ""), "text_missing"), (("", "x"), "no_target")):
        with pytest.raises(RunError) as e:
            mgr.deliver(*args, provenance="cron")
        assert e.value.code == code
    with pytest.raises(RunError) as e:
        mgr.deliver("here", "x", provenance="user")
    assert e.value.code == "bad_provenance"
