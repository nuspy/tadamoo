"""A stateless-era (2026-07-28) MCP server over Streamable HTTP for tests.

It checks what a real one must: the version header against the body's _meta, Mcp-Method and Mcp-Name against the body,
and Mcp-Param-* against the arguments (answering 400 with HeaderMismatch otherwise), and it ignores sessions.
mode: ""        modern only
      "dual"    a client that opens with initialize gets the legacy handshake, with a session id
      "legacy"  a legacy server that answers anything but initialize with 400 and a JSON-RPC error of the older kind
      "unsupported"  answers modern requests with UnsupportedProtocolVersion listing `supported`
      "state" / "elicit"  input_required results as in the stdio fake
"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

META = "io.modelcontextprotocol/"
TOOLS = [
    {"name": "echo", "description": "Echo", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "execute_sql", "description": "Runs SQL", "inputSchema": {"type": "object", "properties": {
        "region": {"type": "string", "x-mcp-header": "Region"}, "query": {"type": "string"}, "limit": {"type": "integer", "x-mcp-header": "Limit"},
        "dry": {"type": "boolean", "x-mcp-header": "Dry"}}, "required": ["region", "query"]}},
    {"name": "bad_number", "description": "x-mcp-header on a number", "inputSchema": {"type": "object", "properties": {
        "x": {"type": "number", "x-mcp-header": "X"}}}},
    {"name": "bad_nested", "description": "x-mcp-header under items", "inputSchema": {"type": "object", "properties": {
        "xs": {"type": "array", "items": {"type": "string", "x-mcp-header": "Xs"}}}}},
    {"name": "bad_dup", "description": "duplicate names", "inputSchema": {"type": "object", "properties": {
        "a": {"type": "string", "x-mcp-header": "Same"}, "b": {"type": "string", "x-mcp-header": "same"}}}},
    {"name": "bad_empty", "description": "empty name", "inputSchema": {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": ""}}}},
    {"name": "bad_chars", "description": "not a token", "inputSchema": {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "a b"}}}},
]


def decode_header(value):
    if value.startswith("=?base64?") and value.endswith("?="):
        return base64.b64decode(value[9:-2]).decode("utf-8")
    return value


class FakeMcpModernHttp:
    def __init__(self, mode="", sse=False, token="", supported=("2026-07-28",)):
        self.mode, self.sse, self.token, self.supported = mode, sse, token, list(supported)
        self.requests = []          # (method, lower-cased headers, body)
        self.deleted = False
        self.rounds = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body=b"", ctype="application/json", extra=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _error(self, status, rid, code, message, data=None):
                err = {"code": code, "message": message}
                if data is not None:
                    err["data"] = data
                self._send(status, json.dumps({"jsonrpc": "2.0", "id": rid, "error": err}).encode())

            def _result(self, rid, result, extra=None):
                reply = {"jsonrpc": "2.0", "id": rid, "result": result}
                if outer.sse:
                    events = (b"event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}).encode()
                              + b"\n\nevent: message\ndata: " + json.dumps(reply).encode() + b"\n\n")
                    self._send(200, events, "text/event-stream", extra)
                else:
                    self._send(200, json.dumps(reply).encode(), "application/json", extra)

            def do_DELETE(self):
                outer.requests.append(("DELETE", {k.lower(): v for k, v in self.headers.items()}, None))
                outer.deleted = True
                self._send(200)

            def do_GET(self):
                outer.requests.append(("GET", {k.lower(): v for k, v in self.headers.items()}, None))
                self._send(405)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
                h = {k.lower(): v for k, v in self.headers.items()}
                outer.requests.append(("POST", h, body))
                if outer.token and h.get("authorization") != f"Bearer {outer.token}":
                    return self._send(401, b'{"error": "no"}')
                method, rid, params = body.get("method"), body.get("id"), body.get("params") or {}
                if method == "initialize":
                    if outer.mode in ("dual", "legacy"):
                        return self._result(rid, {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": {"name": "fake-legacy"}},
                                            {"Mcp-Session-Id": "sess-1"})
                    return self._error(400, rid, -32602, "initialize is not a method of this revision")
                if rid is None:
                    return self._send(202)
                if outer.mode == "legacy" and h.get("mcp-session-id") != "sess-1":
                    return self._error(400, None, -32000, "Bad Request: Mcp-Session-Id header is required")
                if outer.mode == "dual" and h.get("mcp-session-id") == "sess-1":
                    return self._legacy(rid, method, params)
                meta = params.get("_meta") or {}
                version = meta.get(META + "protocolVersion")
                if outer.mode == "unsupported" or version not in outer.supported:
                    return self._error(400, rid, -32022, "Unsupported protocol version", {"supported": outer.supported, "requested": version})
                if h.get("mcp-protocol-version") != version:
                    return self._error(400, rid, -32020, "Header mismatch: MCP-Protocol-Version")
                if h.get("mcp-method") != method:
                    return self._error(400, rid, -32020, "Header mismatch: Mcp-Method")
                if method in ("tools/call", "prompts/get", "resources/read"):
                    name = params.get("uri") if method == "resources/read" else params.get("name")
                    if decode_header(h.get("mcp-name", "")) != name:
                        return self._error(400, rid, -32020, "Header mismatch: Mcp-Name")
                if method == "tools/call":
                    tool = next((t for t in TOOLS if t["name"] == params.get("name")), None)
                    for key, spec in ((tool or {}).get("inputSchema", {}).get("properties") or {}).items():
                        if "x-mcp-header" not in spec:
                            continue
                        value = (params.get("arguments") or {}).get(key)
                        header = h.get("mcp-param-" + spec["x-mcp-header"].lower())
                        want = None if value is None else ("true" if value is True else "false" if value is False else str(value))
                        if (header is None) != (want is None) or (header is not None and decode_header(header) != want):
                            return self._error(400, rid, -32020, f"Header mismatch: Mcp-Param-{spec['x-mcp-header']}")
                if META + "clientCapabilities" not in meta:
                    return self._error(400, rid, -32602, "missing client capabilities")
                if method == "server/discover":
                    return self._result(rid, {"resultType": "complete", "supportedVersions": outer.supported, "capabilities": {"tools": {}},
                                              "_meta": {META + "serverInfo": {"name": "fake-modern-http", "version": "2"}}})
                if method == "tools/list":
                    return self._result(rid, {"resultType": "complete", "tools": TOOLS})
                if method == "tools/call":
                    key = (rid, params.get("name"))
                    if outer.mode == "elicit":
                        return self._result(rid, {"resultType": "input_required", "inputRequests": {"who": {"method": "elicitation/create", "params": {}}}})
                    if outer.mode == "state" and not params.get("requestState"):
                        return self._result(rid, {"resultType": "input_required", "requestState": "opaque-state-1"})
                    text = "echo: " + str((params.get("arguments") or {}).get("text", params.get("name")))
                    if params.get("requestState"):
                        text += " (state " + params["requestState"] + ")"
                    return self._result(rid, {"resultType": "complete", "content": [{"type": "text", "text": text}]})
                return self._error(404, rid, -32601, "Method not found")

            def _legacy(self, rid, method, params):
                if method == "tools/list":
                    return self._result(rid, {"tools": TOOLS[:1]})
                if method == "tools/call":
                    return self._result(rid, {"content": [{"type": "text", "text": "legacy echo: " + params["arguments"]["text"]}]})
                return self._error(200, rid, -32601, "no")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
