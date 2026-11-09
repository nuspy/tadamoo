"""The avatar files a phone or the ESP32 downloads (the zip, the manifest, the Wav2Lip model) and the sprite sheet of the creation page, through
TaDaMoo's server: one address, the phone's key, the service token added here and never shown, ranges and ETags passed through. The voice service
is a stand-in on loopback."""
from __future__ import annotations

import hashlib
import http.client
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from serve import handler, voice_service
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.plans import PlanService
from serve.remote import ApiKeys, RemoteAccess
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
MODEL = bytes(range(256)) * 64
DIGEST = hashlib.sha256(MODEL).hexdigest()
ZIP = b"PK\x03\x04fake-zip"
MANIFEST = {"version": 1, "avatar": "ada", "created": "", "render_modes": ["wav2lip", "visemes"], "files": {}}


class FakeVoiceService:
    def __init__(self, token):
        outer = self
        self.token, self.seen, self.model_installed = token, [], True

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, code, body, ctype, extra=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Range"), self.headers.get("If-None-Match")))
                if self.headers.get("Authorization") != "Bearer " + outer.token:
                    return self.reply(401, b'{"error": "unauthorized"}', "application/json")
                if self.path == "/avatar/ada/assets.zip":
                    return self.reply(200, ZIP, "application/zip", [("Content-Disposition", "attachment; filename=ada-avatar.zip")])
                if self.path == "/avatar/ada/manifest.json":
                    return self.reply(200, json.dumps(MANIFEST).encode(), "application/json")
                if self.path == "/avatar/ada/sprites.png":
                    return self.reply(200, b"\x89PNGsheet", "image/png")
                if self.path == "/avatar/models/wav2lip.onnx":
                    if not outer.model_installed:
                        return self.reply(404, b'{"error": "Wav2Lip INT8 is not installed", "code": "model_not_installed"}', "application/json")
                    common = [("ETag", f'"{DIGEST}"'), ("Accept-Ranges", "bytes"), ("Cache-Control", "private, no-cache"), ("X-Content-SHA256", DIGEST)]
                    if self.headers.get("If-None-Match") == f'"{DIGEST}"':
                        self.send_response(304)
                        self.send_header("ETag", f'"{DIGEST}"')
                        self.end_headers()
                        return
                    rng = self.headers.get("Range", "")
                    if rng.startswith("bytes="):
                        a, b = rng[6:].split("-")
                        a, b = int(a), int(b or len(MODEL) - 1)
                        return self.reply(206, MODEL[a:b + 1], "application/octet-stream", common + [("Content-Range", f"bytes {a}-{b}/{len(MODEL)}")])
                    return self.reply(200, MODEL, "application/octet-stream", common)
                return self.reply(404, b'{"error": "no"}', "application/json")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class DeviceFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.env = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(base / "data"), "TADAMOO_CACHE": str(base / "cache")})
        cls.env.start()
        cls.svc.keys = ApiKeys(base / "keys.json", key=bytes(32))
        cls.svc.remote = RemoteAccess(base / "remote.json", 0, lambda ip, port: None)
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner"))]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.phone = handler._listen(cls.svc, "127.0.0.1", 0, remote=True)                  # stands for the listener on the Tailscale address
        cls.key = cls.svc.keys.create("Phone")[1]
        cls.token = voice_service.ensure_token().read_text(encoding="utf-8").strip()
        cls.voice = FakeVoiceService(cls.token)
        (base / "data").mkdir(parents=True, exist_ok=True)
        (base / "data" / "voice-service.json").write_text(json.dumps({"host": "127.0.0.1", "port": cls.voice.port, "pid": 1, "started": "x"}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        for s in (cls.httpd, cls.phone):
            s.shutdown()
            s.server_close()
        cls.voice.close()
        for p in cls.patches:
            p.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.svc.remote._httpd = None
        self.svc.api_key = ""
        self.voice.seen.clear()
        self.voice.model_installed = True

    def listen(self):
        self.svc.remote._httpd = object()                                   # as if the listener on the Tailscale address were up

    def fetch(self, path, outside=False, key=None, headers=None):
        port = (self.phone if outside else self.httpd).server_address[1]
        h = dict(headers or {})
        if key:
            h["Authorization"] = "Bearer " + key
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers=h)
            r = conn.getresponse()
            return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
        finally:
            conn.close()

    def test_a_phone_with_its_key_downloads_the_zip_and_the_manifest(self):
        self.listen()
        status, headers, body = self.fetch("/api/voice/avatar/ada/assets.zip", outside=True, key=self.key)
        self.assertEqual((status, body, headers["content-type"]), (200, ZIP, "application/zip"))
        self.assertIn("ada-avatar.zip", headers["content-disposition"])
        status, headers, body = self.fetch("/api/voice/avatar/ada/manifest.json", outside=True, key=self.key)
        self.assertEqual((status, json.loads(body)), (200, MANIFEST))
        self.assertEqual([s[:2] for s in self.voice.seen], [("/avatar/ada/assets.zip", "Bearer " + self.token), ("/avatar/ada/manifest.json", "Bearer " + self.token)])
        self.assertNotIn(self.token.encode(), body)

    def test_the_model_passes_through_whole_in_ranges_and_as_not_modified_with_its_etag(self):
        self.listen()
        status, headers, body = self.fetch("/api/voice/avatar/models/wav2lip.onnx", outside=True, key=self.key)
        self.assertEqual((status, body, headers["etag"], headers["x-content-sha256"]), (200, MODEL, f'"{DIGEST}"', DIGEST))
        self.assertEqual((headers["accept-ranges"], headers["cache-control"]), ("bytes", "private, no-cache"))
        status, headers, body = self.fetch("/api/voice/avatar/models/wav2lip.onnx", outside=True, key=self.key, headers={"Range": "bytes=100-199"})
        self.assertEqual((status, body, headers["content-range"]), (206, MODEL[100:200], f"bytes 100-199/{len(MODEL)}"))
        status, _h, body = self.fetch("/api/voice/avatar/models/wav2lip.onnx", outside=True, key=self.key, headers={"If-None-Match": f'"{DIGEST}"'})
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(self.voice.seen[1][2], "bytes=100-199")                 # the range reached the service

    def test_a_model_that_is_not_installed_is_a_404_with_the_reason_of_the_service(self):
        self.voice.model_installed = False
        self.listen()
        status, _h, body = self.fetch("/api/voice/avatar/models/wav2lip.onnx", outside=True, key=self.key)
        self.assertEqual((status, json.loads(body)["code"]), (404, "model_not_installed"))

    def test_without_a_key_or_with_phones_off_nothing_is_given_and_the_service_is_not_asked(self):
        for path in ("/api/voice/avatar/ada/assets.zip", "/api/voice/avatar/models/wav2lip.onnx"):
            self.assertEqual(self.fetch(path, outside=True, key=self.key)[0], 403)            # phones are turned off
        self.listen()
        for key in (None, "bsk_wrong", self.token):
            self.assertEqual(self.fetch("/api/voice/avatar/ada/manifest.json", outside=True, key=key)[0], 401, key)
        self.assertEqual(self.voice.seen, [])

    def test_this_pc_gets_the_files_too_and_the_phone_gets_no_other_avatar_route(self):
        self.assertEqual(self.fetch("/api/voice/avatar/ada/manifest.json")[0], 200)
        self.listen()
        for path in ("/api/voice/avatar", "/api/voice/avatar/ada/poster", "/api/voice/avatar/ada/sprites.png", "/api/voice/avatar/preview.mjpg"):
            self.assertEqual(self.fetch(path, outside=True, key=self.key)[0], 403, path)
        self.assertEqual([s[0] for s in self.voice.seen], ["/avatar/ada/manifest.json"])

    def test_only_plain_names_are_relayed(self):
        for path in ("/api/voice/avatar/a%2Fb/assets.zip", "/api/voice/avatar/../models/wav2lip.onnx", "/api/voice/avatar/models/other.onnx"):
            self.assertNotEqual(self.fetch(path)[0], 200, path)
        self.assertEqual([s for s in self.voice.seen if "models/other" in s[0] or "a%2F" in s[0]], [])

    def test_the_sprite_sheet_is_for_this_pcs_page(self):
        status, headers, body = self.fetch("/api/voice/avatar/ada/sprites.png")
        self.assertEqual((status, headers["content-type"], body[:4]), (200, "image/png", b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
