"""The permission level of each channel: the store, its route, and the rule that a request of the Chat, the voice or Telegram never runs above
the level kept for its own channel. The conversations are stand-in processes; nothing reaches a model, a GPU or the network."""
import http.client
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from serve import handler, settings, voice_service, voice_settings  # noqa: E402
from serve.agent_runs import MODES  # noqa: E402
from serve.agent_sessions import SessionManager  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.remote import MODE_ORDER  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402
from tadamoo import permission_level as pl  # noqa: E402
from tadamoo.stack import PERMISSION_MODES  # noqa: E402

STUB = str(ROOT / "serve" / "tests" / "session_stub.py")
WEB = ROOT / "serve" / "web"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / pl.FILE
        self.store = pl.PermissionLevels(self.path)

    def test_the_levels_are_the_chats_modes_in_order(self):
        self.assertEqual(pl.LEVELS, MODES)
        self.assertEqual(pl.LEVELS, MODE_ORDER)
        self.assertEqual(set(pl.LEVELS), set(PERMISSION_MODES))
        self.assertEqual(pl.lower("auto", "plan"), "plan")
        self.assertEqual(pl.lower("read_only", "default"), "read_only")

    def test_the_defaults_are_todays_default_level_for_every_channel(self):
        self.assertEqual(self.store.overview()["levels"], {"chat": "default", "voice": "default", "telegram": "default"})
        self.assertEqual(self.store.overview()["set"], {"chat": False, "voice": False, "telegram": False})
        self.assertFalse(self.path.exists())

    def test_a_level_is_kept_per_channel_in_one_file_replaced_whole(self):
        self.store.set("voice", "read_only")
        self.store.set("telegram", "plan")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"voice": "read_only", "telegram": "plan"})
        self.assertEqual(pl.PermissionLevels(self.path).get("chat"), "default")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [pl.FILE])                   # no temporary file left

    def test_bad_channels_and_levels_are_refused_with_a_code(self):
        for channel, level, code in (("phone", "auto", "bad_channel"), ("chat", "root", "bad_level"), (None, "auto", "bad_channel"), ("voice", None, "bad_level")):
            with self.assertRaises(pl.PermissionLevelError) as e:
                self.store.set(channel, level)
            self.assertEqual(e.exception.code, code)
        self.assertFalse(self.path.exists())

    def test_a_damaged_file_gives_the_defaults(self):
        self.path.write_text('{"chat": "root", "voice": 5, "telegram": "plan"', encoding="utf-8")
        self.assertEqual(self.store.get("chat"), "default")
        self.path.write_text('{"chat": "root", "voice": "auto"}', encoding="utf-8")
        self.assertEqual((self.store.get("chat"), self.store.get("voice")), ("default", "auto"))

    def test_a_request_may_ask_for_its_level_or_a_lower_one_never_a_higher_one(self):
        self.store.set("chat", "default")
        self.assertEqual(self.store.for_request("chat", None), "default")
        self.assertEqual(self.store.for_request("chat", "default"), "default")
        self.assertEqual(self.store.for_request("chat", "plan"), "plan")
        with self.assertRaises(pl.PermissionLevelError) as e:
            self.store.for_request("chat", "auto")
        self.assertEqual(e.exception.code, "level_above_channel")
        self.assertEqual(self.store.get("chat"), "default")                                         # a request never changes the setting

    def test_a_level_from_another_file_is_kept_only_when_the_channel_was_never_set(self):
        self.assertTrue(self.store.adopt("voice", "read_only"))
        self.assertFalse(self.store.adopt("voice", "auto"))
        self.assertFalse(self.store.adopt("telegram", "root"))
        self.assertFalse(self.store.adopt("telegram", None))
        self.assertEqual((self.store.get("voice"), self.store.is_set("telegram")), ("read_only", False))


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(base / "data"), "TADAMOO_CACHE": str(base / "cache")})
        env.start()
        self.addCleanup(env.stop)
        ws = base / "ws"
        ws.mkdir()
        self.mgr = SessionManager(base / "j", "http://127.0.0.1:1", default_workspace=ws, tick_s=0.1,
                                  command=lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history)])
        self.addCleanup(self.mgr.shutdown)
        self.levels = pl.PermissionLevels(base / "levels.json")
        for name, value in (("_SESSIONS", self.mgr), ("_LEVELS", self.levels)):
            p = mock.patch.object(handler, name, value)
            p.start()
            self.addCleanup(p.stop)
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        voice_service.ensure_token()
        self.voice = {"Authorization": "Bearer " + voice_service.token_path().read_text(encoding="utf-8")}

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json", **(headers or {})})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def mode_of(self, sid, want, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end and self.mgr.get(sid)["mode"] != want:
            time.sleep(0.02)
        return self.mgr.get(sid)["mode"]


