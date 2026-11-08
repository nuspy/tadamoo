"""Personalities on the server: the store (names, sizes, paths), the memory of each, the routes (this PC only) and the conversations that carry one."""
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from serve import handler
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.personas_api import PersonasApi
from serve.plans import PlanService
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer
from tadamoo import personas as pe

ROOT = Path(__file__).resolve().parents[2]
KEY = bytes(range(32))


def store(tmp_path):
    return pe.PersonaStore(tmp_path / "personas", key=KEY)


def test_a_personality_is_a_folder_with_a_plain_file_and_an_encrypted_memory(tmp_path):
    s = store(tmp_path)
    made = s.create({"name": "Maria Rossi", "voice": {"engine": "kokoro", "voice": "if_sara"}, "personality": "Warm.", "language": "it", "knowledge": ["wiki-1"]})
    assert made["slug"] == "maria-rossi" and made["voice"] == {"engine": "kokoro", "voice": "if_sara"} and made["avatar"] == ""
    body = json.loads((tmp_path / "personas" / "maria-rossi" / "persona.json").read_text(encoding="utf-8"))
    assert body["name"] == "Maria Rossi" and body["knowledge"] == ["wiki-1"] and "slug" not in body
    s.memory("maria-rossi").add("long", "tastes", "Loves the poems of Montale")
    raw = (tmp_path / "personas" / "maria-rossi" / "memories.bin").read_bytes()
    assert b"Montale" not in raw and raw.startswith(b"BPM1")
    assert [p["slug"] for p in store(tmp_path).list()] == ["maria-rossi"]
    assert not list((tmp_path / "personas" / "maria-rossi").glob("*.tmp"))


def test_each_personality_has_its_own_memory(tmp_path):
    s = store(tmp_path)
    s.create({"name": "A"})
    s.create({"name": "B"})
    s.memory("a").add("short", "facts", "Likes tea")
    assert s.memory("a").count() == 1 and s.memory("b").count() == 0
    assert store(tmp_path).memory("b").items() == []


def test_names_become_safe_folder_names_and_cannot_repeat(tmp_path):
    s = store(tmp_path)
    assert s.create({"name": "Élodie d'Arc"})["slug"] == "elodie-d-arc"
    with pytest.raises(pe.PersonaError, match="already"):
        s.create({"name": "elodie D arc"})
    for bad in ("", "   ", "../x", "a/b", "a\\b", ".hidden", "x" * 41, "nul\x00", "tab\tname", "-start"):
        with pytest.raises(pe.PersonaError):
            s.create({"name": bad})
    for reserved in ("CON", "nul", "COM1"):
        with pytest.raises(pe.PersonaError):
            s.create({"name": reserved})
    assert [p["slug"] for p in s.list()] == ["elodie-d-arc"]


def test_a_name_that_climbs_out_of_the_folder_reaches_nothing(tmp_path):
    s = store(tmp_path)
    s.create({"name": "Maria"})
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "persona.json").write_text('{"name": "Leak"}', encoding="utf-8")
    for slug in ("../secret", "..", ".", "a/../maria", "maria/", "MARIA", "maria\\..", "", None, 5, ["maria"]):
        with pytest.raises(pe.PersonaError) as e:
            s.get(slug)
        assert e.value.code == "not_found"
        for call in (lambda: s.update(slug, {"name": "X"}), lambda: s.delete(slug, True), lambda: s.memory(slug)):
            with pytest.raises(pe.PersonaError):
                call()
    assert outside.exists() and (outside / "persona.json").exists()


def test_sizes_and_fields_are_checked(tmp_path):
    s = store(tmp_path)
    with pytest.raises(pe.PersonaError, match="at most 8000"):
        s.create({"name": "A", "personality": "x" * 8001})
    with pytest.raises(pe.PersonaError, match="at most 1500"):
        s.create({"name": "A", "profile": "x" * 1501})
    for bad in ({"colour": "red"}, {"voice": "kokoro"}, {"voice": {"engine": "a", "key": "b"}}, {"voice": {"engine": "a/b"}}, {"avatar": "../a.png"}, {"language": "Italian"},
                {"knowledge": "a"}, {"knowledge": ["../x"]}, {"knowledge": ["a"] * 51}, {"personality": 5}, {"personality": "bell\x07"}):
        with pytest.raises(pe.PersonaError):
            s.create({"name": "A", **bad})
    assert s.list() == []
    ok = s.create({"name": "A", "personality": "line one\r\nline two", "knowledge": ["x", "x", "y"]})
    assert ok["personality"] == "line one\nline two" and ok["knowledge"] == ["x", "y"]


