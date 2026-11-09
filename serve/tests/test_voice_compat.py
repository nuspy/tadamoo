"""callAPIcall's routes on TaDaMoo's port (serve/voice_compat.py): the table, who may use them, what passes through to the voice service
(files, long waits, binary answers), the routes that answer 501 not_available_yet, and that the service's token never reaches a client.
The voice service is a stand-in on a loopback port; nothing real is started."""
from __future__ import annotations

import hashlib
import http.client
import http.server
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from serve import handler, voice_compat, voice_settings
from serve.personas_api import PersonasApi
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.service import Service
from serve.util.text import ByteTokenizer
from tadamoo.personas import PersonaStore

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "b" * 64
BLOB = bytes(range(256)) * 800                       # 200 KB that is not text


class FakeVoice:
    """The voice service: records every request and answers by path."""

    def __init__(self):
        self.seen = []
        self.leak = None                              # "body", "split" (across the 1 MiB chunk edge) or "header": what the binary route puts the token in
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                                   "ctype": self.headers.get("Content-Type"), "custom": self.headers.get("X-Custom"),
                                   "sha": hashlib.sha256(body).hexdigest(), "size": len(body)})
                path = self.path.split("?")[0]
                code, kind, out = 200, "application/json", {"ok": True}
                if path == "/analysis/start":
                    code, out = 202, {"id": "j1", "state": "queued", "got": len(body), "sha": hashlib.sha256(body).hexdigest()}
                elif path.endswith("/heard"):
                    time.sleep(0.5)
                    out = {"text": "hello", "index": 1}
                elif path == "/analysis/list":
                    out = {"analyses": [], "echo": self.headers.get("Authorization")}       # a service that repeats its own credentials
                elif path == "/transcribe":
                    code, out = 409, {"status": "error", "code": "model_not_installed", "message": "Whisper small is not installed: install it from Settings > Voice."}
                elif path == "/tts/models":
                    kind, out = "application/octet-stream", BLOB
                    if outer.leak == "body":
                        out = BLOB[:5000] + TOKEN.encode() + BLOB[:5000]
                    elif outer.leak == "split":
                        out = b"x" * (voice_compat.CHUNK - 10) + TOKEN.encode() + b"y" * 5000
                elif path == "/analysis/missing":
                    code, out = 404, {"error": "analisi non trovata"}
                raw = out if isinstance(out, bytes) else json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Set-Cookie", "service=1")
                if outer.leak == "header":
                    self.send_header("Content-Disposition", f"attachment; filename={TOKEN}.bin")
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _answer

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
        self.data.mkdir()
        patch = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.data), "TADAMOO_CACHE": str(Path(self.tmp.name) / "cache")})
        patch.start()
        self.addCleanup(patch.stop)
        (self.data / "voice-service.token").write_text(TOKEN, encoding="utf-8")
        self.voice = FakeVoice()
        self.addCleanup(self.voice.close)
        self.announce(self.voice.port)
        self.personas = PersonasApi(PersonaStore(self.data / "personas", key=bytes(range(32))))
        patch = mock.patch.object(handler, "_PERSONAS", self.personas)
        patch.start()
        self.addCleanup(patch.stop)
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        self.httpd = handler._listen(self.svc, "127.0.0.1", 0)
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close()))
        self.port = self.httpd.server_address[1]

    def announce(self, port):
        (self.data / "voice-service.json").write_text(json.dumps({"host": "127.0.0.1", "port": port, "pid": 1, "started": "x"}), encoding="utf-8")

    def request(self, method, path, body=None, headers=None, port=None, timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def jsonrequest(self, method, path, payload=None, headers=None, **kw):
        body = None if payload is None else json.dumps(payload).encode()
        head = {"Content-Type": "application/json", **(headers or {})} if body is not None else dict(headers or {})
        status, hdr, raw = self.request(method, path, body, head, **kw)
        return status, json.loads(raw.decode() or "{}") if hdr.get("Content-Type", "").startswith("application/json") else raw


class TableTests(unittest.TestCase):
    def test_a_path_is_matched_whole_with_its_method(self):
        cases = [("POST", "/analysis/start", True), ("GET", "/analysis/list", True), ("GET", "/analysis/20261109-010101-abc123/transcript", True),
                 ("DELETE", "/analysis/20261109-010101-abc123", True), ("GET", "/external/c_1/heard", True), ("POST", "/transcribe", True),
                 ("GET", "/voices/cloned", True), ("DELETE", "/voices/cloned/ada", True), ("POST", "/session/start", True),
                 ("GET", "/analysis/../etc", False), ("GET", "/analysis/a/b/c", False), ("GET", "/external/a b/state", False),
                 ("DELETE", "/voices/cloned/..", False), ("GET", "/analysisx", False), ("GET", "/v1/analysis/list", False), ("GET", "/api/personas", False)]
        for method, path, found in cases:
            route, _ = voice_compat.find(method, path)
            self.assertEqual(route is not None, found, f"{method} {path}")

    def test_a_known_path_with_another_method_is_known_but_not_found(self):
        self.assertEqual(voice_compat.find("POST", "/analysis/list"), (None, True))
        self.assertEqual(voice_compat.find("GET", "/nothing"), (None, False))

    def test_nothing_is_under_v1_or_api_and_the_table_has_its_kinds(self):
        for row in voice_compat.routes_summary():
            self.assertFalse(row["pattern"].startswith(("/v1", "/api")), row)
            self.assertIn(row["kind"], ("api", "local"))
        kinds = {r["pattern"]: r["kind"] for r in voice_compat.routes_summary()}
        self.assertEqual(kinds["/analysis/start"], "api")
        self.assertEqual(kinds["/external/call"], "local")
        self.assertEqual(kinds["/voices/clone"], "local")

    def test_the_routes_that_wait_or_carry_files_say_so(self):
        heard = voice_compat.find("GET", "/external/c1/heard")[0]
        self.assertEqual(heard.wait, "timeout_s")
        self.assertEqual(voice_compat._wait_s(heard, "after=0&timeout_s=20"), heard.timeout + 20)
        self.assertEqual(voice_compat._wait_s(heard, "timeout_s=99999"), heard.timeout + voice_compat.WAIT_CAP_S)
        self.assertEqual(voice_compat._wait_s(heard, "timeout_s=abc"), heard.timeout)
        for path in ("/analysis/start", "/transcribe"):
            self.assertTrue(voice_compat.find("POST", path)[0].upload)
        self.assertFalse(voice_compat.find("POST", "/tts/test")[0].upload)


class PassThroughTests(Base):
    def test_a_multipart_file_reaches_the_service_byte_for_byte(self):
        audio = os.urandom(3 << 20)
        boundary = "XyZ123"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"speakers\"\r\n\r\n2\r\n--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
        status, hdr, raw = self.request("POST", "/analysis/start", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        out = json.loads(raw)
        self.assertEqual(status, 202)
        self.assertEqual((out["id"], out["got"], out["sha"]), ("j1", len(body), hashlib.sha256(body).hexdigest()))
        seen = self.voice.seen[-1]
        self.assertEqual((seen["method"], seen["path"], seen["ctype"]), ("POST", "/analysis/start", f"multipart/form-data; boundary={boundary}"))

    def test_the_services_token_replaces_the_callers_and_other_headers_stay_here(self):
        self.svc.api_key = "caller-key"
        self.jsonrequest("GET", "/analysis/list", headers={"Authorization": "Bearer caller-key", "X-Custom": "1"})
        seen = self.voice.seen[-1]
        self.assertEqual(seen["auth"], "Bearer " + TOKEN)
        self.assertIsNone(seen["custom"])

    def test_a_long_wait_and_its_query_pass_through(self):
        t0 = time.time()
        status, out = self.jsonrequest("GET", "/external/c1/heard?after=-1&timeout_s=1")
        self.assertEqual((status, out), (200, {"text": "hello", "index": 1}))
        self.assertGreaterEqual(time.time() - t0, 0.45)
        self.assertEqual(self.voice.seen[-1]["path"], "/external/c1/heard?after=-1&timeout_s=1")

    def test_a_binary_answer_streams_whole(self):
        status, hdr, raw = self.request("GET", "/tts/models")
        self.assertEqual((status, hdr["Content-Type"], raw), (200, "application/octet-stream", BLOB))

    def test_a_binary_answer_that_carries_the_token_is_cut_off_before_the_token(self):
        for where in ("body", "split"):
            self.voice.leak = where
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            got = b""
            try:
                conn.request("GET", "/tts/models")
                r = conn.getresponse()
                try:
                    got = r.read()
                except http.client.IncompleteRead as e:
                    got = e.partial                                          # the connection is closed short of the length it promised
            finally:
                conn.close()
            self.assertNotIn(TOKEN.encode(), got, where)
            self.assertLess(len(got), 1 << 21, where)

    def test_a_header_of_a_binary_answer_never_carries_the_token(self):
        self.voice.leak = "header"
        status, hdr, raw = self.request("GET", "/tts/models")
        self.assertEqual((status, raw), (200, BLOB))
        self.assertNotIn(TOKEN, "".join(hdr.values()))
        self.assertIn("[hidden]", hdr["Content-Disposition"])

    def test_the_guard_holds_back_a_token_split_over_any_two_chunks(self):
        token = "t0k3n-" + "z" * 20
        data = b"A" * 40 + token.encode() + b"B" * 40
        for cut in range(1, len(data)):
            guard, sent = voice_compat._TokenGuard(token), b""
            with self.assertRaises(voice_compat.TokenLeak):
                sent += guard.feed(data[:cut])
                sent += guard.feed(data[cut:])
            self.assertNotIn(token[:10].encode() + b"zz", sent)
        clean, guard, sent = b"C" * 100, voice_compat._TokenGuard(token), b""
        for i in range(0, 100, 7):
            sent += guard.feed(clean[i:i + 7])
        self.assertEqual(sent + guard.finish(), clean)                      # a clean answer comes out whole

    def test_every_route_that_streams_goes_through_the_guard(self):
        src = (ROOT / "serve" / "voice_compat.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("resp.read("), 2)                       # the buffered text read and the guarded stream, nowhere else
        self.assertEqual(src.count("h.wfile.write("), 3)
        stream = src.split("guard = _TokenGuard(token)")[1].split("def routes_summary")[0]
        self.assertEqual(stream.count("h.wfile.write("), 2)
        self.assertTrue(all("guard." in line for line in stream.splitlines() if "h.wfile.write(" in line))

    def test_the_services_status_and_body_come_back_unchanged_but_not_its_cookies(self):
        status, hdr, raw = self.request("POST", "/transcribe", b"RIFF", {"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "4"})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw)["code"], "model_not_installed")
        self.assertNotIn("Set-Cookie", hdr)
        status, out = self.jsonrequest("GET", "/analysis/missing")
        self.assertEqual((status, out), (404, {"error": "analisi non trovata"}))

    def test_a_file_without_a_length_or_over_the_limit_is_refused_before_it_is_read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/transcribe")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["code"]), (411, "length_required"))
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/transcribe")
        conn.putheader("Content-Length", str(600 << 20))
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["code"]), (413, "too_large"))
        conn.close()
        self.assertEqual([s for s in self.voice.seen if s["path"] == "/transcribe"], [])

    def test_an_oversized_json_body_is_a_413(self):
        status, out = self.jsonrequest("POST", "/tts/test", {"text": "x" * (voice_compat.JSON_MAX + 10)})
        self.assertEqual((status, out["code"]), (413, "bad_request"))

    def test_the_wrong_method_is_a_405_and_an_unknown_one_still_a_404(self):
        self.assertEqual(self.jsonrequest("POST", "/analysis/list", {})[0], 405)
        status, out = self.jsonrequest("DELETE", "/nothing-here")
        self.assertEqual((status, out["error"]["message"]), (404, "not found"))
        self.assertEqual(self.voice.seen, [])


class AvatarRoutesTests(Base):
    def test_the_avatar_routes_reach_the_service_and_the_live_streams_stay_out(self):
        self.jsonrequest("GET", "/avatar/state")
        self.assertEqual((self.voice.seen[-1]["method"], self.voice.seen[-1]["path"]), ("GET", "/avatar/state"))
        status, out = self.jsonrequest("GET", "/avatar/preview.mjpg")
        self.assertEqual(status, 404)                                                          # the stream is relayed by /api/voice/avatar, not by this table


class MeetingRoutesTests(Base):
    def test_the_meeting_routes_reach_the_service_from_this_pc(self):
        voice_settings.set_meetings({"allow_programs_join": True})                  # the gated routes are tested in test_voice_compat_programs_join.py
        calls = [("POST", "/meetbot/join", {"url": "https://meet.google.com/abc-defg-hij", "rights_confirmed": True}), ("GET", "/meetbot/status", None),
                 ("POST", "/meetbot/leave", {}), ("GET", "/meetings/list", None), ("GET", "/meetings/20261109-100000-meet-ab12", None),
                 ("GET", "/meetings/20261109-100000-meet-ab12/transcript", None), ("DELETE", "/meetings/20261109-100000-meet-ab12", None),
                 ("GET", "/calendar/status", None), ("GET", "/calendar/events", None), ("POST", "/calendar/google/connect", {}),
                 ("POST", "/calendar/microsoft/disconnect", {}), ("POST", "/calendar/events/g:abc123/autojoin", {"enabled": True}),
                 ("GET", "/copilot/state", None), ("POST", "/copilot/toggle", {})]
        for method, path, body in calls:
            status, out = self.jsonrequest(method, path, body)
            self.assertEqual(status, 200, f"{method} {path}")
            self.assertEqual((self.voice.seen[-1]["method"], self.voice.seen[-1]["path"]), (method, path))
            self.assertEqual(self.voice.seen[-1]["auth"], "Bearer " + TOKEN)

    def test_they_are_for_this_pc_only_and_a_wrong_method_or_provider_is_refused(self):
        self.assertEqual(self.jsonrequest("GET", "/meetbot/join")[0], 405)
        self.assertEqual(self.jsonrequest("POST", "/calendar/yahoo/connect", {})[0], 404)
        self.assertEqual(self.jsonrequest("GET", "/meetings/../etc")[0], 404)
        for method, path in (("POST", "/meetbot/join"), ("GET", "/meetings/list"), ("POST", "/copilot/toggle"), ("GET", "/calendar/events")):
            self.assertEqual(next(r.kind for r in voice_compat.ROUTES if r.method == method and r.pattern.fullmatch(path)), "local", path)
        self.assertNotIn("meetings", voice_compat.SOON)


class PersonasTests(Base):
    """/personas: callAPIcall's paths on TaDaMoo's personalities, answered here and never by the voice service."""

    def make(self, **fields):
        status, out = self.jsonrequest("POST", "/personas", {"name": "Maria Rossi", "voice": {"engine": "kokoro", "voice": "if_sara"}, "personality": "Warm.", "system_prompt": "Be kind.", **fields})
        self.assertEqual(status, 201, out)
        return out

    def test_create_list_and_get_in_callapicalls_shape_with_the_slug_as_the_name(self):
        self.assertEqual(self.jsonrequest("GET", "/personas"), (200, {"personas": []}))
        made = self.make()
        self.assertEqual((made["status"], made["name"], made["display_name"]), ("ok", "maria-rossi", "Maria Rossi"))
        status, out = self.jsonrequest("GET", "/personas")
        row = out["personas"][0]
        self.assertEqual((status, row["name"], row["display_name"], row["voice"], row["personality"], row["system_prompt"]),
                         (200, "maria-rossi", "Maria Rossi", {"engine": "kokoro", "voice": "if_sara"}, "Warm.", "Be kind."))
        for key in ("avatar", "language", "knowledge", "learn"):
            self.assertIn(key, row)
        self.assertEqual(self.jsonrequest("GET", "/personas/" + row["name"])[1]["persona"], row)
        self.assertEqual(self.voice.seen, [])                                                   # the voice service is not involved

    def test_a_path_takes_the_slug_or_the_name_with_spaces_or_underscores(self):
        self.make()
        for path in ("/personas/maria-rossi", "/personas/Maria%20Rossi", "/personas/Maria_Rossi", "/personas/maria_rossi", "/personas/maria-rossi/"):
            self.assertEqual(self.jsonrequest("GET", path)[1]["persona"]["name"], "maria-rossi", path)
        for path in ("/personas/nobody", "/personas/..", "/personas/Maria%2FRossi"):
            self.assertEqual(self.jsonrequest("GET", path)[0], 404, path)

    def test_the_memory_of_a_personality_is_in_no_answer_and_cannot_be_written(self):
        made = self.make()
        self.personas.store.update(made["name"], {"profile": "Ada fears spiders and lives in Turin"})
        self.personas.store.memory(made["name"]).add("long", "tastes", "Loves the poems of Montale")
        seen = json.dumps([self.jsonrequest("GET", "/personas")[1], self.jsonrequest("GET", "/personas/maria-rossi")[1],
                           self.jsonrequest("PUT", "/personas/maria-rossi", {"personality": "Calm."})[1]])
        for secret in ("spiders", "Turin", "Montale", "profile", "counts"):
            self.assertNotIn(secret, seen)
        for body in ({"profile": "x"}, {"memory": {}}):
            status, out = self.jsonrequest("PUT", "/personas/maria-rossi", body)
            self.assertEqual((status, out["code"]), (400, "bad_request"))
            self.assertIn("memory", out["error"])
        self.assertEqual(self.personas.store.get("maria-rossi")["profile"], "Ada fears spiders and lives in Turin")
        self.assertEqual(self.personas.store.memory("maria-rossi").count(), 1)

    def test_a_second_personality_with_the_same_name_is_a_409_and_a_bad_body_a_400(self):
        self.make()
        status, out = self.jsonrequest("POST", "/personas", {"name": "maria ROSSI"})
        self.assertEqual((status, out["code"]), (409, "exists"))
        for body in ({}, {"name": ""}, {"name": "../x"}, {"name": "A", "voice": "kokoro"}, {"name": "A", "colour": "red"}, {"name": "A", "knowledge_project": "wiki"}):
            status, out = self.jsonrequest("POST", "/personas", body)
            self.assertEqual((status, out["code"]), (400, "bad_request"), body)
        self.assertIn("no equivalent", self.jsonrequest("POST", "/personas", {"name": "A", "knowledge_project": "wiki"})[1]["error"])
        self.assertEqual(self.jsonrequest("POST", "/personas", {"name": "Empty project", "knowledge_project": ""})[0], 201)    # an empty one says nothing
        self.assertEqual(self.request("POST", "/personas", b"[1]", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/personas", b"{not json", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(len(self.personas.store.list()), 2)

    def test_put_and_patch_change_the_fields_named_and_keep_the_rest(self):
        self.make()
        status, out = self.jsonrequest("PUT", "/personas/maria-rossi", {"voice": {"engine": "xtts", "voice": "ada"}})
        self.assertEqual((status, out["status"], out["persona"]["voice"], out["persona"]["personality"]), (200, "ok", {"engine": "xtts", "voice": "ada"}, "Warm."))
        status, out = self.jsonrequest("PATCH", "/personas/Maria_Rossi", {"system_prompt": "Be brief.", "name": "Maria R"})
        self.assertEqual((status, out["persona"]["name"], out["persona"]["display_name"], out["persona"]["system_prompt"]), (200, "maria-rossi", "Maria R", "Be brief."))
        self.assertEqual(self.jsonrequest("PUT", "/personas/nobody", {})[0], 404)
        self.assertEqual(self.jsonrequest("PUT", "/personas/maria-rossi", {"personality": "x" * 9000})[0], 400)

    def test_deleting_needs_the_confirmation_because_the_memory_goes_with_it(self):
        self.make()
        self.personas.store.memory("maria-rossi").add("long", "tastes", "Loves Montale")
        status, out = self.jsonrequest("DELETE", "/personas/maria-rossi")
        self.assertEqual((status, out["code"]), (400, "confirmation_needed"))
        self.assertIn("1 notes", out["error"])
        self.assertTrue(self.personas.store.exists("maria-rossi"))
        self.assertEqual(self.jsonrequest("DELETE", "/personas/maria-rossi?confirm=false")[0], 400)
        self.assertEqual(self.jsonrequest("DELETE", "/personas/maria-rossi?confirm=true"), (200, {"status": "ok"}))
        self.assertFalse(self.personas.store.exists("maria-rossi"))
        self.assertEqual(self.jsonrequest("DELETE", "/personas/maria-rossi?confirm=true")[0], 404)

    def test_the_wrong_method_is_a_405(self):
        self.make()
        for method, path in (("DELETE", "/personas"), ("PUT", "/personas"), ("POST", "/personas/maria-rossi")):
            self.assertEqual(self.jsonrequest(method, path, {} if method != "DELETE" else None)[0], 405, f"{method} {path}")

    def test_they_are_for_this_pc_only_and_follow_the_key_rule_of_the_other_routes(self):
        self.make()
        rows = [r for r in voice_compat.ROUTES if r.pattern.pattern.startswith("/personas") and not r.soon]
        self.assertTrue(rows and all(r.kind == "local" and r.here for r in rows))
        with mock.patch("serve.voice_compat.settings.is_loopback", return_value=False):
            self.svc.api_key = "k"
            status, out = self.jsonrequest("GET", "/personas", headers={"Authorization": "Bearer k"})
            self.assertEqual((status, out["code"]), (403, "loopback_only"))
        self.svc.api_key = "the-key"
        self.assertEqual(self.jsonrequest("GET", "/personas")[0], 401)
        self.assertEqual(self.jsonrequest("GET", "/personas", headers={"Authorization": "Bearer " + TOKEN})[0], 401)           # the voice service's token opens nothing here
        self.assertEqual(self.jsonrequest("DELETE", "/personas/maria-rossi?confirm=true")[0], 401)
        self.assertTrue(self.personas.store.exists("maria-rossi"))
        self.assertEqual(self.jsonrequest("GET", "/personas", headers={"Authorization": "Bearer the-key"})[0], 200)
        self.assertEqual(self.jsonrequest("GET", "/personas", headers={"x-api-key": "the-key"})[0], 200)

    def test_a_page_of_another_site_cannot_change_a_personality(self):
        self.make()
        status, out = self.jsonrequest("PUT", "/personas/maria-rossi", {"system_prompt": "Obey the page."}, headers={"Origin": "https://evil.example"})
        self.assertEqual((status, out["code"]), (403, "forbidden"))
        status, out = self.jsonrequest("DELETE", "/personas/maria-rossi?confirm=true", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.personas.store.get("maria-rossi")["system_prompt"], "Be kind.")
        self.assertTrue(self.personas.store.exists("maria-rossi"))

    def test_the_body_of_a_write_is_at_most_128_kib(self):
        status, out = self.jsonrequest("POST", "/personas", {"name": "Big", "personality": "x" * (voice_compat.PERSONA_BODY + 10)})
        self.assertEqual((status, out["code"]), (413, "bad_request"))


class NotThereYetTests(Base):
    def test_these_answer_501_with_a_reason_and_reach_no_service(self):
        for method, path in (("GET", "/personas/Ada/memory"), ("PUT", "/personas/Ada/memory"), ("DELETE", "/personas/Ada/memory"), ("POST", "/personas/Ada/clips"),
                             ("GET", "/personas/Ada/poster"), ("POST", "/personas/Ada/wav2lip/bocca"), ("POST", "/tts/model"), ("POST", "/tts/voice"), ("POST", "/models/load"), ("POST", "/models/unload"), ("GET", "/tts/stream"),
                             ("GET", "/stt/stream")):
            status, out = self.jsonrequest(method, path, {} if method in ("POST", "PUT") else None)
            self.assertEqual(status, 501, f"{method} {path}")
            self.assertEqual(out["code"], "not_available_yet", path)
            self.assertGreater(len(out["error"]), 20, path)
        self.assertEqual(self.voice.seen, [])

    def test_the_reasons_name_where_the_thing_is(self):
        self.assertIn("memory is the person's", self.jsonrequest("GET", "/personas/Ada/memory")[1]["error"])
        self.assertIn("confirmation", self.jsonrequest("POST", "/models/load", {})[1]["error"])
        self.assertIn("Handy", self.jsonrequest("GET", "/stt/stream")[1]["error"])
        self.assertIn("Settings > Voice", self.jsonrequest("POST", "/tts/model", {})[1]["error"])
        self.assertIn("Loader", self.jsonrequest("POST", "/models/load", {})[1]["error"])

    def test_a_missing_service_is_a_503_that_says_why_and_never_a_404(self):
        (self.data / "voice-service.json").unlink()
        status, out = self.jsonrequest("GET", "/analysis/list")
        self.assertEqual((status, out["code"]), (503, "voice_service_down"))
        self.assertIn("has not started", out["error"])
        self.announce(1)                                            # a port nobody listens on
        status, out = self.jsonrequest("GET", "/analysis/list")
        self.assertEqual((status, out["code"]), (503, "voice_service_down"))
        self.assertNotIn(TOKEN, json.dumps(out))


class AuthTests(Base):
    def test_with_no_key_set_this_pc_is_accepted_without_a_token(self):
        self.assertEqual(self.jsonrequest("GET", "/analysis/list")[0], 200)

    def test_with_no_key_set_another_pc_is_refused_and_a_local_route_is_local_only(self):
        with mock.patch("serve.voice_compat.settings.is_loopback", return_value=False):
            status, out = self.jsonrequest("GET", "/analysis/list")
            self.assertEqual((status, out["code"]), (403, "loopback_only"))
            self.svc.api_key = "k"
            self.assertEqual(self.jsonrequest("GET", "/analysis/list", headers={"Authorization": "Bearer k"})[0], 200)
            status, out = self.jsonrequest("POST", "/session/start", {}, headers={"Authorization": "Bearer k"})
            self.assertEqual((status, out["code"]), (403, "loopback_only"))
        self.assertEqual(len(self.voice.seen), 1)

    def test_with_a_key_the_bearer_is_needed_and_the_control_secret_counts_as_one(self):
        self.svc.api_key, self.svc.control_token = "the-key", "the-secret"
        self.assertEqual(self.jsonrequest("GET", "/analysis/list")[0], 401)
        self.assertEqual(self.jsonrequest("GET", "/analysis/list", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.jsonrequest("GET", "/analysis/list", headers={"Authorization": "Bearer " + TOKEN})[0], 401)       # the service's token opens nothing here
        self.assertEqual(self.jsonrequest("GET", "/analysis/list", headers={"Authorization": "Bearer the-key"})[0], 200)
        self.assertEqual(self.jsonrequest("GET", "/analysis/list", headers={"x-api-key": "the-key"})[0], 200)
        self.assertEqual(self.jsonrequest("GET", "/analysis/list", headers={"Authorization": "Bearer the-secret"})[0], 200)
        self.assertEqual(len(self.voice.seen), 3)

    def test_a_refused_upload_is_read_away_so_the_caller_sees_the_answer(self):
        self.svc.api_key = "the-key"
        status, hdr, raw = self.request("POST", "/analysis/start", b"x" * 100000, {"Content-Type": "multipart/form-data; boundary=x"})
        self.assertEqual(status, 401)
        self.assertEqual(self.voice.seen, [])

    def test_a_page_of_another_site_or_a_rebound_name_is_refused_when_no_key_is_set(self):
        status, out = self.jsonrequest("POST", "/session/stop", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual((status, out["code"]), (403, "forbidden"))
        status, out = self.jsonrequest("GET", "/analysis/list", headers={"Host": "evil.example:80"})
        self.assertEqual((status, out["code"]), (403, "forbidden"))
        self.assertEqual(self.jsonrequest("GET", "/analysis/list", headers={"Host": f"localhost:{self.port}"})[0], 200)
        self.assertEqual(self.jsonrequest("POST", "/session/stop", {}, headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)

    def test_the_listener_for_phones_offers_none_of_them(self):
        remote = handler._listen(self.svc, "127.0.0.1", 0, remote=True)
        self.addCleanup(lambda: (remote.shutdown(), remote.server_close()))
        port = remote.server_address[1]
        for method, path in (("GET", "/analysis/list"), ("POST", "/transcribe"), ("GET", "/personas")):
            status, hdr, raw = self.request(method, path, b"" if method == "POST" else None, port=port)
            self.assertEqual((status, json.loads(raw)["code"]), (403, "forbidden"), path)
        self.assertEqual(self.voice.seen, [])


class TokenTests(Base):
    def test_the_services_token_never_reaches_a_client(self):
        self.svc.api_key = "k"
        auth = {"Authorization": "Bearer k"}
        status, out = self.jsonrequest("GET", "/analysis/list", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(out["echo"], "Bearer [hidden]")                     # the stand-in repeated it; it is taken out on the way back
        for method, path in (("GET", "/analysis/list"), ("GET", "/analysis/missing"), ("POST", "/transcribe"), ("GET", "/personas")):
            status, hdr, raw = self.request(method, path, b"" if method == "POST" else None, {**auth, "Content-Type": "multipart/form-data; boundary=x", "Content-Length": "0"} if method == "POST" else auth)
            self.assertNotIn(TOKEN.encode(), raw, path)
            self.assertNotIn(TOKEN, json.dumps(hdr), path)

    def test_the_handler_names_no_route_of_the_table_itself(self):
        source = (ROOT / "serve" / "handler.py").read_text(encoding="utf-8")
        for word in ("/analysis", "/external", "/transcribe", "/tts/", "/voices", "/personas", "/meetbot"):
            self.assertNotRegex(source, rf'"{re.escape(word)}')


if __name__ == "__main__":
    unittest.main()
