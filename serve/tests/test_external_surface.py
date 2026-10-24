"""What another application can reach under /v1: the OpenAI and Anthropic interfaces of the model Basalt has loaded.

A route added by accident (a provider relay, an embeddings or files endpoint, a key store) fails here, so widening
the external surface is always a deliberate change to the lists below."""
import json
import re
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]

# the interfaces of the loaded model
LLM_ROUTES = {"/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens", "/v1/models"}
# control of the loaded model, for the server's own page
CONTROL_ROUTES = {"/v1/status", "/v1/vram"}
# prefixes of the above: one model's entry, and the refusal for stored responses; "/v1/" alone is the namespace test
PREFIXES = {"/v1/models/", "/v1/responses/", "/v1/"}
# routes outside /v1: the page, health, and the short forms of the model list and of the control routes
TOP_LEVEL = {"/", "/health", "/metrics", "/slots", "/props", "/models", "/models/", "/status"}

NOT_OFFERED = [
    "/v1/completions", "/v1/embeddings", "/v1/images/generations", "/v1/audio/transcriptions", "/v1/audio/speech",
    "/v1/files", "/v1/batches", "/v1/messages/batches", "/v1/assistants", "/v1/threads", "/v1/moderations",
    "/v1/fine_tuning/jobs", "/v1/providers", "/v1/routes", "/v1/keys", "/v1/aliases", "/v2/chat/completions",
    "/v1/load", "/v1/unload", "/load", "/unload",                        # loading and unloading belong to the page, not to clients
]


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


class ExternalSurfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok,
                          ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.svc.telemetry = _NoTelemetry()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    def test_the_handler_names_no_other_v1_route(self):
        source = (ROOT / "serve" / "handler.py").read_text(encoding="utf-8")
        named = set(re.findall(r'"(/v1/[A-Za-z0-9_/]*)"', source))
        self.assertEqual(sorted(named - LLM_ROUTES - CONTROL_ROUTES - PREFIXES), [])

    def test_the_handler_names_no_other_route_outside_the_page_and_the_api(self):
        source = (ROOT / "serve" / "handler.py").read_text(encoding="utf-8")
        named = {m for m in re.findall(r'"(/[A-Za-z0-9_./-]*)"', source) if not re.match(r"/(v1|api|web|fonts)(/|$)", m)}
        self.assertEqual(sorted(named - TOP_LEVEL), [])

    def test_routes_of_other_services_are_not_offered(self):
        for path in NOT_OFFERED:
            self.assertEqual(self.call("GET", path)[0], 404, f"GET {path}")
            self.assertEqual(self.call("POST", path, {})[0], 404, f"POST {path}")

    def test_the_three_interfaces_answer_for_the_loaded_model(self):
        ask = [{"role": "user", "content": "hi"}]
        code, body = self.call("POST", "/v1/chat/completions", {"messages": ask})
        self.assertEqual((code, body["choices"][0]["message"]["content"]), (200, "ok"))
        code, body = self.call("POST", "/v1/messages", {"max_tokens": 16, "messages": ask})
        self.assertEqual((code, body["content"][0]["text"]), (200, "ok"))
        code, body = self.call("POST", "/v1/responses", {"input": "hi"})
        self.assertEqual(code, 200)
        self.assertIn("ok", json.dumps(body))
        self.assertEqual(self.call("POST", "/v1/messages/count_tokens", {"messages": ask})[0], 200)
        code, body = self.call("GET", "/v1/models")
        self.assertEqual((code, len(body["data"])), (200, 1))

    def test_the_model_name_never_picks_another_backend(self):
        ask = [{"role": "user", "content": "hi"}]
        for name in ("gpt-4o", "claude-opus-5-5", "gemini-2.5-pro", "openrouter/anthropic/claude", "provider:other"):
            code, body = self.call("POST", "/v1/chat/completions", {"model": name, "messages": ask})
            self.assertEqual((code, body["choices"][0]["message"]["content"]), (200, "ok"), name)

    def test_stored_responses_are_not_kept(self):
        self.assertEqual(self.call("GET", "/v1/responses/resp_1")[0], 404)


if __name__ == "__main__":
    unittest.main()
