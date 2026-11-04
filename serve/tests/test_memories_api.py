"""The person's memories on the server: the page's API, the routes (this PC only), the background learning and the conversations that feed it."""
import json
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from tadamoo.personal_memory import PersonalMemory, PersonalMemoryError
from serve import handler
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.memories_api import MemoriesApi
from serve.memory_worker import MemoryWorker, gpu_free
from serve.plans import PlanService
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
KEY = bytes(range(32))


def memory(tmp_path):
    return PersonalMemory(tmp_path / "pm", key=KEY)


def test_the_page_can_read_edit_confirm_and_delete(tmp_path):
    api = MemoriesApi(memory(tmp_path))
    first = api.post("item", {"kind": "long", "section": "place", "text": "Lives in Verona"})["item"]
    assert first["origin"] == "manual"
    api.memory.add("long", "character", "Seems patient", origin="inferred", confidence=0.4)
    view = api.overview()
    assert view["counts"] == {"long": 2, "short": 0} and view["settings"]["use_long"] is True
    assert "character" in view["inferred"] and view["sections"]["short"] == ["facts", "searches", "topics", "projects"]
    guess = next(i for i in view["items"] if i["section"] == "character")
    assert api.post("confirm", {"id": guess["id"]})["item"]["origin"] == "confirmed"
    assert api.post("item", {"id": first["id"], "text": "Lives in Padova"})["item"]["text"] == "Lives in Padova"
    assert [i["text"] for i in api.overview(query="padova")["items"]] == ["Lives in Padova"]
    assert [i["section"] for i in api.overview(section="character")["items"]] == ["character"]
    assert api.post("delete", {"id": first["id"]}) == {"deleted": True}
    with pytest.raises(PersonalMemoryError, match="not there"):
        api.post("delete", {"id": first["id"]})


def test_settings_and_clearing_are_checked(tmp_path):
    api = MemoriesApi(memory(tmp_path))
    assert api.post("settings", {"learn_long": True, "weights": {"emotions": "high"}})["settings"]["weights"]["emotions"] == "high"
    with pytest.raises(PersonalMemoryError):
        api.post("settings", {"weights": {"emotions": "loud"}})
    api.memory.add("long", "place", "Lives in Verona")
    api.memory.add("short", "facts", "Has a dentist visit on Friday")
    with pytest.raises(PersonalMemoryError, match="confirmation"):
        api.post("clear", {"kind": "short"})
    with pytest.raises(PersonalMemoryError, match="long-term"):
        api.post("clear", {"kind": "everything", "confirm": True})
    assert api.post("clear", {"kind": "short", "confirm": True}) == {"deleted": 1} and api.memory.count() == 1
    with pytest.raises(PersonalMemoryError, match="unknown"):
        api.post("frobnicate", {})


class Plain(unittest.TestCase):
    """The routes, on a server with this PC's own page and on the listener for phones."""

    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        from serve.remote import ApiKeys, RemoteAccess
        cls.svc.keys = ApiKeys(base / "keys.json")
        cls.svc.remote = RemoteAccess(base / "remote.json", 0, lambda ip, port: None)
        cls.mem = PersonalMemory(base / "pm", key=KEY)
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner")), mock.patch.object(handler, "_PERSONAL", cls.mem),
                       mock.patch.object(handler, "_MEMORIES", MemoriesApi(cls.mem))]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.phone = handler._listen(cls.svc, "127.0.0.1", 0, remote=True)
        cls.phone_base = f"http://127.0.0.1:{cls.phone.server_address[1]}"
        cls.row, cls.key = cls.svc.keys.create("Phone")

    @classmethod
    def tearDownClass(cls):
        for server in (cls.httpd, cls.phone):
            server.shutdown()
            server.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None, headers=None, base=None):
        h = {"Content-Type": "application/json"} if method == "POST" else {}
        h.update(headers or {})
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request((base or self.base) + path, data=data, headers=h, method=method), timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (json.loads(body) if body[:1] == b"{" else body)

    def test_this_pcs_page_reads_and_writes(self):
        status, body = self.call("POST", "/api/memories/item", {"kind": "short", "section": "topics", "text": "Is comparing two e-bikes"})
        self.assertEqual(status, 200)
        status, view = self.call("GET", "/api/memories?q=bikes")
        self.assertEqual((status, [i["text"] for i in view["items"]]), (200, ["Is comparing two e-bikes"]))
        self.assertEqual(self.call("POST", "/api/memories/delete", {"id": body["item"]["id"]})[1], {"deleted": True})

    def test_bad_requests_are_refused(self):
        status, body = self.call("POST", "/api/memories/item", {"kind": "short", "section": "nothing", "text": "x y z"})
        self.assertEqual(status, 400)
        self.assertIn("sections", body["error"]["message"])

    def test_a_phone_from_away_cannot_reach_them_even_with_its_key(self):
        auth = {"Authorization": "Bearer " + self.key}
        self.svc.remote._httpd = object()
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):                    # the test's own connection is on this PC
            for method, path in (("GET", "/api/memories"), ("POST", "/api/memories/settings"), ("POST", "/api/memories/item")):
                status, _ = self.call(method, path, {} if method == "POST" else None, headers=auth, base=self.phone_base)
                self.assertEqual(status, 403, path)
            self.assertEqual(self.call("GET", "/api/agent/sessions", headers=auth, base=self.phone_base)[0], 200)


