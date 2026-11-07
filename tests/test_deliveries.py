"""Messages for a conversation: kept until it takes them, answered with delivered or dead, never dropped."""
import json

import pytest

from serve.deliveries import DeliveryError, DeliveryQueue


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Port:
    def __init__(self):
        self.sessions = {"aa11": {"alias": "boss", "state": "idle"}, "bb22": {"alias": "log", "state": "dormant"}, "cc33": {"alias": "", "state": "closed"}}
        self.written, self.broke, self.spent = [], set(), set()

    def resolve(self, ref):
        ref = str(ref).lower()
        if ref in self.sessions:
            return ref
        return next((sid for sid, s in self.sessions.items() if s["alias"] == ref and s["state"] != "closed"), None)

    def state_of(self, sid):
        return self.sessions[sid]["state"] if sid in self.sessions else None

    def push_delivery(self, sid, delivery):
        if sid in self.broke:
            raise RuntimeError("no process")
        self.written.append((sid, delivery["id"], delivery["provenance"], delivery["message"]))
        return True

    def over_budget(self, sid):
        return sid in self.spent


@pytest.fixture
def queue(tmp_path):
    clock, port = Clock(), Port()
    return DeliveryQueue(tmp_path, port, clock=clock, busy_timeout_s=600, ack_timeout_s=60), port, clock


def test_a_message_for_a_free_conversation_is_written_at_once_and_delivered_when_it_answers(queue):
    q, port, _ = queue
    item = q.submit("boss", "append the line", "agent:bb22")
    assert item["state"] == "pending" and item["session"] == "aa11" and port.written == [("aa11", item["id"], "agent:bb22", "append the line")]
    assert q.acked(item["id"], "accepted")["state"] == "delivered"
    assert q.get(item["id"])["delivered"] > 0 and q.pending_count() == 0


def test_a_sleeping_conversation_is_written_to_as_well_and_the_alias_names_it(queue):
    q, port, _ = queue
    q.submit("LOG", "wake up", "cron:ab12")
    assert port.written[0][0] == "bb22"


def test_a_message_for_a_busy_conversation_waits_and_goes_when_it_is_free(queue):
    q, port, clock = queue
    port.sessions["aa11"]["state"] = "busy"
    item = q.submit("boss", "later", "cron")
    assert item["state"] == "pending" and port.written == []
    clock.now += 100
    q.pump()
    assert port.written == []
    port.sessions["aa11"]["state"] = "idle"
    q.pump()
    assert [w[1] for w in port.written] == [item["id"]]


def test_a_conversation_that_says_it_is_busy_keeps_the_message_in_the_queue(queue):
    q, port, clock = queue
    item = q.submit("boss", "hello", "plan:p1")
    q.acked(item["id"], "busy")
    assert q.get(item["id"])["state"] == "pending"
    q.pump()
    assert len(port.written) == 2                         # written again, now that no answer is awaited


def test_a_message_written_and_never_answered_is_written_again_after_the_wait(queue):
    q, port, clock = queue
    q.submit("boss", "hello", "cron")
    clock.now += 30
    q.pump()
    assert len(port.written) == 1
    clock.now += 40
    q.pump()
    assert len(port.written) == 2 and port.written[0][1] == port.written[1][1]      # the same id: the target drops the second


def test_a_conversation_busy_for_too_long_makes_a_dead_letter_that_keeps_the_message(queue):
    q, port, clock = queue
    port.sessions["aa11"]["state"] = "asking"
    item = q.submit("boss", "the original words", "cron")
    clock.now += 601
    q.pump()
    dead = q.get(item["id"])
    assert dead["state"] == "dead" and dead["reason"] == "busy_timeout" and dead["message"] == "the original words"
    assert q.dead_count() == 1 and q.dead_count("aa11") == 1 and q.dead_count("bb22") == 0


def test_a_missing_or_closed_conversation_is_dead_at_once(queue):
    q, _, _ = queue
    assert q.submit("nobody", "x", "cron")["reason"] == "unreachable"
    assert q.submit("cc33", "x", "cron")["reason"] == "unreachable"


def test_a_conversation_that_will_not_start_is_dead_with_the_reason(queue):
    q, port, _ = queue
    port.broke.add("bb22")
    item = q.submit("bb22", "x", "cron")
    assert item["state"] == "dead" and item["reason"] == "unreachable" and "would not start" in item["detail"]


def test_a_conversation_over_its_budget_gets_nothing(queue):
    q, port, _ = queue
    port.spent.add("aa11")
    assert q.submit("boss", "x", "cron")["reason"] == "budget" and port.written == []


def test_a_message_that_went_through_its_target_too_often_is_stopped(queue):
    q, port, _ = queue
    assert q.submit("boss", "x", "agent:bb22", chain=["aa11", "bb22", "aa11"])["state"] == "pending"
    assert q.submit("boss", "x", "agent:bb22", chain=["aa11", "bb22", "aa11", "aa11"])["reason"] == "loop"


def test_a_late_answer_turns_a_dead_letter_into_a_delivered_message(queue):
    q, port, clock = queue
    port.sessions["aa11"]["state"] = "busy"
    item = q.submit("boss", "x", "cron")
    clock.now += 700
    q.pump()
    assert q.get(item["id"])["state"] == "dead"
    q.acked(item["id"], "accepted")
    assert q.get(item["id"])["state"] == "delivered"


def test_a_dead_letter_is_sent_again_or_put_away(queue):
    q, port, clock = queue
    port.broke.add("bb22")
    item = q.submit("bb22", "x", "cron")
    port.broke.clear()
    again = q.retry(item["id"])
    assert again["state"] == "pending" and port.written[-1][1] == item["id"]
    with pytest.raises(DeliveryError) as e:
        q.retry(item["id"])
    assert e.value.code == "not_dead"
    port.broke.add("bb22")
    other = q.submit("bb22", "y", "cron")
    assert q.dismiss(other["id"])["state"] == "dismissed" and q.dead_count() == 0
    with pytest.raises(DeliveryError):
        q.dismiss("zzzz")


def test_one_fire_is_one_message(queue):
    q, port, _ = queue
    first = q.submit("boss", "x", "cron:r1", run_id="fire1")
    second = q.submit("boss", "x", "cron:r1", run_id="fire1")
    assert first["id"] == second["id"] and len(port.written) == 1
    assert q.submit("log", "x", "cron:r1", run_id="fire1")["id"] != first["id"]        # the same fire for another target is another message


def test_what_is_wrong_with_a_message_is_said(queue):
    q, _, _ = queue
    for bad, code in ((("boss", "", "cron"), "text_missing"), (("boss", "x" * 8001, "cron"), "too_long"), (("boss", "x", "user"), "bad_provenance"),
                      (("boss", "x", "agent:"), "bad_provenance"), (("", "x", "cron"), "no_target")):
        with pytest.raises(DeliveryError) as e:
            q.submit(*bad)
        assert e.value.code == code


def test_deliveries_survive_a_restart_and_nothing_is_thought_to_be_awaiting_an_answer(tmp_path):
    port, clock = Port(), Clock()
    q = DeliveryQueue(tmp_path, port, clock=clock)
    kept = q.submit("boss", "keep me", "cron")
    port.sessions["bb22"]["state"] = "busy"
    q.submit("log", "me too", "cron")
    again = DeliveryQueue(tmp_path, Port(), clock=clock)
    assert [i["message"] for i in again.entries()] == ["me too", "keep me"]
    assert again.get(kept["id"])["sent"] == 0.0
    assert json.loads((tmp_path / "deliveries.json").read_text(encoding="utf-8"))["schema"] == "1.0"
