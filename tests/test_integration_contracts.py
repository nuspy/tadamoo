"""Contract tests of the wikibridge and callAPIcall clients against local fakes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_services import FakeCallAPICall, FakeWikibridge  # noqa: E402
from harness.integrations import CallAPICallClient, WikiBridgeClient  # noqa: E402
from harness.integrations.call_api_client import build_greeting, has_ai_disclosure  # noqa: E402


@pytest.fixture
def wiki():
    fake = FakeWikibridge(key="k3y")
    yield fake
    fake.close()


@pytest.fixture
def phone():
    fake = FakeCallAPICall()
    yield fake
    fake.close()


def test_wikibridge_reads_the_real_shapes(wiki):
    c = WikiBridgeClient(wiki.url, api_key="k3y")
    assert c.status() == "ok" and c.is_available()
    assert c.list_projects() == ["cervinia", "work"]
    assert c.search("cervinia", "ovini")[0]["path"] == "entities/diana.md"
    assert c.get_page("cervinia", "entities/diana.md") == "Allergica agli ovini."
    assert c.get_page("cervinia", "entities/missing.md") is None
    ctx = c.rag_context("cervinia", "allergie", participants=["diana"])
    assert ctx["blocks"][0]["text"] == "Allergica agli ovini."
    assert wiki.requests[-1][3]["participants"] == ["diana"]


def test_wikibridge_wrong_key_and_offline_are_distinct(wiki):
    assert WikiBridgeClient(wiki.url, api_key="wrong").search("cervinia", "x") == []
    bad = WikiBridgeClient(wiki.url, api_key="wrong")
    bad.search("cervinia", "x")
    assert "unauthorized" in bad.last_error

    off = WikiBridgeClient("http://127.0.0.1:59997", timeout=0.2)
    assert off.status() == "offline"
    assert off.list_projects() == []
    assert "offline" in off.last_error


@pytest.mark.parametrize("bad", ["../secret.md", "/etc/passwd", "a/../../b.md", ""])
def test_wikibridge_rejects_paths_outside_the_wiki(wiki, bad):
    c = WikiBridgeClient(wiki.url, api_key="k3y")
    before = len(wiki.requests)
    assert c.get_page("cervinia", bad) is None
    assert len(wiki.requests) == before


def test_wikibridge_environment_configuration(monkeypatch, wiki):
    monkeypatch.setenv("BASALT_WIKIBRIDGE_URL", wiki.url)
    monkeypatch.setenv("BASALT_WIKIBRIDGE_KEY", "k3y")
    assert WikiBridgeClient.from_environment().is_available()


def test_call_client_follows_the_external_contract(phone):
    c = CallAPICallClient(phone.url, token="t0ken")
    assert c.is_available()
    res = c.make_phone_call("+39 0166 123456", "Verificare l'apertura dell'hotel", persona_prompt="Mario Rossi")
    assert res["call_id"] == "c1" and res["status"] == "calling"
    placed = phone.placed[0]
    assert placed["number"] == "+39 0166 123456" and placed["transport"] == "gsm"
    assert has_ai_disclosure(placed["greeting"]) and "Mario Rossi" in placed["greeting"]

    assert c.say("c1", "Avete camere libere?") and phone.said == ["Avete camere libere?"]
    heard = c.heard("c1", after=-1, timeout_s=1)
    assert heard["text"] == "Pronto?" and heard["index"] == 0
    assert c.get_call_transcript("c1") == "assistant: Buongiorno\ncallee: Pronto?"
    assert c.state("c1")["status"] == "active"
    assert c.hangup("c1") and c.state("c1")["status"] == "ended"


def test_call_client_refuses_to_dial_without_ai_disclosure(phone):
    c = CallAPICallClient(phone.url, token="t0ken")
    with pytest.raises(ValueError, match="AI"):
        c.place_call("+390166123456", "Buongiorno, vorrei prenotare.")
    with pytest.raises(ValueError, match="number"):
        c.place_call("rm -rf /", build_greeting("Mario", "Prenotare"))
    with pytest.raises(ValueError):
        c.make_phone_call("+390166123456", "x", mode="satellite")
    assert phone.placed == []


def test_call_client_without_the_token_is_rejected_and_never_retries(phone):
    c = CallAPICallClient(phone.url, token="wrong")
    res = c.place_call("+390166123456", build_greeting("Mario", "Prenotare un tavolo"))
    assert res["status"] == "failed"
    assert sum(1 for r in phone.requests if r[1] == "/external/call") == 1
    assert phone.placed == []


def test_call_client_rejects_unsafe_call_ids(phone):
    c = CallAPICallClient(phone.url, token="t0ken")
    with pytest.raises(ValueError):
        c.hangup("../admin")
    with pytest.raises(ValueError):
        c.say("c1/../x", "ciao")


def test_call_client_offline_degrades_without_raising():
    c = CallAPICallClient("http://127.0.0.1:59998", timeout=0.1, place_timeout=0.2)
    assert not c.is_available()
    res = c.make_phone_call("+390166123456", "Verifica apertura hotel")
    assert res["status"] == "failed" and "unavailable" in res["error"]
    assert not c.hangup("call_123")
    assert c.get_call_transcript("call_123") is None
