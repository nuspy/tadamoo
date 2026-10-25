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
        mgr = SessionManager(tmp_path / "j", url, api_key, board=board, tick_s=0.2, router="rules", **kw)
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
    assert api("POST", f"/api/agent/sessions/{sid}/send", {"text": "intanto"})[0] == 200        # looked at by the agent after the answer
    assert api("POST", f"/api/agent/sessions/{sid}/answer", {"ask": ask["id"], "value": "boh"})[0] == 400
    assert api("POST", f"/api/agent/sessions/{sid}/answer", {"ask": ask["id"], "value": "confirm"})[0] == 200
    wait_for(lambda: any(e["event"] == "done" for e in events(api, sid)["events"]), seconds=60)
    assert not any(p["plan_id"] == "viaggio" for p in api("GET", "/api/plans")[1]["plans"])
    assert any(p["plan_id"] == "viaggio" for p in api("GET", "/api/plans?archived=1")[1]["plans"])


def test_a_conversation_works_in_its_own_copy_and_the_person_applies_what_it_made(setup, tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=str(repo), capture_output=True, text=True, check=True)
    repo = tmp_path / "proj"
    repo.mkdir()
    git("init", "-q")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "first")
    s = setup([tool_call("write_file", path="nota.txt", content="dal compito"), answer("Scritto.")])
    api = s["api"]
    session = api("POST", "/api/agent/sessions", {"workspace": str(repo), "mode": "auto", "confirm_auto": True, "worktree": "always"})[1]["session"]
    sid = session["id"]
    assert session["worktree"] and session["workdir"] != str(repo)
    assert api("POST", f"/api/agent/sessions/{sid}/send", {"text": "scrivi una nota"})[0] == 200
    wait_for(lambda: any(e["event"] == "done" for e in events(api, sid)["events"]), seconds=60)
    assert (Path(session["workdir"]) / "nota.txt").read_text(encoding="utf-8") == "dal compito" and not (repo / "nota.txt").exists()
    code, body = api("GET", f"/api/agent/sessions/{sid}/copy")
    assert code == 200 and body["changes"] == [{"path": "nota.txt", "status": "added"}]
    code, body = api("GET", f"/api/agent/sessions/{sid}/copy/diff?path=nota.txt")
    assert code == 200 and "+dal compito" in body["diff"]
    assert api("GET", f"/api/agent/sessions/{sid}/copy/diff?path=..%2Fx")[0] in (400, 404)
    code, body = api("POST", f"/api/agent/sessions/{sid}/copy/apply", {})
    assert code == 200 and body["applied"] == ["nota.txt"] and (repo / "nota.txt").read_text(encoding="utf-8") == "dal compito"
    code, body = api("POST", f"/api/agent/sessions/{sid}/copy/discard", {})
    assert code == 200 and body["session"]["state"] == "closed" and not Path(session["workdir"]).exists()
    code, body = api("GET", f"/api/agent/sessions/{sid}/copy")
    assert code in (400, 404) and body["error"]["code"] == "no_copy"


def test_going_back_over_a_command_asks_first_and_the_tree_and_a_comparison_are_served(setup, tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=str(repo), capture_output=True, text=True, check=True)
    repo = tmp_path / "proj"
    repo.mkdir()
    git("init", "-q")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "first")
    s = setup([tool_call("write_file", path="nota.txt", content="uno"), answer("Scritto."), tool_call("shell_command", command="python -c \"print(1)\""), answer("Eseguito."),
               answer("Risposta A."), answer("Risposta B.")])
    api = s["api"]
    sid = api("POST", "/api/agent/sessions", {"workspace": str(repo), "mode": "auto", "confirm_auto": True, "worktree": "always"})[1]["session"]["id"]
    for n, text in enumerate(("scrivi una nota", "esegui un comando"), 1):
        assert api("POST", f"/api/agent/sessions/{sid}/send", {"text": text})[0] == 200
        wait_for(lambda: sum(e["event"] == "done" for e in events(api, sid)["events"]) == n, seconds=60)
    code, impact = api("GET", f"/api/agent/sessions/{sid}/back-impact?turn=1")
    assert code == 200 and impact["needs_yes"] and [a["tool"] for a in impact["not_undone"]] == ["shell_command"] and impact["reversible"] == []
    assert api("GET", f"/api/agent/sessions/{sid}/back-impact?turn=x")[0] == 400
    code, body = api("POST", f"/api/agent/sessions/{sid}/back", {"turn": 1})
    assert code == 409 and body["error"]["code"] == "needs_yes"
    code, body = api("POST", f"/api/agent/sessions/{sid}/back", {"turn": 1, "accept_irreversible": True})
    assert code == 200 and body["session"]["turns"] == 1 and body["session"]["rewinds"][0]["still_done"][0]["tool"] == "shell_command"
    root = api("GET", "/api/agent/sessions/tree")[1]["roots"][0]
    assert root["id"] == sid and root["rewinds"][0]["turn"] == 1
    code, started = api("POST", f"/api/agent/sessions/{sid}/compare", {"turn": 1, "variants": [{"text": "riassumi"}, {"text": "riassumi meglio"}]})
    assert code == 200
    cid = started["compare"]["id"]
    wait_for(lambda: api("GET", f"/api/agent/sessions/compares/{cid}")[1]["state"] == "done", seconds=120)
    result = api("GET", f"/api/agent/sessions/compares/{cid}")[1]
    assert [x["answer"] for x in result["sides"]] == ["Risposta A.", "Risposta B."] and result["files"] == []
    assert api("GET", "/api/agent/sessions/compares")[1]["compares"][0]["id"] == cid
    assert api("POST", f"/api/agent/sessions/{sid}/compare", {"turn": 1, "variants": [{"text": "solo uno"}]})[0] == 400
    assert api("GET", "/api/agent/sessions/compares/nope")[0] == 404
    kids = api("GET", "/api/agent/sessions/tree")[1]["roots"][0]["children"]
    assert [k["compare"]["side"] for k in kids] == ["a", "b"]
