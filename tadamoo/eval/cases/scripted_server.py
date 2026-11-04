"""A small chat-completions server on this machine that answers from a script: text, an HTTP error, or a stall.

Used by the experiments that need a server's behaviour to be exactly what the experiment says (E3), and by their tests. It listens on 127.0.0.1
only. A script is a list of steps; the last step is repeated for ever when `forever` is set, else the server answers 500 once the script is spent.

    {"ok": "text"}                   a normal answer
    {"error": 429, "message": "..."} an HTTP error with a JSON body in the server's shape
    {"stall": 1.5}                   no answer for that many seconds, then the connection is dropped
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


class ScriptedServer:
    def __init__(self, steps: List[Dict[str, Any]], forever: bool = False, context: int = 32768) -> None:
        self.steps, self.forever, self.context = list(steps), forever, context
        self.requests: List[float] = []                       # when each chat request arrived (monotonic)
        self._lock = threading.Lock()
        self._used = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _json(self, code: int, body: Dict[str, Any]) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.split("?")[0] == "/v1/status":
                    self._json(200, {"loaded": True, "context": {"native": outer.context}})
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                try:
                    self._answer()
                except (ConnectionError, OSError):
                    self.close_connection = True                  # the client gave up (a timeout, or it had all it needed)

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                step = outer._next()
                if "stall" in step:
                    time.sleep(float(step["stall"]))
                    self.close_connection = True
                    return
                if "error" in step:
                    self._json(int(step["error"]), {"error": {"message": step.get("message") or f"HTTP {step['error']}"}})
                    return
                text = str(step.get("ok", "ok"))
                usage = {"prompt_tokens": 50, "completion_tokens": max(1, len(text.split())), "prompt_tokens_details": {"cached_tokens": 0}}
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for chunk in ({"choices": [{"delta": {"content": text}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}):
                        self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self._json(200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": usage})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="scripted-server", daemon=True)

    def _next(self) -> Dict[str, Any]:
        with self._lock:
            self.requests.append(time.monotonic())
            i = self._used
            self._used += 1
        if i < len(self.steps):
            return self.steps[i]
        if self.forever and self.steps:
            return self.steps[-1]
        return {"error": 500, "message": "the script is spent"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> "ScriptedServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def count(self) -> int:
        return len(self.requests)
