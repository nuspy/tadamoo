"""Other programs that call this server through the OpenAI or Anthropic interface, with a provider chosen for the function `external`."""
import http.client
import http.server
import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from serve import external_forward
from serve.tests.test_vram_api import Server
from tadamoo import llm
from tadamoo.llm import Ledger
from tadamoo.llm.credentials import FileCredentials
from tadamoo.llm.store import SecureRegistry

CALL = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": json.dumps({"city": "Pécs"})}}


class Provider:
    """A provider on a port of this PC, OpenAI-style at /v1/chat/completions and Anthropic-style at /v1/messages; keeps what it was sent."""

    def __init__(self, fail=False, tools=False):
        self.seen, self.fail, self.tools = [], fail, tools
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                provider.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if provider.fail:
                    return self._send(500, "application/json", json.dumps({"error": {"message": "down"}}).encode())
                if self.path.endswith("/messages"):
                    return self._send(200, "application/json", json.dumps({"content": [{"type": "text", "text": "Ciao"}], "stop_reason": "end_turn",
                                                                           "usage": {"input_tokens": 3, "output_tokens": 4}}).encode())
                if body.get("stream"):
                    raw = (b'data: {"choices": [{"delta": {"content": "Ci"}}]}\n\ndata: {"choices": [{"delta": {"content": "ao"}}]}\n\n'
                           b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
                           b'data: {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}\n\ndata: [DONE]\n\n')
                    return self._send(200, "text/event-stream", raw)
                message = {"role": "assistant", "content": None, "tool_calls": [CALL]} if provider.tools else {"role": "assistant", "content": "Ciao"}
                raw = json.dumps({"choices": [{"message": message, "finish_reason": "tool_calls" if provider.tools else "stop"}],
                                  "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}).encode()
                self._send(200, "application/json", raw)

            def _send(self, code, ctype, raw):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class OtherPrograms(Server):
    kind = "openai_compat"

    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"TADAMOO_CREDENTIALS": "file"})
        env.start()
        self.addCleanup(env.stop)
        self.provider = Provider()
        self.addCleanup(self.provider.httpd.shutdown)
        self.registry = SecureRegistry(base / "llm.json", credentials=FileCredentials(base / "creds.json"))
        patch = mock.patch.object(llm, "_registry", self.registry)
        patch.start()
        self.addCleanup(patch.stop)
        self.cloud = self.registry.add({"type": self.kind, "name": "Cloud", "base_url": self.provider.url, "model": "glm-x", "api_key": "k-123456789012"})
        self.chat = {"model": "whatever", "messages": [{"role": "user", "content": "ciao"}], "max_tokens": 50}
        self.anthropic = {"model": "claude-x", "max_tokens": 50, "system": "Be brief.", "messages": [{"role": "user", "content": "ciao"}]}

    def choose(self):
        self.registry.assign("external", self.cloud["id"])

    def stream(self, path, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request("POST", path, body=json.dumps({**body, "stream": True}), headers={"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        return r.status, text


class WithoutAChoice(OtherPrograms):
    def test_the_loaded_model_answers_and_the_provider_is_not_asked(self):
        code, out = self.req("POST", "/v1/chat/completions", self.chat)
        self.assertEqual(code, 200)
        self.assertEqual(self.provider.seen, [])

    def test_the_function_does_not_follow_the_primary_model_nor_a_swap(self):
        self.registry.set_primary(self.cloud["id"], True)
        self.assertEqual(self.registry.resolve_route("chat")[0], self.cloud["id"])
        self.assertEqual(self.registry.resolve_route("external")[0], None)
        self.registry.begin_swap(self.cloud["id"])
        self.assertEqual(self.registry.resolve_route("external")[0], None)
        self.assertEqual(self.registry.fallback_target("external"), self.cloud["id"])       # a fallback exists for the others...
        self.assertEqual(self.registry.resolve_route("external", False)[0], None)           # ...and this one stays on the loaded model


class OpenAIInterface(OtherPrograms):
    def test_the_request_goes_to_the_provider_with_its_key_and_model_and_the_answer_comes_back(self):
        self.choose()
        code, out = self.req("POST", "/v1/chat/completions", self.chat)
        self.assertEqual((code, out["choices"][0]["message"]["content"], out["usage"]["total_tokens"]), (200, "Ciao", 7))
        self.assertEqual(out["model"], "whatever")
        path, headers, sent = self.provider.seen[0]
        self.assertEqual((path, headers["authorization"], sent["model"], sent["messages"]), ("/v1/chat/completions", "Bearer k-123456789012", "glm-x", self.chat["messages"]))
        self.assertEqual(Ledger().used(self.cloud["id"]), 7)

    def test_a_stream_arrives_in_pieces_with_the_usage_and_the_end(self):
        self.choose()
        code, text = self.stream("/v1/chat/completions", self.chat)
        self.assertEqual(code, 200)
        events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and "[DONE]" not in line]
        self.assertEqual([e["choices"][0]["delta"].get("content") for e in events if e["choices"]][:2], ["Ci", "ao"])
        self.assertEqual(events[-1]["usage"]["total_tokens"], 5)
        self.assertTrue(text.rstrip().endswith("[DONE]"))

    def test_the_pieces_of_tool_calls_come_back_whole(self):
        self.provider.tools = True
        self.choose()
        code, out = self.req("POST", "/v1/chat/completions", {**self.chat, "tools": [{"type": "function", "function": {"name": "get_weather"}}]})
        self.assertEqual(out["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")

    def test_a_provider_that_fails_gives_its_error_and_never_the_loaded_models_answer(self):
        self.provider.fail = True
        self.choose()
        code, out = self.req("POST", "/v1/chat/completions", self.chat)
        self.assertEqual(code, 500)
        self.assertIn("down", out["error"]["message"])

    def test_the_models_listed_are_the_providers(self):
        self.choose()
        code, out = self.req("GET", "/v1/models")
        self.assertEqual((code, [m["id"] for m in out["data"]]), (200, ["glm-x"]))
        code, out = self.req("GET", "/v1/models", headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(out["data"][0]["type"], "model")

    def test_responses_says_it_is_answered_only_by_the_loaded_model(self):
        self.choose()
        code, out = self.req("POST", "/v1/responses", {"model": "x", "input": "hi"})
        self.assertEqual(code, 501)
        self.assertIn("Cloud", out["error"]["message"])
        self.assertEqual(self.provider.seen, [])


class TaDaMoosOwnPieces(OtherPrograms):
    def test_the_chat_page_the_voice_service_and_its_own_clients_still_meet_the_loaded_model(self):
        self.choose()
        for headers in ({"X-Tadamoo-Function": "chat"}, {"User-Agent": "TaDaMoo/1.0"}, {"User-Agent": "TaDaMoo-bench"}, {"X-Tadamoo-Compress": "1"}):
            code, out = self.req("POST", "/v1/chat/completions", self.chat, headers)
            self.assertEqual(code, 200, headers)
        self.assertEqual(self.provider.seen, [])


class AnthropicInterface(OtherPrograms):
    def test_a_conversation_with_tools_is_carried_in_the_providers_dialect_and_answered_in_anthropics(self):
        self.choose()
        body = {**self.anthropic, "tools": [{"name": "get_weather", "description": "Weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
                "tool_choice": {"type": "any"}, "stop_sequences": ["END"],
                "messages": [{"role": "user", "content": "weather?"},
                             {"role": "assistant", "content": [{"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Pécs"}}]},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": [{"type": "text", "text": "12 C"}]}, {"type": "text", "text": "and now?"}]}]}
        code, out = self.req("POST", "/v1/messages?beta=true", body, {"anthropic-version": "2023-06-01", "x-api-key": "unused"})
        self.assertEqual((code, out["type"], out["content"], out["stop_reason"]), (200, "message", [{"type": "text", "text": "Ciao"}], "end_turn"))
        self.assertEqual(out["usage"], {"input_tokens": 3, "output_tokens": 4})
        sent = self.provider.seen[0][2]
        self.assertEqual([m["role"] for m in sent["messages"]], ["system", "user", "assistant", "tool", "user"])
        self.assertEqual(sent["messages"][2]["tool_calls"][0]["function"], {"name": "get_weather", "arguments": json.dumps({"city": "Pécs"})})
        self.assertEqual(sent["messages"][3], {"role": "tool", "tool_call_id": "call_1", "content": "12 C"})
        self.assertEqual((sent["tools"][0]["function"]["name"], sent["tool_choice"], sent["stop"]), ("get_weather", "required", ["END"]))

    def test_a_tool_use_of_the_provider_becomes_a_tool_use_block(self):
        self.provider.tools = True
        self.choose()
        code, out = self.req("POST", "/v1/messages", {**self.anthropic, "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}]})
        self.assertEqual(out["stop_reason"], "tool_use")
        self.assertEqual(out["content"][0], {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Pécs"}})

    def test_a_stream_is_in_anthropics_events(self):
        self.choose()
        code, text = self.stream("/v1/messages", self.anthropic, {"anthropic-version": "2023-06-01"})
        names = [line[7:] for line in text.splitlines() if line.startswith("event: ")]
        self.assertEqual(names, ["message_start", "content_block_start", "content_block_delta", "content_block_delta", "content_block_stop", "message_delta", "message_stop"])
        self.assertIn('"text": "Ci"', text)

    def test_count_tokens_is_an_estimate_and_asks_nobody(self):
        self.choose()
        code, out = self.req("POST", "/v1/messages/count_tokens", self.anthropic)
        self.assertEqual(code, 200)
        self.assertGreater(out["input_tokens"], 0)
        self.assertEqual(self.provider.seen, [])


class AnAnthropicProvider(OtherPrograms):
    kind = "anthropic_compat"

    def test_an_openai_program_is_answered_through_an_anthropic_provider(self):
        self.choose()
        code, out = self.req("POST", "/v1/chat/completions", {**self.chat, "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "ciao"}]})
        self.assertEqual((code, out["choices"][0]["message"]["content"], out["usage"]["total_tokens"]), (200, "Ciao", 7))
        path, headers, sent = self.provider.seen[0]
        self.assertEqual((path, headers["x-api-key"], sent["model"]), ("/v1/messages", "k-123456789012", "glm-x"))
        self.assertIn("Be brief.", json.dumps(sent["system"]))


class Units(unittest.TestCase):
    def test_what_is_internal(self):
        self.assertTrue(external_forward.is_internal({"User-Agent": "TaDaMoo/1.0"}))
        self.assertTrue(external_forward.is_internal({"X-Tadamoo-Function": "voice"}))
        self.assertFalse(external_forward.is_internal({"User-Agent": "claude-cli/1.0", "x-api-key": "k"}))

    def test_an_image_block_becomes_an_image_url(self):
        out = external_forward.anthropic_messages({"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
                                                                                              {"type": "text", "text": "what?"}]}]})
        self.assertEqual(out[0]["content"], [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}, {"type": "text", "text": "what?"}])


if __name__ == "__main__":
    unittest.main()
