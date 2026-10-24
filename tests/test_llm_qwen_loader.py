"""Making sure the model is there: use it as it is, wait while it loads, load it with the route's profile, or give the Loader's refusal."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.client import BasaltError
from harness.llm.client import RoutedClient
from harness.llm.credentials import FileCredentials
from harness.llm.qwen_loader import QwenLoader, QwenNotReady
from harness.llm.store import Ledger, SecureRegistry


class Loader:
    """A server's loader endpoints that the test controls."""

    def __init__(self, loaded=False, loading=False, auto_load=False, refuse=None):
        self.state = {"loaded": loaded, "loading": loading}
        self.auto_load, self.refuse, self.calls, self.polls = auto_load, refuse, [], 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/api/loader/status":
                    outer.polls += 1
                    if outer.state["loading"] and outer.polls >= 3:
                        outer.state = {"loaded": True, "loading": False}
                    return self._send(200, outer.state)
                if self.path == "/v1/status":
                    return self._send(200, {"auto_load": outer.auto_load})
                if self.path == "/api/profiles":
                    return self._send(200, {"load": [{"name": "Chat", "values": {"server.context": 32768}, "model": "E:/m/chat.basalt"},
                                                     {"name": "Machine Default", "values": {"server.context": 65536}}], "default": "Machine Default"})
                self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.calls.append((self.path, body, self.headers.get("Authorization")))
                if outer.refuse:
                    if outer.refuse[1].get("error", {}).get("code") == "busy":
                        outer.state = {"loaded": False, "loading": True}               # the load somebody else started
                    return self._send(*outer.refuse)
                outer.state = {"loaded": False, "loading": True}
                self._send(202, {"status": "reloading"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def make():
    made = []

    def build(**kw):
        server = Loader(**kw)
        made.append(server)
        return server, QwenLoader(server.url, "k", wait_s=5, poll_s=0, sleep=lambda s: None)
    yield build
    for s in made:
        s.close()


def test_a_model_that_is_there_is_used_as_it_is_even_when_the_route_names_a_profile(make):
    server, loader = make(loaded=True)
    assert loader.ensure({"profile": "Chat"}) == "loaded" and server.calls == []


def test_a_model_that_is_loading_is_waited_for(make):
    server, loader = make(loading=True)
    told = []
    loader.on_wait = told.append
    assert loader.ensure(None) == "waited" and server.calls == [] and told == ["loading the model"]


def test_nothing_loaded_loads_with_the_routes_profile_and_waits(make):
    server, loader = make()
    assert loader.ensure({"profile": "Chat"}) == "started"
    path, body, auth = server.calls[0]
    assert path == "/api/loader/load" and body == {"values": {"server.context": 32768}, "model": "E:/m/chat.basalt"} and auth == "Bearer k"
    assert server.state["loaded"] is True


def test_a_server_that_loads_by_itself_is_not_told_what_to_do_unless_the_route_has_a_profile(make):
    server, loader = make(auto_load=True)
    assert loader.ensure(None) == "auto" and server.calls == []
    assert loader.ensure({"profile": "Chat"}) == "started" and server.calls[0][1]["values"] == {"server.context": 32768}


def test_without_a_profile_or_auto_load_the_default_profile_is_used(make):
    server, loader = make()
    assert loader.ensure(None) == "started" and server.calls[0][1]["values"] == {"server.context": 65536}


def test_the_loaders_refusal_is_the_answer_in_its_words(make):
    server, loader = make(refuse=(422, {"error": {"code": "does_not_fit", "message": "another program holds 20 GB of the GPU memory"}}))
    with pytest.raises(QwenNotReady, match="holds 20 GB"):
        loader.ensure(None)
    server2, loader2 = make(refuse=(409, {"error": {"code": "busy", "message": "a load is already running"}}))
    assert loader2.ensure(None) == "waited" and server2.state["loaded"]                      # someone else started it: wait for that one


def test_a_server_that_does_not_answer_is_left_to_the_call():
    loader = QwenLoader("http://127.0.0.1:1", None, wait_s=1, poll_s=0, sleep=lambda s: None)
    assert loader.ensure(None) == "unknown"


def test_the_routed_client_asks_the_loader_before_it_uses_this_server(tmp_path):
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    pid = reg.add({"type": "basalt"})["id"]
    reg.set_profile(pid, "Chat")
    reg.assign_checked("chat", pid)
    asked = []

    class Local:
        base_url = "http://x"

        def chat_completion(self, **kw):
            asked.append("model")
            return {"choices": [{"message": {"content": "ok"}}]}

    class Ensure:
        def ensure(self, route):
            asked.append(("ensure", route["profile"] if route else None))

    out = RoutedClient("chat", reg, Local(), Ledger(tmp_path / "u.json"), loader=Ensure()).chat_completion(messages=[{"role": "user", "content": "x"}])
    assert out["choices"][0]["message"]["content"] == "ok" and asked == [("ensure", "Chat"), "model"]

    class NotReady:
        def ensure(self, route):
            raise QwenNotReady("another program holds the memory")
    with pytest.raises(BasaltError, match="holds the memory"):
        RoutedClient("chat", reg, Local(), Ledger(tmp_path / "u.json"), loader=NotReady()).chat_completion(messages=[])
