"""TaDaMoo's side of the voice service: the info and token files, the status of the service, the loopback route, and that the service's own table of
models and its reading of voice.json agree with serve/voice_catalog.py and serve/voice_settings.py. The service is a stand-in on a loopback port."""
from __future__ import annotations

import http.client
import http.server
import importlib.util
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from serve import settings, voice_catalog as vc, voice_service, voice_settings
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "a" * 64


class FakeService:
    """Answers GET /status with a body when the bearer token is right, 401 otherwise; records what it was asked."""

    def __init__(self, token=TOKEN, status_code=200, models_dir=""):
        self.token, self.status_code, self.models_dir, self.seen = token, status_code, models_dir, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append((self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "Bearer " + outer.token:
                    code, body = 401, {"error": "unauthorized"}
                else:
                    code, body = outer.status_code, {"service": "voice", "state": "standby", "models_dir": outer.models_dir, "services": {}}
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        p = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.data), "TADAMOO_CACHE": str(Path(self.tmp.name) / "cache")})
        p.start()
        self.addCleanup(p.stop)

    def write_info(self, **kw):
        data = {"host": "127.0.0.1", "port": 1, "pid": 1, "started": "2026-11-08T10:00:00+00:00", **kw}
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "voice-service.json").write_text(json.dumps(data), encoding="utf-8")

    def stand_in(self, **kw):
        fake = FakeService(**kw)
        self.addCleanup(fake.close)
        return fake


class TokenTests(Base):
    def test_ensure_token_makes_a_random_hex_token_once(self):
        path = voice_service.ensure_token()
        self.assertEqual(path, self.data / "voice-service.token")
        token = path.read_text(encoding="utf-8")
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        self.assertEqual(voice_service.ensure_token().read_text(encoding="utf-8"), token)

    def test_an_empty_token_file_is_made_again_but_a_token_is_never_replaced(self):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "voice-service.token").write_text("", encoding="utf-8")
        voice_service.ensure_token()
        token = (self.data / "voice-service.token").read_text(encoding="utf-8")
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        (self.data / "voice-service.token").write_text("chosen-by-the-app", encoding="utf-8")
        voice_service.ensure_token()
        self.assertEqual((self.data / "voice-service.token").read_text(encoding="utf-8"), "chosen-by-the-app")

    @unittest.skipIf(os.name == "nt", "file modes are not enforced on Windows")
    def test_the_token_file_is_for_the_owner_only(self):
        self.assertEqual(voice_service.ensure_token().stat().st_mode & 0o777, 0o600)

    def test_two_tokens_differ(self):
        first = voice_service.ensure_token().read_text(encoding="utf-8")
        voice_service.token_path().unlink()
        self.assertNotEqual(voice_service.ensure_token().read_text(encoding="utf-8"), first)


class InfoTests(Base):
    def test_no_file_and_bad_files(self):
        self.assertIsNone(voice_service.info())
        self.data.mkdir(parents=True, exist_ok=True)
        for text in ("", "{", "[1]", "null", '{"port": "7"}', '{"port": 0, "pid": 1}', '{"port": 7}', '{"port": true, "pid": 1}', '{"port": 70000, "pid": 1}'):
            (self.data / "voice-service.json").write_text(text, encoding="utf-8")
            self.assertIsNone(voice_service.info(), text)

    def test_a_good_file(self):
        self.write_info(port=7321, pid=99)
        self.assertEqual(voice_service.info(), {"host": "127.0.0.1", "port": 7321, "pid": 99, "started": "2026-11-08T10:00:00+00:00"})


