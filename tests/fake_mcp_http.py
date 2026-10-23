"""An MCP server over Streamable HTTP for tests: answers as JSON or as an event stream, keeps a session id."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeMcpHttp:
    def __init__(self, sse: bool = False, token: str = "", version: str = "2025-11-25"):
        self.sse, self.token, self.version = sse, token, version
        self.session = "sess-123"
        self.requests = []          # (method, headers, body)
        self.deleted = False
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

            def do_DELETE(self):
                outer.requests.append(("DELETE", dict(self.headers), None))
                outer.deleted = True
                self._send(200)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
                outer.requests.append(("POST", {k.lower(): v for k, v in self.headers.items()}, body))
                if outer.token and self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self._send(401, b'{"error": "no"}')
                method, rid = body.get("method"), body.get("id")
                if method != "initialize" and self.headers.get("Mcp-Session-Id") != outer.session:
                    return self._send(404, b'{"error": "unknown session"}')
                if rid is None:
                    return self._send(202)
                if method == "initialize":
                    result = {"protocolVersion": outer.version, "capabilities": {}, "serverInfo": {"name": "fake-http"}}
                    extra = {"Mcp-Session-Id": outer.session}
                elif method == "tools/list":
                    result, extra = {"tools": [{"name": "echo", "description": "Echo", "inputSchema": {
                        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}, {}
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": "echo: " + body["params"]["arguments"]["text"]}]}
                    extra = {}
                else:
                    return self._send(200, json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}}).encode())
                reply = {"jsonrpc": "2.0", "id": rid, "result": result}
                if outer.sse:
                    events = (b"event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "method": "notifications/message",
                                                                       "params": {}}).encode() + b"\n\n"
                              b"event: message\ndata: " + json.dumps(reply).encode() + b"\n\n")
                    self._send(200, events, "text/event-stream", extra)
                else:
                    self._send(200, json.dumps(reply).encode(), "application/json", extra)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
