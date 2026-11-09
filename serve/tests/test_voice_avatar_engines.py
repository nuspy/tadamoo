"""The avatar engines on the server: the list (the service describes the engines, voice.json says which are chosen), choosing one for a target,
the settings that hold the choice and what they refuse, and the route on the proxy's list. The voice service is a stand-in on loopback."""
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

from serve import handler, voice_avatar, voice_proxy, voice_service, voice_settings
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.plans import PlanService
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
ENGINES = [{"id": i, "name": i, "runs_on": list(v[0]), "resolutions": list(v[1]), "status": {"available": i == "visemes", "reason": "" if i == "visemes" else "x"}}
           for i, v in voice_settings.AVATAR_ENGINES.items()]


class FakeVoice:
    def __init__(self, token):
        outer = self
        self.token, self.seen, self.answer = token, [], (200, {"avatar": "ada", "engines": ENGINES, "selection": {"stale": True}})

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append((self.path, self.headers.get("Authorization")))
                code, body = outer.answer
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Engines(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.env = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(base / "data"), "TADAMOO_CACHE": str(base / "cache")})
        cls.env.start()
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner"))]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.port = cls.httpd.server_address[1]
        cls.token = voice_service.ensure_token().read_text(encoding="utf-8").strip()
        cls.voice = FakeVoice(cls.token)
        (base / "data").mkdir(parents=True, exist_ok=True)
        (base / "data" / "voice-service.json").write_text(json.dumps({"host": "127.0.0.1", "port": cls.voice.port, "pid": 1, "started": "x"}), encoding="utf-8")
        cls.file = base / "data" / "voice.json"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.voice.close()
        for p in cls.patches:
            p.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.svc.api_key = ""
        self.voice.seen.clear()
        self.voice.answer = (200, {"avatar": "ada", "engines": ENGINES, "selection": {"stale": True}})
        if self.file.exists():
            self.file.unlink()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"} if body is not None else {})
            r = conn.getresponse()
            data = json.loads(r.read() or b"{}")
            return r.status, data
        finally:
            conn.close()

    def stored(self):
        return json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}

    # ---- the list
    def test_the_list_is_the_services_engines_with_the_choice_read_from_voice_json(self):
        status, data = self.request("GET", "/api/voice/avatar/engines")
        self.assertEqual(status, 200)
        self.assertEqual(data["avatar"], "ada")
        self.assertEqual([e["id"] for e in data["engines"]], list(voice_settings.AVATAR_ENGINES))
        self.assertEqual(data["selection"], {"engines": voice_settings.AVATAR_DEFAULT_ENGINES, "resolution": voice_settings.AVATAR_DEFAULT_RESOLUTION, "fps_hint": 25})
        self.assertEqual(data["resolutions"]["android"], list(voice_settings.AVATAR_ENGINES["wav2lip_gan96"][1]))
        self.assertEqual(data["resolutions"]["esp32"], list(voice_settings.AVATAR_ENGINES["visemes"][1]))
        self.assertEqual(self.voice.seen, [("/avatar/engines", "Bearer " + self.token)])

    def test_a_choice_just_made_shows_at_once_not_the_services_older_copy(self):
        self.request("POST", "/api/voice/avatar/engines/select", {"target": "android", "engine": "visemes", "resolution": 144})
        _s, data = self.request("GET", "/api/voice/avatar/engines")
        self.assertEqual((data["selection"]["engines"]["android"], data["selection"]["resolution"]["android"]), ("visemes", 144))
        self.assertEqual(data["resolutions"]["android"], list(voice_settings.AVATAR_ENGINES["visemes"][1]))

    def test_a_service_that_is_down_is_said_plainly(self):
        with mock.patch.object(voice_service, "call", side_effect=voice_service.ServiceDown("the voice service is not running")):
            status, data = self.request("GET", "/api/voice/avatar/engines")
        self.assertEqual(status, 503)
        self.assertEqual(data["error"]["code"], "voice_service_down")

    def test_a_service_error_is_passed_on(self):
        self.voice.answer = (404, {"error": "no"})
        self.assertEqual(self.request("GET", "/api/voice/avatar/engines")[0], 404)

    def test_the_proxy_lists_the_route_and_the_server_never_gives_the_service_token(self):
        self.assertIn(("GET", "/avatar/engines"), voice_proxy.ROUTES)
        self.assertNotIn(self.token, json.dumps(self.request("GET", "/api/voice/avatar/engines")[1]))

    # ---- choosing
    def test_choosing_an_engine_for_a_target_is_kept_in_voice_json(self):
        status, data = self.request("POST", "/api/voice/avatar/engines/select", {"target": "android", "engine": "visemes", "resolution": 192})
        self.assertEqual(status, 200)
        self.assertEqual((data["selection"]["engines"]["android"], data["selection"]["resolution"]["android"]), ("visemes", 192))
        self.assertEqual(data["resolutions"]["android"], list(voice_settings.AVATAR_ENGINES["visemes"][1]))
        self.assertEqual(self.stored()["avatar"]["engines"]["android"], "visemes")
        _s, mine = self.request("GET", "/api/voice/avatar")
        self.assertEqual((mine["engines"]["android"], mine["resolution"]["android"], mine["fps_hint"]), ("visemes", 192, 25))

    def test_the_height_is_kept_when_the_new_engine_offers_it_else_a_default_is_taken(self):
        self.request("POST", "/api/voice/avatar/engines/select", {"target": "pc", "engine": "visemes", "resolution": 240})
        _s, data = self.request("POST", "/api/voice/avatar/engines/select", {"target": "pc", "engine": "wav2lip_gan96"})
        self.assertEqual(data["selection"]["resolution"]["pc"], 480)                       # 240 is not offered by Wav2Lip: the target's default is
        _s, data = self.request("POST", "/api/voice/avatar/engines/select", {"target": "pc", "engine": "sadtalker"})
        self.assertEqual(data["selection"]["resolution"]["pc"], 256)

    def refused(self, path, body, code):
        before = self.stored()
        status, data = self.request("POST", path, body)
        self.assertEqual(status, 400, body)
        self.assertEqual(data["error"]["code"], code, body)
        self.assertTrue(data["error"]["message"] and data["error"]["message_it"])
        self.assertEqual(self.stored(), before, "a refused request must not change the file")

    def test_an_engine_that_does_not_run_on_the_target_is_refused_and_nothing_is_written(self):
        for target, engine in (("android", "musetalk_15"), ("esp32", "wav2lip_gan96"), ("web", "sadtalker"), ("web", "er_nerf"), ("android", "er_nerf")):
            self.refused("/api/voice/avatar/engines/select", {"target": target, "engine": engine}, "engine")
        self.refused("/api/voice/avatar/engines/select", {"target": "pc", "engine": "nope"}, "engine")
        self.refused("/api/voice/avatar/engines/select", {"target": "pc", "engine": 3}, "engine")
        self.refused("/api/voice/avatar/engines/select", {"target": "moon", "engine": "visemes"}, "target")

    def test_a_height_the_engine_does_not_offer_is_refused(self):
        for height in (100, 0, -1, True, "96", 96.5, 4000):
            self.refused("/api/voice/avatar/engines/select", {"target": "android", "engine": "wav2lip_gan96", "resolution": height}, "resolution")

    def test_the_body_is_checked(self):
        for body in ({}, {"target": "pc"}, {"engine": "visemes"}, {"target": "pc", "engine": "visemes", "extra": 1}, [], "x"):
            self.refused("/api/voice/avatar/engines/select", body, "body")

    # ---- the settings
    def test_the_settings_hold_engines_resolutions_and_the_frame_rate_hint(self):
        status, data = self.request("POST", "/api/voice/avatar", {"engines": {"web": "visemes", "pc": "sadtalker"}, "resolution": {"web": 288, "pc": 384}, "fps_hint": 30})
        self.assertEqual(status, 200)
        self.assertEqual((data["engines"]["pc"], data["resolution"]["pc"], data["engines"]["web"], data["resolution"]["web"], data["fps_hint"]), ("sadtalker", 384, "visemes", 288, 30))
        self.assertEqual(self.stored()["avatar"], {"engines": data["engines"], "resolution": data["resolution"], "fps_hint": 30})

    def test_a_height_is_checked_against_the_engine_chosen_in_the_same_request(self):
        self.refused("/api/voice/avatar", {"engines": {"pc": "sadtalker"}, "resolution": {"pc": 480}}, "resolution")
        self.refused("/api/voice/avatar", {"resolution": {"android": 480 + 1}}, "resolution")
        self.refused("/api/voice/avatar", {"engines": {"android": "musetalk_15"}}, "engine")
        status, _d = self.request("POST", "/api/voice/avatar", {"engines": {"android": "visemes"}, "resolution": {"android": 144}})
        self.assertEqual(status, 200)

    def test_the_settings_refuse_what_they_do_not_know_or_that_is_out_of_range(self):
        for fps in (4, 61, "30", True, 12.5):
            self.refused("/api/voice/avatar", {"fps_hint": fps}, "fps_hint")
        self.refused("/api/voice/avatar", {"engines": "visemes"}, "engines")
        self.refused("/api/voice/avatar", {"resolution": [96]}, "resolution")
        self.refused("/api/voice/avatar", {"engines": {"moon": "visemes"}}, "target")
        self.refused("/api/voice/avatar", {"driver": "install"}, "field")

    def test_the_camera_and_the_acceleration_are_not_touched_by_a_choice_of_engine(self):
        self.request("POST", "/api/voice/avatar", {"virtual_camera": True, "enhance": {"upscale": "basic"}})
        self.request("POST", "/api/voice/avatar/engines/select", {"target": "web", "engine": "visemes"})
        stored = self.stored()["avatar"]
        self.assertEqual((stored["virtual_camera"], stored["enhance"]["upscale"]), (True, "basic"))


class Settings(unittest.TestCase):
    def test_a_damaged_stored_value_reads_as_the_default_for_that_target_only(self):
        got = voice_settings.avatar_selection({"engines": {"pc": "nope", "android": "visemes"}, "resolution": {"android": 99, "pc": 384}, "fps_hint": 500})
        self.assertEqual(got["engines"], {**voice_settings.AVATAR_DEFAULT_ENGINES, "android": "visemes"})
        self.assertEqual((got["resolution"]["android"], got["resolution"]["pc"], got["fps_hint"]), (96, 384, 25))
        self.assertEqual(voice_settings.avatar_selection("junk"), voice_settings.avatar_selection(None))

    def test_the_defaults_run_where_they_are_set(self):
        for target, engine in voice_settings.AVATAR_DEFAULT_ENGINES.items():
            runs, heights, _own = voice_settings.AVATAR_ENGINES[engine]
            self.assertIn(target, runs)
            self.assertIn(voice_settings.AVATAR_DEFAULT_RESOLUTION[target], heights)


if __name__ == "__main__":
    unittest.main()
