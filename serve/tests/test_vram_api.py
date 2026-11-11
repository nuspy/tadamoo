"""The routes of the VRAM broker, on a real server with a stand-in engine; the voice service's token opens the lease routes and no others."""
import http.client
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from serve import settings, vram_api, voice_service  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.tests.test_vram_broker import FakeService  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402
from serve.vram_broker import BrokerPrefs, VramBroker  # noqa: E402


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(base / "data"), "TADAMOO_CACHE": str(base / "cache")})
        env.start()
        self.addCleanup(env.stop)
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        self.engine = FakeService()
        self.svc.vram_broker = VramBroker(self.engine, probe=lambda: self.engine.free, start_reserve=lambda: 1500,
                                          prefs=BrokerPrefs(base / "b.json"), timer=False)
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


class LeaseRoutes(Server):
    def test_a_lease_is_taken_touched_shown_and_released(self):
        code, out = self.req("POST", "/api/vram/lease", {"owner": "docling", "mib": 3000, "purpose": "pdf", "hold_s": 30})
        self.assertEqual((code, out["ok"], out["lease"]["granted_mib"]), (200, True, 3000))
        lid = out["lease"]["id"]
        code, state = self.req("GET", "/api/vram/leases")
        self.assertEqual((code, state["leases"][0]["id"], state["lent_mib"], state["engine"]), (200, lid, 3000, "running"))
        self.assertEqual(self.req("POST", f"/api/vram/lease/{lid}/touch")[0], 200)
        self.assertEqual(self.req("POST", f"/api/vram/lease/{lid}/release")[1], {"ok": True, "released": lid})
        self.assertEqual(self.req("GET", "/api/vram/leases")[1]["leases"], [])
        self.assertEqual(self.engine.calls, [4500, None])

    def test_a_refusal_is_a_409_with_its_code_in_both_languages(self):
        self.req("POST", "/api/vram/lease", {"owner": "a", "mib": 3000})
        code, out = self.req("POST", "/api/vram/lease", {"owner": "b", "mib": 100})
        self.assertEqual((code, out["ok"], out["reason"]), (409, False, "busy"))
        self.assertTrue(out["message"] and out["message_it"])
        self.assertEqual(self.req("POST", "/api/vram/lease", {"owner": "", "mib": 100})[0], 400)
        self.assertEqual(self.req("POST", "/api/vram/lease/0123456789abcdef/release")[0], 404)
        self.assertEqual(self.req("POST", "/api/vram/lease/nothex/release")[0], 404)

    def test_an_engine_that_cannot_lend_is_unavailable(self):
        del self.svc.vram_broker
        code, out = self.req("POST", "/api/vram/lease", {"owner": "a", "mib": 900000})
        self.assertEqual((code, out["reason"]), (409, "unavailable"))

    def test_settings_are_read_and_changed(self):
        self.assertEqual(self.req("GET", "/api/vram/settings")[1], {"enabled": True, "hold_s": 60})
        self.assertEqual(self.req("POST", "/api/vram/settings", {"hold_s": 120}), (200, {"enabled": True, "hold_s": 120}))
        self.assertEqual(self.req("POST", "/api/vram/settings", {"hold_s": 2})[0], 400)
        vram_api.prefs().write({"enabled": True, "hold_s": 60})

    def test_only_this_pc(self):
        with mock.patch.object(settings, "is_loopback", return_value=False):
            self.assertEqual(self.req("GET", "/api/vram/leases")[0], 403)
            self.assertEqual(self.req("POST", "/api/vram/lease", {"owner": "a", "mib": 10})[0], 403)
        self.assertEqual(self.req("POST", "/api/vram/lease", {"owner": "a", "mib": 10}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/vram/leases")[1]["leases"], [])


class VoiceToken(Server):
    def test_the_voice_service_token_opens_the_lease_routes_and_no_others(self):
        tok = self.voice["Authorization"][7:]
        for method, path in (("POST", "/api/vram/lease"), ("POST", "/api/vram/lease/0123456789abcdef/touch"), ("POST", "/api/vram/lease/0123456789abcdef/release")):
            self.assertTrue(voice_service.accepts_token(tok, method, path, "127.0.0.1"), path)
        for method, path in (("GET", "/api/vram/leases"), ("POST", "/api/vram/settings"), ("POST", "/api/vram/lease/x/release"), ("GET", "/api/vram/lease")):
            self.assertFalse(voice_service.accepts_token(tok, method, path, "127.0.0.1"), path)
        self.assertFalse(voice_service.accepts_token(tok, "POST", "/api/vram/lease", "192.168.1.5"))

    def test_the_voice_service_takes_a_lease_with_its_token(self):
        code, out = self.req("POST", "/api/vram/lease", {"owner": "voice-avatar", "mib": 3000}, self.voice)
        self.assertEqual((code, out["ok"]), (200, True))


if __name__ == "__main__":
    unittest.main()
