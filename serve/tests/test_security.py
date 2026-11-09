"""Which Host names and Origins the server answers (serve/origin_guard.py), and the API key and the control secret, against the mock engine."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve.frontend import ChatTemplate  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.security import api_key_of, key_matches  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


class OverHttp(unittest.TestCase):
    """The running server, as a browser and as curl/the SDKs send their requests."""

    def setUp(self):
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=4096), tok,
                           ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.control_token = "control-secret"
        self.httpd = None

    def start(self, **attrs):
        for k, v in attrs.items():
            setattr(self.svc, k, v)
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def req(self, method, path, body=None, headers=None, host=None):
        """(status, JSON, what the server printed, response headers); `host` replaces the Host header http.client sends."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"X-Tadamoo-Control": self.svc.control_token, **(headers or {})} if path.startswith("/api/") else dict(headers or {})
        if host is not None:
            h["Host"] = host
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                c.request(method, path, body=data, headers=h)
                r = c.getresponse()
                raw = r.read()
            return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw, out.getvalue(), r.headers
        finally:
            c.close()

    def chat_body(self):
        return {"model": "qwen3.8-flash-next", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}

    # --- Host
    def test_ip_addresses_and_localhost_are_answered_and_dns_names_are_not(self):
        # Without an API key a page of any site could point a DNS name of its own at 127.0.0.1 (DNS rebinding) and read and write as if it were
        # this server's page. An IP address cannot be rebound, so devices on the network that use the PC's address (or its own name, or a name
        # in server.allowed-hosts) keep working; the open API /v1/* is left to its CORS policy, which already lets every page use it.
        self.start()
        for method, path in (("GET", "/status"), ("GET", "/metrics"), ("GET", "/"), ("GET", "/health"),
                             ("GET", "/api/logs"), ("POST", "/v1/chat/completions"), ("OPTIONS", "/v1/models")):
            for host in (f"192.168.1.20:{self.port}", f"[::1]:{self.port}", f"localhost:{self.port}"):
                code = self.req(method, path, self.chat_body() if method == "POST" else None,
                                {"Content-Type": "application/json"}, host=host)[0]
                self.assertIn(code, (200, 204), (path, host))
            for host in (f"evil.example.com:{self.port}", f"box.lan:{self.port}"):
                code = self.req(method, path, self.chat_body() if method == "POST" else None,
                                {"Content-Type": "application/json"}, host=host)[0]
                proven = path.startswith("/api/")                               # req() sends the control secret in its header there
                self.assertIn(code, (200, 204) if path.startswith("/v1/") or proven else (403,), (path, host))

    def test_no_host_header_passes(self):
        self.start()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:      # an HTTP/1.0 client
            s.sendall(b"GET /health HTTP/1.0\r\n\r\n")
            raw = b""
            while chunk := s.recv(65536):
                raw += chunk
        self.assertTrue(raw.startswith(b"HTTP/1.0 200"), raw[:40])

    def test_with_a_key_the_key_decides_whatever_the_name(self):
        # every route but the open ones needs the key, which a rebound page does not have: a tunnel's name is answered
        self.start(api_key="s3cret")
        tunnel = "random-words.trycloudflare.com"
        self.assertEqual(self.req("GET", "/status", host=tunnel)[0], 401)
        self.assertEqual(self.req("GET", "/status", headers={"Authorization": "Bearer s3cret"}, host=tunnel)[0], 200)
        self.assertEqual(self.req("GET", "/health", host=tunnel)[0], 200)

    # --- Origin and CORS
    def test_any_content_type_and_any_origin_use_the_model(self):
        self.start()
        for origin in (None, "http://evil.example.com", "null", f"http://127.0.0.1:{self.port}", "chrome-extension://abcdef"):
            for ctype in ("application/json", "text/plain", None):              # curl -d sends a form type
                headers = {k: v for k, v in (("Content-Type", ctype), ("Origin", origin)) if v}
                for path in ("/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens"):
                    code, body, _, _ = self.req("POST", path, self.chat_body(), headers)
                    self.assertEqual(code, 200, (origin, ctype, path))
                    if path == "/v1/chat/completions":
                        self.assertEqual(body["choices"][0]["message"]["content"], "ok")

    def test_the_models_answer_carries_cors_headers_for_every_page_and_the_preflight_too(self):
        self.start()
        code, _, _, headers = self.req("GET", "/v1/models", headers={"Origin": "http://evil.example.com"})
        self.assertEqual((code, headers["Access-Control-Allow-Origin"]), (200, "*"))
        code, _, _, headers = self.req("OPTIONS", "/v1/chat/completions", headers={
            "Origin": "https://chat.example.com", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
        self.assertEqual((code, headers["Access-Control-Allow-Origin"], headers["Access-Control-Allow-Headers"]), (204, "*", "authorization,content-type"))
        self.assertIsNone(self.req("GET", "/status", headers={"Origin": "http://evil.example.com"})[3]["Access-Control-Allow-Origin"])    # only /v1/*

    def test_with_a_key_the_key_decides(self):
        self.start(api_key="s3cret")
        headers = {"Content-Type": "text/plain", "Origin": "http://evil.example.com"}
        self.assertEqual(self.req("POST", "/v1/chat/completions", self.chat_body(), headers)[0], 401)
        self.assertEqual(self.req("POST", "/v1/chat/completions", self.chat_body(),
                                  {**headers, "Authorization": "Bearer s3cret"})[0], 200)

    # --- /unload and /load
    def test_control_body_arrives_before_the_operation_and_reply(self):
        self.start()
        for body, expected in ((b"{}", 200), (b"{", 400)):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                with mock.patch.object(self.svc, "unload", return_value="unloaded") as unload:
                    c.putrequest("POST", "/api/control/unload")
                    c.putheader("X-Tadamoo-Control", self.svc.control_token)
                    c.putheader("Content-Type", "application/json")
                    c.putheader("Content-Length", "2")
                    c.endheaders()
                    c.sock.settimeout(0.1)
                    with self.assertRaises(socket.timeout):
                        c.sock.recv(1)
                    unload.assert_not_called()
                    c.sock.settimeout(5)
                    c.send(body)
                    if len(body) < 2:
                        c.sock.shutdown(socket.SHUT_WR)
                    r = c.getresponse()
                    self.assertEqual(r.status, expected)
                    r.read()
                    self.assertEqual(unload.call_count, int(expected == 200))
            finally:
                c.close()

    def test_unload_and_load_need_the_control_secret_in_the_header_whatever_the_page(self):
        # the secret in X-Tadamoo-Control is a header no page of another site can send, so it proves the request whatever its Origin
        self.start()
        for path in ("/api/control/unload", "/api/control/load"):
            self.assertEqual(self.req("POST", path, {}, {"Content-Type": "application/json", "X-Tadamoo-Control": "wrong"})[0], 403, path)
            for headers in ({"Content-Type": "application/x-www-form-urlencoded"}, {}, {"Content-Type": "application/json", "Origin": "http://evil.example.com"}):
                self.assertEqual(self.req("POST", path, b"a=1", headers)[0], 200, (path, headers))


class ApiKeyForms(unittest.TestCase):
    """A key no client could send (spaces or a line end around it), and a key outside ASCII."""

    def test_the_key_loses_what_a_header_cannot_carry(self):
        self.assertEqual(api_key_of(" s3cret "), "s3cret")
        self.assertEqual(api_key_of("s3cret\r\n"), "s3cret")
        self.assertEqual(api_key_of("two words"), "two words")
        self.assertEqual(api_key_of(12345), "12345")
        self.assertEqual(api_key_of(""), "")
        self.assertEqual(api_key_of(None), "")

    def test_a_blank_key_is_an_error_not_no_key(self):
        for blank in (" ", "\r\n", "\t "):
            with self.assertRaises(ValueError):
                api_key_of(blank)

    def test_a_key_outside_ascii_matches_as_utf8_and_as_latin1(self):
        def as_read(key, enc):                                    # what http.server hands the handler
            return key.encode(enc).decode("latin-1")
        self.assertTrue(key_matches("s3cret", "s3cret"))
        self.assertFalse(key_matches("s3cret ", "s3cret"))
        self.assertFalse(key_matches("", "s3cret"))
        self.assertTrue(key_matches(as_read("clé", "utf-8"), "clé"))
        self.assertTrue(key_matches(as_read("clé", "latin-1"), "clé"))
        self.assertTrue(key_matches(as_read("ключ", "utf-8"), "ключ"))
        self.assertFalse(key_matches(as_read("ключ", "utf-8"), "ключx"))

    def test_a_utf8_key_over_http(self):
        tok = ByteTokenizer()
        svc = Service(MockEngine(tok, "ok", max_context=4096), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        svc.api_key = "ключ"
        httpd = serve(svc, port=0)
        port = httpd.server_address[1]
        try:
            for token, want in (("Bearer " + svc.api_key, 200), ("Bearer ???", 401)):
                sent = token.encode().decode("latin-1")             # UTF-8 bytes, as curl and the web app send
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    conn.request("GET", "/v1/models", headers={"Authorization": sent.encode("latin-1")})
                    self.assertEqual(conn.getresponse().status, want)
                finally:
                    conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
