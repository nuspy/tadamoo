"""The server side of the voice brains: the settings in voice.json, the service's token accepted for its own routes only, and the Voice tab's calls to
the voice service (a fixed list, the token never leaving)."""
from __future__ import annotations

import http.client
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from serve import settings, voice_api, voice_proxy, voice_service, voice_settings
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        p = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.data), "TADAMOO_CACHE": str(Path(self.tmp.name) / "cache")})
        p.start()
        self.addCleanup(p.stop)


class BrainSettingsTests(Base):
    def test_defaults(self):
        self.assertEqual(voice_settings.brain(), {"mode": "direct", "persona": "", "workspace": "", "hotkeys": False, "base_url": ""})

    def test_a_choice_is_kept_and_only_the_named_fields_change(self):
        voice_settings.set_brain({"mode": "harness"})
        voice_settings.set_brain({"persona": "maria-rossi", "hotkeys": True})
        b = voice_settings.brain()
        self.assertEqual((b["mode"], b["persona"], b["hotkeys"]), ("harness", "maria-rossi", True))
        self.assertNotIn("permission", b)

    def test_it_sits_next_to_the_services_and_leaves_them_alone(self):
        voice_settings.set_enabled("tts", True)
        voice_settings.set_brain({"mode": "harness"})
        data = json.loads((self.data / "voice.json").read_text(encoding="utf-8"))
        self.assertEqual(data["services"]["tts"]["enabled"], True)
        self.assertEqual(data["brain"], {"mode": "harness"})

    def test_bad_values_are_refused_in_both_languages(self):
        folder = self.tmp.name
        for req in ({"mode": "hermes"}, {"permission": "root"}, {"permission": "auto"}, {"persona": "Maria Rossi"}, {"persona": "../x"}, {"persona": 5}, {"workspace": "relative"},
                    {"workspace": str(Path(folder) / "nope")}, {"workspace": 3}, {"hotkeys": "yes"}, {"base_url": "http://x"}, {"surprise": 1}, []):
            with self.assertRaises(voice_settings.VoiceSettingsError, msg=str(req)) as e:
                voice_settings.set_brain(req)
            self.assertTrue(e.exception.en and e.exception.it)
        self.assertFalse((self.data / "voice.json").exists())

    def test_a_folder_that_exists_and_no_persona_are_accepted(self):
        voice_settings.set_brain({"workspace": self.tmp.name, "persona": ""})
        self.assertEqual(voice_settings.brain()["workspace"], os.path.normpath(self.tmp.name))

    def test_the_servers_address_is_written_once_per_value(self):
        voice_settings.write_server_address(7242)
        self.assertEqual(voice_settings.brain()["base_url"], "http://127.0.0.1:7242")
        with mock.patch.object(voice_settings, "_write", side_effect=AssertionError("written again")):
            voice_settings.write_server_address(7242)
        voice_settings.write_server_address(7300)
        self.assertEqual(voice_settings.brain()["base_url"], "http://127.0.0.1:7300")

    def test_a_personality_must_exist(self):
        with self.assertRaises(voice_settings.VoiceSettingsError):
            voice_api.set_brain({"persona": "maria-rossi"})
        (self.data / "personas" / "maria-rossi").mkdir(parents=True)
        (self.data / "personas" / "maria-rossi" / "persona.json").write_text("{}", encoding="utf-8")
        self.assertEqual(voice_api.set_brain({"persona": "maria-rossi"})["persona"], "maria-rossi")

    def test_a_level_found_in_voice_json_is_taken_out_once(self):
        (self.data).mkdir(parents=True, exist_ok=True)
        (self.data / "voice.json").write_text(json.dumps({"brain": {"mode": "harness", "permission": "read_only"}}), encoding="utf-8")
        self.assertEqual(voice_settings.take_permission(), "read_only")
        self.assertEqual(json.loads((self.data / "voice.json").read_text(encoding="utf-8"))["brain"], {"mode": "harness"})
        self.assertIsNone(voice_settings.take_permission())


