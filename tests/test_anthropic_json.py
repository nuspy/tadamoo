"""A JSON answer on the Anthropic route is kept: the harness's client asks for it in Anthropic's words, and the server's /v1/messages returns it.

The defect: a call with a JSON `response_format` sent to an Anthropic-dialect provider (a Claude key, or another Basalt reached through
/v1/messages) lost the form, because the dialect has no `response_format`: the request carried nothing that asked for JSON, so the
engine had no grammar and the model wrote prose or a fenced block. Reproduced here against the real server with the scripted engine."""
import contextlib
import io
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.llm.client import RoutedClient, json_in, with_json_format
from harness.llm.credentials import FileCredentials
from harness.llm.store import Ledger, SecureRegistry
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

TEMPLATE = Path(__file__).resolve().parents[1] / "serve" / "chat_template.jinja"
ANSWER = {"a": 1, "b": "x"}
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "string"}}, "required": ["a", "b"], "additionalProperties": False}
FORMAT = {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": SCHEMA}}
ASK = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "give the json"}]


class Basalt:
    """The real server on a free port with a scripted engine, quiet."""

    def __init__(self, script):
        self.tok = ByteTokenizer()
        self.engine = MockEngine(self.tok, script, max_context=16384)
        self.svc = Service(self.engine, self.tok, ChatTemplate(TEMPLATE), grammar=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.httpd = serve(self.svc, port=0)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def post(self, body):
        req = urllib.request.Request(self.url + "/messages", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with contextlib.redirect_stdout(io.StringIO()), urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def basalt():
    made = []

    def make(script):
        made.append(Basalt(script))
        return made[-1]
    yield make
    for b in made:
        b.close()


def routed(tmp_path, url):
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    reg.local_hosts = ()
    pid = reg.add({"type": "claude", "base_url": url, "api_key": "k" * 12, "model": "qwen3.8-flash-next"})["id"]
    reg.assign_checked("eval", pid)
    return RoutedClient("eval", reg, object(), Ledger(tmp_path / "u.json"))


def ask(client, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return client.chat_completion(ASK, max_tokens=300, **kw)["choices"][0]["message"]["content"]


def test_the_server_returns_the_json_of_a_messages_request_blocks_and_stream(basalt):
    b = basalt("reason</think>\n" + json.dumps(ANSWER))
    body = {"model": "qwen3.8-flash-next", "max_tokens": 300, "messages": [{"role": "user", "content": "json"}],
            "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}}
    code, raw = b.post(body)
    assert code == 200
    blocks = json.loads(raw)["content"]
    assert [x["type"] for x in blocks] == ["thinking", "text"] and json.loads(blocks[-1]["text"]) == ANSWER
    assert b.engine.requests[-1][1]["_grammar"]["kind"] == "json_schema"                       # the engine got the grammar from output_config.format
    code, raw = b.post({**body, "stream": True})
    pieces = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: {")]
    text = "".join(p["delta"].get("text", "") for p in pieces if p["type"] == "content_block_delta")
    assert code == 200 and json.loads(text) == ANSWER and pieces[-2]["delta"]["stop_reason"] == "end_turn"


def test_the_server_keeps_the_json_when_the_model_ends_while_thinking(basalt):
    b = basalt(json.dumps(ANSWER))                               # no </think>: the whole answer arrives as thinking, and the server closes it for an answer
    code, raw = b.post({"model": "qwen3.8-flash-next", "max_tokens": 300, "messages": [{"role": "user", "content": "json"}],
                        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}})
    blocks = json.loads(raw)["content"]
    assert code == 200 and json.loads(next(x["text"] for x in blocks if x["type"] == "text")) == ANSWER


def test_a_json_form_asked_of_an_anthropic_route_reaches_the_engine_as_a_grammar(basalt, tmp_path):
    b = basalt(json.dumps(ANSWER))
    out = ask(routed(tmp_path, b.url), response_format=FORMAT)
    assert json.loads(out) == ANSWER
    assert b.engine.requests[-1][1]["_grammar"] == {"kind": "json_schema", "schema": SCHEMA}


def test_the_same_through_the_stream_callback(basalt, tmp_path):
    b = basalt("reason</think>\n" + json.dumps(ANSWER))
    pieces = []
    out = ask(routed(tmp_path, b.url), response_format=FORMAT, on_delta=lambda k, t: pieces.append((k, t)))
    assert json.loads(out) == ANSWER and json.loads("".join(t for k, t in pieces if k == "content")) == ANSWER


def test_a_bare_json_object_becomes_an_instruction_and_a_fenced_answer_is_unwrapped(basalt, tmp_path):
    b = basalt("</think>\nHere you are:\n```json\n" + json.dumps(ANSWER) + "\n```\nAnything else?")
    out = ask(routed(tmp_path, b.url), response_format={"type": "json_object"})
    assert json.loads(out) == ANSWER
    assert "Answer with one JSON value only" in b.tok.decode(b.engine.last_prompt)               # the instruction is in the system text
    assert "_grammar" not in b.engine.requests[-1][1] or not b.engine.requests[-1][1]["_grammar"]


def test_without_a_json_form_the_text_is_left_alone(basalt, tmp_path):
    b = basalt("</think>\n```json\n" + json.dumps(ANSWER) + "\n```")
    assert ask(routed(tmp_path, b.url)).startswith("```json")


def test_the_request_shape():
    body = with_json_format({"model": "m", "system": "S", "messages": []}, {"schema": SCHEMA})
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}} and body["system"] == "S"
    plain = with_json_format({"model": "m", "system": [{"type": "text", "text": "S"}], "messages": []}, {"schema": None})
    assert "output_config" not in plain and plain["system"][-1]["text"].startswith("Answer with one JSON")
    assert with_json_format({"messages": []}, {"schema": None})["system"].startswith("Answer with one JSON")


def test_json_is_found_in_prose_a_fence_or_alone_and_not_invented():
    assert json_in('{"a": 1}') == '{"a": 1}'
    assert json_in('Sure: {"a": [1, 2]} done') == '{"a": [1, 2]}'
    assert json_in('```JSON\n{"a": 1}\n```') == '{"a": 1}'
    assert json_in("no json [brackets] here") is None
    assert json_in("") is None


class Cloud:
    """A provider that answers like Anthropic, but does not know `output_config` (400), to see the fallback."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                if "output_config" in body:
                    code, out = 400, {"error": {"message": "output_config: unknown field"}}
                else:
                    code, out = 200, {"content": [{"type": "text", "text": 'Result: {"a": 2}'}], "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 4}}
                raw = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_a_provider_that_refuses_the_form_gets_the_instruction_and_the_json_is_still_kept(tmp_path):
    cloud = Cloud()
    try:
        out = ask(routed(tmp_path, cloud.url), response_format=FORMAT)
    finally:
        cloud.close()
    assert json.loads(out) == {"a": 2}
    assert len(cloud.requests) == 2 and "output_config" in cloud.requests[0] and "output_config" not in cloud.requests[1]
    system = cloud.requests[1]["system"]
    assert (system[-1]["text"] if isinstance(system, list) else system).endswith("no code fence.")


def test_tools_and_a_json_form_are_not_mixed_on_this_route(tmp_path):
    cloud = Cloud()
    try:
        tools = [{"type": "function", "function": {"name": "t", "description": "", "parameters": {"type": "object", "properties": {}}}}]
        ask(routed(tmp_path, cloud.url), response_format=FORMAT, tools=tools)
    finally:
        cloud.close()
    assert len(cloud.requests) == 1 and "output_config" not in cloud.requests[0]
