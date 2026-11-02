"""How long a model call may take: 60 seconds unless the caller or the client says otherwise; the client can have no limit."""
import json

from tadamoo.client import BasaltClient


class Reply:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}).encode("utf-8")


def seen_timeouts(monkeypatch, client, **kwargs):
    seen = []
    monkeypatch.setattr("tadamoo.client.open_url", lambda req, timeout=0: seen.append(timeout) or Reply())
    client.chat_completion([{"role": "user", "content": "hi"}], **kwargs)
    return seen


def test_a_call_waits_60_seconds_by_default(monkeypatch):
    assert seen_timeouts(monkeypatch, BasaltClient("http://x")) == [60.0]


def test_the_client_can_carry_another_limit_or_none_and_a_caller_still_wins(monkeypatch):
    client = BasaltClient("http://x")
    client.call_timeout = 900.0
    assert seen_timeouts(monkeypatch, client) == [900.0]
    assert seen_timeouts(monkeypatch, client, timeout=5.0) == [5.0]
    client.call_timeout = None
    assert seen_timeouts(monkeypatch, client) == [None]
