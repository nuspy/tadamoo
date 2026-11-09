"""/external/*, /call, /hangup, /transport and /status through the policy, on the real Flask app with a transport router that places nothing."""
import json
import threading

import pytest

from callbridge import external_brain
from callbridge.call_policy import outgoing_greeting
from callbridge.control_server import create_app
from telephony_fakes import FakeRouter, bridge_stub, config

TOKEN = "t0ken"
AUTH = {"Authorization": "Bearer " + TOKEN}
AGENT = {**AUTH, "X-Call-Origin": "agent"}
NUMBER = "+39 333 1234567"
GREETING = outgoing_greeting("Mario Rossi", "it", "Chiamo per un appuntamento.")


@pytest.fixture(autouse=True)
def _clean_sessions():
    external_brain._sessions.clear()
    yield
    external_brain._sessions.clear()


@pytest.fixture
def env(tmp_path):
    cfg = config(tmp_path, control_auth_token=TOKEN)
    router = FakeRouter()
    app = create_app(cfg, bridge=bridge_stub(), ptt=None)
    calls = app.config["CB_CALLS"]
    calls._router_factory = lambda: router
    return type("Env", (), {"cfg": cfg, "app": app, "client": app.test_client(), "router": router, "calls": calls, "tmp": tmp_path})


def audit(env):
    path = env.tmp / "call-audit.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def place(env, headers=AUTH, **body):
    body = {"number": NUMBER, "greeting": GREETING, **body}
    return env.client.post("/external/call", json=body, headers=headers)


def test_the_routes_need_the_token(env):
    assert env.client.post("/external/call", json={}).status_code == 401
    assert env.client.post("/call", json={}).status_code == 401
    assert env.client.get("/call/approvals").status_code == 401
    assert env.client.get("/call/audit").status_code == 401


# ---------------------------------------------------------------------------------------- a program's call: approval
def test_a_program_call_waits_for_the_person_then_goes(env):
    r = place(env)
    assert r.status_code == 202 and r.get_json()["code"] == "approval_required"
    rid = r.get_json()["approval_id"]
    assert env.router.placed == []
    listed = env.client.get("/call/approvals", headers=AUTH).get_json()
    assert listed["mode"] == "manual" and [p["id"] for p in listed["pending"]] == [rid]
    assert env.client.post(f"/call/approvals/{rid}/approve", headers=AUTH).get_json()["status"] == "approved"
    r = place(env, approval_id=rid)
    assert r.status_code == 200 and r.get_json()["status"] == "calling" and r.get_json()["transport"] == "gsm" and r.get_json()["call_id"]
    assert len(env.router.placed) == 1 and env.router.placed[0]["greeting"] == GREETING
    env.router.finish()
    assert place(env, approval_id=rid).get_json()["code"] == "approval_used"
    assert len(env.router.placed) == 1


def test_the_person_can_refuse_and_a_refused_call_is_never_placed(env):
    rid = place(env).get_json()["approval_id"]
    assert env.client.post(f"/call/approvals/{rid}/deny", headers=AUTH).status_code == 200
    r = place(env, approval_id=rid)
    assert r.status_code == 403 and r.get_json()["code"] == "approval_denied" and env.router.placed == []
    assert env.client.post(f"/call/approvals/{rid}/approve", headers=AUTH).status_code == 404
    assert env.client.post("/call/approvals/zzz/approve", headers=AUTH).status_code == 404
    assert env.client.post(f"/call/approvals/{rid}/maybe", headers=AUTH).status_code == 404


def test_the_agent_is_asked_by_its_harness_and_the_service_places_the_call(env):
    r = place(env, headers=AGENT)
    assert r.status_code == 200 and len(env.router.placed) == 1
    start = audit(env)[0]
    assert start["origin"] == "agent" and start["approval"] == "manual"


def test_auto_mode_places_a_program_call_at_once_and_still_discloses(env):
    env.cfg.outgoing_approval = "auto"
    assert place(env).status_code == 200
    assert audit(env)[0]["approval"] == "auto" and audit(env)[0]["origin"] == "program"
    env.router.finish()
    r = place(env, greeting="Buongiorno, chiamo per l'ordine.")
    assert r.status_code == 400 and r.get_json()["code"] == "no_disclosure"


