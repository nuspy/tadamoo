"""The avatar requests the JSON proxy cannot carry: the settings of the camera output, the wizard's files passed through to the service without being
read, the MJPEG preview relayed, a portrait. The voice service is a stand-in on loopback that records what it is sent; the token is the one
the server keeps, and the browser never sees it."""
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

from serve import handler, voice_avatar, voice_service
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.plans import PlanService
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
DRAFT = "a" * 32
BOUNDARY = "----testboundary"


def multipart(payload: bytes, name="x.jpg") -> bytes:
    return (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + payload \
        + f"\r\n--{BOUNDARY}--\r\n".encode()


class FakeService:
    def __init__(self, token):
        outer = self
        self.token, self.seen, self.bodies, self.answer, self.stream_parts = token, [], [], (200, {"ok": True, "file": "foto.jpg"}), [b"--f\r\n", b"frame1", b"frame2"]

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _check(self):
                outer.seen.append((self.command, self.path, self.headers.get("Authorization"), self.headers.get("Content-Type")))
                if self.headers.get("Authorization") != "Bearer " + outer.token:
                    self._reply(401, {"error": "unauthorized"})
                    return False
                return True

            def _reply(self, code, body, ctype="application/json"):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length)
                if self._check():
                    outer.bodies.append(data)
                    self._reply(*outer.answer)

            def do_GET(self):
                if not self._check():
                    return
                if self.path == "/avatar/preview.mjpg":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=avatarframe")
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    self.end_headers()
                    for part in outer.stream_parts:
                        self.wfile.write(part)
                        self.wfile.flush()
                elif self.path == "/personas/ada/poster":
                    self._reply(200, b"\xff\xd8jpeg\xff\xd9", "image/jpeg")
                elif self.path == "/personas/odd/poster":
                    self._reply(200, b"<html>", "text/html")
                else:
                    self._reply(404, {"error": "no"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class AvatarRelay(unittest.TestCase):
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
        cls.voice = FakeService(cls.token)
        (base / "data").mkdir(parents=True, exist_ok=True)
        (base / "data" / "voice-service.json").write_text(json.dumps({"host": "127.0.0.1", "port": cls.voice.port, "pid": 1, "started": "x"}), encoding="utf-8")

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
        self.voice.bodies.clear()
        self.voice.answer = (200, {"ok": True, "file": "foto.jpg"})

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            h = {"Content-Type": "application/json", **(headers or {})} if raw is None and body is not None else dict(headers or {})
            conn.request(method, path, body=payload, headers=h)
            r = conn.getresponse()
            data = r.read()
            return r.status, r.getheader("Content-Type"), data
        finally:
            conn.close()

    def upload(self, step, payload, draft=DRAFT, ctype=f"multipart/form-data; boundary={BOUNDARY}"):
        return self.request("POST", f"/api/voice/avatar/wizard/{draft}/{step}", raw=multipart(payload), headers={"Content-Type": ctype})

    # ---- settings
    def test_the_camera_setting_is_off_by_default_and_is_kept_in_voice_json(self):
        status, _t, data = self.request("GET", "/api/voice/avatar")
        self.assertEqual((status, json.loads(data)), (200, {"virtual_camera": False}))
        status, _t, data = self.request("POST", "/api/voice/avatar", {"virtual_camera": True})
        self.assertEqual((status, json.loads(data)), (200, {"virtual_camera": True}))
        stored = json.loads((Path(self.tmp.name) / "data" / "voice.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["avatar"], {"virtual_camera": True})
        self.request("POST", "/api/voice/avatar", {"virtual_camera": False})
        self.assertEqual(json.loads(self.request("GET", "/api/voice/avatar")[2]), {"virtual_camera": False})

    def test_the_camera_setting_refuses_what_it_does_not_know(self):
        for body in ({"virtual_camera": "yes"}, {"virtual_camera": 1}, {"driver": "install"}, []):
            status, _t, data = self.request("POST", "/api/voice/avatar", body)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.request("POST", "/api/voice/avatar", raw=b"{nope", headers={"Content-Type": "application/json"})[0], 400)

    # ---- the wizard's files
    def test_a_file_passes_through_to_the_service_unchanged_with_the_token_added(self):
        status, _t, data = self.upload("foto", b"\xff\xd8photo-bytes\xff\xd9")
        self.assertEqual((status, json.loads(data)), (200, {"ok": True, "file": "foto.jpg"}))
        method, path, auth, ctype = self.voice.seen[-1]
        self.assertEqual((method, path, auth), ("POST", f"/avatar/wizard/{DRAFT}/foto", "Bearer " + self.token))
        self.assertEqual(ctype, f"multipart/form-data; boundary={BOUNDARY}")
        self.assertEqual(self.voice.bodies[-1], multipart(b"\xff\xd8photo-bytes\xff\xd9"))
        self.assertNotIn(self.token.encode(), data)

    def test_the_answer_of_the_service_is_the_answer_whatever_its_status(self):
        self.voice.answer = (400, {"error": "that file is not a photo that can be read", "code": "bad_request"})
        status, _t, data = self.upload("foto", b"junk")
        self.assertEqual((status, json.loads(data)["code"]), (400, "bad_request"))

    def test_only_the_four_steps_and_a_plain_draft_id_are_relayed(self):
        for path in (f"/api/voice/avatar/wizard/{DRAFT}/elabora", f"/api/voice/avatar/wizard/{DRAFT}/../x", f"/api/voice/avatar/wizard/{'A' * 32}/foto",
                     "/api/voice/avatar/wizard/../foto", f"/api/voice/avatar/wizard/{DRAFT}/foto/extra", "/api/voice/avatar/anything"):
            status, _t, _d = self.request("POST", path, raw=multipart(b"x"), headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"})
            self.assertEqual(status, 404, path)
        self.assertEqual(self.voice.seen, [])

    def test_a_file_over_the_size_of_its_step_is_refused_before_it_is_read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", f"/api/voice/avatar/wizard/{DRAFT}/foto")
            conn.putheader("Content-Type", f"multipart/form-data; boundary={BOUNDARY}")
            conn.putheader("Content-Length", str(voice_avatar.LIMITS["foto"] + (2 << 20)))
            conn.endheaders()
            r = conn.getresponse()
            self.assertEqual((r.status, json.loads(r.read())["error"]["code"]), (413, "too_big"))
        finally:
            conn.close()
        self.assertEqual(self.voice.seen, [])
        self.assertEqual(voice_avatar.LIMITS, {"foto": 20 << 20, "pose": 20 << 20, "video": 500 << 20, "voce": 25 << 20})

    def test_a_body_that_is_not_multipart_or_has_no_length_is_refused(self):
        self.assertEqual(self.upload("foto", b"x", ctype="application/json")[0], 400)
        self.assertEqual(self.upload("foto", b"x", ctype="multipart/form-data")[0], 400)              # no boundary
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", f"/api/voice/avatar/wizard/{DRAFT}/foto")
            conn.putheader("Content-Type", f"multipart/form-data; boundary={BOUNDARY}")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(b"5\r\nhello\r\n0\r\n\r\n")
            self.assertEqual(conn.getresponse().status, 411)
        finally:
            conn.close()
        self.assertEqual(self.voice.seen, [])

    def test_a_service_that_is_not_there_is_a_503_and_the_body_is_not_kept(self):
        info = Path(self.tmp.name) / "data" / "voice-service.json"
        saved = info.read_text(encoding="utf-8")
        info.unlink()
        try:
            status, _t, data = self.upload("foto", b"x")
            self.assertEqual((status, json.loads(data)["error"]["code"]), (503, "voice_service_down"))
        finally:
            info.write_text(saved, encoding="utf-8")

    # ---- the preview and the portrait
    def test_the_preview_stream_is_relayed_as_it_comes(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/api/voice/avatar/preview.mjpg")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.getheader("Content-Type"), "multipart/x-mixed-replace; boundary=avatarframe")
            self.assertIn("no-store", r.getheader("Cache-Control"))
            self.assertEqual(r.read(), b"--f\r\nframe1frame2")
        finally:
            conn.close()
        self.assertEqual(self.voice.seen[-1][:3], ("GET", "/avatar/preview.mjpg", "Bearer " + self.token))

    def test_the_preview_says_so_when_the_service_is_not_there(self):
        info = Path(self.tmp.name) / "data" / "voice-service.json"
        saved = info.read_text(encoding="utf-8")
        info.unlink()
        try:
            status, _t, data = self.request("GET", "/api/voice/avatar/preview.mjpg")
            self.assertEqual((status, json.loads(data)["error"]["code"]), (503, "voice_service_down"))
        finally:
            info.write_text(saved, encoding="utf-8")

    def test_a_portrait_is_relayed_only_as_a_jpeg_of_a_plain_name(self):
        status, ctype, data = self.request("GET", "/api/voice/avatar/ada/poster")
        self.assertEqual((status, ctype, data), (200, "image/jpeg", b"\xff\xd8jpeg\xff\xd9"))
        self.assertEqual(self.request("GET", "/api/voice/avatar/odd/poster")[0], 404)                 # the service sent something that is not an image
        self.assertEqual(self.request("GET", "/api/voice/avatar/nobody/poster")[0], 404)
        for bad in ("../x", "a%2Fb", ".hidden", "a%20b"):
            self.assertEqual(self.request("GET", f"/api/voice/avatar/{bad}/poster")[0], 404, bad)

    # ---- who may
    def test_with_an_api_key_set_the_requests_need_it(self):
        self.svc.api_key = "sekret"
        self.assertEqual(self.request("GET", "/api/voice/avatar")[0], 401)
        self.assertEqual(self.upload("foto", b"x")[0], 401)
        self.assertEqual(self.request("GET", "/api/voice/avatar", headers={"Authorization": "Bearer sekret"})[0], 200)
        self.assertEqual(self.voice.seen, [])

    def test_the_service_is_reached_only_on_loopback(self):
        source = Path(voice_avatar.__file__).read_text(encoding="utf-8")
        self.assertIn("voice_service.endpoint()", source)
        self.assertNotIn("HTTPSConnection", source)


if __name__ == "__main__":
    unittest.main()