class StatusTests(Base):
    def test_not_started(self):
        s = voice_service.status()
        self.assertEqual((s["running"], s["port"], s["pid"]), (False, None, None))
        self.assertIn("not started", s["status"]["reason"])

    def test_running_returns_the_services_own_status(self):
        voice_service.ensure_token()
        fake = self.stand_in(token=voice_service.token_path().read_text(encoding="utf-8"), models_dir=voice_settings.models_dir())
        self.write_info(port=fake.port, pid=4242)
        s = voice_service.status()
        self.assertTrue(s["running"])
        self.assertEqual((s["port"], s["pid"]), (fake.port, 4242))
        self.assertEqual(s["status"]["state"], "standby")
        self.assertTrue(s["models_dir_matches"])
        self.assertEqual(fake.seen[0][0], "/status")

    def test_a_different_models_folder_is_reported(self):
        voice_service.ensure_token()
        fake = self.stand_in(token=voice_service.token_path().read_text(encoding="utf-8"), models_dir=str(Path(self.tmp.name) / "elsewhere"))
        self.write_info(port=fake.port)
        self.assertFalse(voice_service.status()["models_dir_matches"])

    def test_the_wrong_token_is_a_reason_not_a_crash(self):
        voice_service.ensure_token()
        fake = self.stand_in(token="not-the-token")
        self.write_info(port=fake.port)
        s = voice_service.status()
        self.assertFalse(s["running"])
        self.assertIn("refused the token", s["status"]["reason"])

    def test_no_token_file_means_no_request(self):
        fake = self.stand_in()
        self.write_info(port=fake.port)
        s = voice_service.status()
        self.assertFalse(s["running"])
        self.assertIn("no token", s["status"]["reason"])
        self.assertEqual(fake.seen, [])

    def test_nothing_listens(self):
        voice_service.ensure_token()
        fake = self.stand_in()
        port = fake.port
        fake.close()
        self.write_info(port=port)
        s = voice_service.status()
        self.assertFalse(s["running"])
        self.assertIn(f"does not answer on port {port}", s["status"]["reason"])

    def test_a_server_error_is_reported(self):
        voice_service.ensure_token()
        fake = self.stand_in(token=voice_service.token_path().read_text(encoding="utf-8"), status_code=500)
        self.write_info(port=fake.port)
        self.assertIn("answered 500", voice_service.status()["status"]["reason"])

    def test_a_host_that_is_not_this_pc_is_never_called(self):
        voice_service.ensure_token()
        self.write_info(host="192.0.2.7", port=7000)
        with mock.patch.object(voice_service, "_get", side_effect=AssertionError("called")):
            s = voice_service.status()
        self.assertFalse(s["running"])
        self.assertIn("not this PC", s["status"]["reason"])

    def test_the_token_is_in_no_result(self):
        voice_service.ensure_token()
        token = voice_service.token_path().read_text(encoding="utf-8")
        for fake in (self.stand_in(token=token), self.stand_in(token="other")):
            self.write_info(port=fake.port)
            self.assertNotIn(token, json.dumps(voice_service.status()))