class TokenTests(Base):
    PATHS_OK = (("POST", "/v1/chat/completions"), ("POST", "/api/personas/maria-rossi/compose"), ("GET", "/api/personas"), ("POST", "/api/agent/sessions"),
                ("GET", "/api/agent/sessions/abc123/events?after=4"), ("POST", "/api/agent/sessions/abc123/send"),
                ("POST", "/api/agent/sessions/abc123/answer"), ("POST", "/api/agent/sessions/abc123/persona"))
    PATHS_NO = (("GET", "/api/settings"), ("POST", "/api/settings"), ("GET", "/api/agent/sessions"), ("POST", "/api/agent/sessions/abc123/cancel"),
                ("POST", "/api/agent/sessions/abc123/mode"), ("GET", "/api/permission-level"), ("POST", "/api/permission-level"),
                ("POST", "/api/agent/sessions/../../settings/send"), ("POST", "/api/personas/maria-rossi/delete"), ("POST", "/api/personas"),
                ("GET", "/api/personas/maria-rossi"), ("GET", "/api/wallet"), ("POST", "/api/voice/brain"), ("POST", "/v1/messages"),
                ("GET", "/v1/chat/completions"), ("POST", "/api/personas/Bad_Slug/compose"))

    def setUp(self):
        super().setUp()
        voice_service.ensure_token()
        self.token = voice_service.token_path().read_text(encoding="utf-8")

    def test_the_token_opens_its_routes_on_this_pc(self):
        for method, path in self.PATHS_OK:
            self.assertTrue(voice_service.accepts_token(self.token, method, path, "127.0.0.1"), path)
            self.assertTrue(voice_service.accepts_token(self.token, method, path, "::1"), path)

    def test_nothing_else_not_another_pc_not_another_token(self):
        for method, path in self.PATHS_NO:
            self.assertFalse(voice_service.accepts_token(self.token, method, path, "127.0.0.1"), path)
        for method, path in self.PATHS_OK:
            self.assertFalse(voice_service.accepts_token(self.token, method, path, "192.168.1.9"), path)
            self.assertFalse(voice_service.accepts_token(self.token + "x", method, path, "127.0.0.1"), path)
            self.assertFalse(voice_service.accepts_token("", method, path, "127.0.0.1"), path)

    def test_without_a_token_file_nothing_is_accepted(self):
        voice_service.token_path().unlink()
        self.assertFalse(voice_service.accepts_token("", "POST", "/v1/chat/completions", "127.0.0.1"))
        self.assertFalse(voice_service.accepts_token("anything", "POST", "/v1/chat/completions", "127.0.0.1"))


class ServerBase(Base):
    def setUp(self):
        super().setUp()
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.settings_info = {"config": None, "model": None, "cli": [], "running": {}}
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        voice_service.ensure_token()
        self.token = voice_service.token_path().read_text(encoding="utf-8")

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            raw = None if body is None else json.dumps(body)
            c.request(method, path, body=raw, headers={"Content-Type": "application/json", **(headers or {})})
            r = c.getresponse()
            data = r.read()
            try:
                return r.status, json.loads(data or b"null")
            except ValueError:
                return r.status, data
        finally:
            c.close()