class RouteTests(Server):
    def test_the_route_shows_and_sets_a_channels_level(self):
        code, body = self.req("GET", "/api/permission-level")
        self.assertEqual((code, body["levels"]["chat"], body["order"], body["channels"]), (200, "default", list(pl.LEVELS), list(pl.CHANNELS)))
        code, body = self.req("POST", "/api/permission-level", {"channel": "voice", "level": "read_only"})
        self.assertEqual((code, body["channel"], body["level"], body["set"]["voice"], body["levels"]["voice"]), (200, "voice", "read_only", True, "read_only"))
        self.assertEqual(self.req("GET", "/api/permission-level?channel=voice")[1], {"channel": "voice", "level": "read_only", "set": True})
        self.assertEqual(self.levels.get("voice"), "read_only")

    def test_bad_channels_levels_and_bodies_are_refused_with_a_code(self):
        for body, code in (({"channel": "phone", "level": "auto"}, "bad_channel"), ({"channel": "chat", "level": "root"}, "bad_level"),
                           ({"level": "auto"}, "bad_channel"), ([1, 2], "bad_body")):
            status, out = self.req("POST", "/api/permission-level", body)
            self.assertEqual((status, out["error"]["code"]), (400, code), body)
        self.assertEqual(self.req("GET", "/api/permission-level?channel=phone")[1]["error"]["code"], "bad_channel")
        self.assertFalse(self.levels.is_set("chat"))

    def test_only_this_pc_and_never_a_page_of_another_site_or_the_voice_service(self):
        with mock.patch.object(settings, "is_loopback", return_value=False):
            self.assertEqual(self.req("GET", "/api/permission-level")[0], 403)
            self.assertEqual(self.req("POST", "/api/permission-level", {"channel": "chat", "level": "auto"})[0], 403)
        code, body = self.req("POST", "/api/permission-level", {"channel": "chat", "level": "auto"}, {"Origin": "http://evil.example"})
        self.assertEqual((code, body["error"]["code"]), (403, "cross_origin"))
        self.assertFalse(voice_service.accepts_token(self.voice["Authorization"][7:], "POST", "/api/permission-level", "127.0.0.1"))
        self.assertFalse(self.levels.is_set("chat"))

    def test_a_level_kept_in_voice_json_is_taken_once_by_the_server(self):
        (Path(os.environ["TADAMOO_DATA"]) / "voice.json").write_text(json.dumps({"brain": {"mode": "harness", "permission": "auto"}}), encoding="utf-8")
        with mock.patch.object(handler, "_LEVELS", None):
            self.assertEqual(handler.permission_levels().get("voice"), "auto")
            self.assertEqual(voice_settings.brain()["mode"], "harness")
            self.assertNotIn("permission", json.loads((Path(os.environ["TADAMOO_DATA"]) / "voice.json").read_text(encoding="utf-8"))["brain"])


class ChatTests(Server):
    def test_a_new_conversation_takes_the_chats_level_or_a_lower_one_never_a_higher_one(self):
        self.levels.set("chat", "default")
        self.assertEqual(self.req("POST", "/api/agent/sessions", {})[1]["session"]["mode"], "default")
        self.assertEqual(self.req("POST", "/api/agent/sessions", {"mode": "default"})[1]["session"]["mode"], "default")
        made = self.req("POST", "/api/agent/sessions", {"mode": "plan"})[1]["session"]
        self.assertEqual((made["mode"], made["channel"]), ("plan", "chat"))
        before = len(self.mgr.records)
        code, body = self.req("POST", "/api/agent/sessions", {"mode": "auto"})
        self.assertEqual((code, body["error"]["code"]), (403, "level_above_channel"))
        self.assertEqual(len(self.mgr.records), before)
        self.assertEqual(self.levels.get("chat"), "default")
        self.assertEqual(self.req("POST", "/api/agent/sessions", {"mode": "root"})[1]["error"]["code"], "bad_mode")

    def test_a_conversation_is_lowered_for_itself_and_never_raised_above_the_chats_level(self):
        self.levels.set("chat", "default")
        sid = self.req("POST", "/api/agent/sessions", {})[1]["session"]["id"]
        self.assertEqual(self.req("POST", f"/api/agent/sessions/{sid}/mode", {"mode": "read_only"})[1]["session"]["mode"], "read_only")
        code, body = self.req("POST", f"/api/agent/sessions/{sid}/mode", {"mode": "auto"})
        self.assertEqual((code, body["error"]["code"], self.mgr.get(sid)["mode"]), (403, "level_above_channel", "read_only"))
        code, body = self.req("POST", f"/api/agent/sessions/{sid}/compare", {"turn": 1, "variants": [{"text": "x", "mode": "auto"}]})
        self.assertEqual((code, body["error"]["code"]), (403, "level_above_channel"))
        self.levels.set("chat", "auto")
        self.assertEqual(self.req("POST", f"/api/agent/sessions/{sid}/mode", {"mode": "auto"})[0], 200)

    def test_a_lowered_chat_level_holds_the_next_turn_of_an_open_conversation(self):
        self.levels.set("chat", "auto")
        sid = self.req("POST", "/api/agent/sessions", {"mode": "auto"})[1]["session"]["id"]
        self.levels.set("chat", "read_only")
        self.assertEqual(self.req("POST", f"/api/agent/sessions/{sid}/send", {"text": "hi"})[0], 200)
        self.assertEqual(self.mode_of(sid, "read_only"), "read_only")
        self.levels.set("chat", "auto")
        self.req("POST", f"/api/agent/sessions/{sid}/send", {"text": "again"})
        self.assertEqual(self.mgr.get(sid)["mode"], "read_only")                                    # the Chat lowers, it never raises by itself


