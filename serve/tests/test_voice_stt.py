"""The Telegram bot's way to the voice service's speech to text (serve/voice_stt.py), against a stand-in service on a loopback port:
what `check` says for each state of the `stt` function, what `transcribe` sends (a multipart file, the language hint, the token) and how each
answer of the service becomes a `SpeechError`. Nothing real is started."""
from __future__ import annotations

import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from serve import voice_stt
from tadamoo.telegram import SpeechError

TOKEN = "c" * 64


class FakeService:
    def __init__(self):
        self.seen, self.stt, self.transcribe = [], {"state": "loaded", "installed": True}, (200, {"text": "hello there"})
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"), "ctype": self.headers.get("Content-Type"), "body": body})
                if self.path == "/functions":
                    code, out = 200, {"functions": {"stt": outer.stt}}
                else:
                    code, out = outer.transcribe
                raw = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _answer

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class SpeechTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        patch = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.data), "TADAMOO_CACHE": str(self.data / "cache")})
        patch.start()
        self.addCleanup(patch.stop)
        (self.data / "voice-service.token").write_text(TOKEN, encoding="utf-8")
        self.service = FakeService()
        self.addCleanup(self.service.close)
        (self.data / "voice-service.json").write_text(json.dumps({"host": "127.0.0.1", "port": self.service.port, "pid": 1, "started": "x"}), encoding="utf-8")
        self.audio = self.data / "v.ogg"
        self.audio.write_bytes(b"OggS-opus")
        self.speech = voice_stt.VoiceSpeech()

    def test_a_loaded_model_is_ready_and_asking_loads_nothing(self):
        self.speech.check()
        self.assertEqual([(r["method"], r["path"]) for r in self.service.seen], [("GET", "/functions")])

    def test_a_model_that_is_not_installed_gives_the_services_reason(self):
        self.service.stt = {"state": "unloaded", "installed": False, "reason": "Whisper small is not installed: install it from Settings > Voice."}
        with self.assertRaises(SpeechError) as got:
            self.speech.check()
        self.assertEqual((got.exception.kind, got.exception.reason), ("unavailable", "Whisper small is not installed: install it from Settings > Voice."))

    def test_a_model_that_is_not_loaded_says_so_and_names_where_to_load_it(self):
        self.service.stt = {"state": "unloaded", "installed": True, "error": None}
        with self.assertRaises(SpeechError) as got:
            self.speech.check()
        self.assertIn("not loaded (it is unloaded)", got.exception.reason)
        self.assertIn("Voice functions dialog", got.exception.reason)
        self.service.stt = {"state": "error", "installed": True, "error": "CUDA out of memory"}
        with self.assertRaises(SpeechError) as got:
            self.speech.check()
        self.assertEqual(got.exception.reason, "CUDA out of memory")

    def test_a_service_that_is_not_running_gives_its_reason(self):
        (self.data / "voice-service.json").unlink()
        for call in (self.speech.check, lambda: self.speech.transcribe(self.audio, "")):
            with self.assertRaises(SpeechError) as got:
                call()
            self.assertEqual(got.exception.kind, "unavailable")
            self.assertIn("has not started", got.exception.reason)

    def test_the_file_goes_as_multipart_with_the_token_and_the_language_hint(self):
        self.assertEqual(self.speech.transcribe(self.audio, "it"), "hello there")
        seen = self.service.seen[0]
        self.assertEqual((seen["method"], seen["path"], seen["auth"]), ("POST", "/transcribe?lang=it", "Bearer " + TOKEN))
        self.assertTrue(seen["ctype"].startswith("multipart/form-data; boundary="))
        self.assertIn(b"OggS-opus", seen["body"])
        self.assertIn(b'name="file"', seen["body"])
        self.speech.transcribe(self.audio, "")
        self.speech.transcribe(self.audio, "../x&y=1")                      # a hint that is not a language code is not sent
        self.assertEqual([r["path"] for r in self.service.seen[1:]], ["/transcribe", "/transcribe"])

    def test_each_answer_of_the_service_becomes_the_right_error(self):
        cases = [((409, {"status": "error", "code": "model_not_installed", "message": "Whisper small is not installed."}), "unavailable", "Whisper small is not installed."),
                 ((400, {"status": "error", "message": "the audio cannot be read: Format not recognised"}), "undecodable", "the audio cannot be read: Format not recognised"),
                 ((500, {"status": "error", "message": "the helper died"}), "failed", "the helper died"),
                 ((502, {}), "failed", "the voice service answered 502")]
        for answer, kind, reason in cases:
            self.service.transcribe = answer
            with self.assertRaises(SpeechError) as got:
                self.speech.transcribe(self.audio, "")
            self.assertEqual((got.exception.kind, got.exception.reason), (kind, reason))

    def test_the_services_token_is_never_in_an_error(self):
        self.service.transcribe = (500, {"message": "echo " + TOKEN})
        with self.assertRaises(SpeechError) as got:
            self.speech.transcribe(self.audio, "")
        self.assertNotIn(TOKEN, got.exception.reason)


if __name__ == "__main__":
    unittest.main()
