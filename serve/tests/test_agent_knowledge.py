"""The knowledge archives a conversation of the agent has attached: kept in its record, handed to its process, changed while it runs."""
import pytest

from serve.agent_runs import RunError
from serve.agent_sessions import SessionManager
from tadamoo.rag import RagStore, project_key


@pytest.fixture(autouse=True)
def own_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))


@pytest.fixture
def world(tmp_path):
    (tmp_path / "w").mkdir()
    store = RagStore(tmp_path / "rag")
    other = project_key(tmp_path / "elsewhere")
    ids = {"own": store.create("Site notes", "", project_key(tmp_path / "w"), "w")["id"], "forest": store.create("Forest Management", "", other, "Elsewhere")["id"],
           "fires": store.create("Forest Fires", "", other, "Elsewhere")["id"]}
    manager = SessionManager(tmp_path / "j", "http://127.0.0.1:1", rag_dir=tmp_path / "rag", tick_s=0.1)
    yield manager, ids, str(tmp_path / "w")
    manager.shutdown()


def test_an_archive_is_attached_by_name_and_kept_with_the_conversation(world, tmp_path):
    manager, ids, ws = world
    sid = manager.create(ws)["id"]
    out = manager.set_knowledge(sid, "attach", "forest management")
    assert out["result"]["status"] == "attached" and out["result"]["archive"]["name"] == "Forest Management" and out["session"]["knowledge"] == [ids["forest"]]
    assert manager.set_knowledge(sid, "attach", "forest management")["result"]["status"] == "already"
    cmd = manager._command(sid, manager._record(sid), tmp_path / "h")
    assert cmd[cmd.index("--rag-attached") + 1] == ids["forest"] and cmd[cmd.index("--rag-project") + 1] == project_key(ws)
    manager.shutdown()
    again = SessionManager(tmp_path / "j", "http://127.0.0.1:1", rag_dir=tmp_path / "rag", tick_s=0.1)
    try:
        assert again.get(sid)["knowledge"] == [ids["forest"]]                                  # still there after the server restarts
    finally:
        again.shutdown()


def test_nothing_is_attached_on_a_guess_and_the_project_own_archive_needs_no_attaching(world):
    manager, ids, ws = world
    sid = manager.create(ws)["id"]
    amb = manager.set_knowledge(sid, "attach", "forest")
    assert amb["result"]["status"] == "ambiguous" and {c["name"] for c in amb["result"]["candidates"]} == {"Forest Management", "Forest Fires"} and not amb["session"].get("knowledge")
    assert manager.set_knowledge(sid, "attach", "astronomy")["result"]["status"] == "none"
    assert manager.set_knowledge(sid, "attach", "site notes")["result"]["status"] == "this_project" and not manager.get(sid).get("knowledge")


def test_detach_and_clear(world):
    manager, ids, ws = world
    sid = manager.create(ws)["id"]
    manager.set_knowledge(sid, "attach", "management")
    manager.set_knowledge(sid, "attach", "fires")
    assert manager.set_knowledge(sid, "detach", "fires")["session"]["knowledge"] == [ids["forest"]]
    assert manager.set_knowledge(sid, "detach", "fires")["result"]["status"] == "not_attached"
    assert manager.set_knowledge(sid, "clear")["result"] == {"status": "cleared", "dropped": 1} and manager.get(sid)["knowledge"] == []


def test_a_conversation_can_start_with_archives_and_bad_requests_are_refused(world):
    manager, ids, ws = world
    assert manager.create(ws, knowledge=[ids["fires"], ids["fires"], "gone-000000"])["knowledge"] == [ids["fires"]]
    sid = manager.create(ws)["id"]
    for args in (("swap", "x"), ("attach", 5)):
        with pytest.raises(RunError):
            manager.set_knowledge(sid, *args)
    with pytest.raises(RunError):
        manager.create(ws, knowledge="forest")
    manager.close(sid)
    with pytest.raises(RunError):
        manager.set_knowledge(sid, "attach", "forest")


def test_what_the_process_reports_is_kept_in_the_record(world):
    manager, ids, ws = world
    sid = manager.create(ws)["id"]
    live = type("Live", (), {})()
    manager._live[sid] = live
    manager._on_event(sid, live, {"event": "knowledge", "ids": [ids["forest"]], "change": "attached", "name": "Forest Management"})
    assert manager.get(sid)["knowledge"] == [ids["forest"]]
    assert manager.events(sid)["events"][-1]["change"] == "attached"
    manager._live.pop(sid)                                                                   # a stand-in, not a process to end


def test_the_route_attaches_and_detaches_over_http(tmp_path):
    import http.client
    import json
    from unittest import mock

    from serve import handler
    from serve.engine.mock import MockEngine
    from serve.frontend import ChatTemplate
    from serve.handler import serve
    from serve.service import Service
    from serve.util.text import ByteTokenizer

    (tmp_path / "w").mkdir()
    store = RagStore(tmp_path / "rag")
    forest = store.create("Forest Management", "", project_key(tmp_path / "x"), "X")["id"]
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", rag_dir=tmp_path / "rag", tick_s=0.1)
    tok = ByteTokenizer()
    root = handler.ROOT if hasattr(handler, "ROOT") else None
    svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(root / "serve" / "chat_template.jinja"))
    with mock.patch.object(handler, "_SESSIONS", mgr):
        httpd = serve(svc, port=0)
        try:
            def call(path, body):
                c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=15)
                try:
                    c.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
                    r = c.getresponse()
                    return r.status, json.loads(r.read() or b"null")
                finally:
                    c.close()
            sid = mgr.create(str(tmp_path / "w"))["id"]
            status, out = call(f"/api/agent/sessions/{sid}/knowledge", {"attach": "forest"})
            assert status == 200 and out["result"]["status"] == "attached" and out["session"]["knowledge"] == [forest]
            assert call(f"/api/agent/sessions/{sid}/knowledge", {"detach": "forest"})[1]["session"]["knowledge"] == []
            call(f"/api/agent/sessions/{sid}/knowledge", {"attach": "forest"})
            assert call(f"/api/agent/sessions/{sid}/knowledge", {"clear": True})[1]["result"]["status"] == "cleared"
            assert call(f"/api/agent/sessions/{sid}/knowledge", {"swap": "x"})[0] == 400 and call(f"/api/agent/sessions/{sid}/knowledge", {})[0] == 400
        finally:
            httpd.shutdown()
            httpd.server_close()
            mgr.shutdown()
