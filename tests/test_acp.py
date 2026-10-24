import json
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.acp_agent import ACPAgentServer
from harness.client import BasaltError
from test_agent_loop import ScriptedClient, call, say


class Pipe:
    def __init__(self):
        self.q = queue.Queue()

    def push(self, obj):
        self.q.put(obj if isinstance(obj, str) else json.dumps(obj))

    def close(self):
        self.q.put(None)

    def __iter__(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            yield item + "\n"


class Out:
    def __init__(self):
        self.messages, self.cv = [], threading.Condition()

    def write(self, s):
        with self.cv:
            self.messages.append(json.loads(s))
            self.cv.notify_all()

    def flush(self):
        pass


class Editor:
    """Plays the editor's side: sends requests, answers permission requests, records every notification."""

    def __init__(self, tmp_path, client, answer=None):
        self.ws = tmp_path / "ws"
        self.ws.mkdir(exist_ok=True)
        self.pipe, self.out, self.client, self.answer = Pipe(), Out(), client, answer
        self.server = ACPAgentServer(workspace_dir=str(self.ws), client=client, in_stream=self.pipe, out_stream=self.out,
                                     data_dir=str(tmp_path / "data"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._id = 0
        self.permission_requests = []
        self._answered = 0
        self.responder = threading.Thread(target=self._respond, daemon=True)
        self.stop = False
        self.responder.start()

    def _respond(self):
        while not self.stop:
            with self.out.cv:
                self.out.cv.wait(0.05)
                new = self.out.messages[self._answered:]
                self._answered = len(self.out.messages)
            for m in new:
                if m.get("method") == "session/request_permission":
                    self.permission_requests.append(m["params"])
                    if self.answer is not None:
                        choice = self.answer(m["params"])
                        outcome = {"outcome": "cancelled"} if choice is None else {"outcome": "selected", "optionId": choice}
                        self.pipe.push({"jsonrpc": "2.0", "id": m["id"], "result": {"outcome": outcome}})

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.pipe.push(msg)
        return msg.get("id")

    def wait(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        with self.out.cv:
            while True:
                found = [m for m in self.out.messages if predicate(m)]
                if found:
                    return found[0]
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"timed out; saw {self.out.messages}")
                self.out.cv.wait(min(left, 0.1))

    def response(self, req_id, timeout=10):
        return self.wait(lambda m: m.get("id") == req_id and "method" not in m, timeout)

    def call(self, method, params=None):
        return self.response(self.send(method, params))

    def updates(self, kind=None):
        ups = [m["params"]["update"] for m in self.out.messages if m.get("method") == "session/update"]
        return [u for u in ups if kind is None or u["sessionUpdate"] == kind]

    def new_session(self):
        self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        return self.call("session/new", {"cwd": str(self.ws), "mcpServers": []})["result"]["sessionId"]

    def prompt(self, sid, text="do it"):
        return self.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})

    def close(self):
        self.stop = True
        self.pipe.close()
        self.thread.join(10)


@pytest.fixture
def editor_factory(tmp_path):
    made = []

    def make(client, answer=None):
        e = Editor(tmp_path, client, answer)
        made.append(e)
        return e

    yield make
    for e in made:
        e.close()


def test_initialize_follows_the_protocol(editor_factory):
    e = editor_factory(ScriptedClient())
    res = e.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": True}}})["result"]
    assert res["protocolVersion"] == 1 and isinstance(res["protocolVersion"], int)
    assert res["agentCapabilities"]["loadSession"] is False and res["authMethods"] == []
    assert res["agentCapabilities"]["promptCapabilities"]["embeddedContext"] is True
    assert "capabilities" not in res and res["agentInfo"]["name"] == "basalt"


def test_session_new_and_its_errors(editor_factory, tmp_path):
    e = editor_factory(ScriptedClient())
    res = e.call("session/new", {"cwd": str(e.ws)})["result"]
    assert res["sessionId"].startswith("acp_") and res["modes"]["currentModeId"] == "default"
    assert e.call("session/new", {"cwd": str(tmp_path / "missing")})["error"]["code"] == -32602
    assert e.call("session/new", {"cwd": "relative/dir"})["error"]["code"] == -32602


def test_a_prompt_streams_the_answer_and_ends_with_a_stop_reason(editor_factory):
    e = editor_factory(ScriptedClient(say("Hello from the agent")))
    sid = e.new_session()
    res = e.response(e.prompt(sid, "hi"))
    assert res["result"] == {"stopReason": "end_turn"}
    chunk = e.updates("agent_message_chunk")[0]
    assert chunk["content"] == {"type": "text", "text": "Hello from the agent"}
    assert e.client.sent[0][-1]["content"] == "hi"


def test_prompt_blocks_are_read_and_bad_ones_refused(editor_factory):
    e = editor_factory(ScriptedClient(say("ok")))
    sid = e.new_session()
    blocks = [{"type": "text", "text": "Look at this"}, {"type": "resource", "resource": {"uri": "file:///a.py", "text": "x = 1"}},
              {"type": "image", "data": "AAAA", "mimeType": "image/png"}]
    assert e.response(e.send("session/prompt", {"sessionId": sid, "prompt": blocks}))["result"]["stopReason"] == "end_turn"
    sent = e.client.sent[0][-1]["content"]
    assert "Look at this" in sent and "file:///a.py" in sent and "x = 1" in sent and "image attachment was not shown" in sent
    assert e.call("session/prompt", {"sessionId": sid, "prompt": []})["error"]["code"] == -32602
    assert e.call("session/prompt", {"sessionId": sid, "prompt": 5})["error"]["code"] == -32602
    assert e.call("session/prompt", {"sessionId": "nope", "prompt": "x"})["error"]["code"] == -32602


def test_edits_are_reported_as_tool_calls_with_diff_content(editor_factory):
    client = ScriptedClient(call("write_file", {"path": "src/a.txt", "content": "v1\n"}, "t1"),
                            call("write_file", {"path": "src/a.txt", "content": "v2\n"}, "t2"), say("done"))
    e = editor_factory(client)
    sid = e.new_session()
    assert e.response(e.prompt(sid))["result"]["stopReason"] == "end_turn"
    starts = e.updates("tool_call")
    assert starts[0]["toolCallId"] == "t1" and starts[0]["kind"] == "edit" and starts[0]["status"] == "pending"
    assert starts[0]["locations"] == [{"path": str((e.ws / "src" / "a.txt").resolve())}]
    ends = {u["toolCallId"]: u for u in e.updates("tool_call_update")}
    diff1, diff2 = ends["t1"]["content"][0], ends["t2"]["content"][0]
    assert ends["t1"]["status"] == "completed" and diff1["type"] == "diff" and diff1["newText"] == "v1\n" and diff1["oldText"] is None
    assert diff2["oldText"] == "v1\n" and diff2["newText"] == "v2\n" and diff2["path"] == diff1["path"]


def test_permission_requests_go_to_the_editor_and_its_answer_decides(editor_factory):
    cmd = "python -c \"open('ran.txt', 'w').write('1')\""
    client = ScriptedClient(call("shell_command", {"command": cmd}, "s1"), say("done"))
    e = editor_factory(client, answer=lambda p: "allow_once")
    sid = e.new_session()
    assert e.response(e.prompt(sid))["result"]["stopReason"] == "end_turn"
    req = e.permission_requests[0]
    assert req["sessionId"] == sid and req["toolCall"]["toolCallId"] == "s1" and req["toolCall"]["kind"] == "execute"
    assert {o["kind"] for o in req["options"]} == {"allow_once", "reject_once"}
    assert (e.ws / "ran.txt").exists()


@pytest.mark.parametrize("choice", ["reject_once", None])
def test_a_rejected_or_cancelled_permission_runs_nothing(editor_factory, choice):
    cmd = "python -c \"open('ran.txt', 'w').write('1')\""
    e = editor_factory(ScriptedClient(call("shell_command", {"command": cmd}, "s1"), say("done")), answer=lambda p: choice)
    sid = e.new_session()
    assert e.response(e.prompt(sid))["result"]["stopReason"] == "end_turn"
    assert not (e.ws / "ran.txt").exists()
    assert e.updates("tool_call_update")[0]["status"] == "failed"


class BlockingClient(ScriptedClient):
    """Holds the model call until released, so a cancel can arrive while the model is working."""

    def __init__(self, *steps):
        super().__init__(*steps)
        self.started, self.release = threading.Event(), threading.Event()

    def chat_completion(self, messages, tools=None, **kw):
        self.started.set()
        self.release.wait(10)
        return super().chat_completion(messages, tools, **kw)


def test_cancel_during_the_model_call_ends_the_prompt_as_cancelled_without_running_tools(editor_factory):
    client = BlockingClient(call("write_file", {"path": "late.txt", "content": "x"}, "t1"), say("never"))
    e = editor_factory(client)
    sid = e.new_session()
    req = e.prompt(sid)
    assert client.started.wait(5)
    e.send("session/cancel", {"sessionId": sid}, notify=True)
    time.sleep(0.3)                                # the reader must have seen it while the worker is blocked
    client.release.set()
    assert e.response(req)["result"] == {"stopReason": "cancelled"}
    assert not (e.ws / "late.txt").exists()


def test_cancel_while_waiting_for_a_permission_answer(editor_factory):
    client = ScriptedClient(call("shell_command", {"command": "python -c \"open('ran.txt','w')\""}, "s1"), say("x"))
    e = editor_factory(client)                     # the editor never answers
    sid = e.new_session()
    req = e.prompt(sid)
    e.wait(lambda m: m.get("method") == "session/request_permission")
    e.send("session/cancel", {"sessionId": sid}, notify=True)
    assert e.response(req)["result"]["stopReason"] == "cancelled"
    assert not (e.ws / "ran.txt").exists()


def test_a_second_prompt_on_a_busy_session_is_refused(editor_factory):
    client = BlockingClient(say("first"))
    e = editor_factory(client)
    sid = e.new_session()
    first = e.prompt(sid, "one")
    assert client.started.wait(5)
    second = e.response(e.prompt(sid, "two"))
    assert second["error"]["message"].startswith("this session is already running")
    client.release.set()
    assert e.response(first)["result"]["stopReason"] == "end_turn"


def test_plan_mode_blocks_edits_and_announces_the_change(editor_factory):
    e = editor_factory(ScriptedClient(call("write_file", {"path": "a.txt", "content": "x"}, "t1"), say("planned")))
    sid = e.new_session()
    assert e.call("session/set_mode", {"sessionId": sid, "modeId": "plan"})["result"] == {}
    assert e.updates("current_mode_update")[0]["currentModeId"] == "plan"
    e.response(e.prompt(sid))
    assert not (e.ws / "a.txt").exists()
    assert e.call("session/set_mode", {"sessionId": sid, "modeId": "bogus"})["error"]["code"] == -32602


def test_the_conversation_continues_across_prompts(editor_factory):
    e = editor_factory(ScriptedClient(say("one"), say("two")))
    sid = e.new_session()
    e.response(e.prompt(sid, "first"))
    e.response(e.prompt(sid, "second"))
    roles = [m["role"] for m in e.client.sent[1]]
    assert roles[:4] == ["system", "user", "assistant", "user"]


def test_a_server_error_becomes_a_json_rpc_error_and_the_session_survives(editor_factory):
    e = editor_factory(ScriptedClient(BasaltError(503, "model is loading"), say("ok now")))
    sid = e.new_session()
    assert "model is loading" in e.response(e.prompt(sid))["error"]["message"]
    assert e.response(e.prompt(sid))["result"]["stopReason"] == "end_turn"


def test_malformed_messages_and_unknown_methods(editor_factory):
    e = editor_factory(ScriptedClient())
    e.pipe.push("this is not json")
    assert e.wait(lambda m: m.get("id") is None and "error" in m)["error"]["code"] == -32700
    e.pipe.push(json.dumps([1, 2]))
    assert e.wait(lambda m: "error" in m and m["error"]["code"] == -32600)
    assert e.call("session/load", {})["error"]["code"] == -32601
    before = len(e.out.messages)
    e.send("some/notification", {}, notify=True)
    time.sleep(0.2)
    assert len(e.out.messages) == before                       # unknown notifications get no reply


def test_closing_the_input_ends_the_server(tmp_path):
    e = Editor(tmp_path, ScriptedClient())
    e.new_session()
    e.close()
    assert not e.thread.is_alive() and not e.server.running


def test_the_agent_runs_as_a_subprocess_over_stdio_against_the_real_server(tmp_path):
    """What an editor does: start the process, speak JSON lines, read the replies; nothing else on stdout."""
    import subprocess
    from test_e2e_mock_server import answer, tool_call
    from serve.engine.mock import MockEngine
    from serve.frontend import ChatTemplate
    from serve.handler import serve
    from serve.service import Service
    from serve.util.text import ByteTokenizer

    ROOT = Path(__file__).resolve().parents[1]
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, [tool_call("write_file", path="é.txt", content="ciao è"), answer("Fatto: è scritto")], max_context=16384),
                  tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
    svc.telemetry = type("T", (), {"snapshot": lambda self: {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}})()
    httpd = serve(svc, port=0)
    ws = tmp_path / "ws"
    ws.mkdir()
    env = {**__import__("os").environ, "BASALT_DATA": str(tmp_path / "data"), "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen([sys.executable, "-m", "harness.acp_agent", "--workspace", str(ws), "--endpoint",
                             f"http://127.0.0.1:{httpd.server_address[1]}"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=str(ROOT))
    try:
        def send(obj):
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def read_until(rid):
            while True:
                line = proc.stdout.readline()
                assert line, proc.stderr.read().decode("utf-8", "replace")
                msg = json.loads(line.decode("utf-8"))                  # every stdout line must be JSON
                seen.append(msg)
                if msg.get("id") == rid and "method" not in msg:
                    return msg

        seen = []
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
        assert read_until(1)["result"]["protocolVersion"] == 1
        send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(ws)}})
        sid = read_until(2)["result"]["sessionId"]
        send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
              "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "scrivi un file"}]}})
        assert read_until(3)["result"]["stopReason"] == "end_turn"
        texts = [m["params"]["update"]["content"]["text"] for m in seen
                 if m.get("method") == "session/update" and m["params"]["update"]["sessionUpdate"] == "agent_message_chunk"]
        assert texts == ["Fatto: è scritto"]
        assert (ws / "é.txt").read_text(encoding="utf-8") == "ciao è"
        diffs = [c for m in seen if m.get("method") == "session/update"
                 for c in m["params"]["update"].get("content", []) if isinstance(c, dict) and c.get("type") == "diff"]
        assert diffs and diffs[0]["newText"] == "ciao è"
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        httpd.server_close()
