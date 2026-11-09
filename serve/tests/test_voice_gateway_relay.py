"""The phone's way to the voice gateway: the key is checked by the server, the WebSocket is passed to the gateway with the service token, and the
phone never sees the token or the gateway's port. The voice service and the gateway are stand-ins on loopback."""
from __future__ import annotations

import base64
import http.client
import http.server
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from websockets.sync.server import serve as ws_serve

from serve import handler, voice_gateway_relay, voice_service
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.plans import PlanService
from serve.remote import ApiKeys, RemoteAccess
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


class FakeVoiceService:
    """GET /gateway with the right bearer token: the port of the gateway stand-in; 401 otherwise."""

    def __init__(self, token, gateway_port):
        outer = self
        self.token, self.gateway_port, self.seen = token, gateway_port, []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append(self.path)
                if self.headers.get("Authorization") != "Bearer " + outer.token:
                    code, body = 401, {"error": "unauthorized"}
                elif self.path == "/gateway":
                    code, body = 200, {"host": "127.0.0.1", "port": outer.gateway_port, "voice": True, "stt": True, "tts": True, "avatar": False,
                                       "render_modes": ["wav2lip", "visemes", "video"], "brain": "direct", "persona": ""}
                else:
                    code, body = 404, {}
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


class Relay(unittest.TestCase):
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
        cls.received = []                                                                  # what the gateway stand-in saw of each handshake

        def process_request(connection, request):
            cls.received.append(request.headers.get("Authorization", ""))
            if request.headers.get("Authorization") != "Bearer " + cls.token:
                return connection.respond(401, "unauthorized\n")
            return None

        def echo(ws):
            for message in ws:
                ws.send(("echo:" + message) if isinstance(message, str) else message[::-1])

        cls.gateway = ws_serve(echo, "127.0.0.1", 0, process_request=process_request)
        cls.gateway_port = cls.gateway.socket.getsockname()[1]
        threading.Thread(target=cls.gateway.serve_forever, daemon=True).start()
        cls.voice = FakeVoiceService(cls.token, cls.gateway_port)
        (base / "data").mkdir(parents=True, exist_ok=True)
        (base / "data" / "voice-service.json").write_text(json.dumps({"host": "127.0.0.1", "port": cls.voice.port, "pid": 1, "started": "2026-11-09T10:00:00+00:00"}),
                                                          encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        for s in (cls.httpd, cls.phone, cls.gateway):
            s.shutdown()
        cls.httpd.server_close()
        cls.phone.server_close()
        cls.voice.close()
        for p in cls.patches:
            p.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.svc.remote._httpd = None
        self.svc.api_key = ""
        self.received.clear()

    def listen(self):
        self.svc.remote._httpd = object()                                   # as if the listener on the Tailscale address were up

    def url(self, outside=False, path="/api/voice/gateway/ws"):
        return f"ws://127.0.0.1:{(self.phone if outside else self.httpd).server_address[1]}{path}"

    def upgrade(self, outside=False, key=None, path="/api/voice/gateway/ws"):
        """The status of the answer to a WebSocket upgrade request (the server answers refusals as HTTP/1.0, which a WebSocket client library will not parse)."""
        port = (self.phone if outside else self.httpd).server_address[1]
        headers = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode()}
        if key:
            headers["Authorization"] = "Bearer " + key
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers=headers)
            return conn.getresponse().status
        finally:
            conn.close()

    def get(self, path, key=None, outside=False):
        base = f"http://127.0.0.1:{(self.phone if outside else self.httpd).server_address[1]}"
        headers = {"Authorization": "Bearer " + key} if key else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers), timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (json.loads(body) if body[:1] == b"{" else body)

    def test_a_phone_with_its_key_talks_to_the_gateway_and_never_sees_the_token(self):
        self.listen()
        with connect(self.url(outside=True), additional_headers={"Authorization": "Bearer " + self.key}, open_timeout=5) as ws:
            ws.send("hello")
            self.assertEqual(ws.recv(timeout=5), "echo:hello")
            ws.send(b"\x01\x02\x03")
            self.assertEqual(ws.recv(timeout=5), b"\x03\x02\x01")
            for _ in range(20):                                              # a long conversation: many frames, both ways
                ws.send("again")
                self.assertEqual(ws.recv(timeout=5), "echo:again")
        self.assertEqual(self.received, ["Bearer " + self.token])           # the gateway was reached with the service token, which the phone does not have
        self.assertNotIn(self.token, self.key)

    def test_without_a_key_or_with_a_wrong_one_the_phone_does_not_reach_the_gateway(self):
        self.listen()
        for key in (None, "bsk_wrong", self.token):
            self.assertEqual(self.upgrade(outside=True, key=key), 401, key)
        self.assertEqual(self.received, [])

    def test_a_phone_is_refused_while_phones_are_turned_off(self):
        self.assertEqual(self.upgrade(outside=True, key=self.key), 403)
        self.assertEqual(self.received, [])

    def test_this_pc_reaches_the_gateway_through_the_server_too_and_a_set_master_key_is_still_needed(self):
        with connect(self.url(), open_timeout=5) as ws:
            ws.send("pc")
            self.assertEqual(ws.recv(timeout=5), "echo:pc")
        self.svc.api_key = "master"
        self.assertEqual(self.upgrade(), 401)
        with connect(self.url(), additional_headers={"Authorization": "Bearer master"}, open_timeout=5) as ws:
            ws.send("pc")
            self.assertEqual(ws.recv(timeout=5), "echo:pc")

    def test_a_plain_request_to_the_websocket_route_is_not_relayed(self):
        code, body = self.get("/api/voice/gateway/ws")
        self.assertEqual(code, 400)
        self.assertEqual(self.received, [])

    def test_the_app_reads_what_the_pc_can_do_with_its_key_and_gets_no_port_or_token(self):
        self.listen()
        code, body = self.get("/api/voice/gateway", key=self.key, outside=True)
        self.assertEqual(code, 200)
        self.assertEqual(body["available"], True)
        self.assertEqual(body["path"], "/api/voice/gateway/ws")
        self.assertEqual((body["voice"], body["avatar"], body["brain"]), (True, False, "direct"))
        self.assertEqual(body["render_modes"], ["wav2lip", "visemes", "video"])
        text = json.dumps(body)
        self.assertNotIn(str(self.gateway_port), text)
        self.assertNotIn(self.token, text)
        self.assertEqual(self.get("/api/voice/gateway", outside=True)[0], 401)
        self.assertEqual(self.get("/api/voice/gateway", key="bsk_wrong", outside=True)[0], 401)

    def test_a_service_that_is_not_there_is_said_plainly(self):
        self.listen()
        info = self.tmp.name
        path = Path(info) / "data" / "voice-service.json"
        saved = path.read_text(encoding="utf-8")
        path.unlink()
        try:
            code, body = self.get("/api/voice/gateway", key=self.key, outside=True)
            self.assertEqual((code, body["available"]), (200, False))
            self.assertIn("voice service", body["reason"])
            self.assertEqual(self.upgrade(outside=True, key=self.key), 503)
        finally:
            path.write_text(saved, encoding="utf-8")

    def test_a_whole_voice_turn_goes_from_the_phone_through_the_server_to_the_real_gateway(self):
        from voice import gateway as voice_gateway
        from voice.tests.test_gateway import FakeHost
        host = FakeHost()
        host.token = self.token
        gw = voice_gateway.Gateway(host)
        saved = self.voice.gateway_port
        self.voice.gateway_port = gw.start()
        self.listen()
        try:
            with connect(self.url(outside=True), additional_headers={"Authorization": "Bearer " + self.key}, open_timeout=5) as ws:
                ws.send(json.dumps({"type": "auth", "token": self.key}))              # the phone's own key: the server has already checked it
                self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "auth_ok")
                self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "ready")
                ws.send(json.dumps({"type": "text", "text": "hello there"}))
                kinds = []
                while not kinds or kinds[-1] != "done":
                    m = ws.recv(timeout=5)
                    kinds.append("<pcm>" if isinstance(m, bytes) else json.loads(m)["type"])
            self.assertIn("<pcm>", kinds)
            self.assertEqual(host.brains.heard, [("hello there", "remote")])
        finally:
            gw.stop()
            self.voice.gateway_port = saved

    def test_a_key_taken_away_ends_the_conversation_in_progress(self):
        self.listen()
        row, key = self.svc.keys.create("Lost phone")
        with mock.patch.object(voice_gateway_relay, "RECHECK_S", 0.2):
            with connect(self.url(outside=True), additional_headers={"Authorization": "Bearer " + key}, open_timeout=5) as ws:
                ws.send("hello")
                self.assertEqual(ws.recv(timeout=5), "echo:hello")
                self.svc.keys.revoke(row["id"])
                with self.assertRaises(ConnectionClosed):
                    ws.recv(timeout=5)
        self.assertEqual(self.upgrade(outside=True, key=key), 401)

    def test_the_number_of_phones_at_once_is_limited(self):
        self.listen()
        held = []
        try:
            for _ in range(voice_gateway_relay.MAX_CLIENTS):
                held.append(connect(self.url(outside=True), additional_headers={"Authorization": "Bearer " + self.key}, open_timeout=5))
            self.assertEqual(self.upgrade(outside=True, key=self.key), 503)
        finally:
            for ws in held:
                ws.close()


if __name__ == "__main__":
    unittest.main()
