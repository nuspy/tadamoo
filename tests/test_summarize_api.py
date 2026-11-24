"""POST /api/summarize on the real HTTP server driven by the scripted MockEngine (no GPU): the level and the text are checked, the summary is what the
model wrote, and a question about nothing is refused."""
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[1]


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    started = []

    def start(scripts):
        tok = ByteTokenizer()
        svc = Service(MockEngine(tok, scripts, max_context=65536), tok, ChatTemplate.from_file(ROOT / "serve" / "chat_template.jinja"))
        svc.telemetry = _NoTelemetry()
        httpd = serve(svc, port=0)
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def post(url, payload):
    req = urllib.request.Request(url + "/api/summarize", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_summary_is_what_the_model_wrote_at_the_level_asked(server):
    url = server(["The committee approved the library budget of 4,200 euros."])
    text = "The committee met on Monday and approved the budget of 4,200 euros for the library. Nobody objected. The vote was unanimous."
    status, body = post(url, {"text": text, "level": "executive"})
    assert status == 200, body
    assert body["level"] == "executive" and body["summary"] == "The committee approved the library budget of 4,200 euros."
    assert body["chars_in"] == len(text) and body["parts"] == 1 and body["side"] is False


def test_an_unknown_level_an_empty_text_and_a_bad_language_are_refused(server):
    url = server(["</think>\n\nx"])
    assert post(url, {"text": "hello", "level": "tiny"})[0] == 400
    assert post(url, {"text": "   ", "level": "medium"})[0] == 400
    assert post(url, {"level": "medium"})[0] == 400
    assert post(url, {"text": "hello", "level": "medium", "language": "x" * 50})[0] == 400
