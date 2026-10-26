"""Personal skills on the server: the routes of the page, and a skill started in a conversation."""
import base64
import http.client
import json
import sys
from pathlib import Path

import pytest

from serve.agent_runs import RunError
from serve.agent_sessions import SessionManager
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
SECRET = "s3cret-of-this-server"
STUB = str(Path(__file__).with_name("session_stub.py"))
SCRIPT = 'from basalt_skill import tool\n\ndef main(params):\n    return tool("read_file", path=params.file)\n'
DEFINITION = {"name": "Read a day", "description": "reads the file of a day", "params": [
    {"name": "file", "type": "path", "label": "File", "explanation": "which file"}, {"name": "day", "type": "date", "default": "ieri"}]}


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("BASALT_CACHE", str(tmp_path / "data"))
    import serve.handler as handler
    monkeypatch.setattr(handler, "_PSKILLS", None)
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, ["</think>\n\nok"], max_context=16384), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
    svc.telemetry = _NoTelemetry()
    svc.control_token = SECRET
    httpd = serve(svc, port=0)
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json", "X-Basalt-Control": SECRET, **(headers or {})})
    r = c.getresponse()
    raw = r.read()
    c.close()
    ctype = r.getheader("Content-Type", "")
    return r.status, (json.loads(raw.decode() or "{}") if ctype.startswith("application/json") else raw), r


def test_a_skill_is_saved_listed_read_and_deleted_from_the_page(server):
    code, data, _ = call(server, "GET", "/api/personal-skills")
    assert code == 200 and data["skills"] == [] and "date" in data["types"]
    code, data, _ = call(server, "POST", "/api/personal-skills", {"skill": DEFINITION, "script": SCRIPT})
    assert code == 200 and data["skill"]["slug"] == "read-a-day" and data["skill"]["status"] == "approved" and data["skill"]["tools"] == ["read_file"]
    code, data, _ = call(server, "GET", "/api/personal-skills")
    assert [s["name"] for s in data["skills"]] == ["Read a day"] and "script" not in data["skills"][0]
    code, data, _ = call(server, "GET", "/api/personal-skills/read-a-day")
    assert data["skill"]["script"] == SCRIPT and data["skill"]["versions"] == []
    code, data, _ = call(server, "POST", "/api/personal-skills", {"skill": {**DEFINITION, "description": "v2"}, "script": SCRIPT, "slug": "read-a-day"})
    assert data["skill"]["version"] == 2
    code, data, _ = call(server, "POST", "/api/personal-skills/read-a-day/restore", {"version": 1})
    assert code == 200 and data["skill"]["version"] == 3 and data["skill"]["description"] == "reads the file of a day"
    code, data, _ = call(server, "POST", "/api/personal-skills/read-a-day/delete", {})
    assert code == 200 and call(server, "GET", "/api/personal-skills")[1]["skills"] == []
    assert call(server, "GET", "/api/personal-skills/read-a-day")[0] == 400


def test_what_cannot_be_saved_is_said(server):
    for body, word in (({"skill": DEFINITION, "script": "import os\ndef main(params): pass"}, "import os"), ({"skill": {"name": ""}, "script": SCRIPT}, "name"),
                       ({"skill": {**DEFINITION, "mode": "free"}, "script": SCRIPT}, "two confirmations"), ({"script": SCRIPT}, "definition")):
        code, data, _ = call(server, "POST", "/api/personal-skills", body)
        assert code == 400 and word in data["error"]["message"], data
    code, data, _ = call(server, "POST", "/api/personal-skills", {"skill": {**DEFINITION, "mode": "free"}, "script": "import os\ndef main(params): return os.getcwd()", "confirm_free": True})
    assert code == 200 and data["skill"]["mode"] == "free"


def test_the_script_is_checked_as_it_is_written_and_the_form_is_read_as_it_is_filled(server):
    code, data, _ = call(server, "POST", "/api/personal-skills/check", {"script": "import os\ndef main(params): pass"})
    assert code == 200 and data["problems"][0]["line"] == 1 and data["tools"] == []
    code, data, _ = call(server, "POST", "/api/personal-skills/check", {"script": SCRIPT})
    assert data["problems"] == [] and data["tools"] == ["read_file"]
    code, data, _ = call(server, "POST", "/api/personal-skills/preview", {"params": DEFINITION["params"], "values": {"day": "domani", "file": "a.txt"}})
    assert code == 200 and data["errors"] == {} and data["values"]["file"] == "a.txt" and len(data["values"]["day"]) == 10
    code, data, _ = call(server, "POST", "/api/personal-skills/preview", {"params": DEFINITION["params"], "values": {"day": "mai"}})
    assert "not a date" in data["errors"]["day"]


