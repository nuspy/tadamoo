"""A file of the folder a conversation works in can be saved from the page; nothing outside that folder can."""
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.agent_runs import RunError
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from test_e2e_mock_server import start_server  # noqa: F401  (fixture)


@pytest.fixture
def world(tmp_path, monkeypatch, start_server):  # noqa: F811
    url = start_server(["ok"])
    folder = tmp_path / "site"
    (folder / "src").mkdir(parents=True)
    (folder / ".tadamoo").mkdir()
    (folder / ".git").mkdir()
    (folder / "src" / "app.py").write_bytes(b"print('hi')" + bytes([10]))
    (folder / "notes è.txt").write_text("note", encoding="utf-8")
    (folder / ".tadamoo" / "secret.json").write_text("{}", encoding="utf-8")
    (folder / ".git" / "config").write_text("x", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    mgr = SessionManager(tmp_path / "j", url, "", board=ApprovalBoard(), tick_s=0.2, router="rules")
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    sid = mgr.create(str(folder), "read_only", ask_only=True)["id"]
    yield url, mgr, sid, folder
    mgr.shutdown()


def get(url, sid, path):
    try:
        with urllib.request.urlopen(f"{url}/api/agent/sessions/{sid}/file?path={urllib.parse.quote(path)}", timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_a_file_in_the_folder_is_sent_as_a_download_with_its_own_name(world):
    url, mgr, sid, folder = world
    code, headers, body = get(url, sid, "src/app.py")
    assert (code, body) == (200, b"print('hi')\n")
    assert "attachment" in headers["Content-Disposition"] and "app.py" in headers["Content-Disposition"]
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Content-Security-Policy"] == "sandbox"
    code, headers, body = get(url, sid, "notes è.txt")
    assert (code, body) == (200, b"note") and "notes%20%C3%A8.txt" in headers["Content-Disposition"]
    assert get(url, sid, "src\\app.py")[0] == 200                                      # a Windows path works too


@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt", "..\\outside.txt", "/etc/passwd", "C:\\Windows\\win.ini", ".tadamoo/secret.json",
                                  ".git/config", "src", "missing.txt", "", "src/app.py\0"])
def test_nothing_outside_the_folder_or_in_the_agents_own_files_is_sent(world, path):
    url, mgr, sid, folder = world
    code, _, body = get(url, sid, path)
    assert code in (400, 404, 409) and b"outside" not in body and b"secret" not in body


def test_a_big_file_is_saved_and_an_unknown_conversation_is_refused(world):
    url, mgr, sid, folder = world
    (folder / "big.bin").write_bytes(b"0" * 3_000_000)
    code, _, body = get(url, sid, "big.bin")
    assert code == 200 and len(body) == 3_000_000
    assert get(url, "nosuch", "src/app.py")[0] in (404, 409)
    with pytest.raises(RunError):
        mgr.workspace_file(sid, "../outside.txt")