class FakeService:
    def __init__(self, **kw):
        self.stopping = threading.Event()
        self.loading = kw.get("loading", False)
        self._loaded = kw.get("loaded", True)
        self.status_lock = threading.Lock()
        self.status = {"busy": kw.get("busy", False), "queued": kw.get("queued", 0)}
        self.live_reqs = kw.get("live", {})
        self.last_request_at = kw.get("last")
        self.util = kw.get("util", 0)

    def loaded(self):
        return self._loaded

    def hardware_now(self):
        return {"gpus": [{"util_pct": self.util}]}


def test_the_gpu_is_free_only_when_nothing_else_uses_it():
    now = 1000.0
    assert gpu_free(FakeService(), now=now)
    assert not gpu_free(FakeService(busy=True), now=now) and not gpu_free(FakeService(queued=1), now=now)
    assert not gpu_free(FakeService(live={1: object()}), now=now) and not gpu_free(FakeService(loaded=False), now=now)
    assert not gpu_free(FakeService(loading=True), now=now) and not gpu_free(FakeService(util=80), now=now)
    assert not gpu_free(FakeService(last=now - 1), now=now)                                # a request a moment ago
    assert gpu_free(FakeService(last=now - 10), now=now)
    assert gpu_free(FakeService(last=now - 1), mine=now - 1, now=now)                      # that request was ours
    stopping = FakeService()
    stopping.stopping.set()
    assert not gpu_free(stopping, now=now)


def test_the_worker_keeps_a_message_only_when_a_memory_learns_and_learns_when_free(tmp_path):
    notes = memory(tmp_path)
    replies = [json.dumps({"ops": [{"op": "add", "kind": "long", "section": "place", "text": "Lives in Verona", "origin": "said"}]})]
    asked = []

    class Model:
        def chat_completion(self, messages, **kw):
            asked.append(messages)
            return {"choices": [{"message": {"content": replies[0]}}]}

    notes.set_settings({"learn_long": False, "learn_short": False})                                        # the person switched learning off
    free = [False]
    worker = MemoryWorker(notes, lambda: Model(), lambda: free[0], poll_s=0.05, retry_s=0.05)
    assert worker.offer({"text": "I moved to Verona last week"}) is False and notes.queue_len() == 0      # nothing learns yet
    notes.set_settings({"learn_long": True})
    assert worker.offer({"text": "ok"}) is False                                                          # too short to tell anything
    assert worker.offer({"text": "I moved to Verona last week", "project": "", "searches": ["trains to Verona"]}) is True
    worker.start()
    try:
        time.sleep(0.3)
        assert asked == [] and worker.status()["waiting"] == 1                                            # the GPU is busy: it waits
        free[0] = True
        deadline = time.time() + 5
        while worker.status()["waiting"] and time.time() < deadline:
            time.sleep(0.05)
    finally:
        worker.stop()
    assert [i["text"] for i in notes.items()] == ["Lives in Verona"] and worker.status()["waiting"] == 0
    assert "trains to Verona" in asked[0][1]["content"]


class Stub:
    """The conversation manager with a stand-in process, and a list of what it handed over to learn from."""

    def __init__(self, tmp_path):
        from serve.agent_sessions import SessionManager
        stub = str(Path(__file__).with_name("session_stub.py"))
        self.jobs = []
        self.mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=lambda sid, rec, history: [__import__("sys").executable, stub, "--session-id", sid, "--history-dir", str(history), "--die-times", "0"],
                                  tick_s=0.1, personal_dir=tmp_path / "pm", personal_keys=tmp_path / "keys", learn=self.jobs.append,
                                  default_workspace=tmp_path / "chat")
        (tmp_path / "chat").mkdir()
        (tmp_path / "proj").mkdir()

    def until(self, predicate, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        raise AssertionError("timed out")


def test_a_turn_that_ends_hands_over_what_the_person_wrote(tmp_path):
    s = Stub(tmp_path)
    try:
        sid = s.mgr.create(str(tmp_path / "proj"), title="")["id"]
        s.mgr.send(sid, "primo messaggio")
        s.until(lambda: s.jobs)
        assert s.jobs == [{"text": "primo messaggio", "project": "proj", "conversation": sid, "searches": []}]      # a folder's name is the project
        chat = s.mgr.create(None, title="")["id"]
        s.mgr.send(chat, "just a chat message")
        s.until(lambda: len(s.jobs) == 2)
        assert s.jobs[1]["project"] == ""                                                                # the Chat has no project
    finally:
        s.mgr.shutdown()


def test_a_routine_is_not_the_person_talking(tmp_path):
    s = Stub(tmp_path)
    try:
        sid = s.mgr.create(str(tmp_path / "proj"), title="Routine: x")["id"]
        s.mgr.keep_out_of_memory(sid)
        s.mgr.send(sid, "check the invoices every morning")
        s.until(lambda: "done" in [e["event"] for e in s.mgr.events(sid)["events"]])
        time.sleep(0.3)
        assert s.jobs == []
    finally:
        s.mgr.shutdown()


def test_the_conversation_process_gets_the_folders_of_the_memories(tmp_path):
    from serve.agent_sessions import SessionManager
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", personal_dir=tmp_path / "pm", personal_keys=tmp_path / "keys", tick_s=0.1)
    try:
        (tmp_path / "w").mkdir()
        sid = mgr.create(str(tmp_path / "w"))["id"]
        cmd = mgr._command(sid, mgr._record(sid), tmp_path / "h")
        assert cmd[cmd.index("--personal-memory") + 1] == str(tmp_path / "pm") and cmd[cmd.index("--personal-memory-keys") + 1] == str(tmp_path / "keys")
        plain = SessionManager(tmp_path / "j2", "http://127.0.0.1:1", tick_s=0.1)
        try:
            assert "--personal-memory" not in plain._command(sid, mgr._record(sid), tmp_path / "h")
        finally:
            plain.shutdown()
    finally:
        mgr.shutdown()