class RouteTests(Base):
    def setUp(self):
        super().setUp()
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.settings_info = {"config": None, "model": None, "cli": [], "running": {}}
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            c.request("GET", path)
            r = c.getresponse()
            raw = r.read()
            return r.status, raw, json.loads(raw or b"null")
        finally:
            c.close()

    def test_not_started(self):
        code, _, body = self.get("/api/voice/service")
        self.assertEqual(code, 200)
        self.assertFalse(body["running"])
        self.assertTrue((self.data / "voice-service.token").is_file())     # made at first use

    def test_running_and_the_token_never_leaves(self):
        voice_service.ensure_token()
        token = voice_service.token_path().read_text(encoding="utf-8")
        fake = self.stand_in(token=token, models_dir=voice_settings.models_dir())
        self.write_info(port=fake.port, pid=7)
        code, raw, body = self.get("/api/voice/service")
        self.assertEqual(code, 200)
        self.assertTrue(body["running"])
        self.assertEqual((body["port"], body["pid"]), (fake.port, 7))
        self.assertNotIn(token.encode(), raw)

    def test_only_on_this_pc(self):
        with mock.patch.object(settings, "is_loopback", return_value=False):
            code, _, body = self.get("/api/voice/service")
        self.assertEqual(code, 403)
        self.assertEqual(body["error"]["type"], "forbidden")
        self.assertFalse((self.data / "voice-service.token").exists())

    def test_the_proxy_route_answers_post_only(self):
        self.assertEqual(self.get("/api/voice/service/call?path=/status")[0], 404)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AgreementTests(Base):
    """The service has its own interpreter and cannot import `serve`: its copies must not drift."""

    def setUp(self):
        super().setUp()
        self.ms = _load("models_store_copy", ROOT / "voice" / "callbridge" / "models_store.py")

    def test_the_table_of_models_is_the_catalog(self):
        self.assertEqual(self.ms.SERVICES, vc.SERVICES)
        self.assertEqual(self.ms.DEFAULTS, vc.DEFAULTS)
        self.assertEqual(self.ms.MARKER, vc.MARKER)
        self.assertEqual(set(self.ms.MODELS), set(vc.BY_ID))
        for model_id, (service, engine, name, probes) in self.ms.MODELS.items():
            e = vc.BY_ID[model_id]
            self.assertEqual((service, engine, tuple(e["probe"])), (e["service"], e["engine"], tuple(probes)), model_id)
            self.assertTrue(e["name"].startswith(name.split(" (")[0]) or name.startswith(e["name"].split(" (")[0]), (name, e["name"]))

    def test_the_folder_of_a_model_is_where_the_catalog_puts_it(self):
        for model_id, e in vc.BY_ID.items():
            self.assertEqual(self.ms.folder(e["service"], model_id, "/r").replace("\\", "/"), "/r/" + vc.folder_of(e))

    def test_installed_means_the_same_as_in_the_catalog(self):
        root = Path(self.tmp.name) / "models"
        for e in vc.CATALOG:
            self.assertEqual(self.ms.is_installed(e["id"], str(root)), vc.installed(e, str(root)), e["id"])
        for e in vc.CATALOG:
            folder = root / e["service"] / e["id"]
            folder.mkdir(parents=True, exist_ok=True)
            for probe in e["probe"]:
                (folder / probe).parent.mkdir(parents=True, exist_ok=True)
                (folder / probe).write_bytes(b"x")
            self.assertEqual(self.ms.is_installed(e["id"], str(root)), vc.installed(e, str(root)), e["id"])
            marker = {"files": [{"name": "a.bin", "size": 3}]}
            (folder / vc.MARKER).write_text(json.dumps(marker), encoding="utf-8")
            self.assertEqual((self.ms.is_installed(e["id"], str(root)), vc.installed(e, str(root))), (False, False), e["id"])
            (folder / "a.bin").write_bytes(b"abc")
            self.assertEqual((self.ms.is_installed(e["id"], str(root)), vc.installed(e, str(root))), (True, True), e["id"])

    def test_the_service_reads_what_settings_voice_wrote(self):
        from voice import settings_bridge as sb
        folder = Path(self.tmp.name) / "chosen"
        voice_settings.set_models_dir(str(folder))
        voice_settings.select("stt", "whisper-medium")
        voice_settings.set_placement("stt", "cuda:1", "vram")
        voice_settings.select("tts", "piper")
        voice_settings.set_placement("tts", "cpu", "ram")
        voice_settings.set_placement("vad", "cpu", "ram")
        loaded = sb.load(self.data / "voice.json")
        self.assertEqual(loaded.models_dir, voice_settings.models_dir())
        for service in vc.SERVICES:
            c = loaded.services[service]
            self.assertEqual((c.model, {"device": c.device, "memory": c.memory}), (voice_settings.selected(service), voice_settings.placement(service)), service)
        self.assertTrue(re.fullmatch(r"cuda:1", loaded.services["stt"].device))


if __name__ == "__main__":
    unittest.main()
