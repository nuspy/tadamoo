"""Agent Client Protocol client over stdio: Basalt drives another ACP agent (protocol version 1).

The agent is started as a subprocess and spoken to in lines of JSON-RPC. `AcpClient` sends `initialize`, `session/new`
and `session/prompt`, collects the `session/update` notifications of a prompt (message text, tool calls) and answers the
agent's `session/request_permission` by asking `ask(title, details)`; with no `ask`, or when it says no, the request is
rejected. `session/cancel` is sent when the caller's cancel flag is set.

The client offers no file or terminal access to the agent (it declares no `fs` or `terminal` capability), so the agent
works on the folder itself. Authentication is not done here: an agent that needs it reports the error it answers with.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness.sandbox import kill_tree, safe_environment

PROTOCOL_VERSION = 1
METHOD_NOT_FOUND = -32601
TEXT_MAX = 20_000
STDERR_KEPT = 4000
TOOL_LINES_MAX = 40


class AcpError(RuntimeError):
    """The agent could not be started, spoke wrongly, answered with an error, or ended."""


def resolve_command(command: List[str]) -> List[str]:
    """The program as the OS will find it (a `.cmd` launcher on Windows is not found by name alone)."""
    if not command or not all(isinstance(c, str) and c for c in command):
        raise AcpError("The command of the agent is empty.")
    found = shutil.which(command[0])
    if found is None:
        raise AcpError(f"The program '{command[0]}' is not on this PC's PATH.")
    return [found, *command[1:]]


class AcpClient:
    def __init__(self, command: List[str], cwd: str | Path, env: Optional[Dict[str, str]] = None,
                 ask: Optional[Callable[[str, Dict[str, Any]], bool]] = None, cancel: Optional[threading.Event] = None,
                 name: str = "agent", request_timeout: float = 60.0) -> None:
        self.command, self.cwd, self.name = list(command), str(cwd), name
        self.env = safe_environment(env)
        self.ask, self.cancel_flag, self.request_timeout = ask, cancel, request_timeout
        self.proc: Optional["subprocess.Popen[bytes]"] = None
        self.agent_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.auth_methods: List[Any] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._waiting: Dict[int, "tuple[threading.Event, List[Dict[str, Any]]]"] = {}
        self._updates: List[Dict[str, Any]] = []
        self._stderr = bytearray()
        self._closed = threading.Event()
        self._helpers: List[threading.Thread] = []

    # -- process and wire ------------------------------------------------------------------------

    def start(self) -> None:
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(resolve_command(self.command), cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
                                         start_new_session=(os.name != "nt"))
        except OSError as exc:
            raise AcpError(f"The agent could not be started: {exc.strerror or exc}") from None
        try:
            from serve.util.winjob import contain
            contain(self.proc)                    # it ends with the server, however the server ends
        except Exception:
            pass
        for target in (self._read_out, self._read_err):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._helpers.append(t)

    def _write(self, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            try:
                assert self.proc and self.proc.stdin
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, AssertionError, ValueError):
                raise AcpError(self._ended("The agent is not listening any more")) from None

    def _ended(self, lead: str) -> str:
        tail = bytes(self._stderr).decode("utf-8", errors="replace").strip()[-STDERR_KEPT:]
        return f"{lead}." + (f" Its error output: {tail}" if tail else "")

    def _read_err(self) -> None:
        assert self.proc and self.proc.stderr
        for chunk in iter(lambda: self.proc.stderr.read(2048), b""):
            if len(self._stderr) < STDERR_KEPT * 4:
                self._stderr += chunk

    def _read_out(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue                           # logs and banners on stdout are not messages
            if isinstance(msg, dict):
                self._dispatch(msg)
        self._closed.set()
        for event, slot in list(self._waiting.values()):
            event.set()

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method, msg_id = msg.get("method"), msg.get("id")
        if method is None:                                          # an answer to one of our requests
            waiter = self._waiting.get(msg_id)
            if waiter:
                waiter[1].append(msg)
                waiter[0].set()
        elif method == "session/update":
            params = msg.get("params")
            if isinstance(params, dict) and isinstance(params.get("update"), dict):
                self._updates.append(params["update"])
        elif msg_id is not None:                                    # a request from the agent
            threading.Thread(target=self._serve_request, args=(msg,), daemon=True).start()

    def _serve_request(self, msg: Dict[str, Any]) -> None:
        method, params = msg.get("method"), msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            if method == "session/request_permission":
                result: Dict[str, Any] = {"outcome": self._decide(params)}
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            else:
                self._write({"jsonrpc": "2.0", "id": msg["id"],
                             "error": {"code": METHOD_NOT_FOUND, "message": f"This client does not offer {method}"}})
        except AcpError:
            pass

    def _decide(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The outcome of a permission request: the person's answer, or a refusal when no one can be asked."""
        options = [o for o in params.get("options") or [] if isinstance(o, dict) and isinstance(o.get("optionId"), str)]
        call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        title = str(call.get("title") or call.get("kind") or "an action")
        approved = False
        if self.ask is not None:
            try:
                approved = bool(self.ask(title, {"agent": self.name, "kind": call.get("kind"), "input": call.get("rawInput")}))
            except Exception:
                approved = False
        wanted = ("allow_once",) if approved else ("reject_once",)
        for option in options:
            if option.get("kind") in wanted:
                return {"outcome": "selected", "optionId": option["optionId"]}
        if approved:                                              # the agent offered only "always" choices: take the first allowing one
            for option in options:
                if str(option.get("kind", "")).startswith("allow"):
                    return {"outcome": "selected", "optionId": option["optionId"]}
        return {"outcome": "cancelled"}

    def request(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None, watch_cancel: bool = False,
                session_id: str = "") -> Dict[str, Any]:
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
        slot: "tuple[threading.Event, List[Dict[str, Any]]]" = (threading.Event(), [])
        self._waiting[req_id] = slot
        deadline = time.monotonic() + (timeout if timeout is not None else self.request_timeout)
        cancelled_at: Optional[float] = None
        try:
            self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            while not slot[0].wait(0.1):
                if watch_cancel and self.cancel_flag is not None and self.cancel_flag.is_set() and cancelled_at is None:
                    cancelled_at = time.monotonic()
                    self.notify("session/cancel", {"sessionId": session_id})
                if cancelled_at is not None and time.monotonic() - cancelled_at > 10:
                    raise AcpError("The agent did not stop when it was cancelled.")
                if time.monotonic() > deadline:
                    raise AcpError(f"The agent did not answer {method} within {timeout or self.request_timeout:g} seconds.")
        finally:
            self._waiting.pop(req_id, None)
        if not slot[1]:
            raise AcpError(self._ended("The agent ended"))
        msg = slot[1][0]
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise AcpError(f"The agent answered {method} with an error: {err.get('message') or msg['error']}")
        return msg.get("result") if isinstance(msg.get("result"), dict) else {}

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # -- the protocol ----------------------------------------------------------------------------

    def initialize(self) -> Dict[str, Any]:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "basalt", "title": "Basalt", "version": "1.0"}})
        version = result.get("protocolVersion")
        if not isinstance(version, int) or version < 1 or version > PROTOCOL_VERSION:
            raise AcpError(f"The agent speaks ACP protocol version {version!r}; this client speaks {PROTOCOL_VERSION}.")
        self.agent_info = result.get("agentInfo") if isinstance(result.get("agentInfo"), dict) else {}
        self.capabilities = result.get("agentCapabilities") if isinstance(result.get("agentCapabilities"), dict) else {}
        self.auth_methods = result.get("authMethods") if isinstance(result.get("authMethods"), list) else []
        return result

    def new_session(self) -> str:
        result = self.request("session/new", {"cwd": self.cwd, "mcpServers": []})
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError("The agent did not give a session id.")
        return session_id

    def prompt(self, session_id: str, text: str, timeout: float = 900.0) -> Dict[str, Any]:
        """Sends the prompt and waits for the end of the turn. {"stop_reason", "text", "tool_calls": [...]}."""
        self._updates.clear()
        result = self.request("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
                              timeout=timeout, watch_cancel=True, session_id=session_id)
        return {"stop_reason": str(result.get("stopReason") or ""), **self._digest()}

    def _digest(self) -> Dict[str, Any]:
        text: List[str] = []
        calls: Dict[str, Dict[str, Any]] = {}
        for u in self._updates:
            kind = u.get("sessionUpdate")
            content = u.get("content")
            if kind == "agent_message_chunk" and isinstance(content, dict) and content.get("type") == "text":
                text.append(str(content.get("text", "")))
            elif kind in ("tool_call", "tool_call_update") and isinstance(u.get("toolCallId"), str):
                entry = calls.setdefault(u["toolCallId"], {"title": "", "status": ""})
                entry["title"] = str(u.get("title") or entry["title"])
                entry["status"] = str(u.get("status") or entry["status"])
        listed = [f"{c['title'] or 'tool call'} ({c['status'] or 'unknown'})" for c in calls.values()]
        return {"text": "".join(text).strip()[:TEXT_MAX], "tool_calls": listed[:TOOL_LINES_MAX]}

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=2)
        except Exception:
            pass
        if proc.poll() is None:
            kill_tree(proc)
        for t in self._helpers:
            t.join(2)
        self.proc = None

    def __enter__(self) -> "AcpClient":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def run_task(command: List[str], cwd: str | Path, task: str, env: Optional[Dict[str, str]] = None,
             ask: Optional[Callable[[str, Dict[str, Any]], bool]] = None, cancel: Optional[threading.Event] = None,
             name: str = "agent", timeout: float = 900.0) -> Dict[str, Any]:
    """One task for one agent, from start to end: the process lives only as long as the task."""
    with AcpClient(command, cwd, env=env, ask=ask, cancel=cancel, name=name) as client:
        client.initialize()
        session = client.new_session()
        return {"agent": (client.agent_info.get("title") or client.agent_info.get("name") or name), **client.prompt(session, task, timeout)}
