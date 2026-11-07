"""/api/sessions-base through the real handler: where the conversations are kept, a check of a move, and the move itself."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler, userdata
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from test_agent_gui_api import Api  # noqa: E402
from test_e2e_mock_server import start_server, ws  # noqa: F401,E402  (fixtures)


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server(["unused"])
    mgr = SessionManager(userdata.sessions_base(), url, board=ApprovalBoard())
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    yield Api(url), mgr, tmp_path
    mgr.shutdown()


def test_it_says_where_the_conversations_are_and_how_many_run(api):
    client, mgr, tmp = api
    code, got = client("GET", "/api/sessions-base")
    assert code == 200 and got["chosen"] is False and got["running"] == 0
    assert Path(got["current"]) == Path(got["default"]) == tmp / "data" / "journal" / "sessions"


def test_a_check_then_a_move_keeps_the_original_and_asks_for_a_restart(api, ws):
    client, mgr, tmp = api
    mgr.create(str(ws))
    target = tmp / "chats"
    code, checked = client("POST", "/api/sessions-base/check", {"path": str(target)})
    assert code == 200 and checked["ok"] and checked["files"] >= 1 and not target.exists()
    code, done = client("POST", "/api/sessions-base/move", {"path": str(target)})
    assert code == 200 and done["restart"] and (target / "sessions" / "agent_sessions.json").exists()
    assert (tmp / "data" / "journal" / "sessions" / "agent_sessions.json").exists()
    assert client("GET", "/api/sessions-base")[1]["chosen"] is True
    assert json.loads((tmp / "data" / "sessions-base.json").read_text(encoding="utf-8"))["path"] == str(target)


def test_a_path_that_cannot_be_used_is_a_clear_refusal(api):
    client, mgr, tmp = api
    assert client("POST", "/api/sessions-base/check", {})[0] == 400
    code, bad = client("POST", "/api/sessions-base/move", {"path": "relative/dir"})
    assert code == 409 and bad["error"]["code"] == "relative"
