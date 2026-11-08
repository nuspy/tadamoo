"""Stand-ins for the brains in tests that do not talk to a model: no network, no thread."""
from __future__ import annotations


class FakeBrain:
    """The brain interface (`respond_stream`, `ask_stream`, `fork`, `set_mode`) answering with a fixed sentence."""
    mode = "direct"

    def __init__(self, reply: str = "ok. "):
        self.reply = reply
        self.heard: list[tuple] = []

    def respond_stream(self, text, source="mic"):
        self.heard.append((text, source))
        yield self.reply

    ask_stream = respond_stream

    def fork(self, mode=""):
        return self

    def set_mode(self, mode):
        self.mode = mode
        return True

    def begin_session(self, persona=None):
        pass

    def reset_session(self):
        return True

    def set_reply_language(self, name):
        pass

    def end(self):
        pass


# ---------------------------------------------------------------------------------------- TaDaMoo's API
from callbridge.tadamoo_api import ApiError  # noqa: E402


class FakeApi:
    """TaDaMoo's server as the brains use it, in memory: personality compose, a streamed answer, and agent conversations whose events are
    scripted as batches (one batch is returned by each poll). Every request is kept in `calls`."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.compose = {"system": "", "memory": "", "chunks": []}
        self.compose_error: ApiError | None = None
        self.pieces: list[str] = ["Hello there. "]
        self.chat_error: ApiError | None = None
        self.script: list[list[dict]] = []            # batches the next polls return
        self.after_answer: list[list[dict]] = []      # batches that replace `script` once an approval is answered
        self.turn = "t1"
        self.sessions = 0
        self._seq = 0
        self.down = False
        self.temperature = None

    # -- requests
    def _check(self):
        if self.down:
            raise ApiError("TaDaMoo does not answer (ConnectionRefusedError)")

    def post(self, path, body=None):
        self._check()
        self.calls.append(("POST", path, body))
        if path.startswith("/api/personas/") and path.endswith("/compose"):
            if self.compose_error:
                raise self.compose_error
            return dict(self.compose)
        if path == "/api/agent/sessions":
            self.sessions += 1
            return {"session": {"id": f"s{self.sessions}"}}
        if path.endswith("/send"):
            return {"turn": self.turn, "session": {}}
        if path.endswith("/answer"):
            if self.after_answer:
                self.script, self.after_answer = self.after_answer, []
            return {"session": {}}
        return {}

    def get(self, path):
        self._check()
        self.calls.append(("GET", path, None))
        batch = self.script.pop(0) if self.script else []
        self._seq += len(batch)
        return {"events": batch, "next": self._seq, "state": "busy"}

    def chat_stream(self, messages, max_tokens=400, timeout=120.0, temperature=None):
        self._check()
        self.calls.append(("CHAT", messages, max_tokens))
        self.temperature = temperature
        if self.chat_error:
            raise self.chat_error
        yield from self.pieces

    def sent(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


# ---------------------------------------------------------------------------------------- events of an agent conversation
def ev_tool(name, status="completed", arguments=None, call="c1", turn="t1"):
    return [{"event": "agent", "turn": turn, "data": {"type": "tool_start", "call_id": call, "name": name, "arguments": arguments or {}}},
            {"event": "agent", "turn": turn, "data": {"type": "tool_end", "call_id": call, "name": name, "status": status, "output": "SECRET OUTPUT"}}]


def ev_done(final="", stop="end_turn", error="", turn="t1"):
    return {"event": "done", "turn": turn, "stop_reason": stop, "error": error, "final": final}


def ev_ask(tool="edit_file", path="app.py", kind="permission", ask_id="a1", turn="t1", **extra):
    return {"event": "ask", "id": ask_id, "turn": turn, "mode": "confirm", "kind": kind, "tool": tool, "arguments": {"path": path},
            "question": "Allow?", "options": [{"value": "confirm"}, {"value": "chat"}, {"value": "cancel"}], **extra}
