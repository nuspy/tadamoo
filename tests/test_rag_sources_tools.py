"""The tools that check and update an archive for a conversation: they ask the server, follow its job, and never remove or reveal what they should not."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sources_support import PROJ, Counting, body, make
from tadamoo.rag.recall import ProjectRecall
from tadamoo.rag.sync_tools import register_sync_tools, server_call
from tadamoo.tools.registry import EffectClass, ToolRegistry


def setup(tmp_path, call, local=True):
    store, sync, aid = make(tmp_path, Counting())
    src = tmp_path / "docs"
    src.mkdir()
    (src / "a.md").write_text(body(1, "alpha"), encoding="utf-8")
    sync.add_source(aid, "folder", {"path": str(src)})
    recall = ProjectRecall(store, PROJ, local=lambda: local)
    registry = ToolRegistry()
    register_sync_tools(registry, store, recall, call(sync, aid) if callable(call) else call)
    return registry, store, sync, aid


def direct(sync, aid):
    """Stands in for the server: runs the check or the update itself and answers the result."""
    def call(path, req):
        assert path.startswith(f"archives/{aid}/")
        if path.endswith("check"):
            return sync.check(aid)
        return sync.update(aid, include_new=req["include_new"])
    return call


def test_the_tools_are_described_and_have_the_right_level(tmp_path):
    registry, *_ = setup(tmp_path, direct)
    check, update = registry.get("knowledge_check"), registry.get("knowledge_update")
    assert check.effect == EffectClass.PURE and update.effect == EffectClass.REVERSIBLE and check.untrusted and update.untrusted
    assert "aggiorna il RAG" in update.description and "company wiki" in update.description and "is the company wiki" in check.description


def test_a_conversation_checks_then_updates_an_archive_by_name(tmp_path):
    registry, store, sync, aid = setup(tmp_path, direct)
    out = registry.execute("knowledge_check", {"archive": "sync test"})
    assert out["status"] == "ok" and out["counts"] == {"new": 1} and out["documents"] == [{"document": "a.md", "state": "new", "detail": out["documents"][0]["detail"]}]
    assert store.get(aid)["documents"] == 0
    done = registry.execute("knowledge_update", {"archive": "Sync test"})
    assert done["status"] == "ok" and done["counts"] == {"added": 1} and done["embedded"] >= 0 and store.get(aid)["documents"] == 1
    (tmp_path / "docs" / "a.md").unlink()
    gone = registry.execute("knowledge_check", {"archive": "sync test"})
    assert gone["counts"] == {"gone": 1} and "decides" in gone["note"]
    assert registry.execute("knowledge_update", {"archive": "sync test"})["counts"] == {"gone": 1} and store.get(aid)["documents"] == 1      # never removed


def test_a_wrong_name_and_an_archive_that_may_not_leave_this_pc(tmp_path):
    registry, store, sync, aid = setup(tmp_path, direct, local=False)
    assert registry.execute("knowledge_check", {"archive": "nothing like it"})["status"] == "none"
    assert registry.execute("knowledge_check", {"archive": "sync test"})["status"] == "not_allowed"
    store.update(aid, {"cloud_ok": True})
    assert registry.execute("knowledge_check", {"archive": "sync test"})["status"] == "ok"


def test_without_a_server_on_this_pc_the_tools_say_so(tmp_path):
    registry, *_ = setup(tmp_path, None)
    out = registry.execute("knowledge_update", {"archive": "sync test"})
    assert out["status"] == "error" and "server" in out["note"]
    assert server_call("https://example.org") is None and server_call("http://192.168.1.5:7232") is None


def test_a_failure_of_the_server_is_told(tmp_path):
    def broken(sync, aid):
        def call(path, req):
            raise RuntimeError("a heavy job has the local model out")
        return call
    registry, *_ = setup(tmp_path, broken)
    out = registry.execute("knowledge_update", {"archive": "sync test"})
    assert out["status"] == "error" and "heavy job" in out["note"]

    def slow(sync, aid):
        return lambda path, req: {"running": "abc"}
    registry, *_ = setup(tmp_path / "two", slow)
    assert registry.execute("knowledge_update", {"archive": "sync test"})["status"] == "running"


def test_the_server_client_follows_a_job_to_its_result():
    state = {"polls": 0, "paths": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            state["paths"].append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self._send({"job": {"id": "j1", "state": "running"}})

        def do_GET(self):
            state["polls"] += 1
            self._send({"job": {"id": "j1", "state": "done" if state["polls"] > 1 else "running", "result": {"summary": "nothing changed", "counts": {}, "items": []}}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        call = server_call(f"http://127.0.0.1:{server.server_address[1]}", "key", sleep=lambda s: None)
        assert call("archives/x/check", {})["summary"] == "nothing changed"
        assert state["paths"][0] == ("/api/rag/archives/x/check", "Bearer key", {}) and state["polls"] == 2
        state["polls"] = -100
        assert server_call(f"http://127.0.0.1:{server.server_address[1]}", wait_s=0, sleep=lambda s: None)("archives/x/check", {}) == {"running": "j1"}
    finally:
        server.shutdown()
        server.server_close()