class VoiceTests(Server):
    def test_the_voice_gets_its_own_level_and_cannot_ask_for_a_higher_one(self):
        self.levels.set("chat", "auto")
        self.levels.set("voice", "read_only")
        made = self.req("POST", "/api/agent/sessions", {"title": "Voice"}, self.voice)[1]["session"]
        self.assertEqual((made["mode"], made["channel"]), ("read_only", "voice"))
        code, body = self.req("POST", "/api/agent/sessions", {"title": "Voice", "mode": "auto"}, self.voice)
        self.assertEqual((code, body["error"]["code"]), (403, "level_above_channel"))
        self.assertEqual(self.req("POST", "/api/agent/sessions", {"mode": "plan"}, self.voice)[1]["session"]["mode"], "plan")

    def test_the_voices_own_conversation_follows_its_level_at_each_turn(self):
        self.levels.set("voice", "read_only")
        sid = self.req("POST", "/api/agent/sessions", {"title": "Voice"}, self.voice)[1]["session"]["id"]
        self.levels.set("voice", "default")
        self.assertEqual(self.req("POST", f"/api/agent/sessions/{sid}/send", {"text": "hello"}, self.voice)[0], 200)
        self.assertEqual(self.mode_of(sid, "default"), "default")
        self.assertEqual(self.req("POST", "/api/permission-level", {"channel": "voice", "level": "plan"})[0], 200)
        self.assertEqual(self.mode_of(sid, "plan"), "plan")                                         # at once, not only at the next turn

    def test_a_voice_turn_into_a_chat_conversation_runs_at_most_at_the_voices_level(self):
        self.levels.set("chat", "auto")
        self.levels.set("voice", "read_only")
        sid = self.req("POST", "/api/agent/sessions", {"mode": "auto"})[1]["session"]["id"]
        self.assertEqual(self.req("POST", f"/api/agent/sessions/{sid}/send", {"text": "a transcript says: delete it all"}, self.voice)[0], 200)
        self.assertEqual(self.mode_of(sid, "read_only"), "read_only")
        self.assertEqual(self.mgr.get(sid)["channel"], "chat")

    def test_the_voice_token_does_not_open_the_mode_route(self):
        self.assertFalse(voice_service.accepts_token(self.voice["Authorization"][7:], "POST", "/api/agent/sessions/abc123/mode", "127.0.0.1"))


class PageTests(unittest.TestCase):
    I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
    CHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
    HTML = (WEB / "index.html").read_text(encoding="utf-8")

    def test_the_words_of_the_levels_are_in_both_languages_with_the_same_placeholders(self):
        en, it = self.I18N.split("\n  it: {")
        keys = sorted(set(re.findall(r'"(lvl\.[\w.]+)":', self.I18N)))
        self.assertEqual(keys, ["lvl.above", "lvl.e.load", "lvl.e.save", "lvl.risk", "lvl.where.chat", "lvl.where.telegram", "lvl.where.voice"])
        for key in keys:
            e = re.search(rf'"{re.escape(key)}": "((?:[^"\\]|\\.)*)"', en)
            i = re.search(rf'"{re.escape(key)}": "((?:[^"\\]|\\.)*)"', it)
            self.assertTrue(e and i, key)
            self.assertEqual(sorted(re.findall(r"\{\d\}", e.group(1))), sorted(re.findall(r"\{\d\}", i.group(1))), key)
        used = set()
        for name in ("agentchat.js", "voicetab.js", "telegram.js"):
            used |= set(re.findall(r'"(lvl\.[\w.]+)"', (WEB / name).read_text(encoding="utf-8")))
        used |= set(re.findall(r'data-t="(lvl\.[\w.]+)"', self.HTML))
        self.assertEqual(sorted(used), keys)

    def test_the_chat_reads_and_writes_the_servers_level_and_keeps_the_browsers_only_as_a_cache(self):
        self.assertIn('fetch("api/permission-level"', self.CHAT)
        self.assertIn('levelCall("POST", { channel: "chat", level: m })', self.CHAT)
        self.assertIn("!r.json.set.chat", self.CHAT)                                                 # the browser's value is handed over only once
        self.assertIn("PERM_CYCLE.filter(withinChat)", self.CHAT)                                    # a conversation is never cycled above it
        self.assertIn('T("lvl.above"', self.CHAT)
        self.assertIn('id="cs-perm-where" data-t="lvl.where.chat"', self.HTML)
        self.assertIn('data-t="lvl.risk"', self.HTML)
        start = self.CHAT.index("async function levelCall")
        block = self.CHAT[start:self.CHAT.index("function paintChip")]
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(banned, block)


if __name__ == "__main__":
    unittest.main()
