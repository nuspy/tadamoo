"""Agent Client Protocol agent over stdio, so an editor (Zed, JetBrains, ...) can drive Basalt.

The editor is the client: it starts this process, sends `initialize`, `session/new` and
`session/prompt`, and may send `session/cancel` at any moment. While a prompt runs the agent reports
`session/update` notifications (message and thought text, tool calls with their results, edits as
`diff` content) and asks `session/request_permission` before anything that needs approval.
Prompts run on worker threads so the reader stays free for cancels and for the editor's answers.

Not offered: `session/load`, authentication, image/audio input and delegating file access to the
editor (`fs/*`); the agent edits the files itself and reports each edit as a diff.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

from harness import agent_loop as al
from harness.client import BasaltClient
from harness.stack import AgentConfig, BuiltAgent, build_agent

PROTOCOL_VERSION = 1
STOP_REASONS = {al.FINAL: "end_turn", al.MAX_TURNS: "max_turn_requests", al.CANCELLED: "cancelled", al.BUDGET: "max_tokens",
                al.LOOP: "end_turn"}
KINDS = {"read_file": "read", "list_directory": "read", "write_file": "edit", "edit_file": "edit",
         "shell_command": "execute", "search_web": "fetch", "browse_url": "fetch", "mcp_search": "search",
         "mcp_call": "other", "phone_call": "other", "request_payment": "other"}
MODES = [
    {"id": "default", "name": "Default", "description": "Edits in the workspace are allowed; the rest asks"},
    {"id": "plan", "name": "Plan", "description": "Read only: nothing is changed or run"},
    {"id": "auto", "name": "Auto", "description": "Runs without asking, except where untrusted input meets sensitive data"},
]
INVALID_PARAMS, METHOD_NOT_FOUND, INVALID_REQUEST, PARSE_ERROR, SERVER_ERROR = -32602, -32601, -32600, -32700, -32000


class _Session:
    def __init__(self, session_id: str, cwd: str, built: BuiltAgent) -> None:
        self.id = session_id
        self.cwd = cwd
        self.built = built
        self.busy = threading.Lock()
        self.current_call: Optional[Dict[str, Any]] = None


class ACPAgentServer:
    def __init__(
        self,
        workspace_dir: str = ".",
        client: Optional[BasaltClient] = None,
        in_stream: Optional[TextIO] = None,
        out_stream: Optional[TextIO] = None,
        endpoint: str = "http://127.0.0.1:7232",
        api_key: Optional[str] = None,
        data_dir: Optional[str] = None,
        max_turns: int = 15,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.client = client
        self.endpoint, self.api_key, self.data_dir, self.max_turns = endpoint, api_key, data_dir, max_turns
        self.in_stream = in_stream or sys.stdin
        self.out_stream = out_stream or sys.stdout
        self.sessions: Dict[str, _Session] = {}
        self.client_capabilities: Dict[str, Any] = {}
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
                self._reply(req_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentCapabilities": {"loadSession": False,
                                          "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True}},
                    "authMethods": [],
                    "agentInfo": {"name": "basalt", "title": "Basalt Agent", "version": "1.0"},
                })
            elif method == "session/new":
                self._reply(req_id, self._new_session(params))
            elif method == "session/set_mode":
                self._reply(req_id, self._set_mode(params))
            elif method == "session/prompt":
                self._start_prompt(req_id, params)
            elif req_id is not None:
                self._reply(req_id, error={"code": METHOD_NOT_FOUND, "message": f"Method not found: {method}"})
        except ValueError as exc:
            if req_id is not None:
                self._reply(req_id, error={"code": INVALID_PARAMS, "message": str(exc)})
        except Exception as exc:
            if req_id is not None:
                self._reply(req_id, error={"code": SERVER_ERROR, "message": f"{type(exc).__name__}: {exc}"})

    def _new_session(self, params: Dict[str, Any]) -> Dict[str, Any]:
        cwd = params.get("cwd") or self.workspace_dir
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() and cwd != self.workspace_dir:
            raise ValueError("cwd must be an absolute path")
        if not Path(cwd).is_dir():
            raise ValueError(f"cwd is not a folder: {cwd}")
        session_id = f"acp_{uuid.uuid4().hex[:12]}"
        holder: List[_Session] = []
        cfg = AgentConfig(workspace=cwd, endpoint=self.endpoint, api_key=self.api_key, max_turns=self.max_turns,
                          data_dir=self.data_dir, confirm=lambda tool, args: self._confirm(holder[0], tool, args))
        built = build_agent(cfg, client=self.client)
        session = _Session(session_id, cwd, built)
        holder.append(session)
        built.agent.on_event = lambda event: self._on_event(session, event)
        self.sessions[session_id] = session
        return {"sessionId": session_id, "modes": {"currentModeId": "default", "availableModes": MODES}}

    def _set_mode(self, params: Dict[str, Any]) -> Dict[str, Any]:
        session = self.sessions.get(params.get("sessionId"))
        mode = params.get("modeId")
        if session is None or mode not in {m["id"] for m in MODES}:
            raise ValueError("unknown session or mode")
        from harness.stack import PERMISSION_MODES
        level, plan = PERMISSION_MODES[mode]
        session.built.permissions.level, session.built.permissions.plan_mode = level, plan
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
            agent.run(text, retrieved=session.built.retrieved_message(text))
            reason = agent.stop_reason
            if reason == al.ERROR:
                return self._reply(req_id, error={"code": SERVER_ERROR, "message": agent.error or "the model request failed"})
            if reason == al.LOOP:
                self._update(session.id, {"sessionUpdate": "agent_message_chunk", "content": {
                    "type": "text", "text": "\n\n[Stopped: the same action kept repeating.]"}})
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.acp_agent", description="Basalt as an ACP agent on stdio")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--endpoint", default="http://127.0.0.1:7232")
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ACPAgentServer(workspace_dir=str(Path(args.workspace).resolve()), endpoint=args.endpoint, api_key=args.api_key).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
