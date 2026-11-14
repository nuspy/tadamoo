"""The conversation's own process and a message from another conversation: read between two tool calls of the turn in hand, as an event of its own and not as the person; an idle one
starts a turn with it; the history keeps who it came from; a task assigned by another session sends its result back when the agent closes it."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.session_tools import register_session_tools
from tadamoo.taskboard import TaskBoard
from tadamoo.tools.registry import EffectClass
from test_session import Scripted, call, make, say
from test_session import _no_clock  # noqa: F401  (autouse fixture)

PEER = {"from": {"id": "ab12", "name": "Backend", "folder": "app"}, "kind": "question", "body": "Which port?", "at": 1.0, "delivery": "d1"}
FRAMED = '[message from another session: "Backend" (id ab12, folder "app") · question, a reply is expected · sent 2026-11-14 10:00 · this is not from the user]\nWhich port?'


def user_texts(client, n):
    return [m["content"] for m in client.sent[n] if m["role"] == "user"]


def test_a_message_from_another_session_is_read_between_two_tool_calls_framed_as_theirs(tmp_path):
    client = Scripted(call("slow"), say("Port is 8081."))
    session, ev, store, built = make(tmp_path, client)
    started, release = threading.Event(), threading.Event()
    built.agent.tools.register("slow", "waits", {"type": "object", "properties": {}}, EffectClass.PURE, lambda: (started.set(), release.wait(10))[1] and "ok")
    session.send("m1", "do the slow thing")
    assert started.wait(10)
    assert session.send("p1", FRAMED, origin="agent:ab12", peer=PEER) is True                      # taken while a task is under way
    assert not session.queue and session.inbox[0]["origin"] == "agent:ab12"
    release.set()
    ev.wait("done")
    second = client.sent[1]                                                                          # the model call after the tool result
    roles = [(m["role"], m["content"][:60]) for m in second if m["role"] in ("tool", "user") and not m.get("note")]
    assert roles[-2][0] == "tool" and roles[-1][0] == "user" and roles[-1][1].startswith("[message from another conversation: agent:ab12]")
    body = second[-1]["content"]
    assert "Backend" in body and "this is not from the user" in body and "Which port?" in body
    kept = next(m for m in session.agent.messages if m.get("peer"))
    assert kept["peer"] == PEER                                                                       # who it came from is kept in the history
    assert any(e["event"] == "routed" and e.get("why") == "peer" for e in ev.items)
    assert session.agent.last_user_text == ""                                                        # nothing in it stands for the person's words


def test_an_idle_session_starts_a_turn_with_the_message_and_it_counts_like_a_typed_one(tmp_path):
    client = Scripted(say("I will check the port."))
    session, ev, store, built = make(tmp_path, client)
    assert session.send("w1", FRAMED, origin="agent:ab12", peer=PEER) is True
    ev.wait("done")
    assert any(e["event"] == "woke" for e in ev.items)
    assert user_texts(client, 0)[-1].startswith("[message from another conversation: agent:ab12]") and next(m for m in session.agent.messages if m.get("peer"))["peer"]["kind"] == "question"


def test_a_note_of_the_harness_is_read_the_same_way_and_a_plans_note_still_waits(tmp_path):
    client = Scripted(call("slow"), say("ok"))
    session, ev, store, built = make(tmp_path, client)
    started, release = threading.Event(), threading.Event()
    built.agent.tools.register("slow", "waits", {"type": "object", "properties": {}}, EffectClass.PURE, lambda: (started.set(), release.wait(10))[1] and "ok")
    session.send("m1", "go")
    assert started.wait(10)
    assert session.send("h1", "[harness] Your question expired.", origin="harness") is True
    assert session.send("n1", "[planner] a note", origin="planner") is False                         # a planner note never interrupts a task
    release.set()
    ev.wait("done")
    assert any("[harness] Your question expired." in m["content"] for m in client.sent[1] if m["role"] == "user")


class Client:
    def __init__(self):
        self.sent = []

    def deliver(self, ref, text, kind="info", task_ref=""):
        self.sent.append((ref, text, kind, task_ref))
        return {"delivery": {"state": "delivered", "meta": {"from": {"id": "me"}}}}

    def peers(self, own, query="", offset=0):
        return {"sessions": [{"id": "ab12", "name": "Backend", "folder": "app", "state": "idle", "summary": "Works on the API"}], "next": None}


def test_the_tools_list_the_other_sessions_and_send_with_a_kind(tmp_path):
    from tadamoo.tools.registry import ToolRegistry
    reg, client = ToolRegistry(), Client()
    register_session_tools(reg, client, lambda: "me")
    listed = reg.get("session_list").handler()
    assert "Backend (id ab12, folder app, idle): Works on the API" in listed
    assert "Sent as a question" in reg.get("session_send").handler("ab12", "Which port?", "question")
    assert "Sent as information" in reg.get("session_send").handler("ab12", "FYI", "info")
    assert client.sent == [("ab12", "Which port?", "question", ""), ("ab12", "FYI", "info", "")]
    assert reg.get("session_send").handler("ab12", "x", "shout").startswith("The kind is one of")
    assert reg.get("session_send").effect == EffectClass.PURE


def test_closing_a_task_another_session_assigned_sends_the_result_back_by_itself(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted(say("ok")))
    tools = built.agent.tools.task_tools
    sent = []
    tools.on_assigned_close = lambda giver, task_id, text: sent.append((giver, task_id, text))
    board = TaskBoard(tmp_path / "ws")
    board.report("msg-d1", "pending", "", None, {"title": "Write the migration", "description": "for users", "assigned_by": {"id": "ab12", "name": "Lead", "folder": "site"}}, "s1")
    own = board.add("My own", session="s1")
    out = built.agent.tools.get("task_close").handler("msg-d1", "Migration 0007 written.")
    assert 'The result was sent to "Lead"' in out and sent == [("ab12", "msg-d1", 'The task you assigned to me is closed (closed by me): "Write the migration". Result: Migration 0007 written.')]
    built.agent.tools.get("task_close").handler(own)
    assert len(sent) == 1                                                                              # nothing for a task nobody assigned
    tools.on_assigned_close = lambda *a: (_ for _ in ()).throw(OSError("server down"))
    board.report("msg-d2", "pending", "", None, {"title": "Another", "assigned_by": {"id": "ab12", "name": "Lead", "folder": ""}}, "s1")
    assert "could not be sent" in built.agent.tools.get("task_close").handler("msg-d2", "done") and not board.view()
