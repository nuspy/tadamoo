"""Live telephony tests. Skipped unless the user authorizes real calls (see QUEUE_live_calls.md)."""
import os
import time

import pytest

from harness.integrations.call_api_client import CallAPICallClient, build_greeting, has_ai_disclosure

pytestmark = pytest.mark.skipif(
    os.environ.get("BASALT_LIVE_CALLS") != "1",
    reason="real phone calls need the user's explicit authorization (BASALT_LIVE_CALLS=1)",
)


@pytest.fixture
def client():
    return CallAPICallClient.from_environment()


@pytest.fixture
def number():
    value = os.environ.get("BASALT_LIVE_CALL_NUMBER")
    if not value:
        pytest.skip("BASALT_LIVE_CALL_NUMBER is not set")
    return value


def _place(client, number, purpose):
    greeting = build_greeting("il proprietario di questo telefono", purpose)
    assert has_ai_disclosure(greeting)
    res = client.place_call(number, greeting)
    assert res.get("status") == "calling", res
    return res["call_id"]


def test_service_reachable_and_authenticated(client):
    assert client.is_available()
    assert client.state("_probe") is None  # unknown id: reachable, no call involved


def test_single_call_with_ai_disclosure_and_hangup(client, number):
    call_id = _place(client, number, "Questa è una chiamata di prova del sistema Basalt.")
    try:
        time.sleep(5)
        assert "assistant:" in (client.get_call_transcript(call_id) or "")
    finally:
        assert client.hangup(call_id)
    assert client.state(call_id)["status"] == "ended"


def test_say_and_hear_roundtrip(client, number):
    call_id = _place(client, number, "Verifico il collegamento audio.")
    try:
        assert client.say(call_id, "Riesci a sentirmi?")
        heard = client.heard(call_id, after=-1, timeout_s=30)
        assert heard is not None and "index" in heard
    finally:
        client.hangup(call_id)


def test_hangup_is_idempotent(client, number):
    call_id = _place(client, number, "Prova di chiusura.")
    assert client.hangup(call_id)
    assert client.hangup(call_id)
    assert client.state(call_id)["status"] == "ended"


@pytest.mark.skip(reason="needs the agent's phone tools (queued: runs after the live model and the user's approval)")
def test_agent_places_call_only_after_approval():
    raise NotImplementedError
