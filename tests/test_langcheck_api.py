"""The routes of the grammar check on the real HTTP server (scripted MockEngine, no GPU) with a small local model server standing in for the provider:
the list is set and read, a provider that does not run here is refused, the check goes to the provider and comes back with the code untouched, and the offer
follows the list."""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from serve import handler as handler_mod
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer
from tadamoo import llm

ROOT = Path(__file__).resolve().parents[1]


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


class Proofreader(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Proofreader.seen.append(body)
        text = body["messages"][-1]["content"].replace("teh", "the")
        data = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(llm, "_registry", None)
    monkeypatch.setattr(handler_mod, "_LLM", None)
    Proofreader.seen = []
    model = HTTPServer(("127.0.0.1", 0), Proofreader)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, ["x"], max_context=65536), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
    svc.telemetry = _NoTelemetry()
    httpd = serve(svc, port=0)
    prov = llm.registry().add({"type": "llamacpp", "name": "Hungarian editor", "base_url": f"http://127.0.0.1:{model.server_address[1]}/v1", "model": "hu-editor"})
    yield f"http://127.0.0.1:{httpd.server_address[1]}", prov["id"]
    httpd.shutdown()
    httpd.server_close()
    model.shutdown()


def call(url, path, payload=None):
    req = urllib.request.Request(url + path, data=None if payload is None else json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_list_is_set_read_and_removed(stack):
    url, pid = stack
    status, view = call(url, "/api/language-check")
    assert status == 200 and view["languages"] == {} and view["auto_offer"] is True
    assert pid in [p["id"] for p in view["providers"]] and "hu" in [s["code"] for s in view["supported"]]
    status, view = call(url, "/api/language-check", {"set": {"language": "Hungarian", "provider": pid, "model": ""}})
    assert status == 200 and view["languages"]["hu"]["provider"] == pid and view["languages"]["hu"]["provider_name"] == "Hungarian editor" and view["languages"]["hu"]["ok"]
    assert call(url, "/api/language-check", {"auto_offer": False})[1]["auto_offer"] is False
    assert call(url, "/api/language-check", {"remove": "hu"})[1]["languages"] == {}


def test_what_cannot_be_chosen_is_refused(stack):
    url, pid = stack
    assert call(url, "/api/language-check", {"set": {"language": "klingon", "provider": pid}})[0] == 400
    assert call(url, "/api/language-check", {"set": {"language": "hu", "provider": "no-such"}})[0] == 400
    assert call(url, "/api/language-check", {})[0] == 400
    assert call(url, "/api/language-check/run", {"text": "hello", "language": "hu"})[0] == 400            # no model in the list for it


def test_the_check_goes_to_the_provider_and_the_code_stays_out_of_it(stack):
    url, pid = stack
    call(url, "/api/language-check", {"set": {"language": "hu", "provider": pid}})
    text = "I think teh plan is good: see `teh()` and https://example.com/teh\n\n```\nteh = 1\n```\n"
    status, out = call(url, "/api/language-check/run", {"text": text, "language": "hu"})
    assert status == 200, out
    assert out["text"] == "I think the plan is good: see `teh()` and https://example.com/teh\n\n```\nteh = 1\n```\n" and out["changed"] == 1 and out["kept"] == 0
    sent = Proofreader.seen[0]
    assert sent["model"] == "hu-editor" and "native Hungarian proofreader" in sent["messages"][0]["content"]
    assert "teh()" not in sent["messages"][1]["content"] and "```" not in sent["messages"][1]["content"] and "example.com" not in sent["messages"][1]["content"]


def test_the_offer_follows_the_list(stack):
    url, pid = stack
    ask = lambda text: call(url, "/api/language-check/offer", {"request": text})[1]["language"]
    assert ask("Translate this into Hungarian") == ""
    call(url, "/api/language-check", {"set": {"language": "hu", "provider": pid}})
    assert ask("Translate this into Hungarian") == "hu" and ask("Write a poem") == ""
