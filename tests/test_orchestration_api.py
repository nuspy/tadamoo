"""OrchestrationClient against the real handler: a conversation's own id and token let it deliver a message or spawn another one."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from tadamoo.integrations._http import ServiceError
from tadamoo.integrations.orchestration_client import OrchestrationClient
from test_e2e_mock_server import start_server  # noqa: E402  (fixture)


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
                         command=lambda sid, rec, history: [sys.executable, str(Path(__file__).resolve().parent.parent / "serve" / "tests" / "session_stub.py"),
                                                            "--session-id", sid, "--history-dir", str(history)])
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    yield url, mgr
    mgr.shutdown()


def until(predicate, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def test_a_conversation_delivers_a_message_to_another_with_its_own_identity(setup, ws):
    url, mgr = setup
    boss = mgr.create(ws, alias="boss")["id"]
    mgr.create(ws, alias="log")
    client = OrchestrationClient(url, session_id=boss, session_token=mgr.session_token(boss))
    out = client.deliver("log", "append: sold at 12:00")
    assert out["delivery"]["provenance"] == "agent:boss" and out["delivery"]["state"] == "pending"
    until(lambda: mgr.deliveries.get(out["delivery"]["id"])["state"] == "delivered")


def test_without_a_valid_token_the_client_is_refused(setup, ws):
    url, mgr = setup
    mgr.create(ws, alias="log")
    client = OrchestrationClient(url, session_id="deadbeef", session_token="wrong")
    with pytest.raises(ServiceError) as e:
        client.deliver("log", "x")
    assert e.value.kind == "unauthorized"


def test_status_names_the_state_of_the_other_conversation(setup, ws):
    url, mgr = setup
    mgr.create(ws, alias="night")
    client = OrchestrationClient(url)
    status = client.status("night")
    assert status["alias"] == "night" and status["state"] == "dormant"


def test_a_conversation_spawns_another_one_with_its_own_rules_and_sends_it_the_task(setup, ws):
    url, mgr = setup
    parent = mgr.create(ws, alias="boss")["id"]
    client = OrchestrationClient(url, session_id=parent, session_token=mgr.session_token(parent))
    made = client.create(ws, title="watch it", alias="watcher", unattended=True, rules="Never spend money.")
    sid = made["session"]["id"]
    assert mgr.get(sid)["parent"] == parent and mgr.get(sid)["depth"] == 1 and mgr.get(sid)["rules_file"]
    client.send(sid, "watch the log")
    until(lambda: mgr.get(sid)["turns"] >= 1 or "done" in [e["event"] for e in mgr.events(sid)["events"]])
