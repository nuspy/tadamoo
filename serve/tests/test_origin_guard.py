"""The origin guard: which Host names and Origins reach the server, as pure functions and through the running server (mock engine)."""
from __future__ import annotations

import base64
import contextlib
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve import origin_guard as g  # noqa: E402
from serve.config import build_parser, load_config_file  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PC = "pluto-max"


def refused(method="POST", path="/api/voice/service/call", host_rule=True, proven=False, allowed=(), **headers):
    found = g.refusal(method, path, {k.replace("_", "-"): v for k, v in headers.items()}, host_rule=host_rule, proven=proven,
                      allowed=allowed, own_name=PC)
    return None if found is None else found.code


class Matrix(unittest.TestCase):
    def test_host_names_that_no_other_site_can_point_here_pass(self):
        for host in ("127.0.0.1:7232", "127.0.0.1", "[::1]:7232", "[::1]", "::1", "192.168.1.20:7232", "100.84.1.2:7232", "[fe80::1%25eth0]:7232",
                     "localhost:7232", "LOCALHOST", "app.localhost:7232", "PLUTO-MAX:7232", "pluto-max.:7232", ""):
            self.assertIsNone(refused("GET", "/", Host=host), host)

    def test_a_dns_name_is_refused_unless_listed(self):
        for host in ("evil.example.com:7232", "box.lan:7232", "pluto-max.evil.example.com", "localhost.evil.example.com", "2130706433:7232"):
            self.assertEqual(refused("GET", "/api/logs", Host=host), "host_not_allowed", host)
        self.assertIsNone(refused("GET", "/api/logs", allowed=("mypc.tail1234.ts.net",), Host="mypc.tail1234.ts.net:7232"))
        self.assertIsNone(refused("GET", "/api/logs", host_rule=False, Host="evil.example.com"))      # the phones' listener, or a server with a key
        self.assertIsNone(refused("GET", "/api/logs", allowed=("*",), Host="box.lan:7232"))             # the person turned the Host rule off
        self.assertEqual(refused(allowed=("*",), Host="box.lan:7232", Origin="http://evil.example.com"), "cross_origin")   # not the Origin rule

    def test_cross_site_requests_are_refused(self):
        here = "127.0.0.1:7232"
        for origin in ("http://evil.example.com", "null", "http://127.0.0.1:8000", "http://localhost:3000",
                       "chrome-extension://abc", "file://"):
            self.assertEqual(refused(Host=here, Origin=origin), "cross_origin", origin)
            self.assertEqual(refused("GET", "/api/voice/gateway/ws", Host=here, Origin=origin), "cross_origin", origin)     # the WebSocket upgrade
        self.assertEqual(refused(Host=here, Sec_Fetch_Site="cross-site"), "cross_origin")
        self.assertEqual(refused("DELETE", "/api/x", Host=here, Sec_Fetch_Site="cross-site"), "cross_origin")

    def test_this_servers_own_pages_and_programs_pass(self):
        self.assertIsNone(refused(Host="127.0.0.1:7232"))                                        # curl, the apps, Python programs
        self.assertIsNone(refused(Host="127.0.0.1:7232", Origin="http://127.0.0.1:7232", Sec_Fetch_Site="same-origin"))
        self.assertIsNone(refused(Host="localhost:7232", Origin="http://127.0.0.1:7232"))       # the other loopback spelling, same port
        self.assertIsNone(refused(Host="[::1]:7232", Origin="http://[::1]:7232"))
        self.assertIsNone(refused(Host="pluto-max:7232", Origin="http://PLUTO-MAX:7232"))
        self.assertIsNone(refused(Host="192.168.1.20:7232", Origin="http://192.168.1.20:7232"))
        self.assertIsNone(refused(Host="mypc.ts.net", Origin="https://mypc.ts.net", allowed=("mypc.ts.net",)))
        self.assertIsNone(refused(Host="127.0.0.1:7232", Origin="https://mypc.ts.net", allowed=("mypc.ts.net",)))     # a proxy that rewrites Host
        self.assertIsNone(refused("GET", "/api/mcp/auth/callback", Host="127.0.0.1:7232", Sec_Fetch_Site="cross-site"))   # a sign-in coming back
        self.assertIsNone(refused("OPTIONS", "/api/x", Host="127.0.0.1:7232", Origin="http://evil.example.com"))

    def test_a_secret_in_a_header_and_the_open_api_skip_the_rules(self):
        self.assertIsNone(refused(Host="evil.example.com", Origin="http://evil.example.com", proven=True))
        for path in ("/v1/chat/completions", "/v1/models"):
            self.assertIsNone(refused("POST", path, Host="evil.example.com", Origin="http://evil.example.com", Sec_Fetch_Site="cross-site"))

    def test_allowed_host_names_are_bare_names(self):
        self.assertEqual(g.check_hostname(" MyPC.tail1234.ts.net. "), "mypc.tail1234.ts.net")
        self.assertEqual(g.check_hostname("*"), "*")
        for bad in ("http://mypc", "mypc:7232", "mypc/x", "*.ts.net", "", "1.2.3.4", "::1", "-bad.example", "a b", 5, "a" * 64 + ".com"):
            with self.assertRaises(ValueError, msg=bad):
                g.check_hostname(bad)

    def test_the_config_file_takes_a_list_of_names(self):
        ap = build_parser()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "config.json"
            tmp.write_text(json.dumps({"server": {"allowed_hosts": ["mypc.tail1234.ts.net", "PLUTO-MAX.lan"]}}), encoding="utf-8")
            argv, _ = load_config_file(str(tmp), ap)
            self.assertEqual(ap.parse_args(argv).allowed_hosts, ["mypc.tail1234.ts.net", "pluto-max.lan"])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ap.parse_args(["--allowed-hosts", "http://mypc/"])