def test_a_skill_goes_to_another_pc_as_a_file_and_comes_back_to_approve(server):
    call(server, "POST", "/api/personal-skills", {"skill": DEFINITION, "script": SCRIPT})
    code, data, resp = call(server, "GET", "/api/personal-skills/read-a-day/export")
    assert code == 200 and resp.getheader("Content-Disposition") == 'attachment; filename="read-a-day.pskill"' and data[:2] == b"PK"
    code, imported, _ = call(server, "POST", "/api/personal-skills/import", {"data": base64.b64encode(data).decode()})
    assert code == 200 and imported["skill"]["status"] == "pending" and imported["skill"]["slug"] == "read-a-day-2"
    code, approved, _ = call(server, "POST", "/api/personal-skills/read-a-day-2/approve", {})
    assert approved["skill"]["status"] == "approved"
    for bad in ("###", "", None):
        assert call(server, "POST", "/api/personal-skills/import", {"data": bad})[0] == 400


def test_only_the_servers_own_page_may_change_skills(server):
    assert call(server, "POST", "/api/personal-skills", {"skill": DEFINITION, "script": SCRIPT}, {"Origin": "http://evil.example"})[0] == 403
    assert call(server, "POST", "/api/personal-skills", {"skill": DEFINITION, "script": SCRIPT}, {"X-Basalt-Control": "wrong"})[0] == 403


# ---- a skill started in a conversation (the manager, with the stand-in process) ----------------------------------------------------

def stub_command(sid, rec, history):
    return [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history)]


@pytest.fixture
def manager(tmp_path):
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=stub_command, tick_s=0.1, skills_dir=tmp_path / "skills")
    yield mgr
    mgr.shutdown()


def until(predicate, seconds=15):
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_a_skill_starts_in_the_conversation_with_its_values_worked_out_and_is_said_as_the_request(manager, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    manager.skills.save(DEFINITION, SCRIPT)
    sid = manager.create(str(ws))["id"]
    out = manager.run_skill(sid, "read-a-day", {"file": "notes.txt"})
    events = lambda: [e["event"] for e in manager.events(sid)["events"]]
    until(lambda: "done" in events())
    first = manager.events(sid)["events"][0]
    assert first["event"] == "user" and first["text"].startswith("Skill “Read a day”: File = notes.txt; day = 20") and first["turn"] == out["turn"]
    done = next(e for e in manager.events(sid)["events"] if e["event"] == "done")
    values = json.loads(done["final"].split(" ", 4)[-1])
    assert values["file"] == {"type": "path", "value": "notes.txt"} and values["day"]["type"] == "date" and len(values["day"]["value"]) == 10
    assert manager.skills.get("read-a-day")["uses"] == 1
    assert manager.get(sid)["title"] == "Read a day"


def test_a_skill_that_is_not_approved_or_has_bad_values_or_does_not_exist_does_not_start(manager, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    manager.skills.save(DEFINITION, SCRIPT, approve=False)
    sid = manager.create(str(ws))["id"]
    with pytest.raises(RunError) as err:
        manager.run_skill(sid, "read-a-day", {"file": "a"})
    assert err.value.code == "needs_approval"
    manager.skills.approve("read-a-day")
    for slug, values, code in (("read-a-day", {}, "bad_values"), ("read-a-day", {"file": "a", "day": "never"}, "bad_values"), ("read-a-day", {"file": "a", "x": 1}, "bad_values"),
                               ("nope", {}, "no_skill"), (5, {}, "bad_skill"), ("read-a-day", [], "bad_skill")):
        with pytest.raises(RunError) as err:
            manager.run_skill(sid, slug, values)
        assert err.value.code == code, (slug, values)
    assert [e["event"] for e in manager.events(sid)["events"]] == []


def test_a_skill_waits_for_the_agent_to_finish_and_a_closed_conversation_takes_none(manager, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    manager.skills.save(DEFINITION, SCRIPT)
    sid = manager.create(str(ws))["id"]
    manager.send(sid, "question")
    with pytest.raises(RunError) as err:
        manager.run_skill(sid, "read-a-day", {"file": "a"})
    assert err.value.code == "busy"
    until(lambda: manager.get(sid)["state"] == "idle")
    manager.close(sid)
    with pytest.raises(RunError) as err:
        manager.run_skill(sid, "read-a-day", {"file": "a"})
    assert err.value.code == "closed"
