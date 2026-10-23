"""Model Context Protocol (MCP) Client: connects to external tool providers via stdio JSON-RPC 2.0."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional
from harness.tools.registry import EffectClass, ToolRegistry


class MCPClient:
    def __init__(self, command: List[str], cwd: Optional[str] = None) -> None:
        self.command = command
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self._msg_id = 0
        self._pending: Dict[int, Any] = {}
        self._lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
        )
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set() and self.proc and self.proc.stdout:
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                req_id = msg.get("id")
                if req_id is not None:
                    with self._lock:
                        if req_id in self._pending:
                            self._pending[req_id] = msg
            except json.JSONDecodeError:
                pass

    def send_request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError("MCP process is not running")

        req_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        with self._lock:
            self._pending[req_id] = None

        req_str = json.dumps(payload) + "\n"
        self.proc.stdin.write(req_str)
        self.proc.stdin.flush()

        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                res = self._pending.get(req_id)
                if res is not None:
                    del self._pending[req_id]
                    if "error" in res:
                        raise RuntimeError(f"MCP RPC Error: {res['error']}")
                    return res.get("result", {})
            time.sleep(0.02)

        with self._lock:
            self._pending.pop(req_id, None)
        raise TimeoutError(f"MCP request '{method}' (id={req_id}) timed out after {timeout}s")

    def initialize(self) -> Dict[str, Any]:
        params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "BasaltHarness", "version": "1.0"},
        }
        res = self.send_request("initialize", params)
        # Send initialized notification (notifications have NO id)
        notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        self.proc.stdin.write(notif)
        self.proc.stdin.flush()
        return res

    def list_tools(self) -> List[Dict[str, Any]]:
        res = self.send_request("tools/list", {})
        return res.get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        params = {"name": name, "arguments": arguments}
        res = self.send_request("tools/call", params)
        content_items = res.get("content", [])
        if content_items and isinstance(content_items, list):
            text_blocks = [c.get("text", "") for c in content_items if c.get("type") == "text"]
            return "\n".join(text_blocks)
        return res

    def register_into(self, registry: ToolRegistry, prefix: str = "") -> None:
        """Registers all tools exposed by the MCP server into a local ToolRegistry."""
        tools = self.list_tools()
        for t in tools:
            name = f"{prefix}{t['name']}" if prefix else t["name"]
            desc = t.get("description", "")
            schema = t.get("inputSchema", {"type": "object", "properties": {}})

            # Handler captures tool name
            def make_handler(tool_target):
                return lambda **kwargs: self.call_tool(tool_target, kwargs)

            registry.register(
                name=name,
                description=desc,
                parameters=schema,
                effect=EffectClass.IRREVERSIBLE,
                handler=make_handler(t["name"]),
            )

    def close(self) -> None:
        self._stop_event.set()
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                self.proc.kill()
            self.proc = None
