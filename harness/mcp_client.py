"""Model Context Protocol client: stdio and Streamable HTTP, and a hub with deferred tool search.

Servers are third-party code. Their tool descriptions, annotations and results are untrusted: tools
are registered as irreversible whatever a server claims about itself, results are fenced as data,
and a stdio server starts with a scrubbed environment (only what is passed explicitly).

Two eras are spoken and detected per server. The stateless revision (2026-07-28) carries its version and
capabilities in every request and has no session; it is probed first (server/discover on stdio, a first
modern request on HTTP). Anything that is not a recognized modern answer is taken as a legacy server and
spoken to with the initialize handshake, revisions 2025-11-25 down to 2024-11-05. OAuth is not implemented.
"""
from __future__ import annotations

import base64
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
from harness.mcp_oauth import AuthRequired
from harness.sandbox import safe_environment
from harness.tools.registry import EffectClass, ToolArgumentError, ToolRegistry, validate_arguments

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MODERN_VERSION = "2026-07-28"
CLIENT_INFO = {"name": "BasaltHarness", "version": "1.0"}
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_INFO = "io.modelcontextprotocol/clientInfo"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
MODERN_ERRORS = (-32020, -32021, -32022)      # header mismatch, missing capability, unsupported version
MAX_ROUNDS = 5
MAX_PAGES = 50
MAX_DESCRIPTION = 1000
_TOOL_NAME = re.compile(r"[^A-Za-z0-9_\-]")
_HEADER_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_SENTINEL = re.compile(r"=\?base64\?.*\?=", re.S)
_NAMED = ("tools/call", "prompts/get", "resources/read")
_SAFE_INT = 2 ** 53 - 1


class MCPError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None, status: Optional[int] = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code                # the JSON-RPC code when the peer sent one, else the HTTP status
        self.status = status            # the HTTP status, for errors that came with one
        self.data = data


class MCPAuthRequired(MCPError):
    """The server wants a sign-in the person has not done (or that ran out); `www_authenticate` is what it said about where to do it."""

    def __init__(self, message: str, www_authenticate: str = "") -> None:
        super().__init__(message, 401, 401)
        self.www_authenticate = www_authenticate


def is_modern_error(err: "MCPError") -> bool:
    """An answer only a server of the stateless era gives: its own error codes, or an unknown-method error on a 404."""
    return err.code in MODERN_ERRORS or (err.status == 404 and err.code == -32601)


def header_value(value: str) -> str:
    """A value as an HTTP header: plain when it is visible ASCII, else base64 behind a marker."""
    plain = all(0x20 <= ord(c) <= 0x7E for c in value) and value == value.strip() and bool(value)
    if plain and not _SENTINEL.fullmatch(value):
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def header_annotations(schema: Dict[str, Any]) -> Optional[List[Tuple[str, Tuple[str, ...]]]]:
    """(header name, property path) for each x-mcp-header of an input schema, or None if any annotation is invalid."""
    found: List[Tuple[Any, List[Any], Dict[str, Any]]] = []

    def walk(node: Any, path: List[Any]) -> None:
        if isinstance(node, dict):
            if "x-mcp-header" in node:
                found.append((node["x-mcp-header"], path, node))
            for key, child in node.items():
                if isinstance(child, (dict, list)):
                    walk(child, path + [key])
        elif isinstance(node, list):
            for i, child in enumerate(node):
                if isinstance(child, (dict, list)):
                    walk(child, path + [i])

    walk(schema, [])
    out: List[Tuple[str, Tuple[str, ...]]] = []
    seen = set()
    for name, path, node in found:
        reachable = len(path) % 2 == 0 and all(path[i] == "properties" for i in range(0, len(path), 2)) and len(path) > 0
        if not (isinstance(name, str) and _HEADER_TOKEN.fullmatch(name) and reachable and node.get("type") in ("integer", "string", "boolean")):
            return None
        if name.lower() in seen:
            return None
        seen.add(name.lower())
        out.append((name, tuple(path[1::2])))
    return out