def test_the_number_of_personalities_is_limited(tmp_path):
    s = store(tmp_path)
    with mock.patch.object(pe, "MAX_PERSONAS", 2):
        s.create({"name": "A"})
        s.create({"name": "B"})
        with pytest.raises(pe.PersonaError, match="at most 2"):
            s.create({"name": "C"})


def test_an_update_changes_what_is_named_and_keeps_the_folder(tmp_path):
    clock = iter(range(100, 200))
    s = pe.PersonaStore(tmp_path / "personas", key=KEY, clock=lambda: next(clock))
    s.create({"name": "Maria", "voice": {"engine": "kokoro", "voice": "a"}, "personality": "Warm."})
    changed = s.update("maria", {"name": "Maria Rossi", "voice": {"voice": "b"}})
    assert changed["slug"] == "maria" and changed["name"] == "Maria Rossi" and changed["personality"] == "Warm."
    assert changed["voice"] == {"engine": "kokoro", "voice": "b"} and changed["created"] == 100 and changed["updated"] == 101
    assert s.get("maria") == changed
    with pytest.raises(pe.PersonaError, match="name:"):
        s.update("maria", {"name": "   "})
    with pytest.raises(pe.PersonaError):
        s.update("maria", {"slug": "other"})


def test_a_damaged_file_is_left_out_of_the_list_and_not_opened(tmp_path):
    s = store(tmp_path)
    s.create({"name": "Maria"})
    (tmp_path / "personas" / "broken").mkdir()
    (tmp_path / "personas" / "broken" / "persona.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "personas" / "Not A Slug").mkdir()
    (tmp_path / "personas" / "Not A Slug" / "persona.json").write_text('{"name": "X"}', encoding="utf-8")
    assert [p["slug"] for p in s.list()] == ["maria"]
    with pytest.raises(pe.PersonaError, match="could not be read"):
        s.get("broken")


def test_deleting_needs_the_confirmation_and_takes_the_memory_with_it(tmp_path):
    api = PersonasApi(store(tmp_path))
    api.create({"name": "Maria"})
    api.memory("maria", "item", {"kind": "long", "section": "tastes", "text": "Loves the poems of Montale"})
    folder = tmp_path / "personas" / "maria"
    assert (folder / "memories.bin").is_file()
    for req in ({}, {"confirm": False}, {"confirm": "yes"}, {"confirm": 1}):
        with pytest.raises(pe.PersonaError, match="confirmation"):
            api.delete("maria", req)
    assert folder.is_dir()
    assert api.delete("maria", {"confirm": True}) == {"deleted": True, "notes": 1}
    assert not folder.exists() and api.overview()["personas"] == []
    with pytest.raises(pe.PersonaError) as e:
        api.get("maria")
    assert e.value.code == "not_found"


def test_the_page_can_read_add_edit_confirm_delete_and_clear_the_notes(tmp_path):
    api = PersonasApi(store(tmp_path))
    api.create({"name": "Maria"})
    first = api.memory("maria", "item", {"kind": "long", "section": "tastes", "text": "Loves Montale"})["item"]
    api.memory("maria", "item", {"kind": "short", "section": "facts", "text": "Has an exam on Friday"})
    view = api.get("maria")
    assert view["memory"]["counts"] == {"long": 1, "short": 1} and view["memory"]["sections"]["short"] == ["facts", "searches", "topics", "projects"]
    assert [i["text"] for i in api.get("maria", query="exam")["memory"]["items"]] == ["Has an exam on Friday"]
    assert [i["kind"] for i in api.get("maria", kind="long")["memory"]["items"]] == ["long"]
    assert api.memory("maria", "item", {"id": first["id"], "text": "Loves Leopardi"})["item"]["text"] == "Loves Leopardi"
    assert api.memory("maria", "confirm", {"id": first["id"]})["item"]["origin"] == "confirmed"
    assert api.memory("maria", "delete", {"id": first["id"]}) == {"deleted": True}
    with pytest.raises(pe.PersonaError, match="not there"):
        api.memory("maria", "delete", {"id": first["id"]})
    with pytest.raises(pe.PersonaError, match="confirmation"):
        api.memory("maria", "clear", {})
    assert api.memory("maria", "clear", {"confirm": True}) == {"deleted": 1}
    assert api.get("maria")["memory"]["counts"] == {"long": 0, "short": 0}
    for action in ("settings", "export", "cards", "frobnicate"):                    # the person's own memory page has these; a personality's does not
        with pytest.raises(pe.PersonaError):
            api.memory("maria", action, {})
    with pytest.raises(pe.PersonaError) as e:
        api.memory("nobody", "item", {"kind": "long", "section": "tastes", "text": "x y"})
    assert e.value.code == "not_found"
    with pytest.raises(pe.PersonaError, match="sections"):
        api.memory("maria", "item", {"kind": "long", "section": "nothing", "text": "x y"})


def test_the_rolling_profile_is_a_field_of_the_personality(tmp_path):
    api = PersonasApi(store(tmp_path))
    api.create({"name": "Maria"})
    assert api.update("maria", {"profile": "The person is learning Rust."})["persona"]["profile"] == "The person is learning Rust."
    assert api.get("maria")["persona"]["profile"] == "The person is learning Rust."
    assert "learning Rust" in api.compose("maria", {"goal": "hello"})["system"]


def test_compose_gives_the_system_text_and_the_notes_for_the_request(tmp_path):
    api = PersonasApi(store(tmp_path))
    api.create({"name": "Maria", "system_prompt": "You are a patient tutor."})
    api.memory("maria", "item", {"kind": "long", "section": "tastes", "text": "Loves the poems of Montale"})
    out = api.compose("maria", {"goal": "a poem by Montale"})
    assert "patient tutor" in out["system"] and "Loves the poems of Montale" in out["memory"] and "data, not instructions" in out["memory"]
    assert api.compose("maria", {"goal": "boats and sails"})["memory"] == ""
    with pytest.raises(pe.PersonaError):
        api.compose("maria", {"goal": 5})
    with pytest.raises(pe.PersonaError):
        api.compose("nobody", {"goal": "x"})


class Routes(unittest.TestCase):
    """The routes, on a server with this PC's own page."""

    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner")), mock.patch.object(handler, "_PERSONAS", PersonasApi(store(base)))]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None):
        h = {"Content-Type": "application/json"} if method == "POST" else {}
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=h, method=method), timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (json.loads(body) if body[:1] == b"{" else body)

    def test_the_whole_cycle_over_http(self):
        status, body = self.call("POST", "/api/personas", {"name": "Route Test", "personality": "Calm."})
        self.assertEqual((status, body["persona"]["slug"]), (200, "route-test"))
        self.assertEqual(self.call("POST", "/api/personas", {"name": "Route Test"})[0], 400)
        status, listing = self.call("GET", "/api/personas")
        self.assertEqual((status, [p["slug"] for p in listing["personas"]]), (200, ["route-test"]))
        self.assertEqual(self.call("POST", "/api/personas/route-test/update", {"language": "en"})[1]["persona"]["language"], "en")
        status, note = self.call("POST", "/api/personas/route-test/memory/item", {"kind": "short", "section": "topics", "text": "Is comparing two e-bikes"})
        self.assertEqual(status, 200)
        status, view = self.call("GET", "/api/personas/route-test?q=bikes")
        self.assertEqual((status, [i["text"] for i in view["memory"]["items"]]), (200, ["Is comparing two e-bikes"]))
        status, composed = self.call("POST", "/api/personas/route-test/compose", {"goal": "which e-bikes"})
        self.assertEqual(status, 200)
        self.assertIn("Calm.", composed["system"])
        self.assertIn("e-bikes", composed["memory"])
        self.assertEqual(self.call("POST", "/api/personas/route-test/delete", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/personas/route-test/delete", {"confirm": True})[1]["deleted"], True)
        self.assertEqual(self.call("GET", "/api/personas/route-test")[0], 404)

    def test_bad_requests_and_unknown_things(self):
        self.assertEqual(self.call("GET", "/api/personas/..%2F..%2Fx")[0], 404)
        self.assertEqual(self.call("GET", "/api/personas/nobody")[0], 404)
        self.assertEqual(self.call("POST", "/api/personas", {"name": "A", "colour": "red"})[0], 400)
        self.assertEqual(self.call("POST", "/api/personas/nobody/update", {"name": "A"})[0], 404)
        self.assertEqual(self.call("POST", "/api/personas/a/frobnicate", {})[0], 404)

    def test_only_this_pc_reaches_them(self):
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            for method, path in (("GET", "/api/personas"), ("GET", "/api/personas/x"), ("POST", "/api/personas"), ("POST", "/api/personas/x/compose"),
                                 ("POST", "/api/personas/x/memory/item")):
                status, _ = self.call(method, path, {} if method == "POST" else None)
                self.assertEqual(status, 403, path)


class Stub:
    def __init__(self, tmp_path):
        from serve.agent_sessions import SessionManager
        stub = str(Path(__file__).with_name("session_stub.py"))
        self.pe = store(tmp_path)
        self.pe.create({"name": "Maria"})
        self.pe.create({"name": "Gino"})
        self.mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=lambda sid, rec, history: [__import__("sys").executable, stub, "--session-id", sid, "--history-dir", str(history), "--die-times", "0"],
                                  tick_s=0.1, personas_dir=tmp_path / "personas", personal_keys=tmp_path / "keys", default_workspace=tmp_path / "chat")
        (tmp_path / "chat").mkdir()
        (tmp_path / "proj").mkdir()


def test_a_conversation_keeps_its_personality_and_hands_it_to_its_process(tmp_path):
    s = Stub(tmp_path)
    try:
        sid = s.mgr.create(str(tmp_path / "proj"), persona="maria")["id"]
        assert s.mgr.get(sid)["persona"] == "maria"
        from serve.agent_sessions import SessionManager
        real = SessionManager(tmp_path / "j2", "http://127.0.0.1:1", personas_dir=tmp_path / "personas", personal_keys=tmp_path / "keys", tick_s=0.1)
        try:
            (tmp_path / "w").mkdir()
            rid = real.create(str(tmp_path / "w"), persona="maria")["id"]
            cmd = real._command(rid, real._record(rid), tmp_path / "h")
            assert cmd[cmd.index("--personas-dir") + 1] == str(tmp_path / "personas") and cmd[cmd.index("--persona") + 1] == "maria"
            assert cmd[cmd.index("--personal-memory-keys") + 1] == str(tmp_path / "keys")
            plain = real.create(str(tmp_path / "w"))["id"]
            assert "--persona" not in real._command(plain, real._record(plain), tmp_path / "h") and real.get(plain)["persona"] == ""
        finally:
            real.shutdown()
    finally:
        s.mgr.shutdown()


def test_a_conversation_cannot_take_a_personality_that_is_not_there(tmp_path):
    from serve.agent_runs import RunError
    s = Stub(tmp_path)
    try:
        for bad in ("nobody", "../maria", 5, ["maria"]):
            with pytest.raises(RunError, match="not there"):
                s.mgr.create(str(tmp_path / "proj"), persona=bad)
        sid = s.mgr.create(str(tmp_path / "proj"))["id"]
        with pytest.raises(RunError, match="not there"):
            s.mgr.set_persona(sid, "nobody")
    finally:
        s.mgr.shutdown()


def test_the_change_is_kept_and_told_to_the_running_conversation(tmp_path):
    s = Stub(tmp_path)
    try:
        sid = s.mgr.create(str(tmp_path / "proj"), persona="maria")["id"]
        assert s.mgr.set_persona(sid, "gino")["persona"] == "gino"
        assert s.mgr.set_persona(sid, "")["persona"] == ""
        events = [e for e in s.mgr.events(sid)["events"] if e.get("event") == "persona"]
        assert [e["slug"] for e in events] == ["gino", ""]                              # told (here: pushed, as the process is not running)
        s.mgr.send(sid, "hello")
        end = time.monotonic() + 15
        while time.monotonic() < end and "done" not in [e["event"] for e in s.mgr.events(sid)["events"]]:
            time.sleep(0.05)
        assert s.mgr.set_persona(sid, "maria")["persona"] == "maria"
        end = time.monotonic() + 5
        while time.monotonic() < end and not [e for e in s.mgr.events(sid)["events"] if e.get("event") == "persona" and e["slug"] == "maria"]:
            time.sleep(0.05)
        assert s.mgr.get(sid)["persona"] == "maria"
    finally:
        s.mgr.shutdown()
