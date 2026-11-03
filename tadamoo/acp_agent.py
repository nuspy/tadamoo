"""Agent Client Protocol agent over stdio, so an editor (Zed, JetBrains, ...) can drive TaDaMoo.

The editor is the client: it starts this process, sends `initialize`, `session/new` and
`session/prompt`, and may send `session/cancel` at any moment. While a prompt runs the agent reports
`session/update` notifications (message and thought text, tool calls with their results, edits as
`diff` content) and asks `session/request_permission` before anything that needs approval.
Prompts run on worker threads so the reader stays free for cancels and for the editor's answers.

The agent is built the way the command line builds it (tadamoo/stack.py): it takes the same options (MCP servers and skills found on
this PC, browser, language servers, vault and payments, hooks...), and the `mcpServers` the editor sends with `session/new` are
connected for that session.

A conversation is saved after each prompt, so `session/load` brings it back: the agent replays it as updates and goes on from there.
When the editor offers `fs/read_text_file` and `fs/write_text_file`, the file tools read and write through it (so unsaved text is
seen and an edit goes through the editor's own undo); every edit is also reported as a diff. When the server wants a key and the
agent has none, `initialize` offers an `authenticate` method and `session/new` is refused until the key is accepted.

Not offered: image/audio input and the editor's terminals (`terminal/*`).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

from tadamoo import agent_loop as al
from tadamoo.client import TadamooClient
from tadamoo.replay import infer_decision
from tadamoo.session import SessionStore
from tadamoo.stack import AgentConfig, BuiltAgent, add_agent_arguments, agent_settings, build_agent, data_dir_for
from tadamoo.tools.registry import clock_paused

PROTOCOL_VERSION = 1
STOP_REASONS = {al.FINAL: "end_turn", al.MAX_TURNS: "max_turn_requests", al.CANCELLED: "cancelled", al.BUDGET: "max_tokens",
                al.LOOP: "end_turn", al.LENGTH: "max_tokens", al.EMPTY: "end_turn", al.DEGENERATE: "end_turn"}
KINDS = {"read_file": "read", "list_directory": "read", "tree": "read", "search_code": "search", "write_file": "edit", "edit_file": "edit",
         "shell_command": "execute", "search_web": "fetch", "browse_url": "fetch", "mcp_search": "search",
         "mcp_call": "other", "mcp_read": "fetch", "read_capability": "fetch", "phone_call": "other", "request_payment": "other", "present_plan": "think"}
MODES = [
    {"id": "default", "name": "Default", "description": "Edits in the workspace are allowed; the rest asks"},
    {"id": "plan", "name": "Plan", "description": "Read only: nothing is changed or run until you approve a plan"},
    {"id": "read_only", "name": "Read only", "description": "Reads freely; anything else asks"},
    {"id": "auto", "name": "Auto", "description": "Runs without asking, except where untrusted input meets sensitive data"},
]
INVALID_PARAMS, METHOD_NOT_FOUND, INVALID_REQUEST, PARSE_ERROR, SERVER_ERROR = -32602, -32601, -32600, -32700, -32000
AUTH_REQUIRED = -32000
AUTH_METHOD = {"id": "tadamoo_api_key", "name": "TaDaMoo API key",
               "description": "The TaDaMoo server wants an API key. Start this agent with TADAMOO_API_KEY set (or --api-key), then authenticate."}
SESSION_ID = re.compile(r"acp_[0-9a-f]{12}")
SESSIONS_FOLDER = "acp_sessions"
REPLAY_OUTPUT = 4000


class _AuthRequired(Exception):
    """The server wants an API key that this agent has not been given."""


class _Session:
    def __init__(self, session_id: str, cwd: str, built: BuiltAgent) -> None:
        self.id = session_id
        self.cwd = cwd
        self.built = built
        self.busy = threading.Lock()
        self.current_call: Optional[Dict[str, Any]] = None
        self.store: Optional[SessionStore] = None
        self.prompts = 0
        self.mode = "default"


class ACPAgentServer:
    def __init__(
        self,
        workspace_dir: str = ".",
        client: Optional[TadamooClient] = None,
        in_stream: Optional[TextIO] = None,
        out_stream: Optional[TextIO] = None,
        endpoint: str = "http://127.0.0.1:7232",
        api_key: Optional[str] = None,
        data_dir: Optional[str] = None,
        max_turns: int = 15,
        settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.client = client
        self.endpoint, self.api_key, self.data_dir, self.max_turns = endpoint, api_key, data_dir, max_turns
        self.settings = dict(settings or {})          # the AgentConfig fields the command-line options set (stack.agent_settings)
        self.in_stream = in_stream or sys.stdin
        self.out_stream = out_stream or sys.stdout
        self.sessions: Dict[str, _Session] = {}
        self.client_capabilities: Dict[str, Any] = {}
        self.key_needed = False                       # the server wants a key this agent does not have, until `authenticate` succeeds
        self.running = False
        self._out_lock = threading.Lock()
        self._next_id = 0
        self._waiting: Dict[int, "tuple[threading.Event, List[Any]]"] = {}
        self._workers: List[threading.Thread] = []

    # -- wire ------------------------------------------------------------------------------------

    def send(self, obj: Dict[str, Any]) -> None:
        with self._out_lock:
            self.out_stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.out_stream.flush()

    def _reply(self, req_id: Any, result: Optional[Dict[str, Any]] = None, error: Optional[Dict[str, Any]] = None) -> None:
        self.send({"jsonrpc": "2.0", "id": req_id, **({"error": error} if error else {"result": result or {}})})

    def _update(self, session_id: str, update: Dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": update}})

    def _ask_client(self, method: str, params: Dict[str, Any], session: Optional[_Session] = None) -> Optional[Dict[str, Any]]:
        """A request to the editor; None if it was cancelled or the connection ended before an answer."""
        with self._out_lock:
            self._next_id += 1
            req_id = self._next_id
        slot = (threading.Event(), [])
        self._waiting[req_id] = slot
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            with clock_paused():                                # the person's time is not the tool's
                while not slot[0].wait(0.2):
                    if not self.running or (session and session.built.cancel.is_set()):
                        return None
        finally:
            self._waiting.pop(req_id, None)
        msg = slot[1][0]
        return None if "error" in msg else msg.get("result") or {}

    # -- dispatch --------------------------------------------------------------------------------

    def handle_message(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return self._reply(None, error={"code": INVALID_REQUEST, "message": "Invalid request"})
        method, req_id = msg.get("method"), msg.get("id")
        if method is None:                                      # an answer to one of our requests
            slot = self._waiting.get(req_id)
            if slot:
                slot[1].append(msg)
                slot[0].set()
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            if method == "session/cancel":
                session = self.sessions.get(params.get("sessionId"))
                if session:
                    session.built.cancel.set()
            elif method == "initialize":
                self.client_capabilities = params.get("clientCapabilities") or {}
                self.key_needed = self._server_wants_key()
                self._reply(req_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentCapabilities": {"loadSession": True,
                                          "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                                          "mcpCapabilities": {"http": True, "sse": False}},
                    "authMethods": [AUTH_METHOD] if self.key_needed else [],
                    "agentInfo": {"name": "basalt", "title": "TaDaMoo Agent", "version": "1.0"},
                })
            elif method == "authenticate":
                self._reply(req_id, self._authenticate(params))
            elif method == "session/new":
                self._need_key()
                self._reply(req_id, self._new_session(params))
            elif method == "session/load":
                self._need_key()
                self._reply(req_id, self._load_session(params))
            elif method == "session/set_mode":
                self._reply(req_id, self._set_mode(params))
            elif method == "session/prompt":
                self._start_prompt(req_id, params)
            elif req_id is not None:
                self._reply(req_id, error={"code": METHOD_NOT_FOUND, "message": f"Method not found: {method}"})
        except _AuthRequired as exc:
            if req_id is not None:
                self._reply(req_id, error={"code": AUTH_REQUIRED, "message": str(exc), "data": {"authRequired": True}})
        except ValueError as exc:
            if req_id is not None:
                self._reply(req_id, error={"code": INVALID_PARAMS, "message": str(exc)})
        except Exception as exc:
            if req_id is not None:
                self._reply(req_id, error={"code": SERVER_ERROR, "message": f"{type(exc).__name__}: {exc}"})

    # -- the key of the server ---------------------------------------------------------------------

    def _status(self, key: Optional[str]) -> Optional[int]:
        """The HTTP status of the server's status page with `key`; None if it cannot be reached."""
        req = urllib.request.Request(self.endpoint.rstrip("/") + "/v1/status", headers={"Authorization": f"Bearer {key}"} if key else {})
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _server_wants_key(self) -> bool:
        """True when this agent talks to the server itself, has no key, and the server refuses a request without one."""
        return self.client is None and not self.api_key and self._status(None) in (401, 403)

    def _need_key(self) -> None:
        if self.key_needed:
            raise _AuthRequired("Authentication required: the TaDaMoo server wants an API key. Start this agent with TADAMOO_API_KEY set, then authenticate.")

    def _authenticate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if params.get("methodId") != AUTH_METHOD["id"]:
            raise ValueError("unknown authentication method")
        status = self._status(self.api_key)
        if status is None:
            raise RuntimeError(f"cannot reach the TaDaMoo server at {self.endpoint}")
        if not self.api_key or status in (401, 403):
            raise _AuthRequired("The TaDaMoo server did not accept the API key (TADAMOO_API_KEY is not set or is wrong).")
        self.key_needed = False
        return {}

    # -- the sessions ------------------------------------------------------------------------------

    def _history_dir(self, session_id: str) -> Path:
        root = Path(self.data_dir) if self.data_dir else data_dir_for(Path(self.workspace_dir).resolve())
        return root / SESSIONS_FOLDER / session_id

    def _check_new(self, params: Dict[str, Any]) -> "tuple[str, List[Any]]":
        cwd = params.get("cwd") or self.workspace_dir
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() and cwd != self.workspace_dir:
            raise ValueError("cwd must be an absolute path")
        if not Path(cwd).is_dir():
            raise ValueError(f"cwd is not a folder: {cwd}")
        servers = params.get("mcpServers") or []
        if not isinstance(servers, list):
            raise ValueError("mcpServers must be a list")
        return cwd, servers

    def _new_session(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cwd, servers = self._check_new(params)
        session = self._build_session(f"acp_{uuid.uuid4().hex[:12]}", cwd, servers)
        return {"sessionId": session.id, "modes": self._modes(session)}

    def _modes(self, session: _Session) -> Dict[str, Any]:
        return {"currentModeId": session.mode if session.mode in {m["id"] for m in MODES} else "default", "availableModes": MODES}

    def _build_session(self, session_id: str, cwd: str, servers: List[Any]) -> _Session:
        holder: List[_Session] = []
        settings = {**self.settings, "workspace": cwd, "endpoint": self.endpoint, "api_key": self.api_key, "max_turns": self.max_turns}
        cfg = AgentConfig(**settings, data_dir=self.data_dir, mcp_servers=servers,
                          confirm=lambda tool, args: self._confirm(holder[0], tool, args))
        built = build_agent(cfg, client=self.client)
        session = _Session(session_id, cwd, built)
        session.mode = cfg.permission_mode
        session.store = SessionStore(self._history_dir(session_id), redact=built.agent.secrets.mask_secrets if built.agent.secrets else None)
        holder.append(session)
        built.agent.on_event = lambda event: self._on_event(session, event)
        if built.payment_confirm is not None:
            built.payment_confirm.ask = lambda request: self._confirm(session, "request_payment", request)
        if built.plan_gate is not None:
            built.plan_gate.ask = lambda plan, modes, outside: self._ask_plan(session, plan, modes, outside)
        for name, why in built.mcp_errors.items():
            print(f"MCP server '{name}' could not be used: {why}", file=sys.stderr, flush=True)
        fs = self.client_capabilities.get("fs") if isinstance(self.client_capabilities.get("fs"), dict) else {}
        if fs.get("readTextFile") is True:
            built.sandbox.read_through = lambda path: self._editor_read(session, path)
        if fs.get("writeTextFile") is True:
            built.sandbox.write_through = lambda path, text: self._editor_write(session, path, text)
        self.sessions[session_id] = session
        return session

    def _load_session(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """`session/load`: the saved conversation comes back, replayed to the editor as updates, and the agent goes on from it."""
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError("unknown sessionId")
        folder = self._history_dir(session_id)
        if not (folder / "history.json").is_file():
            raise ValueError("unknown sessionId")
        cwd, servers = self._check_new(params)
        session = self.sessions.get(session_id)
        if session is None:
            session = self._build_session(session_id, cwd, servers)
            data = session.store.load() if session.store else None
            if data:
                session.built.agent.messages = [session.built.agent.messages[0], *data["messages"]]
        self._replay(session)
        return {"modes": self._modes(session)}

    @staticmethod
    def _unwrapped(text: str) -> str:
        """What the person wrote, from the request as the model saw it."""
        if text.startswith("<request>\n") and "\n</request>" in text:
            return text[len("<request>\n"):text.index("\n</request>")]
        return text

    def _replay(self, session: _Session) -> None:
        tools: Dict[str, str] = {}
        for m in session.built.agent.messages[1:]:
            if not isinstance(m, dict) or m.get("note") or m.get("summary"):
                continue
            role, text = m.get("role"), m.get("content") if isinstance(m.get("content"), str) else ""
            if role == "user" and text:
                self._update(session.id, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": self._unwrapped(text)}})
            elif role == "assistant":
                if m.get("reasoning_content"):
                    self._update(session.id, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": str(m["reasoning_content"])}})
                if text:
                    self._update(session.id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    args = args if isinstance(args, dict) else {}
                    tools[c.get("id", "")] = fn.get("name", "")
                    self._update(session.id, {"sessionUpdate": "tool_call", "toolCallId": c.get("id", ""), "title": f"{fn.get('name', '')} {str(args.get('command') or args.get('path') or args.get('query') or '')[:80]}".strip(),
                                              "kind": KINDS.get(fn.get("name", ""), "other"), "status": "pending", "rawInput": args, "locations": self._location(session, args)})
            elif role == "tool":
                failed = infer_decision(text) != "executed"
                self._update(session.id, {"sessionUpdate": "tool_call_update", "toolCallId": m.get("tool_call_id", ""), "status": "failed" if failed else "completed",
                                          "content": [{"type": "content", "content": {"type": "text", "text": text[:REPLAY_OUTPUT]}}]})

    def _save(self, session: _Session) -> None:
        """The conversation after each prompt, for `session/load`."""
        if session.store is None:
            return
        session.store.save(session.built.agent.messages[1:], {"session": session.id, "cwd": session.cwd, "turns": session.prompts})

    # -- the editor holds the files ------------------------------------------------------------------

    def _editor_read(self, session: _Session, path: Path) -> Optional[str]:
        """A file's text as the editor has it, unsaved changes included; None when the editor does not know it."""
        result = self._ask_client("fs/read_text_file", {"sessionId": session.id, "path": str(path)}, session)
        content = (result or {}).get("content")
        return content if isinstance(content, str) else None

    def _editor_write(self, session: _Session, path: Path, text: str) -> None:
        if self._ask_client("fs/write_text_file", {"sessionId": session.id, "path": str(path), "content": text}, session) is None:
            raise OSError(f"the editor did not write {path}")

    def _set_mode(self, params: Dict[str, Any]) -> Dict[str, Any]:
        session = self.sessions.get(params.get("sessionId"))
        mode = params.get("modeId")
        if session is None or mode not in {m["id"] for m in MODES}:
            raise ValueError("unknown session or mode")
        session.built.set_mode(mode)
        session.mode = mode
        self._update(session.id, {"sessionUpdate": "current_mode_update", "currentModeId": mode})
        return {}

    # -- one prompt ------------------------------------------------------------------------------

    @staticmethod
    def prompt_text(blocks: Any) -> str:
        if isinstance(blocks, str):
            return blocks
        if not isinstance(blocks, list):
            raise ValueError("prompt must be a list of content blocks")
        parts: List[str] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif b.get("type") == "resource" and isinstance(b.get("resource"), dict):
                r = b["resource"]
                parts.append(f"[{r.get('uri', 'attached file')}]\n{r.get('text', '')}")
            elif b.get("type") == "resource_link":
                parts.append(f"[attached: {b.get('uri') or b.get('name', '')}]")
            else:
                parts.append(f"[a {b.get('type', 'unknown')} attachment was not shown: this agent reads text only]")
        text = "\n\n".join(p for p in parts if p).strip()
        if not text:
            raise ValueError("the prompt is empty")
        return text

    def _start_prompt(self, req_id: Any, params: Dict[str, Any]) -> None:
        session = self.sessions.get(params.get("sessionId"))
        if session is None:
            raise ValueError("unknown sessionId")
        text = self.prompt_text(params.get("prompt"))
        if not session.busy.acquire(blocking=False):
            return self._reply(req_id, error={"code": SERVER_ERROR, "message": "this session is already running a prompt"})
        session.built.cancel.clear()
        worker = threading.Thread(target=self._run_prompt, args=(req_id, session, text), daemon=True)
        self._workers.append(worker)
        worker.start()

    def _run_prompt(self, req_id: Any, session: _Session, text: str) -> None:
        try:
            agent = session.built.agent
            try:
                agent.run(text, retrieved=session.built.retrieved_message(text))
            finally:
                session.prompts += 1
                self._save(session)                         # before the answer, so a `session/load` right after it finds this prompt
            reason = agent.stop_reason
            if reason == al.ERROR:
                return self._reply(req_id, error={"code": SERVER_ERROR, "message": agent.error or "the model request failed"})
            if reason == al.LOOP:
                self._update(session.id, {"sessionUpdate": "agent_message_chunk", "content": {
                    "type": "text", "text": "\n\n[Stopped: the same action kept repeating.]"}})
            elif reason in (al.LENGTH, al.EMPTY, al.DEGENERATE):
                self._update(session.id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"\n\n[{agent.error}]"}})
            self._reply(req_id, {"stopReason": STOP_REASONS.get(reason, "end_turn")})
        except Exception as exc:
            self._reply(req_id, error={"code": SERVER_ERROR, "message": f"{type(exc).__name__}: {exc}"})
        finally:
            session.busy.release()

    # -- what the loop reports -------------------------------------------------------------------

    def _location(self, session: _Session, args: Dict[str, Any]) -> List[Dict[str, str]]:
        path = args.get("path") if isinstance(args, dict) else None
        if isinstance(path, str) and path:
            return [{"path": str((Path(session.cwd) / path).resolve())}]
        return []

    def _on_event(self, session: _Session, event: Dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "assistant":
            if event["reasoning"]:
                self._update(session.id, {"sessionUpdate": "agent_thought_chunk",
                                          "content": {"type": "text", "text": event["reasoning"]}})
            if event["content"]:
                self._update(session.id, {"sessionUpdate": "agent_message_chunk",
                                          "content": {"type": "text", "text": event["content"]}})
        elif kind == "tool_start":
            args = event["arguments"]
            summary = (args.get("command") or args.get("path") or args.get("query") or "")
            session.current_call = {
                "toolCallId": event["call_id"], "title": f"{event['name']} {str(summary)[:80]}".strip(),
                "kind": KINDS.get(event["name"], "other"), "status": "pending", "rawInput": args,
                "locations": self._location(session, args),
                "_changes_before": len(session.built.sandbox.changes),
            }
            self._update(session.id, {"sessionUpdate": "tool_call",
                                      **{k: v for k, v in session.current_call.items() if not k.startswith("_")}})
        elif kind == "tool_end":
            start = session.current_call or {}
            content: List[Dict[str, Any]] = []
            for change in session.built.sandbox.changes[start.get("_changes_before", 0):]:
                path = str((Path(session.cwd) / change["file"]).resolve())
                if change.get("new_text") is not None:
                    content.append({"type": "diff", "path": path, "oldText": change.get("old_text") or None,
                                    "newText": change["new_text"]})
                else:
                    content.append({"type": "content", "content": {"type": "text", "text": change["diff"][:4000]}})
            if not content:
                content.append({"type": "content", "content": {"type": "text", "text": event["output"][:4000]}})
            self._update(session.id, {"sessionUpdate": "tool_call_update", "toolCallId": event["call_id"],
                                      "status": event["status"], "content": content})
            session.current_call = None

    def _confirm(self, session: _Session, tool: str, args: Dict[str, Any]) -> bool:
        call = {k: v for k, v in (session.current_call or {"toolCallId": f"perm_{uuid.uuid4().hex[:8]}", "title": tool,
                                                             "kind": KINDS.get(tool, "other"), "status": "pending",
                                                             "rawInput": args}).items() if not k.startswith("_")}
        result = self._ask_client("session/request_permission", {
            "sessionId": session.id, "toolCall": call,
            "options": [{"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}],
        }, session)
        outcome = (result or {}).get("outcome") or {}
        return outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow_once"

    def _ask_plan(self, session: _Session, plan: str, modes: List[str], outside: bool) -> str:
        """The plan the agent presents in plan mode: one button for each mode it can go on in, and one to send it back."""
        names = {"default": "Approve: run it, ask before risky actions", "auto": "Approve: run it without asking"}
        call = {"toolCallId": f"plan_{uuid.uuid4().hex[:8]}", "title": "Approve this plan?", "kind": "think", "status": "pending",
                "rawInput": {"plan": plan, "outside_content": outside},
                "content": [{"type": "content", "content": {"type": "text", "text": plan + (
                    "\n\nThe agent read content from outside this conversation before it wrote this plan." if outside else "")}}]}
        result = self._ask_client("session/request_permission", {
            "sessionId": session.id, "toolCall": call,
            "options": [*({"optionId": f"mode:{m}", "name": names.get(m, m), "kind": "allow_once"} for m in modes),
                        {"optionId": "reject_once", "name": "Not yet: keep planning", "kind": "reject_once"}]}, session)
        outcome = (result or {}).get("outcome") or {}
        chosen = str(outcome.get("optionId", "")).partition("mode:")[2] if outcome.get("outcome") == "selected" else ""
        if chosen in modes:
            self._update(session.id, {"sessionUpdate": "current_mode_update", "currentModeId": chosen})
        return chosen if chosen in modes else ""

    # -- loop ------------------------------------------------------------------------------------

    def serve_forever(self) -> None:
        self.running = True
        try:
            for line in self.in_stream:
                if not self.running:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._reply(None, error={"code": PARSE_ERROR, "message": "Parse error"})
                    continue
                self.handle_message(msg)
        finally:
            self.running = False
            for session in self.sessions.values():
                session.built.cancel.set()
            for worker in self._workers:
                worker.join(timeout=5)
            for session in self.sessions.values():
                try:
                    session.built.close()
                except Exception:
                    pass


def server_from_args(argv: Optional[List[str]] = None, **kw: Any) -> ACPAgentServer:
    """The server the command line asks for: it takes the options every way of running the agent takes (tadamoo/stack.py)."""
    parser = argparse.ArgumentParser(prog="python -m tadamoo.acp_agent", description="TaDaMoo as an ACP agent on stdio")
    add_agent_arguments(parser)
    args = parser.parse_args(argv)
    return ACPAgentServer(workspace_dir=str(Path(args.workspace).resolve()), endpoint=args.endpoint, api_key=args.api_key, max_turns=args.max_turns,
                          settings=agent_settings(args), **kw)


def main(argv: Optional[List[str]] = None) -> int:
    server = server_from_args(argv)
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
