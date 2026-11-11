"""The Chat page while a heavy job has the local model out: its request goes to the swap's provider, as the page's own and nobody else's."""
import http.client
import http.server
import json
import threading
import unittest
from pathlib import Path

from serve.heavy_swap import HeavyPrefs, HeavySwap
from serve.tests.test_vram_api import Server
from tadamoo.llm import Ledger
from tadamoo.llm.credentials import FileCredentials
from tadamoo.llm.store import SecureRegistry


class Provider:
    """An OpenAI-style provider on a port of this PC; keeps what it was sent."""

    def __init__(self):
        self.seen = []
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                provider.seen.append((self.path, self.headers.get("Authorization"), body))
                if body.get("stream"):
                    raw = (b'data: {"choices": [{"delta": {"content": "Ci"}}]}\n\n' b'data: {"choices": [], "usage": {"total_tokens": 42}}\n\n' b"data: [DONE]\n\n")
                    ctype = "text/event-stream"
                else:
                    raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Ciao"}}], "usage": {"total_tokens": 7}}).encode()
                    ctype = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class ChatThroughTheSwap(Server):
    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name)
        self.provider = Provider()
        self.addCleanup(self.provider.httpd.shutdown)
        self.registry = SecureRegistry(base / "llm.json", credentials=FileCredentials(base / "creds.json"))
        self.cloud = self.registry.add({"type": "openai_compat", "name": "Cloud", "base_url": self.provider.url, "model": "glm-x", "api_key": "k-123456789012"})
        self.svc.heavy_swap = HeavySwap(object(), self.registry, HeavyPrefs(base / "heavy.json"), holders=lambda: [])
        self.body = {"messages": [{"role": "user", "content": "ciao"}], "max_tokens": 50, "long_answer": {"x": 1}, "model": "local"}
        self.ui = {"X-Tadamoo-Function": "chat"}

    def test_without_a_swap_the_request_is_answered_by_the_local_model_as_before(self):
        code, out = self.req("POST", "/v1/chat/completions", {"messages": self.body["messages"], "max_tokens": 5}, self.ui)
        self.assertEqual(code, 200)
        self.assertEqual(self.provider.seen, [])

    def test_during_a_swap_the_pages_request_goes_to_the_provider_with_its_key_and_model_and_only_what_it_takes(self):
        self.registry.begin_swap(self.cloud["id"])
        code, out = self.req("POST", "/v1/chat/completions", self.body, self.ui)
        self.assertEqual((code, out["choices"][0]["message"]["content"]), (200, "Ciao"))
        path, auth, sent = self.provider.seen[0]
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer k-123456789012"))
        self.assertEqual(sent, {"messages": self.body["messages"], "max_tokens": 50, "model": "glm-x"})
        self.assertEqual(Ledger().used(self.cloud["id"]), 7)

    def test_a_stream_is_handed_back_whole_and_its_tokens_are_counted(self):
        self.registry.begin_swap(self.cloud["id"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request("POST", "/v1/chat/completions", body=json.dumps({**self.body, "stream": True}), headers={"Content-Type": "application/json", **self.ui})
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        self.assertEqual(r.status, 200)
        self.assertIn('"Ci"', text)
        self.assertIn("[DONE]", text)
        self.assertEqual(Ledger().used(self.cloud["id"]), 42)

    def test_a_request_without_the_pages_header_still_meets_the_local_model(self):
        self.registry.begin_swap(self.cloud["id"])
        code, out = self.req("POST", "/v1/chat/completions", {"messages": self.body["messages"], "max_tokens": 5})
        self.assertEqual(code, 200)
        self.assertEqual(self.provider.seen, [])


if __name__ == "__main__":
    unittest.main()
