"""A stand-in for Strata's `python -m serve.server`: the same start-up, ready-file and /load protocol and a few /v1 routes,
with no model. Tests copy it into a fake bin/strata/serve/ (fake_strata/__init__.py: install)."""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--engine")
ap.add_argument("--config")
ap.add_argument("--port", type=int, default=7232)
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--lazy", action="store_true")
ap.add_argument("--ready-file")
a = ap.parse_args()
cfg = json.load(open(a.config, encoding="utf-8"))
state = {"loaded": False}
NAME = cfg.get("model_name", "qwen3.8-flash-next")
FAIL = os.environ.get("FAKE_STRATA_FAIL", "")
if FAIL == "start":
    print("the model's tokenizer is missing", file=sys.stderr, flush=True)
    sys.exit(2)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/v1/models", "/models"):
            self.send(200, {"object": "list", "data": [{"id": NAME, "object": "model", "owned_by": "strata"}]})
        elif p == "/v1/status":
            self.send(200, {"service": "strata", "model": NAME, "loaded": state["loaded"], "args": cfg.get("args"),
                            "parallel": cfg.get("parallel"), "pid": os.getpid()})
        else:
            self.send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        if p == "/load":
            time.sleep(float(os.environ.get("FAKE_STRATA_LOAD_S", "0")))
            if FAIL == "load":
                self.send(503, {"error": {"type": "server_error", "message": "out of memory"}})
                return
            state["loaded"] = True
            self.send(200, {"status": "loaded"})
        elif p == "/unload":
            state["loaded"] = False
            self.send(200, {"status": "unloaded"})
        elif p == "/v1/chat/completions":
            if req.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for word in ("Hello", " from", " Strata"):
                    chunk = {"id": "c1", "object": "chat.completion.chunk", "model": NAME,
                             "choices": [{"index": 0, "delta": {"content": word}}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                return
            self.send(200, {"id": "c1", "object": "chat.completion", "model": NAME,
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello from Strata",
                                                                 "reasoning_content": "thinking"},
                                         "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}})
        elif p == "/v1/messages":
            self.send(200, {"id": "m1", "type": "message", "role": "assistant", "model": NAME,
                            "content": [{"type": "text", "text": "Hello from Strata"}], "stop_reason": "end_turn"})
        else:
            self.send(404, {"error": {"message": "not found"}})


srv = ThreadingHTTPServer((a.host, a.port), H)
if a.ready_file:
    tmp = a.ready_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"api_state": "ready", "bound_port": a.port, "pid": os.getpid(), "schema_version": 1,
                   "model_state": "unloaded" if a.lazy else "ready"}, f)
    os.replace(tmp, a.ready_file)
print(f"ready: http://{a.host}:{a.port}/v1", flush=True)
srv.serve_forever()
