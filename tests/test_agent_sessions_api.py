"""The Chat's conversations through the server's API, with the real agent process and the scripted engine."""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from test_e2e_mock_server import answer, start_server, tool_call, ws  # noqa: F401  (fixtures)
from test_agent_gui_api import Api, wait_for  # noqa: E402


@pytest.fixture
def setup(tmp_path, monkeypatch, start_server):  # noqa: F811
    state = {}

    def make(scripts, api_key="", **kw):
        url = start_server(scripts, api_key=api_key)
        board = ApprovalBoard()
        mgr = SessionManager(tmp_path / "j", url, api_key, board=board, tick_s=0.2, **kw)
        monkeypatch.setattr(handler, "_APPROVALS", board)
        monkeypatch.setattr(handler, "_SESSIONS", mgr)
        state.update(mgr=mgr, api=Api(url, api_key))
        return state

    yield make
    if "mgr" in state:
        state["mgr"].shutdown()


def events(api, sid, after=0):
    return api("GET", f"/api/agent/sessions/{sid}/events?after={after}")[1]


def test_two_messages_through_the_api_the_second_sees_the_first(setup, ws):
    s = setup([answer("Mi chiamo Basalt."), answer("Me lo hai appena chiesto.")])
    api = s["api"]
    code, body = api("POST", "/api/agent/sessions", {"workspace": str(ws), "mode": "read_only", "ask_only": True})
    assert code == 200
    sid = body["session"]["id"]
    assert api("POST", f"/api/agent/sessions/{sid}/send", {"text": "Come ti chiami?"})[0] == 200
    wait_for(lambda: any(e["event"] == "done" for e in events(api, sid)["events"]), seconds=60)
    code, again = api("POST", f"/api/agent/sessions/{sid}/send", {"text": "Che cosa ti ho chiesto?"})
    assert code == 200
    wait_for(lambda: sum(e["event"] == "done" for e in events(api, sid)["events"]) == 2, seconds=60)
    done = [e for e in events(api, sid)["events"] if e["event"] == "done"]
    assert done[1]["final"] == "Me lo hai appena chiesto." and done[0]["stop_reason"] == "final"
    hist = api("GET", f"/api/agent/sessions/{sid}/history")[1]
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant", "user", "assistant"]
    assert api("GET", "/api/agent/sessions")[1]["sessions"][0]["id"] == sid
    assert sum(e["event"] == "ready" for e in events(api, sid)["events"]) == 1             # one process for the whole conversation


def test_a_tool_runs_inside_the_conversation_and_the_page_sees_it(setup, ws):
    s = setup([tool_call("write_file", path="nota.txt", content="ciao"), answer("Scritto.")])
    api = s["api"]
    sid = api("POST", "/api/agent/sessions", {"workspace": str(ws), "mode": "auto", "confirm_auto": True})[1]["session"]["id"]
    api("POST", f"/api/agent/sessions/{sid}/send", {"text": "scrivi nota.txt"})
    wait_for(lambda: any(e["event"] == "done" for e in events(api, sid)["events"]), seconds=60)
    assert (Path(ws) / "nota.txt").read_text(encoding="utf-8") == "ciao"
    seen = [e["data"]["type"] for e in events(api, sid)["events"] if e["event"] == "agent"]
    assert "tool_start" in seen and "tool_end" in seen


def test_stopping_a_turn_and_closing_a_conversation(setup, ws):
    s = setup([answer("lento")] * 3)
    api = s["api"]
    sid = api("POST", "/api/agent/sessions", {"workspace": str(ws), "ask_only": True})[1]["session"]["id"]
    api("POST", f"/api/agent/sessions/{sid}/send", {"text": "uno"})
    wait_for(lambda: any(e["event"] == "done" for e in events(api, sid)["events"]), seconds=60)
    assert api("POST", f"/api/agent/sessions/{sid}/cancel", {})[0] == 200
    assert api("POST", f"/api/agent/sessions/{sid}/close", {})[1]["session"]["state"] == "closed"
    code, body = api("POST", f"/api/agent/sessions/{sid}/send", {"text": "due"})
    assert code == 409 and body["error"]["code"] == "closed"


def test_what_the_api_refuses(setup, ws):
    s = setup([answer("x")])
    api = s["api"]
    assert api("POST", "/api/agent/sessions", {"workspace": "relativo"})[0] == 400
    assert api("POST", "/api/agent/sessions", {"workspace": str(ws), "mode": "auto"})[1]["error"]["code"] == "auto_confirm"
    assert api("GET", "/api/agent/sessions/nope/events")[0] == 404
    sid = api("POST", "/api/agent/sessions", {"workspace": str(ws)})[1]["session"]["id"]
    assert api("POST", f"/api/agent/sessions/{sid}/send", {"text": "   "})[0] == 400
    assert api("POST", f"/api/agent/sessions/{sid}/resume", {})[0] == 409
    code, _ = api("POST", "/api/agent/sessions", {"workspace": str(ws)}, headers={"Origin": "http://evil.example"})
    assert code == 403


def test_a_confirmation_in_the_conversation_is_answered_through_the_api(setup, ws):
    from harness.session import SessionStore  # noqa: F401  (the real process is used below)
    plan = {"plan_id": "viaggio", "title": "Viaggio a Cervinia", "nodes": {"s1": {"id": "s1", "type": "action", "title": "Prenota", "description": "", "assignee": "agent",
                                                                                     "dependencies": [], "outputs": [], "status": "pending"}}}
    s = setup([tool_call("plan_archive", plan_id="viaggio"), answer("Archiviato."), answer("x")])
    api = s["api"]
    code, _ = api("POST", "/api/plans", {"plan": {**plan, "target_type": "occasional"}})
    assert code == 201
    sid = api("POST", "/api/agent/sessions", {"workspace": str(ws), "mode": "default"})[1]["session"]["id"]
    api("POST", f"/api/agent/sessions/{sid}/send", {"text": "metti via il piano del viaggio"})
    ask = wait_for(lambda: api("GET", f"/api/agent/sessions/{sid}")[1]["session"].get("ask"), seconds=60)
    assert ask["facts"]["kind"] == "plan_archive" and "Viaggio a Cervinia" in ask["question"]
    assert any(p["plan_id"] == "viaggio" for p in api("GET", "/api/plans")[1]["plans"])           # nothing archived yet
    assert api("POST", f"/api/agent/sessions/{sid}/send", {"text": "intanto"})[0] == 409
    assert api("POST", f"/api/agent/sessions/{sid}/answer", {"ask": ask["id"], "value": "boh"})[0] == 400
    assert api("POST", f"/api/agent/sessions/{sid}/answer", {"ask": ask["id"], "value": "confirm"})[0] == 200
    wait_for(lambda: any(e["event"] == "done" for e in events(api, sid)["events"]), seconds=60)
    assert not any(p["plan_id"] == "viaggio" for p in api("GET", "/api/plans")[1]["plans"])
    assert any(p["plan_id"] == "viaggio" for p in api("GET", "/api/plans?archived=1")[1]["plans"])
