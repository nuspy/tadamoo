"""A2A client against a local fake agent: card discovery, a task that ends at once, one that is polled, errors, cancel."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness import a2a
from harness.a2a import A2AClient, A2AError, ask_agent


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(a2a, "POLL_S", 0.05)


class FakeAgent:
    """Serves a card and the REST endpoints; `mode` decides how a message is answered."""

    def __init__(self):
        self.mode = "complete"
        self.requests = []
        self.polls = 0
        self.cancelled = []
        self.card_override = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/a2a+json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append(("GET", self.path, dict(self.headers), None))
                if self.path == "/.well-known/agent-card.json":
                    return self._send(200, outer.card_override or outer.card())
                if self.path == "/rest/tasks/t-1":
                    outer.polls += 1
                    state = "TASK_STATE_COMPLETED" if outer.polls >= 3 else "TASK_STATE_WORKING"
                    return self._send(200, outer.task(state))
                self._send(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "no such task",
                                           "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "TASK_NOT_FOUND"}]}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.requests.append(("POST", self.path, dict(self.headers), body))
                if self.path == "/rest/message:send":
                    if outer.mode == "reply":
                        return self._send(200, {"message": {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": "just a reply"}]}})
                    if outer.mode == "working":
                        return self._send(200, {"task": outer.task("TASK_STATE_WORKING")})
                    if outer.mode == "input":
                        return self._send(200, {"task": outer.task("TASK_STATE_INPUT_REQUIRED", text="Which file?")})
                    return self._send(200, {"task": outer.task("TASK_STATE_COMPLETED")})
                if self.path == "/rest/tasks/t-1:cancel":
                    outer.cancelled.append(True)
                    return self._send(200, outer.task("TASK_STATE_CANCELED"))
                self._send(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "nope"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def card(self):
        return {"name": "Fake A2A", "description": "d", "version": "1.0.0", "capabilities": {}, "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"], "skills": [],
                "supportedInterfaces": [{"url": "http://x/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
                                        {"url": self.url + "/rest", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}]}

    @staticmethod
    def task(state, text="The answer is 42."):
        task = {"id": "t-1", "contextId": "c-1", "status": {"state": state}}
        if state == "TASK_STATE_COMPLETED":
            task["artifacts"] = [{"artifactId": "a", "parts": [{"text": text}]}]
        elif state == "TASK_STATE_INPUT_REQUIRED":
            task["status"]["message"] = {"messageId": "q", "role": "ROLE_AGENT", "parts": [{"text": text}]}
        return task


@pytest.fixture
def agent():
    fake = FakeAgent()
    yield fake
    fake.server.shutdown()


def test_card_then_a_task_that_is_complete_at_once(agent):
    out = ask_agent(agent.url, "what is the answer?")
    assert out == {"agent": "Fake A2A", "state": "TASK_STATE_COMPLETED", "task_id": "t-1", "text": "The answer is 42."}
    method, path, headers, body = agent.requests[-1]
    assert (method, path) == ("POST", "/rest/message:send")           # the HTTP+JSON interface, not the JSONRPC one
    assert headers["A2A-Version"] == "1.0" and headers["Content-Type"] == "application/a2a+json"
    assert body["message"]["role"] == "ROLE_USER" and body["message"]["parts"] == [{"text": "what is the answer?"}]
    assert body["message"]["messageId"]


def test_a_working_task_is_polled_until_it_ends(agent):
    agent.mode = "working"
    out = ask_agent(agent.url, "go")
    assert out["state"] == "TASK_STATE_COMPLETED" and out["text"] == "The answer is 42." and agent.polls == 3


def test_a_plain_message_reply_is_returned(agent):
    agent.mode = "reply"
    out = ask_agent(agent.url, "hi")
    assert out["state"] == "message" and out["text"] == "just a reply"


def test_a_task_that_needs_input_is_reported_as_it_stands(agent):
    agent.mode = "input"
    out = ask_agent(agent.url, "do it")
    assert out["state"] == "TASK_STATE_INPUT_REQUIRED" and out["text"] == "Which file?"


def test_cancelling_the_run_cancels_the_remote_task(agent):
    agent.mode = "working"
    agent.polls = -10_000                                              # it never finishes
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    with pytest.raises(A2AError, match="cancelled"):
        ask_agent(agent.url, "go", cancel=cancel)
    assert agent.cancelled == [True]


def test_the_wait_has_a_limit_and_the_task_is_cancelled(agent):
    agent.mode = "working"
    agent.polls = -10_000
    with pytest.raises(A2AError, match="did not finish in 0.3 seconds"):
        ask_agent(agent.url, "go", wait_s=0.3)
    assert agent.cancelled == [True]


def test_a_token_is_sent_only_as_the_bearer_header(agent):
    ask_agent(agent.url, "hi", token="secret-token")
    assert all(r[2].get("Authorization") == "Bearer secret-token" for r in agent.requests)
    agent.requests.clear()
    ask_agent(agent.url, "hi")
    assert all("Authorization" not in r[2] for r in agent.requests)


def test_a_card_without_the_rest_binding_or_with_missing_fields_is_refused(agent):
    card = agent.card()
    card["supportedInterfaces"] = [card["supportedInterfaces"][0]]
    agent.card_override = card
    with pytest.raises(A2AError, match=r"no HTTP\+JSON interface .*JSONRPC"):
        A2AClient(agent.url).discover()
    agent.card_override = {"name": "x"}
    with pytest.raises(A2AError, match="lacks required fields"):
        A2AClient(agent.url).discover()


def test_an_old_protocol_version_is_refused(agent):
    card = agent.card()
    card["supportedInterfaces"][1]["protocolVersion"] = "0.3"
    agent.card_override = card
    with pytest.raises(A2AError, match="only 1.x"):
        A2AClient(agent.url).discover()


def test_an_error_body_is_summarised_with_its_reason(agent):
    client = A2AClient(agent.url)
    client.discover()
    client.interface = {**client.interface, "url": agent.url + "/rest-missing"}
    with pytest.raises(A2AError, match=r"HTTP 404.*nope"):
        client.send("x")
    with pytest.raises(A2AError, match=r"HTTP 404 \(TASK_NOT_FOUND\)"):
        client._http("GET", agent.url + "/rest/tasks/other")


def test_an_unreachable_agent_and_a_bad_address_are_errors():
    with pytest.raises(A2AError, match="could not be reached"):
        ask_agent("http://127.0.0.1:9", "hi")
    with pytest.raises(A2AError, match="starts with http"):
        A2AClient("ftp://x")
