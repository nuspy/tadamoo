"""ACP agent: `session/load` brings a saved conversation back, the file tools go through an editor that offers `fs/*`, and a server that wants
a key makes the agent offer `authenticate`. All against a scripted editor over in-memory pipes: no Zed or JetBrains was involved."""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_acp import Editor, editor_factory, _no_clock  # noqa: F401  (fixtures)
from test_agent_loop import ScriptedClient, call, say

CAPS_FS = {"fs": {"readTextFile": True, "writeTextFile": True}}


class FsEditor(Editor):
    """An editor that holds the text of some files (`buffers`, by absolute path) and answers the agent's fs requests."""

    def __init__(self, *args, buffers=None, fail_write=False, **kw):
        self.buffers, self.fail_write, self.fs_calls = dict(buffers or {}), fail_write, []
        super().__init__(*args, **kw)

    def _respond(self):
        while not self.stop:
            with self.out.cv:
                self.out.cv.wait(0.05)
                new = self.out.messages[self._answered:]
                self._answered = len(self.out.messages)
            for m in new:
                method, params = m.get("method"), m.get("params") or {}
                if method == "fs/read_text_file":
                    self.fs_calls.append(("read", params["path"]))
                    text = self.buffers.get(params["path"])
                    reply = {"jsonrpc": "2.0", "id": m["id"], **({"result": {"content": text}} if text is not None else {"error": {"code": -32002, "message": "not open"}})}
                    self.pipe.push(reply)
                elif method == "fs/write_text_file":
                    self.fs_calls.append(("write", params["path"], params["content"]))
                    if self.fail_write:
                        self.pipe.push({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": "read only"}})
                    else:
                        self.buffers[params["path"]] = params["content"]
                        self.pipe.push({"jsonrpc": "2.0", "id": m["id"], "result": {}})


# -- session/load --------------------------------------------------------------------------------------------------


def test_a_saved_conversation_comes_back_as_updates_and_the_next_prompt_goes_on_from_it(tmp_path):
    first = Editor(tmp_path, ScriptedClient(call("write_file", {"path": "a.txt", "content": "v1\n"}, "t1"), say("Wrote a.txt.")))
    sid = first.new_session()
    assert first.response(first.prompt(sid, "make a.txt"))["result"]["stopReason"] == "end_turn"
    first.close()

    second = Editor(tmp_path, ScriptedClient(say("Here again.")))
    second.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    loaded = second.call("session/load", {"sessionId": sid, "cwd": str(second.ws), "mcpServers": []})
    assert loaded["result"]["modes"]["currentModeId"] == "default"
    kinds = [(u["sessionUpdate"], u.get("content", {}).get("text") if isinstance(u.get("content"), dict) else None) for u in second.updates()]
    assert kinds[0] == ("user_message_chunk", "make a.txt")
    assert [k for k, _ in kinds] == ["user_message_chunk", "tool_call", "tool_call_update", "agent_message_chunk"]
    assert kinds[-1] == ("agent_message_chunk", "Wrote a.txt.")
    update = second.updates("tool_call_update")[0]
    assert update["toolCallId"] == "t1" and update["status"] == "completed"
    assert second.updates("tool_call")[0]["kind"] == "edit"
    assert second.response(second.prompt(sid, "and again"))["result"]["stopReason"] == "end_turn"
    sent = second.client.sent[0]
    assert [m["content"] for m in sent if m["role"] == "user"][-1] == "and again" and any(m.get("content") == "Wrote a.txt." for m in sent)
    second.close()


def test_a_session_that_is_not_there_or_a_name_that_climbs_out_of_the_folder_is_refused(tmp_path):
    e = Editor(tmp_path, ScriptedClient())
    e.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    for name in ("acp_000000000000", "../data", "..\\data", "acp_../../x", "", None, 5):
        assert e.call("session/load", {"sessionId": name, "cwd": str(e.ws)})["error"]["code"] == -32602, name
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "history.json").write_text(json.dumps({"messages": [{"role": "user", "content": "SECRET"}]}), encoding="utf-8")
    assert e.call("session/load", {"sessionId": "../../secret", "cwd": str(e.ws)})["error"]["code"] == -32602
    assert not [u for u in e.updates() if "SECRET" in json.dumps(u)]
    e.close()


def test_loading_a_session_that_is_still_open_replays_it_without_building_it_again(tmp_path):
    e = Editor(tmp_path, ScriptedClient(say("one")))
    sid = e.new_session()
    e.response(e.prompt(sid, "first"))
    before = e.server.sessions[sid]
    e.call("session/load", {"sessionId": sid, "cwd": str(e.ws)})
    assert e.server.sessions[sid] is before and [u["content"]["text"] for u in e.updates("user_message_chunk")] == ["first"]
    e.close()


# -- fs/* ---------------------------------------------------------------------------------------------------------


def test_the_file_tools_read_the_editors_text_and_write_through_the_editor(tmp_path):
    e = FsEditor(tmp_path, ScriptedClient(call("read_file", {"path": "a.txt"}, "r1"), call("edit_file", {"path": "a.txt", "target": "unsaved", "replacement": "saved"}, "e1"),
                                          call("write_file", {"path": "new.txt", "content": "fresh\n"}, "w1"), say("done")))
    a, new = str((e.ws / "a.txt").resolve()), str((e.ws / "new.txt").resolve())
    (e.ws / "a.txt").write_text("on disk\n", encoding="utf-8")
    e.buffers[a] = "unsaved text\n"
    e.call("initialize", {"protocolVersion": 1, "clientCapabilities": CAPS_FS})
    sid = e.call("session/new", {"cwd": str(e.ws)})["result"]["sessionId"]
    assert e.response(e.prompt(sid, "go"))["result"]["stopReason"] == "end_turn"
    assert "unsaved text" in e.client.sent[1][-1]["content"]                       # the model saw the editor's text, not the disk's
    assert e.buffers[a] == "saved text\n" and e.buffers[new] == "fresh\n"
    assert (e.ws / "a.txt").read_text(encoding="utf-8") == "on disk\n" and not (e.ws / "new.txt").exists()      # the agent wrote nothing itself
    assert [c[0] for c in e.fs_calls] == ["read", "read", "write", "write"] and e.fs_calls[2][2] == "saved text\n"
    diffs = [u["content"][0] for u in e.updates("tool_call_update") if u["toolCallId"] in ("e1", "w1")]
    assert diffs[0]["type"] == "diff" and diffs[0]["oldText"] == "unsaved text\n" and diffs[0]["newText"] == "saved text\n"
    e.close()


def test_a_file_the_editor_does_not_know_is_read_from_disk_and_a_refused_write_is_an_error_for_the_model(tmp_path):
    e = FsEditor(tmp_path, ScriptedClient(call("read_file", {"path": "b.txt"}, "r1"), call("write_file", {"path": "c.txt", "content": "x"}, "w1"), say("done")), fail_write=True)
    (e.ws / "b.txt").write_text("disk text\n", encoding="utf-8")
    e.call("initialize", {"protocolVersion": 1, "clientCapabilities": CAPS_FS})
    sid = e.call("session/new", {"cwd": str(e.ws)})["result"]["sessionId"]
    e.response(e.prompt(sid, "go"))
    assert "disk text" in e.client.sent[1][-1]["content"]
    assert "the editor did not write" in e.client.sent[2][-1]["content"] and not (e.ws / "c.txt").exists()
    e.close()


def test_without_the_capability_the_agent_asks_the_editor_for_nothing(tmp_path):
    e = FsEditor(tmp_path, ScriptedClient(call("write_file", {"path": "d.txt", "content": "x"}, "w1"), say("done")))
    e.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": True}}})        # reads only: the write stays with the agent
    sid = e.call("session/new", {"cwd": str(e.ws)})["result"]["sessionId"]
    e.response(e.prompt(sid, "go"))
    assert (e.ws / "d.txt").read_text(encoding="utf-8") == "x" and [c[0] for c in e.fs_calls] == []
    e.close()


# -- authenticate -------------------------------------------------------------------------------------------------


@pytest.fixture
def keyed_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            ok = self.headers.get("Authorization") == "Bearer right-key"
            self.send_response(200 if ok else 401)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_a_server_that_wants_a_key_makes_the_agent_offer_authenticate_and_refuse_sessions_until_then(tmp_path, keyed_server):
    e = Editor(tmp_path, None, endpoint=keyed_server, api_key=None)
    res = e.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})["result"]
    assert [m["id"] for m in res["authMethods"]] == ["basalt_api_key"]
    refused = e.call("session/new", {"cwd": str(e.ws)})["error"]
    assert refused["code"] == -32000 and refused["data"] == {"authRequired": True} and "BASALT_API_KEY" in refused["message"]
    assert e.call("session/load", {"sessionId": "acp_000000000000", "cwd": str(e.ws)})["error"]["code"] == -32000
    assert e.call("authenticate", {"methodId": "nope"})["error"]["code"] == -32602
    assert e.call("authenticate", {"methodId": "basalt_api_key"})["error"]["code"] == -32000          # there is still no key
    e.close()


def test_a_key_the_server_accepts_authenticates_and_one_it_refuses_does_not(tmp_path, keyed_server):
    (tmp_path / "good").mkdir()
    good = Editor(tmp_path / "good", None, endpoint=keyed_server, api_key="right-key")
    assert good.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})["result"]["authMethods"] == []
    assert good.call("authenticate", {"methodId": "basalt_api_key"})["result"] == {}
    assert good.call("session/new", {"cwd": str(good.ws)})["result"]["sessionId"].startswith("acp_")
    good.close()
    (tmp_path / "bad").mkdir()
    bad = Editor(tmp_path / "bad", None, endpoint=keyed_server, api_key="wrong-key")
    assert bad.call("authenticate", {"methodId": "basalt_api_key"})["error"]["code"] == -32000
    bad.close()


def test_an_injected_client_never_probes_the_server(tmp_path):
    e = Editor(tmp_path, ScriptedClient(), endpoint="http://127.0.0.1:9", api_key=None)               # nothing listens there: a probe would be slow or fail
    started = time.monotonic()
    assert e.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})["result"]["authMethods"] == []
    assert time.monotonic() - started < 2
    e.close()
