"""session_status, session_digest, session_spawn: thin tools over a fake client, with their own rate limit and the rules precondition."""
from tadamoo.integrations._http import ServiceError
from tadamoo.orchestration_tools import register_orchestration_tools
from tadamoo.tools.registry import ToolRegistry


class FakeClient:
    def __init__(self):
        self.sent, self.created = [], []
        self.deliver_result = {"delivery": {"state": "delivered"}}
        self.status_result = {"id": "bb22", "alias": "log", "state": "idle", "title": "", "pending_messages": 0, "dead_letters": 0, "last_answer": ""}
        self.digest_result = {"id": "bb22", "alias": "log", "lines": [], "pending_messages": 0, "dead_letters": 0}
        self.create_result = {"session": {"id": "cc33", "alias": ""}}
        self.fail = None

    def status(self, ref):
        if self.fail:
            raise self.fail
        return self.status_result

    def digest(self, ref, n=20):
        if self.fail:
            raise self.fail
        return self.digest_result

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


def test_session_digest_shows_lines_or_says_nothing_yet():
    reg, client = make()
    client.digest_result = {"id": "bb22", "alias": "log", "lines": [], "pending_messages": 0, "dead_letters": 0}
    assert "nothing to show yet" in reg.get("session_digest").handler("log")
    client.digest_result = {"id": "bb22", "alias": "log", "lines": ["10:00 you: hi", "10:01 done: ok"], "pending_messages": 1, "dead_letters": 2}
    out = reg.get("session_digest").handler("log")
    assert "10:00 you: hi" in out and "10:01 done: ok" in out and "1 message(s) still waiting" in out and "2 could not be delivered" in out


def test_session_digest_reports_a_failure(): 
    reg, client = make()
    client.fail = ServiceError("timeout", "x")
    assert "timeout" in reg.get("session_digest").handler("log")
