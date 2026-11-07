"""session_send, session_status, session_spawn: thin tools over a fake client, with their own rate limit and the rules precondition."""
from tadamoo.integrations._http import ServiceError
from tadamoo.orchestration_tools import RateLimiter, register_orchestration_tools
from tadamoo.tools.registry import ToolRegistry


class FakeClient:
    def __init__(self):
        self.sent, self.created = [], []
        self.deliver_result = {"delivery": {"state": "delivered"}}
        self.status_result = {"id": "bb22", "alias": "log", "state": "idle", "title": "", "pending_messages": 0, "dead_letters": 0, "last_answer": ""}
        self.create_result = {"session": {"id": "cc33", "alias": ""}}
        self.fail = None

    def deliver(self, ref, message):
        if self.fail:
            raise self.fail
        self.sent.append((ref, message))
        return self.deliver_result

    def status(self, ref):
        if self.fail:
            raise self.fail
        return self.status_result

    def create(self, workspace, title="", alias="", unattended=False, rules=""):
        self.created.append((workspace, title, alias, unattended, rules))
        return self.create_result

    def send(self, sid, text):
        self.sent.append((sid, text))
        return {}


def make():
    reg = ToolRegistry()
    client = FakeClient()
    register_orchestration_tools(reg, client)
    return reg, client


def test_session_send_delivers_and_reports_the_state():
    reg, client = make()
    out = reg.get("session_send").handler("log", "append: 12:00 sold")
    assert client.sent == [("log", "append: 12:00 sold")] and "Message delivered" in out


def test_session_send_names_what_is_wrong_without_calling_the_client():
    reg, client = make()
    assert "Name the conversation" in reg.get("session_send").handler("", "x")
    assert "Write the message" in reg.get("session_send").handler("log", "")
    assert "too long" in reg.get("session_send").handler("log", "x" * 4001)
    assert client.sent == []


def test_session_send_reports_a_dead_letter_and_a_pending_one():
    reg, client = make()
    client.deliver_result = {"delivery": {"state": "dead", "reason": "unreachable", "detail": "no such conversation"}}
    assert "Not delivered" in reg.get("session_send").handler("nobody", "x") and "no such conversation" in reg.get("session_send").handler("nobody", "x")
    client.deliver_result = {"delivery": {"state": "pending"}}
    assert "waiting" in reg.get("session_send").handler("log", "x")


def test_session_send_is_rate_limited_per_target():
    reg, client = ToolRegistry(), FakeClient()
    now = [0.0]
    limiter = RateLimiter(limit=2, window_s=100.0, clock=lambda: now[0])
    register_orchestration_tools(reg, client, limiter=limiter)
    assert "delivered" in reg.get("session_send").handler("log", "one")
    assert "delivered" in reg.get("session_send").handler("log", "two")
    assert "Too many" in reg.get("session_send").handler("log", "three")
    assert "delivered" in reg.get("session_send").handler("boss", "one")          # a different target has its own count
    assert client.sent == [("log", "one"), ("log", "two"), ("boss", "one")]
    now[0] += 101
    assert "delivered" in reg.get("session_send").handler("log", "again")          # the window moved on


def test_session_status_reads_the_other_conversation():
    reg, client = make()
    line = reg.get("session_status").handler("log")
    assert "log: idle" in line
    client.status_result = {**client.status_result, "pending_messages": 2, "dead_letters": 1, "last_answer": "done"}
    line = reg.get("session_status").handler("log")
    assert "2 message(s) waiting" in line and "1 could not be delivered" in line and 'Its last answer: "done"' in line


def test_session_status_reports_a_failure_from_the_client():
    reg, client = make()
    client.fail = ServiceError("not_found", "x", 404)
    assert "not_found" in reg.get("session_status").handler("nobody")


def test_session_spawn_creates_and_sends_the_task():
    reg, client = make()
    client.create_result = {"session": {"id": "cc33", "alias": "worker"}}
    out = reg.get("session_spawn").handler("D:/work", "watch the log")
    assert client.created == [("D:/work", "watch the log", "", False, "")]
    assert client.sent == [("cc33", "watch the log")]
    assert "Started worker" in out


def test_session_spawn_in_the_background_needs_rules_of_its_own():
    reg, client = make()
    out = reg.get("session_spawn").handler("D:/work", "watch the log", background=True)
    assert "needs operating rules" in out and client.created == []
    out = reg.get("session_spawn").handler("D:/work", "watch the log", background=True, rules="Never spend money.")
    assert client.created == [("D:/work", "watch the log", "", True, "Never spend money.")]


def test_session_spawn_names_what_is_wrong_without_calling_the_client():
    reg, client = make()
    assert "Give the folder" in reg.get("session_spawn").handler("", "x")
    assert "Write what it should do" in reg.get("session_spawn").handler("D:/work", "")
    assert "too long" in reg.get("session_spawn").handler("D:/work", "x" * 8001)
    assert client.created == []
