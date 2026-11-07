"""/api/agent/sessions/<ref>/deliver, /status, /alias, /cross-commands and /api/agent/deliveries through the real handler."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.agent_sessions import TOKEN_HEADER, SessionManager
from serve.approvals import ApprovalBoard
from test_agent_gui_api import Api  # noqa: E402
from test_e2e_mock_server import start_server  # noqa: F401,E402  (fixture)

STUB = str(Path(__file__).resolve().parent.parent / "serve" / "tests" / "session_stub.py")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return str(root)


@pytest.fixture
def setup(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server(["unused"])
    mgr = SessionManager(tmp_path / "j", url, board=ApprovalBoard(), tick_s=0.2,
                         command=lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history)])
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    yield Api(url), mgr
    mgr.shutdown()


def until(predicate, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def test_only_a_conversation_with_its_token_may_send_a_message(setup, ws):
    api, mgr = setup
    boss = mgr.create(ws, alias="boss")["id"]
    mgr.create(ws, alias="log")
    code, data = api("POST", "/api/agent/sessions/log/deliver", {"message": "hi"})
    assert code == 403 and data["error"]["code"] == "not_a_conversation"
    code, data = api("POST", "/api/agent/sessions/log/deliver", {"message": "hi"}, headers={TOKEN_HEADER: f"{boss}.wrong"})
    assert code == 403
    code, data = api("POST", "/api/agent/sessions/log/deliver", {"message": "append this"}, headers={TOKEN_HEADER: f"{boss}.{mgr.session_token(boss)}"})
    assert code == 200 and data["delivery"]["provenance"] == "agent:boss" and data["delivery"]["chain"] == [boss]
    until(lambda: mgr.deliveries.get(data["delivery"]["id"])["state"] == "delivered")


def test_the_status_names_the_state_and_what_is_stuck(setup, ws):
    api, mgr = setup
    sid = mgr.create(ws, alias="night")["id"]
    code, data = api("GET", "/api/agent/sessions/night/status")
    assert code == 200 and data["id"] == sid and data["alias"] == "night" and data["state"] == "dormant" and data["dead_letters"] == 0
    assert api("GET", "/api/agent/sessions/nobody/status")[0] == 404


def test_a_dead_letter_is_listed_sent_again_or_put_away(setup, ws):
    api, mgr = setup
    sid = mgr.create(ws, alias="night")["id"]
    mgr.close(sid)
    boss = mgr.create(ws, alias="boss")["id"]
    header = {TOKEN_HEADER: f"{boss}.{mgr.session_token(boss)}"}
    code, made = api("POST", f"/api/agent/sessions/{sid}/deliver", {"message": "too late"}, headers=header)
    assert code == 200 and made["delivery"]["state"] == "dead"
    code, listed = api("GET", "/api/agent/deliveries?state=dead")
    assert code == 200 and [d["id"] for d in listed["deliveries"]] == [made["delivery"]["id"]] and listed["deliveries"][0]["message"] == "too late"
    code, again = api("POST", f"/api/agent/deliveries/{made['delivery']['id']}/retry", {})
    assert code == 200 and again["delivery"]["state"] == "dead"                       # still closed
    code, away = api("POST", f"/api/agent/deliveries/{made['delivery']['id']}/dismiss", {})
    assert code == 200 and away["delivery"]["state"] == "dismissed"
    assert api("POST", f"/api/agent/deliveries/{made['delivery']['id']}/dismiss", {})[0] == 409
    assert api("GET", "/api/agent/deliveries?state=dead")[1]["deliveries"] == []


def test_a_name_and_the_senders_that_may_give_orders_are_set_by_the_person(setup, ws):
    api, mgr = setup
    sid = mgr.create(ws)["id"]
    code, data = api("POST", f"/api/agent/sessions/{sid}/alias", {"alias": "Night"})
    assert code == 200 and data["session"]["alias"] == "night"
    other = mgr.create(ws)["id"]
    code, data = api("POST", f"/api/agent/sessions/{other}/alias", {"alias": "night"})
    assert code == 409 and data["error"]["code"] == "alias_taken"
    assert api("POST", f"/api/agent/sessions/{sid}/alias", {"alias": "no good!"})[0] == 400
    code, data = api("POST", f"/api/agent/sessions/{sid}/cross-commands", {"senders": ["boss", "team-*"]})
    assert code == 200 and data["session"]["cross_commands"] == ["boss", "team-*"]
    assert api("POST", f"/api/agent/sessions/{sid}/cross-commands", {"senders": "boss"})[0] == 400
    code, data = api("POST", "/api/agent/sessions", {"workspace": ws, "alias": "made", "cross_commands": ["night"]})
    assert code == 200 and data["session"]["alias"] == "made" and data["session"]["cross_commands"] == ["night"]


def test_budget_is_set_through_the_api_and_shown_in_status(setup, ws):
    api, mgr = setup
    sid = mgr.create(ws, alias="boss")["id"]
    code, data = api("POST", f"/api/agent/sessions/{sid}/budget", {"budget_day_tokens": 2000})
    assert code == 200 and data["session"]["budget_day_tokens"] == 2000
    assert api("GET", "/api/agent/sessions/boss/status")[1]["budget_day_tokens"] == 2000
    code, data = api("POST", f"/api/agent/sessions/{sid}/budget", {"budget_day_tokens": "x"})
    assert code == 400 and data["error"]["code"] == "bad_budget"


def test_the_digest_is_read_through_the_api(setup, ws):
    api, mgr = setup
    mgr.create(ws, alias="night")
    code, data = api("GET", "/api/agent/sessions/night/digest?n=5")
    assert code == 200 and data["alias"] == "night" and data["lines"] == []
    assert api("GET", "/api/agent/sessions/nowhere/digest")[0] == 404