# ---------------------------------------------------------------------------------------- the greeting, the numbers
def test_a_greeting_without_a_disclosure_marker_is_refused_while_the_setting_is_on(env):
    r = place(env, headers=AGENT, greeting="Buongiorno, chiamo per l'ordine.")
    assert r.status_code == 400 and r.get_json()["code"] == "no_disclosure" and env.router.placed == []
    assert place(env, headers=AGENT, greeting="").get_json()["code"] == "greeting_missing"


def test_with_the_setting_off_the_callers_greeting_is_used_and_the_row_says_off(env):
    env.cfg.ai_disclosure_on_start = False
    env.cfg.outgoing_approval = "auto"
    assert place(env, greeting="Buongiorno, chiamo per l'ordine.").status_code == 200
    assert env.router.placed[0]["greeting"] == "Buongiorno, chiamo per l'ordine." and audit(env)[0]["disclosure"] == "off"


def test_emergency_and_bad_numbers_are_refused(env):
    for number, status, code in (("112", 403, "emergency_number"), ("+39 118", 403, "emergency_number"), ("abc", 400, "invalid_number"),
                                 ("", 400, "number_missing")):
        r = place(env, headers=AGENT, number=number)
        assert (r.status_code, r.get_json()["code"]) == (status, code), number
    assert env.router.placed == []


def test_one_external_call_at_a_time(env):
    assert place(env, headers=AGENT).status_code == 200
    r = place(env, headers=AGENT, number="+39 333 7654321")
    assert r.status_code == 409 and len(env.router.placed) == 1


def test_a_failed_placement_is_one_attempt_and_leaves_no_live_session(env):
    env.router.result = "error"
    r = place(env, headers=AGENT)
    assert r.status_code == 502 and len(env.router.placed) == 1 and external_brain.active_session() is None
    env.router.result = "calling"
    assert place(env, headers=AGENT).status_code == 200 and len(env.router.placed) == 2        # the caller decided to try again, not the service


# ---------------------------------------------------------------------------------------- the conversation routes
def test_say_heard_transcript_state_and_hangup_work_through_the_policy(env):
    cid = place(env, headers=AGENT).get_json()["call_id"]
    brain = env.router.brain
    assert env.client.post(f"/external/{cid}/say", json={"text": "Siete aperti?"}, headers=AUTH).get_json()["status"] == "queued"
    out = []
    t = threading.Thread(target=lambda: out.extend(brain.respond_stream("Pronto, dica.", "remote")))
    t.start()
    t.join(5)
    assert out == ["Siete aperti?"]
    heard = env.client.get(f"/external/{cid}/heard?after=-1&timeout_s=1", headers=AUTH).get_json()
    assert heard["text"] == "Pronto, dica." and heard["index"] >= 0
    assert env.client.get(f"/external/{cid}/heard?after=5&timeout_s=0.1", headers=AUTH).get_json()["text"] is None
    tr = env.client.get(f"/external/{cid}/transcript", headers=AUTH).get_json()["transcript"]
    assert [x["text"] for x in tr if x["speaker"] == "callee"] == ["Pronto, dica."]
    state = env.client.get(f"/external/{cid}/state", headers=AUTH).get_json()
    assert state["status"] == "active" and state["transport"] == "gsm" and "transcript" not in state
    ended = env.client.post(f"/external/{cid}/hangup", headers=AUTH).get_json()
    assert ended["status"] == "ended" and env.router.hangups == 1
    assert env.client.post(f"/external/{cid}/say", json={"text": "x"}, headers=AUTH).status_code == 409
    assert env.client.post(f"/external/{cid}/hangup", headers=AUTH).get_json()["status"] == "ended"      # idempotent
    assert env.client.get("/external/nope/state", headers=AUTH).status_code == 404
    assert audit(env)[-1]["event"] == "end" and audit(env)[-1]["outcome"] == "hung_up"


def test_the_transcript_never_enters_the_audit(env):
    cid = place(env, headers=AGENT).get_json()["call_id"]
    env.router.brain._inner.reply_wait_s = 0.01
    list(env.router.brain.respond_stream("la parola d'ordine e' ciliegia"))
    env.client.post(f"/external/{cid}/hangup", headers=AUTH)
    assert "ciliegia" not in (env.tmp / "call-audit.jsonl").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------- a call by hand, transport, status
