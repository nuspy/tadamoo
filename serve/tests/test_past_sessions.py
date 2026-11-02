"""The conversations are added to the index of earlier conversations as their turns end; routines only when asked."""
import json
import time
from pathlib import Path

from harness.past import PastIndex


class Stub:
    def __init__(self, tmp_path):
        from serve.agent_sessions import SessionManager
        stub = str(Path(__file__).with_name("session_stub.py"))
        self.past = PastIndex(tmp_path / "past.db")
        self.mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=lambda sid, rec, history: [__import__("sys").executable, stub, "--session-id", sid, "--history-dir", str(history), "--die-times", "0"],
                                  tick_s=0.1, default_workspace=tmp_path / "chat", past=self.past)
        (tmp_path / "chat").mkdir()
        (tmp_path / "proj").mkdir()

    def until(self, predicate, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        raise AssertionError("timed out")

    def said(self, sid, text):
        self.mgr.send(sid, text)
        self.until(lambda: "done" in [e["event"] for e in self.mgr.events(sid)["events"]])


def test_a_turn_that_ends_puts_the_conversation_in_the_index(tmp_path):
    s = Stub(tmp_path)
    try:
        sid = s.mgr.create(str(tmp_path / "proj"), title="")["id"]
        s.said(sid, "ricordami il vertice di Bruxelles")
        s.until(lambda: s.past.search("Bruxelles"))
        hit = s.past.search("Bruxelles")[0]
        assert hit["session"] == sid and hit["kind"] == "agent" and hit["project"] == "proj"
        chat = s.mgr.create(None, title="")["id"]
        s.said(chat, "a chat about tulips")
        s.until(lambda: s.past.search("tulips"))
        assert s.past.search("tulips")[0]["kind"] == "chat" and s.past.search("tulips")[0]["project"] == ""
    finally:
        s.mgr.shutdown()


def test_a_routine_is_left_out_unless_the_person_switched_its_indexing_on(tmp_path):
    s = Stub(tmp_path)
    try:
        plain = s.mgr.create(str(tmp_path / "proj"), title="Routine: a", unattended=True)["id"]
        s.mgr.keep_out_of_memory(plain)
        s.said(plain, "weekly invoices check")
        time.sleep(0.5)
        assert s.past.search("invoices") == []
        wanted = s.mgr.create(str(tmp_path / "proj"), title="Routine: b", unattended=True)["id"]
        s.mgr.keep_out_of_memory(wanted)
        s.mgr.routine_run(wanted, "Weekly report", True)
        s.said(wanted, "weekly report on turbines")
        s.until(lambda: s.past.search("turbines"))
        hit = s.past.search("turbines")[0]
        assert hit["kind"] == "routine" and hit["routine"] == "Weekly report"
    finally:
        s.mgr.shutdown()


def test_the_conversations_saved_before_the_index_are_added_later(tmp_path):
    s = Stub(tmp_path)
    try:
        sid = s.mgr.create(str(tmp_path / "proj"), title="")["id"]
        s.said(sid, "an old talk about owls")
        s.until(lambda: s.past.search("owls"))
        s.past.drop(sid)
        assert s.past.search("owls") == []
        assert s.mgr.index_all_past() >= 1
        assert s.past.search("owls")[0]["session"] == sid
        assert s.mgr.index_all_past() == 0                                  # nothing changed since: nothing to do
    finally:
        s.mgr.shutdown()


def test_the_conversation_process_is_told_where_the_index_is(tmp_path):
    from serve.agent_sessions import SessionManager
    past = PastIndex(tmp_path / "past.db")
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", tick_s=0.1, past=past)
    try:
        (tmp_path / "w").mkdir()
        sid = mgr.create(str(tmp_path / "w"))["id"]
        cmd = mgr._command(sid, mgr._record(sid), tmp_path / "h")
        assert cmd[cmd.index("--past-index") + 1] == str(past.path)
        plain = SessionManager(tmp_path / "j2", "http://127.0.0.1:1", tick_s=0.1)
        try:
            assert "--past-index" not in plain._command(sid, mgr._record(sid), tmp_path / "h")
        finally:
            plain.shutdown()
    finally:
        mgr.shutdown()
