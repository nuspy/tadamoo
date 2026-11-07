"""/api/file-owners through the real handler: set, list and remove who owns a shared file; the owner is resolved to an id."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from test_agent_gui_api import Api  # noqa: E402
from test_e2e_mock_server import start_server, ws  # noqa: F401,E402  (fixtures)


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server(["unused"])
    mgr = SessionManager(tmp_path / "j", url, board=ApprovalBoard())
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    monkeypatch.setattr(handler, "_FILE_OWNERS", None)
    yield Api(url), mgr
    mgr.shutdown()


def test_setting_an_owner_resolves_its_name_to_an_id_and_lists_it(api, ws):
    client, mgr = api
    sid = mgr.create(str(ws), alias="boss")["id"]
    target = Path(ws) / "log.md"
    target.write_text("x", encoding="utf-8")
    code, made = client("POST", "/api/file-owners/set", {"path": str(target), "owner": "boss", "mode": "append"})
    assert code == 200 and made["file"]["owner"] == sid and made["file"]["mode"] == "append"
    code, listed = client("GET", "/api/file-owners")
    assert code == 200 and len(listed["files"]) == 1 and next(iter(listed["files"].values()))["owner"] == sid
    code, removed = client("POST", "/api/file-owners/remove", {"path": str(target)})
    assert code == 200 and removed["removed"] is True
    assert client("GET", "/api/file-owners")[1]["files"] == {}


def test_an_unknown_owner_name_is_refused(api):
    client, _ = api
    code, data = client("POST", "/api/file-owners/set", {"path": "x", "owner": "nobody"})
    assert code == 404 and data["error"]["code"] == "gone"


def test_what_is_wrong_with_a_setting_is_said(api, ws):
    client, mgr = api
    mgr.create(str(ws), alias="boss")
    code, data = client("POST", "/api/file-owners/set", {"path": "x", "owner": "boss", "mode": "delete"})
    assert code == 400 and data["error"]["field"] == "mode"


def test_the_session_manager_passes_the_file_owners_path_to_the_conversations_command(tmp_path, monkeypatch, ws):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", board=ApprovalBoard(), file_owners_path=tmp_path / "data" / "file_owners.json")
    sid = mgr.create(str(ws))["id"]
    cmd = mgr._command(sid, mgr.get(sid), mgr.history_dir(sid))
    assert "--file-owners" in cmd and cmd[cmd.index("--file-owners") + 1] == str(tmp_path / "data" / "file_owners.json")
    mgr.shutdown()
