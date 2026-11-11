"""While a heavy job has the local model out, the voice service's requests (the copilot, the meetings, the calls, the avatar's direct brain) go to the swap's provider
like the Chat page's, with the same safeguards: this PC only, the voice token for `voice`, and no passage of an archive that is not cleared for cloud models."""
import io
import json

import pytest

from serve import heavy_forward
from tadamoo.rag import rt_block
from tadamoo.llm import functions


class Handler:
    def __init__(self, header="voice", voice=True, remote=False, ip="127.0.0.1"):
        self.headers = {heavy_forward.HEADER: header} if header else {}
        self._via_voice, self.remote_listener, self.client_address = voice, remote, (ip, 5000)
        self.sent, self.status, self.out, self.wfile, self.closed = [], 0, [], io.BytesIO(), False

    def _json(self, code, obj):
        self.status, self.out = code, obj

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.sent.append((k, v))

    def _cors(self):
        pass

    def end_headers(self):
        pass


class Registry:
    def __init__(self, swapped=True):
        self.swapped, self.asked = swapped, []

    def swap(self):
        return {"pid": "p"} if self.swapped else None

    def provider_for(self, function):
        self.asked.append(function)
        return {"id": "p", "name": "Cloud", "type": "zai_coding"}

    def endpoint_for(self, function, carica=True):
        return "https://cloud.example/v1", "key", "big-model"


class Svc:
    def __init__(self, swapped=True):
        self.heavy_swap = type("S", (), {"registry": Registry(swapped)})()


def test_voice_is_a_function_the_registry_knows():
    assert functions.known("voice") and functions.spec("voice")["data"] == "personal" and "voice" in functions.names()


def test_the_voice_service_and_the_page_are_told_apart_and_nobody_else_is():
    assert heavy_forward.function_of(Handler("voice", voice=True)) == "voice"
    assert heavy_forward.function_of(Handler("chat", voice=False)) == "chat"
    assert heavy_forward.function_of(Handler("voice", voice=False)) == ""                  # another program on this PC cannot claim to be the voice service
    assert heavy_forward.function_of(Handler("voice", remote=True)) == "" and heavy_forward.function_of(Handler("voice", ip="192.168.1.5")) == ""
    assert heavy_forward.function_of(Handler(None)) == "" and heavy_forward.function_of(Handler("executor")) == ""
    assert heavy_forward.wants(Handler("voice"), Svc(True)) and not heavy_forward.wants(Handler("voice"), Svc(False)) and not heavy_forward.wants(Handler("voice", voice=False), Svc(True))


def test_a_voice_request_goes_to_the_swaps_provider_for_the_voice_function_without_local_archive_passages(monkeypatch):
    sent = {}

    class Reply:
        status_code, headers = 200, {"Content-Type": "text/event-stream"}

        def iter_content(self, chunk_size=None):
            yield b'data: {"usage": {"total_tokens": 9}}\n\n'

        def close(self):
            pass

    monkeypatch.setattr(heavy_forward.requests, "post", lambda url, json=None, headers=None, **k: sent.update(url=url, body=json, headers=headers) or Reply())
    monkeypatch.setattr(heavy_forward, "_record", lambda pid, tokens: sent.update(tokens=tokens))
    local = rt_block.build(["[Secret / plan.pdf]\nthe launch is in May"], True, False)
    cleared = rt_block.build(["[Open / notes.txt]\nthe fair is in June"], True, True)
    body = {"messages": [{"role": "system", "content": "voice base"}, {"role": "user", "content": "when?\n\n" + local}, {"role": "user", "content": "and?\n\n" + cleared}],
            "stream": True, "max_tokens": 50, "cache_slot": "side", "side_use": "voice_reply"}
    svc, handler = Svc(), Handler("voice")
    assert heavy_forward.forward(handler, svc, json.dumps(body).encode()) is True
    assert svc.heavy_swap.registry.asked == ["voice"] and sent["url"].endswith("/chat/completions") and sent["body"]["model"] == "big-model"
    text = json.dumps(sent["body"]["messages"])
    assert "launch is in May" not in text and "knowledge-archive\"" not in text and "the fair is in June" in text and "cache_slot" not in sent["body"]
    assert sent["tokens"] == 9


def test_the_chat_page_still_uses_the_chat_function(monkeypatch):
    seen = {}
    monkeypatch.setattr(heavy_forward.requests, "post", lambda *a, **k: seen.setdefault("called", True) and (_ for _ in ()).throw(heavy_forward.requests.ConnectionError("x")))
    svc, handler = Svc(), Handler("chat", voice=False)
    assert heavy_forward.forward(handler, svc, b'{"messages": []}') is True and svc.heavy_swap.registry.asked == ["chat"] and handler.status == 502
