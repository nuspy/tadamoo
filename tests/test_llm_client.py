"""A function's client: it speaks the dialect of the provider it is pointed at, and holds back what must not leave this PC."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.client import BasaltError
from harness.llm.client import RoutedClient, from_anthropic, to_anthropic
from harness.llm.credentials import FileCredentials
from harness.llm.store import Ledger, SecureRegistry


class Cloud:
    """A provider's endpoint on this PC: it records every request and answers as OpenAI or Anthropic would."""

    def __init__(self):
        self.requests, self.status = [], 200
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if outer.status != 200:
                    raw = json.dumps({"error": {"message": "provider says no"}}).encode()
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path.endswith("/messages"):
                    raw = json.dumps({"content": [{"type": "text", "text": "Ciao da Claude"},
                                                  {"type": "tool_use", "id": "tu1", "name": "plan_list", "input": {"archived": False}}],
                                      "stop_reason": "tool_use", "usage": {"input_tokens": 11, "output_tokens": 7}}).encode()
                    ctype = "application/json"
                elif body.get("stream"):
                    chunks = [{"choices": [{"delta": {"content": "Ciao "}}]}, {"choices": [{"delta": {"content": "dal cloud"}}]},
                              {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}]
                    raw = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                    ctype = "text/event-stream"
                else:
                    raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Ciao dal cloud"}, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}).encode()
                    ctype = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Local:
    """The loaded model of this Basalt: what a function without a provider uses."""

    def __init__(self):
        self.calls = []

    def chat_completion(self, **kw):
        self.calls.append(kw)
        if kw.get("on_delta"):
            kw["on_delta"]("content", "dal modello locale")
        return {"choices": [{"message": {"role": "assistant", "content": "dal modello locale"}, "finish_reason": "stop"}], "usage": {}}


@pytest.fixture
def world(tmp_path):
    cloud, local = Cloud(), Local()
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    reg.local_hosts = ()                                   # the fake cloud is on this PC for the test, and counts as outside it
    ledger = Ledger(tmp_path / "u.json")
    yield cloud, local, reg, ledger
    cloud.close()


MSG = [{"role": "system", "content": "Sei Basalt."}, {"role": "user", "content": "ciao"}]


def test_with_nothing_assigned_the_loaded_model_answers(world):
    cloud, local, reg, ledger = world
    out = RoutedClient("executor", reg, local, ledger).chat_completion(MSG)
    assert out["choices"][0]["message"]["content"] == "dal modello locale" and len(local.calls) == 1 and cloud.requests == []


def test_an_openai_compatible_provider_gets_the_key_the_model_and_the_reasoning_in_its_own_words(world):
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "openai_compat", "base_url": cloud.url + "/v1", "api_key": "sk-test-abcdef", "model": "gpt-test"})["id"]
    reg.update(pid, {"reasoning_level": "high"})
    reg.set_consent("relevance", True)
    reg.assign_checked("relevance", pid)
    out = RoutedClient("relevance", reg, local, ledger).chat_completion(MSG, temperature=0.2, max_tokens=50)
    req = cloud.requests[0]
    assert out["choices"][0]["message"]["content"] == "Ciao dal cloud" and req["path"] == "/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer sk-test-abcdef"
    assert req["body"]["model"] == "gpt-test" and req["body"]["messages"] == MSG and req["body"]["temperature"] == 0.2
    assert req["body"]["reasoning_effort"] == "high" and req["body"]["max_tokens"] > 50       # the level's room is added to the visible cap
    assert local.calls == [] and ledger.used(pid) == 8


def test_a_setting_for_this_server_only_is_not_sent_to_a_cloud_provider(world):
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "openai_compat", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})["id"]
    reg.assign_checked("eval", pid)
    RoutedClient("eval", reg, local, ledger).chat_completion(MSG, extra={"seed": 3, "end_turn_bias": -2})
    assert cloud.requests[0]["body"]["seed"] == 3 and "end_turn_bias" not in cloud.requests[0]["body"]
    RoutedClient("chat", reg, local, ledger).chat_completion(MSG, extra={"end_turn_bias": -2})
    assert local.calls[-1]["extra"] == {"end_turn_bias": -2}                                       # the loaded model does get it


def test_the_text_arrives_in_pieces_from_a_cloud_provider_too(world):
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "openai_compat", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})["id"]
    reg.assign_checked("eval", pid)
    pieces = []
    out = RoutedClient("eval", reg, local, ledger).chat_completion(MSG, on_delta=lambda k, t: pieces.append((k, t)))
    assert pieces == [("content", "Ciao "), ("content", "dal cloud")] and out["choices"][0]["message"]["content"] == "Ciao dal cloud"
    assert cloud.requests[0]["body"]["stream"] is True


