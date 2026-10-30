"""The technical session from the harness's side: the field a short call carries, and where it never goes."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import side
from harness.client import BasaltClient
from harness.intake import Context, Intake
from harness.llm.client import THIS_SERVER_ONLY, RoutedClient
from harness.llm.credentials import FileCredentials
from harness.llm.store import Ledger, SecureRegistry
from harness.router import judge_with_model
from harness.stack import _summarizer
from harness.suggestions import Suggester
from test_llm_client import Cloud, Local, MSG


class FakeServer:
    """A Basalt server's status route, with or without a technical session; counts the questions."""

    def __init__(self, side_slot):
        self.asked = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.asked += 1
                raw = json.dumps({"concurrency": {"serving": 2, "requested": 2, "side_slot": side_slot}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Quick:
    """A client that records the `extra` of every call and may report a technical session."""

    def __init__(self, ctx=0):
        self.ctx, self.extras = ctx, []

    def side_context(self):
        return self.ctx

    def chat_completion(self, messages, **kw):
        self.extras.append(kw.get("extra"))
        return {"choices": [{"message": {"role": "assistant", "content": '{"items": []}'}, "finish_reason": "stop"}], "usage": {}}


@pytest.fixture(autouse=True)
def no_default():
    side.use(None)
    yield
    side.use(None)


def test_the_field_is_for_this_server_only():
    assert "cache_slot" in THIS_SERVER_ONLY


def test_extra_is_empty_without_a_technical_session():
    assert side.side_extra() == {}
    assert side.side_extra(Quick(0)) == {}
    assert side.side_extra(object()) == {}                                  # a client that cannot tell
    assert side.side_extra(Quick(30000)) == {"cache_slot": "side"}
    assert side.SIDE_CONTEXT_TOKENS == 30000


def test_a_routed_client_asks_the_loaded_model_it_wraps(tmp_path):
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    assert side.side_extra(RoutedClient("triage", reg, Quick(30000), Ledger(tmp_path / "u.json"))) == {"cache_slot": "side"}
    assert side.side_extra(RoutedClient("triage", reg, Quick(0), Ledger(tmp_path / "u.json"))) == {}
    assert side.side_extra(RoutedClient("triage", reg, Local(), Ledger(tmp_path / "u.json"))) == {}


def test_a_registered_client_answers_the_call_without_one():
    side.use(Quick(8000))
    assert side.side_extra() == {"cache_slot": "side"}


def test_the_client_reads_the_session_from_the_server_status_and_remembers_it():
    on, off = FakeServer({"slot": 3, "context": 30000}), FakeServer(None)
    try:
        client = BasaltClient(base_url=on.url)
        assert client.side_context() == 30000 and client.side_context() == 30000 and on.asked == 1
        assert side.side_extra(client) == {"cache_slot": "side"}
        assert BasaltClient(base_url=off.url).side_context() == 0
        assert side.side_extra(BasaltClient(base_url=off.url)) == {}
    finally:
        on.close()
        off.close()
    assert BasaltClient(base_url="http://127.0.0.1:9").side_context() == 0         # nobody there: no session


def test_the_field_never_reaches_a_cloud_route_but_the_loaded_model_gets_it(tmp_path):
    cloud, local = Cloud(), Local()
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    reg.local_hosts = ()
    ledger = Ledger(tmp_path / "u.json")
    try:
        pid = reg.add({"type": "openai_compat", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})["id"]
        reg.assign_checked("eval", pid)
        RoutedClient("eval", reg, local, ledger).chat_completion(MSG, extra={"seed": 3, "cache_slot": "side"})
        assert cloud.requests[0]["body"]["seed"] == 3 and "cache_slot" not in cloud.requests[0]["body"]
        RoutedClient("chat", reg, local, ledger).chat_completion(MSG, extra={"cache_slot": "side"})
        assert local.calls[-1]["extra"] == {"cache_slot": "side"}
    finally:
        cloud.close()


def test_the_short_calls_ask_for_the_session_when_there_is_one():
    ctx = Context()
    for ctx_tokens, want in ((30000, "side"), (0, None)):
        quick = Quick(ctx_tokens)
        Intake("full", quick).triage("please fix the failing test in the parser", ctx)
        Suggester(quick).generate([{"role": "user", "content": "hi"}], None, lambda: False)
        _summarizer(quick)("Summarise.", "some older steps")
        judge_with_model(quick)("one more thing", {"task": "t", "progress": "p"})
        assert len(quick.extras) == 4, quick.extras
        for extra in quick.extras:
            assert extra.get("reasoning_effort") == "none"
            assert extra.get("cache_slot") == want
