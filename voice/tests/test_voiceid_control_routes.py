"""The control server's voice-identification routes: who the current call or push-to-talk session's voice most recently matched, and
confirming it for a contact - over the real Flask app, with a stub session owner instead of a real phone or microphone."""
import types

from callbridge.control_server import create_app
from telephony_fakes import config

TOKEN = "t0ken-voice"
AUTH = {"Authorization": "Bearer " + TOKEN}


def _bridge(voice_status=None, voice_confirm_result=""):
    return types.SimpleNamespace(
        gate=None, hangups=0, watchers=0, start_incoming_watcher=lambda: None, hangup=lambda: {"status": "ok"},
        voice_status=lambda: voice_status or {"active": False}, voice_confirm=lambda contact_id: voice_confirm_result)


def _ptt(voice_status=None, voice_confirm_result=""):
    return types.SimpleNamespace(
        voice_status=lambda: voice_status or {"active": False}, voice_confirm=lambda contact_id: voice_confirm_result)


def test_no_call_active_reports_so(tmp_path):
    client = create_app(config(tmp_path, control_auth_token=TOKEN), bridge=_bridge()).test_client()
    assert client.get("/call/voice", headers=AUTH).get_json() == {"active": False}


def test_an_active_call_with_a_confident_match_is_reported(tmp_path):
    status = {"active": True, "channel": "phone", "identity": {"contact_id": "c1", "name": "Mario", "score": 0.95, "confident": True}}
    client = create_app(config(tmp_path, control_auth_token=TOKEN), bridge=_bridge(status)).test_client()
    assert client.get("/call/voice", headers=AUTH).get_json() == status


def test_confirming_without_a_contact_id_is_rejected(tmp_path):
    client = create_app(config(tmp_path, control_auth_token=TOKEN), bridge=_bridge()).test_client()
    r = client.post("/call/voice/confirm", json={}, headers=AUTH)
    assert r.status_code == 400


def test_confirming_with_no_call_active_says_so(tmp_path):
    client = create_app(config(tmp_path, control_auth_token=TOKEN), bridge=_bridge(voice_confirm_result="no_session")).test_client()
    r = client.post("/call/voice/confirm", json={"contact_id": "c1"}, headers=AUTH)
    assert r.status_code == 409


def test_confirming_successfully_enrolls(tmp_path):
    client = create_app(config(tmp_path, control_auth_token=TOKEN), bridge=_bridge()).test_client()
    r = client.post("/call/voice/confirm", json={"contact_id": "c1"}, headers=AUTH)
    assert r.status_code == 200 and r.get_json() == {"status": "enrolled"}


def test_the_session_routes_mirror_the_same_shape_for_push_to_talk(tmp_path):
    status = {"active": True, "channel": "mic", "identity": None}
    client = create_app(config(tmp_path, control_auth_token=TOKEN), bridge=_bridge(), ptt=_ptt(status)).test_client()
    assert client.get("/session/voice", headers=AUTH).get_json() == status
    r = client.post("/session/voice/confirm", json={"contact_id": "c1"}, headers=AUTH)
    assert r.status_code == 200 and r.get_json() == {"status": "enrolled"}


def test_the_session_routes_are_unavailable_without_a_push_to_talk_manager(tmp_path):
    client = create_app(config(tmp_path, control_auth_token=TOKEN), bridge=_bridge(), ptt=None).test_client()
    assert client.get("/session/voice", headers=AUTH).status_code == 503
    assert client.post("/session/voice/confirm", json={"contact_id": "c1"}, headers=AUTH).status_code == 503
