"""Model Context Protocol client: stdio and Streamable HTTP, and a hub with deferred tool search.

Servers are third-party code. Their tool descriptions, annotations and results are untrusted: tools
are registered as irreversible whatever a server claims about itself, results are fenced as data,
and a stdio server starts with a scrubbed environment (only what is passed explicitly).

Revisions spoken: 2025-11-25 down to 2024-11-05, negotiated at initialize. The stateless revision
(2026-07-28, server/discover) is not implemented: its schema was not verified here.
"""
from __future__ import annotations

import collections
import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from harness.integrations._http import _OPENER
from harness.sandbox import safe_environment
from harness.tools.registry import EffectClass, ToolArgumentError, ToolRegistry, validate_arguments

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_INFO = {"name": "BasaltHarness", "version": "1.0"}
MAX_PAGES = 50
MAX_DESCRIPTION = 1000
_TOOL_NAME = re.compile(r"[^A-Za-z0-9_\-]")


class MCPError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


def clean_description(text: Any) -> str:
    text = text if isinstance(text, str) else ""
    return "".join(ch for ch in text if ch == "\n" or ch >= " ")[:MAX_DESCRIPTION].strip()


def flatten_result(res: Dict[str, Any]) -> str:
    """The text of a tools/call result: text blocks, and a placeholder for anything else."""
    parts: List[str] = []
    for item in res.get("content") or []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} content, {item.get('mimeType', 'unknown type')}, not shown]")
        elif kind == "resource":
            r = item.get("resource") or {}
            parts.append(str(r.get("text") or f"[resource {r.get('uri', '')}, not shown]"))
        elif kind == "resource_link":
            parts.append(f"[resource link {item.get('uri', '')}]")
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
    return "\n".join(parts)


class _Client:
    """The protocol on top of a transport that provides send_request and notify."""

    request_timeout = 15.0
    call_timeout = 120.0

    def __init__(self) -> None:
        self.server_info: Dict[str, Any] = {}
        self.server_version = ""
        self.capabilities: Dict[str, Any] = {}
        self.tools_changed = False

    def send_request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        raise NotImplementedError

    def initialize(self) -> Dict[str, Any]:
        res = self.send_request("initialize", {"protocolVersion": SUPPORTED_VERSIONS[0], "capabilities": {},
                                               "clientInfo": CLIENT_INFO})
        version = res.get("protocolVersion")
        if version not in SUPPORTED_VERSIONS:
            raise MCPError(f"The server speaks protocol {version!r}; supported: {', '.join(SUPPORTED_VERSIONS)}")
        self.server_version = version
        self.server_info = res.get("serverInfo") or {}
        self.capabilities = res.get("capabilities") or {}
        self.notify("notifications/initialized")
        return res

    def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(MAX_PAGES):
            res = self.send_request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in res.get("tools", []) if isinstance(t, dict) and t.get("name"))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools_changed = False
        return tools

    def call_tool(self, name: str, arguments: Dict[str, Any], timeout: Optional[float] = None) -> str:
        res = self.send_request("tools/call", {"name": name, "arguments": arguments}, timeout or self.call_timeout)
        text = flatten_result(res)
        if res.get("isError"):
            raise MCPError(text or f"The tool '{name}' reported an error")
        return text

    def register_into(self, registry: ToolRegistry, prefix: str = "") -> List[str]:
        """Registers every tool of the server. They are all irreversible and untrusted: a server's own
        read-only hint is not believed. Returns the registered names."""
        names: List[str] = []
        for t in self.list_tools():
            name = f"{prefix}{_TOOL_NAME.sub('_', t['name'])}"[:64]
            if registry.get(name) is not None:
                continue
            schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}
            if schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}
            target = t["name"]
            registry.register(
                name=name,
                description=clean_description(t.get("description")) or f"Tool {target} of an MCP server",
                parameters={"type": "object", "properties": schema.get("properties") or {},
                            "required": [r for r in schema.get("required") or [] if isinstance(r, str)]},
                effect=EffectClass.IRREVERSIBLE,
                handler=lambda _t=target, **kwargs: self.call_tool(_t, kwargs),
                untrusted=True,
            )
            names.append(name)
        return names

    def close(self) -> None:
        pass


