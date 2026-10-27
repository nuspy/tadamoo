"""POST /api/export and the held page for the desktop app's PDF: origin, content type, size and secret checks, the selection, the answers."""
import io
import json
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from serve import export_api
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import CONTROL_HEADER, serve
from serve.remote import remote_path_ok
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


def snapshot():
    return {
        "title": "Trip", "lang": "en", "base": "http://127.0.0.1:8080/", "session": "abcd1234",
        "items": [
            {"kind": "prompt", "text": "PROMPT-MARK"}, {"kind": "reasoning", "turn": 1, "text": "REASON-MARK"},
            {"kind": "action", "turn": 1, "name": "shell", "arguments": {"command": "ACTION-MARK"}, "result": "ok"},
            {"kind": "answer", "turn": 1, "text": "ANSWER-MARK with [a link](https://example.com/)"},
        ],
    }


def body(**over):
    req = {"snapshot": snapshot(), "select": {"prompts": True, "reasoning": False, "actions": False, "answers": True}, "format": "md", "name": "my chat"}
    req.update(over)
    return req


class Plain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.svc.api_key = ""
        self.svc.control_token = ""

    def post(self, payload, headers=None, raw=None, path="/api/export"):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=h, method="POST"), timeout=20) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def get(self, path, headers=None):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers or {}), timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def error(self, data):
        return json.loads(data)["error"]["message"]

    def test_every_format_comes_back_with_its_type_and_a_safe_name(self):
        for fmt, ctype, ext in (("docx", "wordprocessingml.document", "docx"), ("odt", "opendocument.text", "odt"), ("md", "text/markdown", "md"),
                                ("html", "text/html", "html"), ("pdf", "text/html", "pdf")):
            status, headers, data = self.post(body(format=fmt, name="../Plan: the trip?"))
            self.assertEqual(status, 200, fmt)
            self.assertIn(ctype, headers["Content-Type"])
            name = urllib.parse.unquote(headers["X-Basalt-Filename"])
            self.assertTrue(name.endswith("." + ext) and "/" not in name and ":" not in name and "?" not in name, name)
            self.assertIn("attachment", headers["Content-Disposition"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(int(headers["Content-Length"]), len(data))
            if fmt in ("docx", "odt"):
                self.assertTrue(zipfile.ZipFile(io.BytesIO(data)).testzip() is None)

    def test_the_selection_is_applied_on_the_server(self):
        status, _, data = self.post(body())
        text = data.decode()
        self.assertEqual(status, 200)
        self.assertIn("PROMPT-MARK", text)
        self.assertIn("ANSWER-MARK", text)
        self.assertNotIn("REASON-MARK", text)
        self.assertNotIn("ACTION-MARK", text)
        self.assertIn("[a link](https://example.com/)", text)
        _, _, everything = self.post(body(select={"prompts": True, "reasoning": True, "actions": True, "answers": True}))
        self.assertIn("REASON-MARK", everything.decode())
        self.assertIn("ACTION-MARK", everything.decode())

    def test_bad_requests_are_refused_with_a_reason(self):
        for payload, word in ((body(format="exe"), "format"), (body(select={}), "Choose"), (body(select=None), "Choose"),
                              (body(snapshot={"items": "no"}), "no conversation"), ([1, 2], "JSON object")):
            status, _, data = self.post(payload)
            self.assertEqual(status, 400, payload if isinstance(payload, list) else payload.get("format"))
            self.assertIn(word, self.error(data))
        self.assertEqual(self.post(None, raw=b"{not json")[0], 400)
        self.assertEqual(self.post(None, raw=b"\xff\xfe")[0], 400)

    def test_the_content_type_and_the_origin_are_checked(self):
        status, _, _ = self.post(None, raw=json.dumps(body()).encode(), headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, data = self.post(body(), headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("own page", self.error(data))
        self.assertEqual(self.post(body(), headers={"Origin": self.base})[0], 200)

    def test_a_body_over_the_limit_is_refused(self):
        with mock.patch.object(export_api, "BODY_MAX", 2000):
            status, _, data = self.post(body(snapshot={"items": [{"kind": "answer", "text": "x" * 5000}]}))
        self.assertEqual(status, 413)
        self.assertIn("larger than", self.error(data))
        self.assertEqual(self.post(body())[0], 200)                                      # the connection and the route are fine afterwards

    def test_the_servers_secret_and_key_are_needed_when_they_are_set(self):
        self.svc.control_token = "s3cret"
        self.assertEqual(self.post(body())[0], 403)
        self.assertEqual(self.post(body(), headers={CONTROL_HEADER: "s3cret"})[0], 200)
        self.svc.api_key = "k"
        self.assertEqual(self.post(body(), headers={CONTROL_HEADER: "s3cret"})[0], 401)
        self.assertEqual(self.post(body(), headers={CONTROL_HEADER: "s3cret", "Authorization": "Bearer k"})[0], 200)

    def test_a_pdf_for_the_desktop_app_is_held_for_one_fetch_by_this_pc(self):
        status, _, data = self.post(body(format="pdf", hold=True, name="Trip"))
        self.assertEqual(status, 200)
        held = json.loads(data)
        self.assertEqual(held["name"], "Trip.pdf")
        self.assertRegex(held["token"], r"^[0-9a-f]{32}$")
        status, headers, page = self.get("/api/export/held/" + held["token"])
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"ANSWER-MARK", page)
        self.assertNotIn(b'class="hint"', page)                                          # the app prints it: no hint to the reader
        self.assertEqual(self.get("/api/export/held/" + held["token"])[0], 404)           # once
        for bad in ("0" * 32, "x", "../../etc/passwd", "a" * 31):
            self.assertEqual(self.get("/api/export/held/" + bad)[0], 404, bad)

    def test_the_held_page_needs_only_its_token_even_with_a_key(self):
        self.svc.api_key = "k"
        _, _, data = self.post(body(format="pdf", hold=True), headers={"Authorization": "Bearer k"})
        token = json.loads(data)["token"]
        self.assertEqual(self.get("/api/export/held/" + token)[0], 200)

    def test_a_phone_does_not_have_these_routes(self):
        self.assertFalse(remote_path_ok("/api/export"))
        self.assertFalse(remote_path_ok("/api/export/held/" + "a" * 32))


class HoldTest(unittest.TestCase):
    def test_held_pages_expire_and_are_few(self):
        api = export_api.ExportApi()
        now = [1000.0]
        with mock.patch.object(export_api.time, "monotonic", lambda: now[0]):
            tokens = [api.run(body(format="pdf", hold=True), True).token for _ in range(6)]
            alive = [t for t in tokens if api._held.get(t)]
            self.assertEqual(len(alive), export_api.HOLD_MAX)
            self.assertEqual(alive, tokens[-export_api.HOLD_MAX:])                         # the oldest were dropped
            now[0] += export_api.HOLD_SECONDS + 1
            self.assertIsNone(api.take(alive[0]))                                          # too late

    def test_only_this_pc_can_ask_for_a_held_page(self):
        api = export_api.ExportApi()
        with self.assertRaises(PermissionError):
            api.run(body(format="pdf", hold=True), False)
        self.assertEqual(api.run(body(format="pdf"), False).token, "")                    # a plain download is for anyone allowed in


def test_disposition_keeps_the_name_safe_in_the_header():
    text = export_api.disposition('Riunione "è" già;\r\nX: y.docx')
    assert "\r" not in text and "\n" not in text and 'filename="Riunione _' in text and "filename*=UTF-8''" in text
