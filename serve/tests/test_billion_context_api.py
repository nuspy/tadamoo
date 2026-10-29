"""The Billion Context setting over HTTP, and the hand-over of a streamed chat request in the server: a stand-in for the proxy
relays it back to the server, which must answer it once and never send it round again."""
import http.client
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from serve import billion_context as bc
from serve.tests import test_api_plans as base


class Relay(ThreadingHTTPServer):
    """What the proxy does: take /bili/<upstream>/<path> and post it to the upstream, then stream the answer back."""
    daemon_threads = True


def relay_handler(seen, status=200):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append({"path": self.path, "opt": self.headers.get(bc.OPT_HEADER), "hop": self.headers.get(bc.HOP_HEADER)})
            if status != 200:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            upstream = self.path[len("/bili/"):]
            req = urllib.request.Request(upstream, data=body, method="POST", headers={
                "Content-Type": "application/json", "X-Bili-Hop": "x", bc.HOP_HEADER: self.headers.get(bc.HOP_HEADER) or ""})
            with urllib.request.urlopen(req, timeout=20) as r:
                self.send_response(200)
                self.send_header("Content-Type", r.headers.get("Content-Type", "text/event-stream"))
                self.end_headers()
                while True:
                    block = r.read(4096)
                    if not block:
                        break
                    self.wfile.write(block)
    return H


class FakeManager:
    def __init__(self, port, server_port):
        self.port, self.server_port, self.failures = port, server_port, 0

    def target(self):
        return self.port

    def enter(self):
        pass

    def leave(self):
        pass

    def failed(self):
        self.failures += 1


class BillionContextApiTest(base.PlansApiBase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.data = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"BASALT_DATA": cls.data.name})
        cls.env.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.env.stop()
        cls.data.cleanup()

    def chat(self, headers=None, stream=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("POST", "/v1/chat/completions", json.dumps({"messages": [{"role": "user", "content": "hi"}], "stream": stream, "max_tokens": 8}),
                     {"Content-Type": "application/json", **(headers or {})})
        r = conn.getresponse()
        out = r.status, r.read().decode("utf-8")
        conn.close()
        return out

    def with_relay(self, status=200):
        seen = []
        srv = Relay(("127.0.0.1", 0), relay_handler(seen, status))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        fake = FakeManager(srv.server_address[1], self.port)
        p = mock.patch.object(bc, "_MANAGER", fake)
        p.start()
        self.addCleanup(p.stop)
        return seen, fake

    def test_the_setting_is_read_and_saved_and_a_bad_port_is_refused(self):
        code, v = self.call("GET", "/api/billion-context")
        self.assertEqual(code, 200)
        self.assertTrue(v["enabled"])
        self.assertEqual(v["port"], bc.DEFAULT_PORT)
        with mock.patch.object(bc.Manager, "apply"):
            code, v = self.call("POST", "/api/billion-context", {"enabled": False})
            self.assertEqual((code, v["enabled"]), (200, False))
            self.assertEqual(self.call("POST", "/api/billion-context", {"port": 5})[0], 400)
            self.assertEqual(self.call("POST", "/api/billion-context", {"enabled": True})[1]["enabled"], True)

    def test_a_request_that_asks_goes_through_the_proxy_once_and_comes_back_answered(self):
        seen, fake = self.with_relay()
        code, direct = self.chat()
        self.assertEqual(code, 200)
        seen.clear()
        code, out = self.chat({bc.OPT_HEADER: "1"})
        self.assertEqual(code, 200)
        self.assertEqual(len(seen), 1)                                              # handed over once; the inner request was not sent round again
        self.assertEqual(seen[0]["hop"], "1")                                      # the mark that stops the relayed request from being handed over again
        self.assertEqual(seen[0]["path"], f"/bili/http://127.0.0.1:{self.port}/v1/chat/completions")
        self.assertIn("[DONE]", out)
        self.assertEqual(json.loads(out.split("data: ")[1].strip())["object"], json.loads(direct.split("data: ")[1].strip())["object"])

    def test_a_request_that_does_not_ask_or_does_not_stream_never_goes_through(self):
        seen, fake = self.with_relay()
        self.assertEqual(self.chat()[0], 200)
        self.assertEqual(self.chat({bc.OPT_HEADER: "1"}, stream=False)[0], 200)
        self.assertEqual(seen, [])

    def test_a_proxy_that_refuses_leaves_the_request_to_be_answered_directly(self):
        seen, fake = self.with_relay(status=502)
        code, out = self.chat({bc.OPT_HEADER: "1"})
        self.assertEqual(code, 200)
        self.assertIn("[DONE]", out)
        self.assertEqual(len(seen), 1)

    def test_a_proxy_that_is_down_leaves_the_request_to_be_answered_directly(self):
        fake = FakeManager(1, self.port)                                           # nothing listens on port 1
        with mock.patch.object(bc, "_MANAGER", fake):
            code, out = self.chat({bc.OPT_HEADER: "1"})
        self.assertEqual(code, 200)
        self.assertIn("[DONE]", out)
        self.assertEqual(fake.failures, 1)
