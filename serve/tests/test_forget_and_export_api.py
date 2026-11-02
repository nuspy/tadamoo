"""Private conversations, "forget this conversation", and the export of the memories: the manager, the routes (this PC only) and the page's API."""
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from tadamoo.forget import Forgetter
from tadamoo.past import PastIndex
from tadamoo.personal_memory import PersonalMemory, PersonalMemoryError
from serve import handler
from serve.agent_sessions import SessionManager
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.memories_api import MemoriesApi
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
KEY = bytes(range(32))


class Stub:
    """The conversation manager with a stand-in process, the index, the memories and a list of what it handed over to learn from."""

    def __init__(self, base: Path):
        stub = str(Path(__file__).with_name("session_stub.py"))
        self.jobs = []
        self.past = PastIndex(base / "past.db")
        self.memory = PersonalMemory(base / "pm", key=KEY)
        self.export = base / "export"
        self.mgr = SessionManager(base / "j", "http://127.0.0.1:1", tick_s=0.1, default_workspace=base / "chat", past=self.past, learn=self.jobs.append,
                                  forgetter=Forgetter(self.past, self.memory, self.export),
                                  command=lambda sid, rec, history: [sys.executable, stub, "--session-id", sid, "--history-dir", str(history), "--die-times", "0"])
        (base / "chat").mkdir()

    def until(self, predicate, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        raise AssertionError("timed out")

    def said(self, sid, text):
        before = sum(1 for e in self.mgr.events(sid)["events"] if e["event"] == "done")
        self.mgr.send(sid, text)
        self.until(lambda: sum(1 for e in self.mgr.events(sid)["events"] if e["event"] == "done") > before)


class PrivateConversation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Stub(Path(self.tmp.name))

    def tearDown(self):
        self.s.mgr.shutdown()
        self.s.past.close()
        self.tmp.cleanup()

    def test_a_private_conversation_is_neither_indexed_nor_learned_from(self):
        s = self.s
        sid = s.mgr.create(None, title="")["id"]
        self.assertFalse(s.mgr._record(sid).get("private"))
        self.assertTrue(s.mgr.set_private(sid, True)["private"])
        s.said(sid, "my secret plan about walruses")
        time.sleep(0.5)
        self.assertEqual((s.jobs, s.past.search("walruses"), s.mgr.index_past(sid), s.mgr.index_all_past()), ([], [], False, 0))

    def test_the_mark_is_kept_with_the_conversation_across_a_restart(self):
        s = self.s
        sid = s.mgr.create(None, title="")["id"]
        s.mgr.set_private(sid, True)
        self.assertTrue(s.mgr.get(sid)["private"])
        reopened = SessionManager(s.mgr.directory.parent, "http://127.0.0.1:1", tick_s=0.1)
        try:
            self.assertTrue(reopened.get(sid)["private"])
        finally:
            reopened.shutdown()

    def test_marking_it_private_takes_it_out_of_the_index_and_drops_what_waited_to_be_learned(self):
        s = self.s
        sid = s.mgr.create(None, title="")["id"]
        s.said(sid, "talking about octopuses today")
        s.until(lambda: s.past.search("octopuses"))
        s.memory.enqueue({"text": "talking about octopuses today", "conversation": sid})
        s.mgr.set_private(sid, True)
        self.assertEqual((s.past.search("octopuses"), s.memory.queue_len()), ([], 0))
        s.mgr.set_private(sid, False)                                                  # taken again from the next turn on, and now
        s.until(lambda: s.past.search("octopuses"))

    def test_only_true_or_false_is_accepted(self):
        sid = self.s.mgr.create(None, title="")["id"]
        for bad in ("yes", 1, None):
            with self.assertRaises(Exception) as caught:
                self.s.mgr.set_private(sid, bad)
            self.assertEqual(getattr(caught.exception, "code", ""), "bad_private")

    def test_forgetting_removes_the_index_pieces_and_the_notes_learned_from_it_only(self):
        s = self.s
        sid = s.mgr.create(None, title="")["id"]
        other = s.mgr.create(None, title="")["id"]
        s.said(sid, "I keep ferrets in Verona")
        s.until(lambda: s.past.search("ferrets"))
        s.memory.add("long", "place", "Lives in Verona", conversation=sid)
        s.memory.add("long", "tastes", "Likes ferrets", conversation=other)
        s.memory.enqueue({"text": "I keep ferrets", "conversation": sid})
        got = s.mgr.forget(sid)["forgotten"]
        self.assertEqual((got["notes"], got["queued"], got["chunks"] > 0), (1, 1, True))
        self.assertEqual((s.past.search("ferrets"), [i["text"] for i in s.memory.items()], s.memory.queue_len()), ([], ["Likes ferrets"], 0))
        self.assertTrue(s.mgr.get(sid))                                                # the conversation itself stays
        with self.assertRaises(Exception) as caught:
            s.mgr.forget("nothere")
        self.assertEqual(getattr(caught.exception, "code", ""), "gone")

    def test_what_is_handed_over_to_learn_names_its_conversation(self):
        s = self.s
        sid = s.mgr.create(None, title="")["id"]
        s.said(sid, "a message that is learned from")
        s.until(lambda: s.jobs)
        self.assertEqual(s.jobs[0]["conversation"], sid)


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.base_dir = base
        cls.s = Stub(base)
        cls.mem = cls.s.memory
        cls.patches = [mock.patch.object(handler, "_PERSONAL", cls.mem), mock.patch.object(handler, "_PAST", cls.s.past),
                       mock.patch.object(handler, "_SESSIONS", cls.s.mgr), mock.patch.object(handler, "_MEMORIES", MemoriesApi(cls.mem, None, base / "export")),
                       mock.patch.object(handler.userdata, "path", lambda name: base / name)]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        cls.s.mgr.shutdown()
        cls.s.past.close()
        cls.tmp.cleanup()

    def post(self, path, payload):
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_a_conversation_is_marked_private_and_forgotten_through_its_routes(self):
        sid = self.s.mgr.create(None, title="")["id"]
        self.s.past.add_session(sid, [{"role": "user", "content": "lemurs of Madagascar"}])
        self.mem.add("short", "topics", "Is reading about lemurs", conversation=sid)
        status, body = self.post(f"/api/agent/sessions/{sid}/private", {"private": True})
        self.assertEqual((status, body["session"]["private"]), (200, True))
        self.assertEqual(self.s.past.search("lemurs"), [])                              # marking it took it out of the index
        self.assertEqual(self.post(f"/api/agent/sessions/{sid}/private", {"private": "yes"})[0], 400)
        status, body = self.post(f"/api/agent/sessions/{sid}/forget", {})
        self.assertEqual((status, body["forgotten"]["notes"]), (200, 1))
        self.assertEqual(self.mem.search("lemurs", older=True), [])
        self.assertEqual(self.post("/api/agent/sessions/nothere/forget", {})[0], 409)

    def test_forgetting_a_conversation_is_only_for_the_pc_the_server_runs_on(self):
        sid = self.s.mgr.create(None, title="")["id"]
        self.mem.add("short", "topics", "Is reading about otters", conversation=sid)
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            self.assertEqual(self.post(f"/api/agent/sessions/{sid}/forget", {})[0], 403)
            self.assertEqual(self.post("/api/past/forget", {"id": "abc12345"})[0], 403)
            self.assertEqual(self.post("/api/memories/export", {})[0], 403)
        self.assertEqual(self.mem.count(), 1)
        self.mem.forget_conversation(sid)

    def test_the_model_only_chat_forgets_its_conversation_by_its_id(self):
        self.s.past.add_session("chat-abc12345", [{"role": "user", "content": "badgers at dusk"}], kind="chat")
        self.mem.add("short", "topics", "Is reading about badgers", conversation="chat-abc12345")
        status, body = self.post("/api/past/forget", {"id": "abc12345"})
        self.assertEqual((status, body["forgotten"]["notes"], body["forgotten"]["chunks"] > 0), (200, 1, True))
        self.assertEqual(self.s.past.search("badgers"), [])
        self.assertEqual(self.post("/api/past/forget", {"id": "x"})[0], 400)
        self.assertEqual(self.post("/api/past/forget", {})[0], 400)

    def test_the_page_exports_the_notes_and_is_told_the_folder(self):
        self.mem.add("long", "place", "Lives in Verona", conversation="c1")
        status, body = self.post("/api/memories/export", {})
        folder = Path(body["folder"])
        self.assertEqual((status, folder, body["notes"] >= 1), (200, self.base_dir / "export", True))
        self.assertTrue((folder / "index.md").exists() and any("Verona" in p.read_text(encoding="utf-8") for p in (folder / "long").glob("*.md")))
        self.mem.forget_conversation("c1")


def test_the_page_api_exports_only_where_it_has_a_folder(tmp_path):
    notes = PersonalMemory(tmp_path / "pm", key=KEY)
    notes.add("long", "place", "Lives in Verona")
    assert MemoriesApi(notes, None, tmp_path / "out").post("export", {})["notes"] == 1
    try:
        MemoriesApi(notes).post("export", {})
    except PersonalMemoryError as e:
        assert "folder" in str(e)
    else:
        raise AssertionError("no folder, no export")
    blocked = tmp_path / "file"
    blocked.write_text("x")
    try:
        MemoriesApi(notes, None, blocked / "inside").post("export", {})
    except PersonalMemoryError as e:
        assert "could not be written" in str(e)
    else:
        raise AssertionError("an unwritable folder is reported")


if __name__ == "__main__":
    unittest.main()
