"""What the harness side does so that the service's call rules (voice/callbridge/call_policy.py) can work: no socket, no call."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.integrations import call_api_client as client  # noqa: E402
from tadamoo.integrations.call_api_client import CallAPICallClient, build_greeting  # noqa: E402
from tadamoo.integrations.telephony_tools import register_phone_tools  # noqa: E402
from tadamoo.tools.registry import ToolRegistry  # noqa: E402


def test_a_placement_says_it_comes_from_the_agent_and_the_other_requests_do_not(monkeypatch):
    seen = []
    monkeypatch.setattr(client, "request_json", lambda method, url, payload, headers, timeout: seen.append((url, dict(headers))) or {"call_id": "c1", "status": "calling"})
    c = CallAPICallClient("http://127.0.0.1:1", token="t0ken")
    assert c.place_call("+390166123456", build_greeting("Mario", "Verifico l'orario."))["call_id"] == "c1"
    assert c.say("c1", "Buongiorno")
    placed, said = seen
    assert placed[1] == {"Authorization": "Bearer t0ken", "X-Call-Origin": "agent"}
    assert said[1] == {"Authorization": "Bearer t0ken"}


def test_the_transcript_a_hang_up_returns_is_untrusted_like_what_the_call_heard():
    registry = ToolRegistry()
    register_phone_tools(registry, CallAPICallClient("http://127.0.0.1:1", token="t"))
    assert registry.get("phone_listen").untrusted and registry.get("phone_hangup").untrusted
    assert not registry.get("phone_call").untrusted and not registry.get("phone_say").untrusted


def test_a_call_the_service_holds_for_the_persons_approval_is_a_clear_failure_not_a_doubtful_one(monkeypatch):
    monkeypatch.setattr(client, "request_json", lambda *a, **k: {"code": "approval_required", "approval_id": "0123456789abcdef", "error": "waits"})
    res = CallAPICallClient("http://127.0.0.1:1", token="t").place_call("+390166123456", build_greeting("Mario", "Verifico l'orario."))
    assert (res["status"], res["reason"]) == ("failed", "approval_required") and "nothing was placed" in res["error"]