def annotated_values(arguments: Dict[str, Any], annotations: List[Tuple[str, Tuple[str, ...]]]) -> Dict[str, str]:
    """Mcp-Param-* headers for the arguments of a call: values that are present and not null."""
    headers: Dict[str, str] = {}
    for name, path in annotations:
        node: Any = arguments
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            continue
        if isinstance(node, bool):
            text = "true" if node else "false"
        elif isinstance(node, int) and abs(node) <= _SAFE_INT:
            text = str(node)
        elif isinstance(node, str):
            text = node
        else:
            continue
        headers[f"Mcp-Param-{name}"] = header_value(text)
    return headers


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

    def __init__(self, modern: bool = True) -> None:
        self.server_info: Dict[str, Any] = {}
        self.server_version = ""
        self.capabilities: Dict[str, Any] = {}
        self.tools_changed = False
        self.try_modern = modern
        self.era = ""                   # "modern" or "legacy" once connected
        self._probing = False
        self.instructions = ""

    def send_request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        raise NotImplementedError

    @staticmethod
    def modern_meta() -> Dict[str, Any]:
        return {META_VERSION: MODERN_VERSION, META_INFO: CLIENT_INFO, META_CAPS: {}}

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """A request in the era this server speaks. Modern requests carry their metadata and may be answered with a
        request for more input, which this client cannot give, or with state to echo back on a retry."""
        if self.era != "modern":
            return self.send_request(method, params, timeout)
        sent = dict(params or {})
        for _ in range(MAX_ROUNDS):
            res = self.send_request(method, {**sent, "_meta": {**(sent.get("_meta") or {}), **self.modern_meta()}}, timeout)
            kind = res.get("resultType", "complete")
            if kind == "complete":
                return res
            if kind != "input_required":
                raise MCPError(f"The server answered with an unknown result type {kind!r}")
            if res.get("inputRequests"):
                raise MCPError("The server asked for input (sampling, elicitation or roots) that this client does not offer")
            if res.get("requestState") is None:
                raise MCPError("The server asked for more input without saying what")
            sent["requestState"] = res["requestState"]
        raise MCPError("The server kept asking for more input")

    def _discover(self) -> Dict[str, Any]:
        return self.send_request("server/discover", {"_meta": self.modern_meta()}, self.request_timeout)

    def _probe_modern(self) -> Optional[Dict[str, Any]]:
        """The discover result if the server is of the stateless era, None if it is a legacy server."""
        self._probing = True
        try:
            res = self._discover()
            supported = res.get("supportedVersions")
            error: Optional[MCPError] = None
        except MCPError as exc:
            if not is_modern_error(exc):
                if exc.status is None and "Cannot reach" in str(exc):
                    raise                                    # the server is not there: that is not a legacy server
                return None
            res, error = {}, exc
            supported = (exc.data or {}).get("supported") if isinstance(exc.data, dict) else None
        except TimeoutError:
            return None
        finally:
            self._probing = False
        if not isinstance(supported, list):
            if error is not None:
                raise MCPError(f"The server refused the request: {error}", error.code, error.status)
            return None
        if MODERN_VERSION in supported:
            if error is not None:
                raise MCPError(f"The server rejected protocol {MODERN_VERSION} although it lists it: {error}", error.code)
            return res
        if any(v in SUPPORTED_VERSIONS for v in supported):
            return None
        raise MCPError(f"The server speaks protocol {', '.join(map(str, supported)) or 'unknown'}; supported: {MODERN_VERSION}, {', '.join(SUPPORTED_VERSIONS)}")

    def initialize(self) -> Dict[str, Any]:
        if self.try_modern:
            found = self._probe_modern()
            if found is not None:
                self.era = "modern"
                self.server_version = MODERN_VERSION
                self.capabilities = found.get("capabilities") or {}
                self.server_info = (found.get("_meta") or {}).get(META_SERVER) or {}
                self.instructions = found.get("instructions") if isinstance(found.get("instructions"), str) else ""
                return found
        self.era = "legacy"
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
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in res.get("tools", []) if isinstance(t, dict) and t.get("name") and self._tool_is_valid(t))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools_changed = False
        return tools

    def _tool_is_valid(self, tool: Dict[str, Any]) -> bool:
        return True

    def call_tool(self, name: str, arguments: Dict[str, Any], timeout: Optional[float] = None) -> str:
        res = self.request("tools/call", {"name": name, "arguments": arguments}, timeout or self.call_timeout)
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

    def __init__(self, command: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, modern: bool = True) -> None:
        super().__init__(modern)
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
                try:
                    self.notify("notifications/cancelled", {"requestId": req_id, "reason": "timeout"})
                except MCPError:
                    pass
                raise TimeoutError(f"MCP request '{method}' timed out after {limit}s")
        finally:
            with self._lock:
                self._pending.pop(req_id, None)
        res = slot[1][0]
        if "error" in res:
            err = res["error"] if isinstance(res["error"], dict) else {"message": str(res["error"])}
            raise MCPError(f"MCP error: {err.get('message', err)}", err.get("code"), data=err.get("data"))
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

    def __init__(self, url: str, token: Optional[str] = None, headers: Optional[Dict[str, str]] = None, modern: bool = True,
                 auth: Optional[Any] = None) -> None:
        super().__init__(modern)
        self.auth = auth                                     # a McpOAuth: the token is read from it for every request
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
        self._param_headers: Dict[str, List[Tuple[str, Tuple[str, ...]]]] = {}

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.extra_headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.server_version:
            h["MCP-Protocol-Version"] = self.server_version
        h.update(extra or {})
        if self.auth is not None:
            token = self.auth.access_token()
            if token:
                h["Authorization"] = f"Bearer {token}"
        return h

    def _modern_headers(self, method: str, params: Dict[str, Any]) -> Dict[str, str]:
        """What a stateless-era request mirrors from its body, so a proxy can route it without reading it."""
        h = {"MCP-Protocol-Version": MODERN_VERSION, "Mcp-Method": method}
        name = params.get("uri") if method == "resources/read" else params.get("name")
        if method in _NAMED and isinstance(name, str):
            h["Mcp-Name"] = header_value(name)
        if method == "tools/call" and isinstance(params.get("arguments"), dict):
            h.update(annotated_values(params["arguments"], self._param_headers.get(params.get("name"), [])))
        return h

    def _tool_is_valid(self, tool: Dict[str, Any]) -> bool:
        if self.era != "modern":
            return True
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        annotations = header_annotations(schema)
        if annotations is None:
            return False                                     # a tool whose header annotations are invalid is left out
        self._param_headers[tool["name"]] = annotations
        return True

    def _post(self, payload: Dict[str, Any], timeout: float, extra: Optional[Dict[str, str]] = None, _again: bool = False):
        try:
            headers = self._headers(extra)
        except AuthRequired as e:
            raise MCPAuthRequired(f"{e} (Settings, Capabilities, Sign in)") from None
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            return _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                www = e.headers.get("WWW-Authenticate", "") if e.headers else ""
                e.read(2000)
                if self.auth is not None and not _again and self.auth.signed_in() and self.auth.refresh():
                    return self._post(payload, timeout, extra, True)          # the token had run out: once more with the new one
                raise MCPAuthRequired("This MCP server answered HTTP 401: it needs you to sign in (Settings, Capabilities, Sign in).", www) from None
            detail = e.read(2000).decode("utf-8", errors="replace")
            if e.code == 404 and self.session_id:
                raise MCPError("The MCP session expired; reconnect", 404, 404) from None
            code, data, text = e.code, None, detail[:500]
            try:
                body = json.loads(detail)
                err = body.get("error") if isinstance(body, dict) else None
                if isinstance(err, dict) and isinstance(err.get("code"), int):
                    code, data, text = err["code"], err.get("data"), str(err.get("message", ""))
            except json.JSONDecodeError:
                pass
            raise MCPError(f"MCP server answered HTTP {e.code}: {text}", code, e.code, data) from None
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
        modern = self.era == "modern" or self._probing
        resp = self._post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
                          timeout if timeout is not None else self.request_timeout,
                          self._modern_headers(method, params or {}) if modern else None)
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
                        raise MCPError(f"MCP error: {err.get('message', err)}", err.get("code"), data=err.get("data"))
                    return msg.get("result") or {}
        raise MCPError(f"The MCP server never answered '{method}'")

    def close(self) -> None:
        if self.session_id:                                  # only a legacy server gives a session
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