class ServerTests(ServerBase):
    # -- the token, with an API key set on the server
    def test_with_an_api_key_the_service_token_opens_only_its_routes(self):
        self.svc.api_key = "the-api-key"
        auth = {"Authorization": "Bearer " + self.token}
        self.assertEqual(self.req("GET", "/api/personas")[0], 401)                              # no credentials
        self.assertEqual(self.req("GET", "/api/personas", headers=auth)[0], 200)                  # the service
        self.assertEqual(self.req("GET", "/api/settings", headers=auth)[0], 401)                  # not a route of the service
        self.assertEqual(self.req("GET", "/api/personas", headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self.req("GET", "/api/settings", headers={"Authorization": "Bearer the-api-key"})[0], 200)

    def test_with_a_control_secret_the_service_token_still_writes_to_its_routes_and_no_other(self):
        self.svc.control_token = "the-secret"
        auth = {"Authorization": "Bearer " + self.token}
        code, body = self.req("POST", "/api/personas/maria-rossi/compose", {"goal": "x"}, auth)
        self.assertEqual(code, 404, body)                                                          # past the secret, no such personality
        code, body = self.req("POST", "/api/personas/maria-rossi/compose", {"goal": "x"})
        self.assertEqual(code, 403)                                                                # no secret, no token
        code, _ = self.req("POST", "/api/voice/brain", {"mode": "harness"}, auth)
        self.assertEqual(code, 403)                                                                # the token does not open this one
        self.assertEqual(voice_settings.brain()["mode"], "direct")

    # -- the settings routes
    def test_the_brain_settings_round_trip_and_refuse_bad_values(self):
        code, body = self.req("GET", "/api/voice/brain")
        self.assertEqual((code, body["mode"]), (200, "direct"))
        code, body = self.req("POST", "/api/voice/brain", {"mode": "harness", "hotkeys": True})
        self.assertEqual((code, body["mode"], body["hotkeys"]), (200, "harness", True))
        code, body = self.req("POST", "/api/voice/brain", {"permission": "auto"})
        self.assertEqual(code, 400)
        self.assertEqual(body["error"]["code"], "field")                                       # the level is /api/permission-level
        self.assertTrue(body["error"]["message_it"])
        self.assertNotIn("permission", self.req("GET", "/api/voice/brain")[1])

    def test_the_brain_settings_only_on_this_pc(self):
        with mock.patch.object(settings, "is_loopback", return_value=False):
            self.assertEqual(self.req("GET", "/api/voice/brain")[0], 403)
            self.assertEqual(self.req("POST", "/api/voice/brain", {"mode": "harness"})[0], 403)
            self.assertEqual(self.req("POST", "/api/voice/service/call", {"method": "GET", "path": "/status"})[0], 403)
        self.assertEqual(voice_settings.brain()["mode"], "direct")

    def test_the_servers_own_start_writes_its_address(self):
        voice_settings.write_server_address(self.port)
        self.assertEqual(self.req("GET", "/api/voice/brain")[1]["base_url"], f"http://127.0.0.1:{self.port}")


class StandIn:
    """The voice service: records every request and answers with a body that names it."""

    def __init__(self, token):
        self.seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.seen.append((self.command, self.path, self.headers.get("Authorization"), json.loads(raw) if raw else None))
                code = 200 if self.headers.get("Authorization") == "Bearer " + token else 401
                out = json.dumps({"route": self.path, "token_echo": "never"}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            do_GET = do_POST = _answer

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ProxyTests(ServerBase):
    def setUp(self):
        super().setUp()
        self.service = StandIn(self.token)
        self.addCleanup(self.service.close)
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "voice-service.json").write_text(json.dumps({"host": "127.0.0.1", "port": self.service.port, "pid": 1, "started": "t"}), encoding="utf-8")

    def call(self, method, path, body=None):
        return self.req("POST", "/api/voice/service/call", {"method": method, "path": path, **({} if body is None else {"body": body})})

    def test_every_allowed_route_reaches_the_service_with_the_token(self):
        calls = [("GET", "/status", None), ("GET", "/functions", None), ("GET", "/brain/state", None), ("GET", "/session/state", None), ("GET", "/diag/brain", None),
                 ("POST", "/session/start", {"mode": "full", "brain": "direct"}), ("POST", "/session/stop", None), ("POST", "/session/new", None),
                 ("POST", "/brain/say", {"text": "Prova."})]
        for method, path, body in calls:
            code, out = self.call(method, path, body)
            self.assertEqual((code, out["route"]), (200, path), path)
        self.assertEqual([s[:3] for s in self.service.seen], [(m, p, "Bearer " + self.token) for m, p, _ in calls])
        self.assertEqual(self.service.seen[5][3], {"mode": "full", "brain": "direct", "avatar": False})
        self.assertEqual(self.service.seen[8][3], {"text": "Prova."})

    def test_the_token_never_comes_back(self):
        code, out = self.call("GET", "/status")
        self.assertNotIn(self.token, json.dumps(out))

    def test_anything_not_on_the_list_is_refused_and_nothing_is_sent(self):
        for method, path in (("GET", "/config"), ("POST", "/config"), ("POST", "/call"), ("POST", "/hangup"), ("GET", "/personas"), ("POST", "/avatar/stop"),
                             ("POST", "/components/suspend"), ("POST", "/functions/apply"), ("POST", "/external/call"), ("GET", "/status/../config"),
                             ("GET", "/session/start"), ("POST", "/status"), ("get", "/status"), ("GET", "status"), ("DELETE", "/status"), ("GET", "http://evil/x"),
                             ("GET", "/status?x=1"), ("POST", "/diag/echo"), ("POST", "/session/engine"), ("GET", "/health")):
            code, out = self.call(method, path)
            self.assertEqual(code, 403, (method, path))
            self.assertEqual(out["error"]["code"], "not_allowed")
        self.assertEqual(self.service.seen, [])

    def test_bodies_are_checked_and_cleaned(self):
        for path, body in (("/session/start", {"mode": "limited"}), ("/session/start", {"mode": "half", "avatar": True}), ("/session/start", {"brain": "hermes"}),
                           ("/session/start", {"mode": "half", "extra": 1}), ("/brain/say", {"text": ""}), ("/brain/say", {"text": "x" * 301}),
                           ("/brain/say", {"text": "ok", "voice": "x"}), ("/brain/say", {}), ("/brain/say", "text"), ("/session/stop", {"a": 1})):
            code, out = self.call("POST", path, body)
            self.assertEqual(code, 400, (path, body))
        self.assertEqual(self.service.seen, [])
        code, _ = self.call("POST", "/session/start")                           # no body: the default is a half-duplex session with the configured brain
        self.assertEqual(code, 200)
        self.assertEqual(self.service.seen[0][3], {"mode": "half", "brain": "", "avatar": False})

    def test_a_bad_request_shape_is_refused(self):
        for body in ({}, {"method": "GET"}, {"path": "/status"}, {"method": "GET", "path": "/status", "extra": 1}, {"method": 5, "path": []}):
            self.assertEqual(self.req("POST", "/api/voice/service/call", body)[0], 400 if body.get("extra") else 403, body)
        self.assertEqual(self.service.seen, [])

    def test_a_service_that_is_not_there_is_a_503_with_the_reason(self):
        (self.data / "voice-service.json").unlink()
        code, out = self.call("GET", "/status")
        self.assertEqual((code, out["error"]["code"]), (503, "voice_service_down"))
        self.assertIn("has not started", out["error"]["message"])

    def test_a_refusal_of_the_service_comes_through_with_its_status(self):
        voice_service.token_path().write_text("a-different-token", encoding="utf-8")
        code, out = self.call("GET", "/status")
        self.assertEqual(code, 503)                                              # the service refused the token

    def test_the_proxy_takes_post_only(self):
        self.assertEqual(self.req("GET", "/api/voice/service/call")[0], 404)

    def test_the_list_is_what_the_page_needs_and_no_more(self):
        self.assertEqual(sorted(f"{m} {p}" for m, p in voice_proxy.ROUTES),
                         sorted(["GET /status", "GET /components/status", "GET /functions", "GET /brain/state", "GET /session/state", "GET /diag/brain", "POST /session/start",
                                 "GET /session/language", "POST /session/language",
                                 "POST /session/stop", "POST /session/new", "POST /brain/say", "GET /call/approvals", "GET /call/audit", "GET /gateway",
                                 # the avatar (serve/tests/test_voice_avatar_proxy.py)
                                 "GET /avatar/state", "GET /avatar/list", "GET /avatar/output", "GET /avatar/enhance", "GET /avatar/engines", "GET /avatar/prepare", "POST /avatar/prepare", "POST /avatar/prepare/cancel",
                                 "POST /avatar/speak", "POST /avatar/start", "POST /avatar/wizard/start",
                                 "POST /avatar/train/plan", "POST /avatar/train/start", "POST /avatar/train/setup", "POST /avatar/train/python", "POST /avatar/train/export",
                                 "GET /avatar/train/status", "POST /avatar/train/cancel", "POST /avatar/engines/python",
                                 # meetings, the copilot and the calendar (serve/tests/test_voice_meetings_page.py)
                                 "GET /meetbot/status", "POST /meetbot/join", "POST /meetbot/leave", "GET /meetings/list", "GET /meetings/plan", "POST /meetings/plan", "GET /copilot/state", "POST /copilot/toggle",
                                 "GET /calendar/status", "GET /calendar/events"]))


if __name__ == "__main__":
    unittest.main()