def test_anthropic_gets_its_own_shape_and_answers_come_back_in_the_openai_one(world):
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "claude", "base_url": cloud.url + "/v1", "api_key": "sk-ant-abcdef", "model": "claude-test"})["id"]
    reg.assign_checked("eval", pid)
    tools = [{"type": "function", "function": {"name": "plan_list", "description": "lists", "parameters": {"type": "object", "properties": {}}}}]
    history = [*MSG, {"role": "assistant", "content": "", "tool_calls": [{"id": "c0", "type": "function", "function": {"name": "plan_list", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c0", "name": "plan_list", "content": "nessun piano"}]
    out = RoutedClient("eval", reg, local, ledger).chat_completion(history, tools=tools, max_tokens=100)
    req = cloud.requests[0]
    assert req["path"] == "/v1/messages" and req["headers"]["x-api-key"] == "sk-ant-abcdef" and "anthropic-version" in req["headers"]
    assert req["body"]["system"] == "Sei Basalt." and req["body"]["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    assert req["body"]["messages"][-1] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c0", "content": "nessun piano"}]}
    assert req["body"]["messages"][1]["content"][0]["type"] == "tool_use"
    msg = out["choices"][0]["message"]
    assert msg["content"] == "Ciao da Claude" and msg["tool_calls"][0]["function"] == {"name": "plan_list", "arguments": '{"archived": false}'}
    assert out["choices"][0]["finish_reason"] == "tool_calls" and out["usage"]["total_tokens"] == 18


def test_personal_data_is_not_sent_without_the_yes_and_nothing_leaves(world):
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "openai_compat", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})["id"]
    data = reg.load()
    data["assignments"]["executor"] = pid                  # assigned by hand, past the checks: the client checks again
    reg.save(data)
    client = RoutedClient("executor", reg, local, ledger)
    out = client.chat_completion(MSG)
    assert cloud.requests == []                            # the person's data stayed here
    assert out["choices"][0]["message"]["content"] == "dal modello locale" and client.last_provider is None   # the loaded model answered
    reg.set_consent("executor", True)
    client.chat_completion(MSG)
    assert len(cloud.requests) == 1 and client.last_provider == pid


def test_when_the_cloud_fails_the_loaded_model_answers_and_what_was_written_is_not_repeated(world):
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "openai_compat", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})["id"]
    reg.set_consent("chat", True)
    reg.assign_checked("chat", pid)
    cloud.status = 503
    client = RoutedClient("chat", reg, local, ledger)
    assert client.chat_completion(MSG)["choices"][0]["message"]["content"] == "dal modello locale" and client.last_provider is None
    pieces = []
    client.chat_completion(MSG, on_delta=lambda k, t: pieces.append(t))
    assert pieces == ["dal modello locale"]                # one voice, not two


def test_a_local_provider_that_fails_says_why_there_is_no_other_route(tmp_path, world):
    cloud, local, reg, ledger = world
    reg2 = SecureRegistry(tmp_path / "p2.json", credentials=FileCredentials(tmp_path / "c2.json"))      # here the address is this PC's
    pid = reg2.add({"type": "lmstudio", "base_url": cloud.url + "/v1", "model": "m"})["id"]
    reg2.assign_checked("executor", pid)
    cloud.status = 401
    with pytest.raises(BasaltError) as err:
        RoutedClient("executor", reg2, local, Ledger(tmp_path / "u2.json")).chat_completion(MSG)
    assert err.value.status == 401 and "provider says no" in str(err.value)


def test_the_daily_cap_stops_a_provider(world):
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "openai_compat", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})["id"]
    reg.assign_checked("relevance", pid)
    reg.set_cap(pid, 10)
    client = RoutedClient("relevance", reg, local, ledger)
    client.chat_completion(MSG)                            # 8 tokens
    client.chat_completion(MSG)                            # 16: now over
    n = len(cloud.requests)
    out = client.chat_completion(MSG)
    assert len(cloud.requests) == n and out["choices"][0]["message"]["content"] == "dal modello locale"


def test_this_server_as_a_provider_is_the_loaded_model(world):
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "basalt"})["id"]
    reg.assign_checked("executor", pid)
    RoutedClient("executor", reg, local, ledger).chat_completion(MSG)
    assert len(local.calls) == 1 and cloud.requests == []


def test_the_anthropic_translation_alone():
    body = to_anthropic([{"role": "system", "content": "a"}, {"role": "system", "content": "b"}, {"role": "user", "content": "x"}, {"role": "user", "content": "y"}], None)
    assert body["system"] == "a\n\nb" and body["messages"] == [{"role": "user", "content": "x\n\ny"}] and "tools" not in body
    assert from_anthropic({"content": [{"type": "text", "text": "ok"}], "stop_reason": "max_tokens"})["choices"][0]["finish_reason"] == "length"


def test_this_server_takes_every_reasoning_level_llmswitch_offers():
    from harness.llmswitch import reasoning
    from serve.frontend import effort_kwargs
    shown = {level: effort_kwargs(level) for level in reasoning.LEVELS}
    assert shown["none"] == shown["minimal"] == {"enable_thinking": False}
    assert shown["low"] == {"reasoning_effort": "low"} and shown["medium"] == {"reasoning_effort": "medium"}
    assert shown["high"] == shown["xhigh"] == shown["max"] == {"reasoning_effort": "xhigh"}
