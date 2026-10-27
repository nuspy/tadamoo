"""The traces of the Chat conversations through the server: the list, the masked export, the replay and the audit ledger."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.audit import AuditLedger
from harness.stack import data_dir_for
from serve import handler
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from test_agent_gui_api import Api
from test_e2e_mock_server import answer, start_server, ws  # noqa: F401  (fixtures)
from test_replay import record

SECRET = "sk-abcdefghijklmnop1234567890"


@pytest.fixture
def setup(tmp_path, monkeypatch, start_server):  # noqa: F811
    state = {}

    def make(api_key=""):
        url = start_server([answer("ok")], api_key=api_key)
        mgr = SessionManager(tmp_path / "j", url, api_key, board=ApprovalBoard(), tick_s=0.2, router="rules")
        monkeypatch.setattr(handler, "_APPROVALS", mgr.board)
        monkeypatch.setattr(handler, "_SESSIONS", mgr)
        state.update(mgr=mgr, api=Api(url, api_key), url=url, key=api_key)
        return state

    yield make
    if "mgr" in state:
        state["mgr"].shutdown()


def conversation(state, ws, tmp_path):
    """A conversation whose saved history holds an edit, a refused command, a bad call and a final answer, with a secret in it."""
    sid = state["api"]("POST", "/api/agent/sessions", {"workspace": str(ws), "mode": "default", "worktree": "off"})[1]["session"]["id"]
    (tmp_path / "rec").mkdir()
    agent = record(tmp_path / "rec")
    messages = [dict(m) for m in agent.messages]
    messages[1]["content"] = f"make a.txt, my key is {SECRET}"
    folder = state["mgr"].history_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "history.json").write_text(json.dumps({"messages": messages, "turns": 1}), encoding="utf-8")
    return sid


def get_raw(state, path):
    headers = {"Authorization": f"Bearer {state['key']}"} if state["key"] else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(state["url"] + path, headers=headers), timeout=15) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_the_list_names_the_conversations_that_have_a_history(setup, ws, tmp_path):
    s = setup()
    api = s["api"]
    empty = api("POST", "/api/agent/sessions", {"workspace": str(ws), "worktree": "off"})[1]["session"]["id"]
    sid = conversation(s, ws, tmp_path)
    rows = api("GET", "/api/agent/traces")[1]["sessions"]
    assert [r["id"] for r in rows] == [sid] and empty not in [r["id"] for r in rows]
    assert rows[0]["bytes"] > 0 and rows[0]["too_large"] is False


def test_the_export_is_a_jsonl_trace_with_the_secret_masked(setup, ws, tmp_path):
    s = setup()
    sid = conversation(s, ws, tmp_path)
    code, headers, body = get_raw(s, f"/api/agent/traces/{sid}/export")
    assert code == 200 and "attachment" in headers["Content-Disposition"] and ".jsonl" in headers["Content-Disposition"]
    text = body.decode("utf-8")
    assert SECRET not in text and "[secret]" in text
    rows = [json.loads(ln) for ln in text.splitlines()]
    assert rows[0]["type"] == "header" and rows[0]["meta"]["session"] == sid
    assert [r["decision"] for r in rows if r.get("message", {}).get("role") == "tool"] == ["executed", "executed", "denied", "invalid"]


def test_a_replay_of_the_saved_conversation_matches_and_a_looser_level_is_reported(setup, ws, tmp_path):
    s = setup()
    api = s["api"]
    sid = conversation(s, ws, tmp_path)
    code, same = api("POST", f"/api/agent/traces/{sid}/replay", {"mode": "default", "results": True})
    assert code == 200 and same["ok"] and same["replayed_calls"] == 4 and same["divergence"] is None and "matches" in same["summary"]
    code, loose = api("POST", f"/api/agent/traces/{sid}/replay", {"mode": "auto"})                  # the command the person refused now runs
    assert code == 200 and not loose["ok"] and loose["divergence"]["index"] == 2 and loose["divergence"]["kind"] == "decision"
    assert loose["divergence"]["recorded"]["decision"] == "denied" and loose["divergence"]["replayed"]["decision"] == "executed"
    assert api("POST", f"/api/agent/traces/{sid}/replay", {"mode": "root"})[0] == 400


def test_the_ledger_is_verified_and_a_changed_row_is_found(setup, ws, tmp_path):
    s = setup()
    api = s["api"]
    sid = conversation(s, ws, tmp_path)
    assert api("GET", f"/api/agent/traces/{sid}/audit")[1]["status"] == "missing"
    ledger_path = data_dir_for(Path(ws).resolve()) / "audit.jsonl"
    ledger = AuditLedger(ledger_path)
    for n in range(3):
        ledger.record("agent", "tool", ledger.call_details("write_file", {"path": f"f{n}.txt"}, permission="allowed", outcome="ok", duration_s=0.01))
    good = api("GET", f"/api/agent/traces/{sid}/audit")[1]
    assert good["status"] == "intact" and good["count"] == 3 and good["problems"] == []
    assert [r["index"] for r in good["rows"]] == [2, 1, 0] and good["rows"][0]["tool"] == "write_file" and good["rows"][0]["outcome"] == "ok"
    assert len(api("GET", f"/api/agent/traces/{sid}/audit?limit=1")[1]["rows"]) == 1
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("f1.txt", "evil.txt")
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bad = api("GET", f"/api/agent/traces/{sid}/audit")[1]
    assert bad["status"] == "broken" and any("index 1" in p for p in bad["problems"])


def test_the_routes_ask_for_the_key_and_say_when_a_conversation_is_gone_or_has_no_history(setup, ws, tmp_path):
    s = setup(api_key="secret-key")
    api = s["api"]
    assert api("GET", "/api/agent/traces", key="")[0] == 401
    assert api("POST", "/api/agent/traces/x/replay", {}, key="")[0] == 401
    assert api("GET", "/api/agent/traces/nope/export")[0] == 404
    sid = api("POST", "/api/agent/sessions", {"workspace": str(ws), "worktree": "off"})[1]["session"]["id"]
    code, body = api("POST", f"/api/agent/traces/{sid}/replay", {})
    assert code == 400 and body["error"]["code"] == "no_trace"