class OverHttp(unittest.TestCase):
    """The running server: the guard answers before any route, for every method and for the WebSocket upgrade."""

    def setUp(self):
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=4096), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]
        self.here = f"127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                c.request(method, path, body=body, headers=headers or {})
                r = c.getresponse()
                raw = r.read()
            return r.status, json.loads(raw) if raw[:1] == b"{" else raw
        finally:
            c.close()

    def test_rebinding_and_cross_site_writes_are_refused_before_the_route(self):
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            code, body = self.req(method, "/api/voice/models-dir", {"Host": f"rebind.evil.example.com:{self.port}"}, b"{}")
            self.assertEqual((code, body["error"]["code"]), (403, "host_not_allowed"), method)
        code, body = self.req("POST", "/api/voice/models-dir", {"Content-Type": "text/plain", "Origin": "http://evil.example.com"}, b'{"path": "C:/x"}')
        self.assertEqual((code, body["error"]["code"]), (403, "cross_origin"))
        code, body = self.req("POST", "/api/telegram/settings", {"Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"}, b"{}")
        self.assertEqual((code, body["error"]["code"]), (403, "cross_origin"))

    def test_a_foreign_websocket_origin_is_refused_at_the_upgrade(self):
        ws = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode()}
        code, body = self.req("GET", "/api/voice/gateway/ws", {**ws, "Origin": "https://evil.example.com"})
        self.assertEqual((code, body["error"]["code"]), (403, "cross_origin"))

    def test_the_page_programs_ip_names_and_the_open_api_still_work(self):
        self.assertEqual(self.req("GET", "/health")[0], 200)
        self.assertEqual(self.req("GET", "/health", {"Host": f"[::1]:{self.port}"})[0], 200)
        self.assertEqual(self.req("GET", "/health", {"Host": f"192.168.1.20:{self.port}"})[0], 200)
        self.assertEqual(self.req("GET", "/health", {"Host": f"localhost:{self.port}"})[0], 200)
        self.assertEqual(self.req("GET", "/health", {"Host": f"{socket.gethostname()}:{self.port}"})[0], 200)
        self.assertEqual(self.req("GET", "/api/logs", {"Origin": f"http://{self.here}", "Sec-Fetch-Site": "same-origin"})[0], 200)
        chat = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}).encode()
        code, body = self.req("POST", "/v1/chat/completions", {"Host": "evil.example.com", "Origin": "http://evil.example.com",
                                                               "Content-Type": "application/json"}, chat)
        self.assertEqual(code, 200)
        self.svc.allowed_hosts = ("mypc.tail1234.ts.net",)
        self.assertEqual(self.req("GET", "/api/logs", {"Host": f"mypc.tail1234.ts.net:{self.port}",
                                                       "Origin": f"http://mypc.tail1234.ts.net:{self.port}"})[0], 200)

    def test_with_an_api_key_the_name_is_free_and_the_key_proves_the_request(self):
        self.svc.api_key = "s3cret"
        self.assertEqual(self.req("GET", "/health", {"Host": "random-words.trycloudflare.com"})[0], 200)
        self.assertEqual(self.req("GET", "/api/logs", {"Host": "random-words.trycloudflare.com", "Origin": "https://evil.example.com"})[0], 403)
        self.assertEqual(self.req("GET", "/api/logs", {"Host": "random-words.trycloudflare.com", "Origin": "https://evil.example.com",
                                                       "Authorization": "Bearer s3cret"})[0], 200)


if __name__ == "__main__":
    unittest.main()