class MCPClient(_Client):
    """A server started as a subprocess, spoken to over stdio."""

    def __init__(self, command: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> None:
        super().__init__()
        self.command = command
        self.cwd = cwd
        self.env = env
        self.proc: Optional[subprocess.Popen] = None
        self.stderr_tail: Deque[str] = collections.deque(maxlen=200)
        self._msg_id = 0
        self._pending: Dict[Any, Tuple[threading.Event, List[Any]]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stop_event = threading.Event()

    def start(self) -> None:
        exe = shutil.which(self.command[0]) or self.command[0]       # finds npx.cmd and similar shims
        self.proc = subprocess.Popen(
            [exe, *self.command[1:]], cwd=self.cwd, env=safe_environment(self.env),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        threading.Thread(target=self._reader_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

    def _stderr_loop(self) -> None:
        proc = self.proc
        for line in iter(proc.stderr.readline, ""):          # drained so a chatty server never blocks on a full pipe
            self.stderr_tail.append(line.rstrip())

    def _reader_loop(self) -> None:
        proc = self.proc
        for line in iter(proc.stdout.readline, ""):
            if self._stop_event.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg:
                self._on_server_message(msg)
            elif msg.get("id") is not None:
                with self._lock:
                    slot = self._pending.get(msg["id"])
                if slot:
                    slot[1].append(msg)
                    slot[0].set()
        with self._lock:                                      # the process is gone: wake everyone who is waiting
            for event, box in self._pending.values():
                if not box:
                    box.append({"error": {"code": -32000, "message": "the MCP server closed its output"}})
                event.set()

    def _on_server_message(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        if msg.get("id") is None:
            if method == "notifications/tools/list_changed":
                self.tools_changed = True
            return
        if method == "ping":
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        else:                                                 # sampling, roots, elicitation: not offered
            self._write({"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32601, "message": f"Method not supported: {method}"}})

    def _write(self, payload: Dict[str, Any]) -> None:
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            raise MCPError("The MCP process is not running")
        with self._write_lock:
            try:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                raise MCPError("The MCP server closed its input") from None

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._write(msg)

    def send_request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        if not self.proc or self.proc.poll() is not None:
            raise MCPError("The MCP process is not running")
        with self._lock:
            self._msg_id += 1
            req_id = self._msg_id
            slot: Tuple[threading.Event, List[Any]] = (threading.Event(), [])
            self._pending[req_id] = slot
        try:
            self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
            limit = timeout if timeout is not None else self.request_timeout
            if not slot[0].wait(limit):
                raise TimeoutError(f"MCP request '{method}' timed out after {limit}s")
        finally:
            with self._lock:
                self._pending.pop(req_id, None)
        res = slot[1][0]
        if "error" in res:
            err = res["error"] if isinstance(res["error"], dict) else {"message": str(res["error"])}
            raise MCPError(f"MCP error: {err.get('message', err)}", err.get("code"))
        return res.get("result") or {}

    def close(self) -> None:
        self._stop_event.set()
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                self.proc.kill()
            self.proc = None


class MCPHttpClient(_Client):
    """A server reached over Streamable HTTP: JSON-RPC in POST bodies, answered as JSON or as an event stream."""

    def __init__(self, url: str, token: Optional[str] = None, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__()
        parts = urlparse(url)
        loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
        if parts.scheme not in ("http", "https") or not parts.hostname or (parts.scheme == "http" and not loopback):
            raise ValueError("an MCP server must be reached over https (plain http only on this machine)")
        self.url = url
        self.extra_headers = dict(headers or {})
        if token:
            self.extra_headers["Authorization"] = f"Bearer {token}"
        self.session_id: Optional[str] = None
        self._msg_id = 0
        self._lock = threading.Lock()

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.extra_headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.server_version:
            h["MCP-Protocol-Version"] = self.server_version
        return h

    def _post(self, payload: Dict[str, Any], timeout: float):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=self._headers(), method="POST")
        try:
            return _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read(500).decode("utf-8", errors="replace")
            if e.code == 404 and self.session_id:
                raise MCPError("The MCP session expired; reconnect", 404) from None
            raise MCPError(f"MCP server answered HTTP {e.code}: {detail}", e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise MCPError(f"Cannot reach the MCP server: {e}") from None

    @staticmethod
    def _events(resp: Any):
        """Messages of a text/event-stream body."""
        data: List[str] = []
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                if data:
                    try:
                        yield json.loads("\n".join(data))
                    except json.JSONDecodeError:
                        pass
                    data = []
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if data:
            try:
                yield json.loads("\n".join(data))
            except json.JSONDecodeError:
                pass

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._post(msg, self.request_timeout).close()

    def send_request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            self._msg_id += 1
            req_id = self._msg_id
        resp = self._post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
                          timeout if timeout is not None else self.request_timeout)
        with resp:
            if method == "initialize" and resp.headers.get("Mcp-Session-Id"):
                self.session_id = resp.headers["Mcp-Session-Id"]
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" in ctype:
                messages = self._events(resp)
            else:
                try:
                    body = json.loads(resp.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise MCPError("The MCP server answered with something that is not JSON") from None
                messages = body if isinstance(body, list) else [body]
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                if "method" in msg:
                    if msg.get("id") is not None and msg["method"] == "ping":
                        self._post({"jsonrpc": "2.0", "id": msg["id"], "result": {}}, self.request_timeout).close()
                    elif msg.get("method") == "notifications/tools/list_changed":
                        self.tools_changed = True
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg:
                        err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                        raise MCPError(f"MCP error: {err.get('message', err)}", err.get("code"))
                    return msg.get("result") or {}
        raise MCPError(f"The MCP server never answered '{method}'")

    def close(self) -> None:
        if self.session_id:
            req = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
            try:
                _OPENER.open(req, timeout=5).close()
            except Exception:
                pass
            self.session_id = None


class MCPHub:
    """Several servers behind two tools: one that searches their tool definitions, one that calls a tool.

    Only those two definitions sit in the prompt, so the prefix stays short and stable however many
    tools the servers offer."""

    def __init__(self) -> None:
        self.servers: Dict[str, _Client] = {}
        self.catalog: Dict[str, List[Dict[str, Any]]] = {}
        self.errors: Dict[str, str] = {}

    def add(self, name: str, client: _Client) -> None:
        if name in self.servers or not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", name):
            raise ValueError(f"invalid or duplicate server name '{name}'")
        self.servers[name] = client

    def connect_all(self) -> None:
        """Starts every server and reads its tools; a server that fails is recorded and skipped."""
        for name, client in self.servers.items():
            try:
                if isinstance(client, MCPClient) and client.proc is None:
                    client.start()
                client.initialize()
                self.catalog[name] = client.list_tools()
            except Exception as exc:
                self.errors[name] = f"{type(exc).__name__}: {exc}"
                self.catalog.pop(name, None)

    def search(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
        scored: List[Tuple[int, str, str, Dict[str, Any]]] = []
        for server, tools in self.catalog.items():
            for t in tools:
                hay_name = t["name"].lower()
                hay = hay_name + " " + clean_description(t.get("description")).lower()
                score = sum(3 if w in hay_name else 1 for w in words if w in hay)
                if score or not words:
                    scored.append((-score, server, t["name"], t))
        scored.sort(key=lambda x: x[:3])
        return [{"server": s, "tool": n, "description": clean_description(t.get("description")),
                 "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}}}
                for _, s, n, t in scored[:max(1, min(limit, 20))]]

    def call(self, server: str, tool: str, arguments: Dict[str, Any]) -> str:
        if server not in self.servers or server not in self.catalog:
            raise KeyError(f"Unknown or unavailable MCP server '{server}'. Servers: {', '.join(sorted(self.catalog)) or 'none'}")
        definition = next((t for t in self.catalog[server] if t["name"] == tool), None)
        if definition is None:
            raise KeyError(f"Server '{server}' has no tool '{tool}'; use mcp_search to find tools")
        schema = definition.get("inputSchema") if isinstance(definition.get("inputSchema"), dict) else {}
        validate_arguments(tool, {"properties": schema.get("properties") or {}, "required": schema.get("required") or []}, arguments)
        return self.servers[server].call_tool(tool, arguments)

    def register_deferred(self, registry: ToolRegistry) -> None:
        def mcp_search(query: str) -> List[Dict[str, Any]]:
            return self.search(query)

        def mcp_call(server: str, tool: str, arguments: Dict[str, Any]) -> str:
            return self.call(server, tool, arguments)

        registry.register(
            "mcp_search", "Searches the tools offered by connected MCP servers and returns their definitions.",
            {"type": "object", "properties": {"query": {"type": "string", "description": "What you want to do"}},
             "required": ["query"]}, EffectClass.PURE, mcp_search, untrusted=True)
        registry.register(
            "mcp_call", "Calls a tool of an MCP server found with mcp_search.",
            {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"},
                                              "arguments": {"type": "object"}},
             "required": ["server", "tool", "arguments"]}, EffectClass.IRREVERSIBLE, mcp_call, untrusted=True)

    def register_eager(self, registry: ToolRegistry) -> None:
        for name, client in self.servers.items():
            if name in self.catalog:
                client.register_into(registry, prefix=f"{name}__")

    def close(self) -> None:
        for client in self.servers.values():
            try:
                client.close()
            except Exception:
                pass


def mcp_search_backend(client: _Client, tool: str, query_arg: str = "query", limit_arg: Optional[str] = None):
    """A search backend (see harness.tools.search.register_backend) that calls one MCP tool."""
    def search(query: str, limit: int) -> List[Dict[str, Any]]:
        args: Dict[str, Any] = {query_arg: query}
        if limit_arg:
            args[limit_arg] = limit
        return [{"title": query, "snippet": client.call_tool(tool, args)}]
    return search