def test_a_call_by_hand_needs_no_approval_but_opens_with_the_disclosure(env):
    r = env.client.post("/call", json={"number": NUMBER, "greeting": "Buongiorno, sono qui."}, headers=AUTH)
    assert r.status_code == 200 and r.get_json()["transport"] == "gsm"
    sent = env.router.placed[0]["greeting"]
    assert "intelligenza artificiale" in sent and sent.endswith("Buongiorno, sono qui.")
    assert audit(env)[0]["origin"] == "user" and audit(env)[0]["approval"] == "not_needed"
    assert env.client.post("/call", json={"number": NUMBER}, headers=AUTH).status_code == 409           # one call at a time


def test_a_call_by_hand_refuses_an_emergency_number_and_a_bad_one(env):
    assert env.client.post("/call", json={"number": "112"}, headers=AUTH).status_code == 403
    assert env.client.post("/call", json={"number": "x;ATH"}, headers=AUTH).status_code == 400
    assert env.client.post("/call", json={}, headers=AUTH).status_code == 400
    assert env.router.placed == []


def test_with_telephony_off_nothing_is_placed_and_no_device_is_touched(env):
    env.cfg.call_transport = "off"
    assert env.client.post("/call", json={"number": NUMBER}, headers=AUTH).status_code == 409
    assert place(env, headers=AGENT).status_code == 409
    assert env.client.get("/transport", headers=AUTH).get_json() == {"selected": None, "mode": "off"}
    assert env.client.post("/hangup", headers=AUTH).get_json() == {"status": "ok"}
    assert env.calls._router is None


def test_transport_and_hangup_by_hand(env):
    assert env.client.get("/transport?refresh=1", headers=AUTH).get_json()["selected"] == "gsm"
    env.client.post("/call", json={"number": NUMBER}, headers=AUTH)
    assert env.client.post("/hangup", headers=AUTH).get_json() == {"status": "ok"} and env.router.hangups == 1


def test_the_audit_route_returns_masked_rows(env):
    env.client.post("/call", json={"number": NUMBER}, headers=AUTH)
    rows = env.client.get("/call/audit?limit=5", headers=AUTH).get_json()["rows"]
    assert rows and rows[0]["number"] == "***567" and "333" not in json.dumps(rows)


def test_the_summary_reports_the_telephony_settings_without_touching_a_device(env):
    s = env.calls.summary()
    assert s == {"transport": "auto", "outgoing_approval": "manual", "ai_disclosure_on_start": True, "incoming_auto_answer": False, "busy": False}
    assert env.calls._router is None


def test_the_service_starts_with_telephony_off_and_opens_nothing(tmp_path):
    from voice import service
    settings = tmp_path / "voice.json"
    settings.write_text(json.dumps({"models_dir": str(tmp_path / "m")}), encoding="utf-8")
    app, state = service.build_app(settings, TOKEN)
    assert state.config.call_transport == "off" and state.config.outgoing_approval == "manual"
    assert state.config.call_audit_file == str(tmp_path.resolve() / "call-audit.jsonl")
    got = app.test_client().get("/status", headers=AUTH).get_json()["telephony"]
    assert got["transport"] == "off" and got["incoming_auto_answer"] is False
    assert app.test_client().post("/call", json={"number": NUMBER}, headers=AUTH).status_code == 409
    assert state.calls._router is None


def test_no_pairing_token_is_written_in_the_code_and_the_service_takes_it_from_the_environment(tmp_path, monkeypatch):
    from callbridge.config import Config
    from voice import service
    assert Config().auth_token == ""
    monkeypatch.setenv(service.PHONE_TOKEN_ENV, "pairing-from-the-credential-store")
    settings = tmp_path / "voice.json"
    settings.write_text("{}", encoding="utf-8")
    _, state = service.build_app(settings, TOKEN)
    assert state.config.auth_token == "pairing-from-the-credential-store"
    monkeypatch.delenv(service.PHONE_TOKEN_ENV)
    _, state = service.build_app(settings, TOKEN)
    assert state.config.auth_token == ""
