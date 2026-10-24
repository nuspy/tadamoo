"""Upload, listing and download of attached files and of the files the agent made, and sending a message with files attached."""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import attachments as at
from serve import handler
from serve.agent_sessions import SessionManager
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)

STUB = str(Path(__file__).resolve().parent.parent / "serve" / "tests" / "session_stub.py")


class Api:
    def __init__(self, url):
        self.url = url

    def raw(self, method, path, data=None, headers=None):
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        code, h, raw = self.raw(method, path, data, {"Content-Type": "application/json", **(headers or {})})
        return code, json.loads(raw.decode() or "{}")

    def upload(self, session, name, data, headers=None):
        return self.raw("POST", f"/api/attachments?session={session}&name={urllib.request.quote(name)}", data, {"Content-Type": "application/octet-stream", **(headers or {})})


@pytest.fixture
def world(tmp_path, monkeypatch, start_server):  # noqa: F811
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history)],
                         tick_s=0.1, attachments_dir=tmp_path / "attachments", outputs_dir=tmp_path / "outputs")
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    monkeypatch.setattr(handler.userdata, "path", lambda name, _p=handler.userdata.path: tmp_path / name if name in ("attachments", "outputs") else _p(name))
    sid = mgr.create(str(ws))["id"]
    yield Api(start_server([answer("x")])), mgr, sid, tmp_path
    mgr.shutdown()


def until(fn, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_a_file_is_uploaded_recognized_listed_and_shown_back_safely(world):
    api, mgr, sid, tmp = world
    code, _, raw = api.upload(sid, "foto.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 50)
    meta = json.loads(raw)["attachment"]
    assert code == 200 and meta["family"] == "image" and meta["name"] == "foto.png"
    _, _, raw = api.raw("GET", f"/api/attachments?session={sid}")
    assert [i["id"] for i in json.loads(raw)["items"]] == [meta["id"]]
    code, headers, body = api.raw("GET", f"/api/attachments/{sid}/{meta['id']}/file")
    assert code == 200 and headers["Content-Type"] == "image/png" and headers["X-Content-Type-Options"] == "nosniff" and "inline" in headers["Content-Disposition"]
    assert "sandbox" in headers["Content-Security-Policy"]
    text = json.loads(api.upload(sid, "<script>.html", b"<script>alert(1)</script>")[2])["attachment"]
    code, headers, body = api.raw("GET", f"/api/attachments/{sid}/{text['id']}/file")
    assert headers["Content-Type"] == "application/octet-stream" and "attachment" in headers["Content-Disposition"]             # a page is never served as a page
    assert api.raw("GET", f"/api/attachments/{sid}/{'0' * 12}/file")[0] == 404 and api.raw("GET", f"/api/attachments/{sid}/../x/file")[0] == 404


def test_what_is_refused_at_upload(world):
    api, mgr, sid, tmp = world
    assert api.upload("ghost", "a.txt", b"x")[0] == 404                                         # no such conversation
    assert api.upload(sid, "a.txt", b"x", headers={"Origin": "http://evil.example"})[0] == 403
    assert api.raw("POST", f"/api/attachments?session={sid}&name=a.txt", b"", {"Content-Type": "application/octet-stream"})[0] == 400
    assert api.raw("POST", f"/api/attachments?session={sid}&name=a.txt", b"x", {"Transfer-Encoding": "chunked"})[0] in (400, 411)
    assert api.json("GET", f"/api/attachments?session={sid}")[1]["items"] == []


def test_an_upload_larger_than_the_connection_buffer_arrives_whole(world):
    api, mgr, sid, tmp = world
    data = bytes(range(256)) * 20000                                                              # 5 MB
    meta = json.loads(api.upload(sid, "big.bin", data)[2])["attachment"]
    assert meta["size"] == len(data) and at.AttachmentStore(tmp / "attachments").path(sid, meta["id"]).read_bytes() == data
    assert meta["sha256"] == __import__("hashlib").sha256(data).hexdigest()


def test_removing_a_file(world):
    api, mgr, sid, tmp = world
    meta = json.loads(api.upload(sid, "a.txt", b"hello")[2])["attachment"]
    assert api.json("POST", f"/api/attachments/{sid}/{meta['id']}/remove", {})[0] == 200
    assert api.json("GET", f"/api/attachments?session={sid}")[1]["items"] == [] and api.json("POST", f"/api/attachments/{sid}/{meta['id']}/remove", {})[0] == 404


def test_a_file_the_agent_made_downloads_as_an_attachment_with_its_name(world):
    api, mgr, sid, tmp = world
    meta = at.OutputStore(tmp / "outputs").save(sid, "resoconto è.csv", b"a,b\n1,2\n")
    code, headers, body = api.raw("GET", f"/api/outputs/{sid}/{meta['id']}")
    assert code == 200 and body == b"a,b\n1,2\n" and "attachment" in headers["Content-Disposition"] and "resoconto%20%C3%A8.csv" in headers["Content-Disposition"]
    assert headers["Content-Type"] == "application/octet-stream" and headers["X-Content-Type-Options"] == "nosniff"
    assert [i["name"] for i in api.json("GET", f"/api/outputs?session={sid}")[1]["items"]] == ["resoconto è.csv"]
    assert api.raw("GET", f"/api/outputs/{sid}/{'1' * 12}")[0] == 404


def test_a_message_names_its_files_and_the_page_sees_the_words_and_the_chips(world):
    api, mgr, sid, tmp = world
    meta = json.loads(api.upload(sid, "nota.txt", b"Il budget e' 5000.")[2])["attachment"]
    code, body = api.json("POST", f"/api/agent/sessions/{sid}/send", {"text": "quanto e' il budget?", "attachments": [meta["id"]]})
    assert code == 200
    ev = until(lambda: next((e for e in mgr.events(sid)["events"] if e["event"] == "user"), None))
    assert ev["text"] == "quanto e' il budget?" and ev["attachments"] == [{"id": meta["id"], "name": "nota.txt", "family": "text", "size": 18}]
    assert api.json("POST", f"/api/agent/sessions/{sid}/send", {"text": "x", "attachments": ["000000000000"]})[0] == 400
    assert api.json("POST", f"/api/agent/sessions/{sid}/send", {"text": "x", "attachments": "nota"})[0] == 400
    other = mgr.create(str(tmp / "ws"))["id"]
    assert api.json("POST", f"/api/agent/sessions/{other}/send", {"text": "x", "attachments": [meta["id"]]})[0] == 400       # not this conversation's file
