"""A call placed from the phone app (serve/voice_phone_call.py): its origin follows from the channel, never from a header. Only the listener for phones with a
key, with the person's switch on, reaches the voice service's call-by-hand route; a program on this PC cannot claim it. The voice service is a stand-in."""
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

from serve import handler, voice_phone_call, voice_service, voice_settings
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.plans import PlanService
from serve.remote import ApiKeys, RemoteAccess
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
NUMBER = "+39 333 1234567"


class FakeVoiceService:
    def __init__(self, token):
        outer = self
        self.token, self.seen = token, []

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(body or b"{}"),
                                   "origin_header": self.headers.get("X-Call-Origin")})
                out = json.dumps({"status": "calling", "transport": "gsm"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class PhoneCall(unittest.TestCase):
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
        cls.announce(cls.voice.port)

    @classmethod
    def announce(cls, port):
        info = Path(cls.tmp.name) / "data" / "voice-service.json"
        info.write_text(json.dumps({"host": "127.0.0.1", "port": port, "pid": 1, "started": "x"}), encoding="utf-8")

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
        self.svc.remote._httpd = object()                                   # as if the listener on the Tailscale address were up
        self.svc.api_key = ""
        self.voice.seen.clear()
        voice_settings.set_telephony({"phone_calls": True})

    def tearDown(self):
        self.svc.remote._httpd = None

    def post(self, body, outside=True, key=None, headers=None, raw=None):
        port = (self.phone if outside else self.httpd).server_address[1]
        h = {"Content-Type": "application/json", **(headers or {})}
        if key or (outside and key is None):
            h["Authorization"] = "Bearer " + (key or self.key)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", voice_phone_call.PATH, body=raw if raw is not None else json.dumps(body), headers=h)
            r = conn.getresponse()
            data = json.loads(r.read() or b"{}")
            return r.status, data
        finally:
            conn.close()

    def code(self, data):
        return (data.get("error") or {}).get("code")

    def test_a_phone_with_its_key_places_a_call_as_the_person_through_the_route_for_calls_by_hand(self):
        status, data = self.post({"number": NUMBER, "greeting": "Buongiorno.", "lang": "it"})
        self.assertEqual((status, data), (200, {"status": "calling", "transport": "gsm"}))
        self.assertEqual(self.voice.seen, [{"path": "/call", "auth": "Bearer " + self.token, "body": {"number": NUMBER, "greeting": "Buongiorno.", "lang": "it"},
                                            "origin_header": None}])

    def test_a_header_that_claims_an_origin_is_not_read_and_not_passed_on(self):
        for claim in ("agent", "user", "person", "program"):
            self.voice.seen.clear()
            status, _ = self.post({"number": NUMBER}, headers={"X-Call-Origin": claim})
            self.assertEqual(status, 200)
            self.assertEqual((self.voice.seen[0]["path"], self.voice.seen[0]["origin_header"]), ("/call", None), claim)

    def test_the_body_is_cut_to_three_fields_so_the_phone_cannot_pick_the_line_or_say_who_it_is(self):
        for extra in ("transport", "voice", "origin", "approval_id", "brain"):
            status, data = self.post({"number": NUMBER, extra: "gsm"})
            self.assertEqual((status, self.code(data)), (400, "bad_request"), extra)
        for bad in ({}, {"number": ""}, {"number": 5}, {"number": "1" * 41}, {"number": NUMBER, "greeting": "x" * 301}, {"number": NUMBER + "\r\nATD"},
                    {"number": NUMBER, "lang": 7}, []):
            self.assertEqual(self.post(bad)[0], 400, bad)
        self.assertEqual(self.post(None, raw="not json")[0], 400)
        self.assertEqual(self.voice.seen, [])

    def test_with_the_persons_switch_off_nothing_is_placed(self):
        voice_settings.set_telephony({"phone_calls": False})
        status, data = self.post({"number": NUMBER})
        self.assertEqual((status, self.code(data)), (403, "phone_calls_off"))
        self.assertEqual(self.voice.seen, [])

    def test_a_program_on_this_pc_cannot_claim_the_origin_with_or_without_a_key(self):
        for key in ("", self.key):
            for headers in ({}, {"X-Call-Origin": "user"}, {"X-Call-Origin": "person"}):
                status, data = self.post({"number": NUMBER}, outside=False, key=key or None, headers=headers)
                self.assertEqual((status, self.code(data)), (403, "phone_only"), (key, headers))
        self.svc.api_key = "master-key-value"
        status, data = self.post({"number": NUMBER}, outside=False, key="master-key-value")
        self.assertEqual((status, self.code(data)), (403, "phone_only"))                    # the server's own key on this PC does not make it the person's phone
        self.assertEqual(self.voice.seen, [])

    def test_the_listener_refuses_a_request_without_a_valid_key_and_with_phones_turned_off(self):
        for key in ("bsk_wrong", ""):
            h = {"Authorization": "Bearer " + key} if key else {}
            conn = http.client.HTTPConnection("127.0.0.1", self.phone.server_address[1], timeout=10)
            conn.request("POST", voice_phone_call.PATH, body=json.dumps({"number": NUMBER}), headers={"Content-Type": "application/json", **h})
            self.assertEqual(conn.getresponse().status, 401, key)
            conn.close()
        self.svc.remote._httpd = None                                                       # phones turned off
        self.assertEqual(self.post({"number": NUMBER})[0], 403)
        self.assertEqual(self.voice.seen, [])

    def test_a_service_that_is_down_is_a_503_with_the_reason(self):
        self.announce(1)
        try:
            status, data = self.post({"number": NUMBER})
        finally:
            self.announce(self.voice.port)
        self.assertEqual((status, self.code(data)), (503, "voice_service_down"))

    def test_the_route_reads_no_origin_header_and_sends_none(self):
        src = (ROOT / "serve" / "voice_phone_call.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith(("#", '"""')))
        self.assertNotIn("headers.get(", code)
        self.assertNotIn("putheader", code)


if __name__ == "__main__":
    unittest.main()
