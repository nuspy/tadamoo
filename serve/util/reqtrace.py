"""One request's entries in the request log (serve/util/reqlog.py), and the hardware ticker that runs while requests do."""
from __future__ import annotations

import json
import threading
import time

GPU_EVERY_S = 5.0                # seconds between hardware events while a request is in flight
# request fields that are recorded when present: plain values only, the prompt itself is the `text` of the event
PARAM_KEYS = ("model", "temperature", "top_p", "top_k", "min_p", "seed", "presence_penalty", "frequency_penalty",
              "repetition_penalty", "penalty_last_n", "max_tokens", "max_completion_tokens", "max_output_tokens",
              "reasoning_effort", "reasoning_budget_tokens", "structured_retries", "tool_choice", "stream")


def _plain(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def request_params(req: dict) -> dict:
    """The sampling, budget and format fields of a request body that matter when reading its log."""
    out = {k: req[k] for k in PARAM_KEYS if k in req and _plain(req[k])}
    out["stream"] = bool(req.get("stream"))
    stop = req.get("stop") if "stop" in req else req.get("stop_sequences")
    if (_plain(stop) and stop is not None) or (isinstance(stop, list) and all(isinstance(x, str) for x in stop)):
        out["stop"] = stop
    reasoning = req.get("reasoning")
    if isinstance(reasoning, dict):                                  # Responses
        out["reasoning"] = {k: v for k, v in reasoning.items() if _plain(v)}
    thinking = req.get("thinking")
    if isinstance(thinking, dict):                                   # Messages
        out["thinking"] = {k: v for k, v in thinking.items() if _plain(v)}
    fmt = req.get("response_format")
    text = req.get("text")
    if fmt is None and isinstance(text, dict):                       # Responses: text.format
        fmt = text.get("format")
    if isinstance(fmt, dict):
        spec = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), dict) else fmt
        out["response_format"] = {k: v for k, v in {"type": fmt.get("type"), "name": spec.get("name")}.items() if v}
    for key in ("tools", "messages", "input"):
        if isinstance(req.get(key), list):
            out[f"{key}_count"] = len(req[key])
    return out


class GpuTicker:
    """Writes the hardware figures the server already has to the request log: when a request starts and ends, and
    every GPU_EVERY_S seconds while any request is in flight. Nothing is measured here; without readings, nothing is
    written."""

    def __init__(self, svc, every: float = GPU_EVERY_S):
        self.svc, self.every = svc, every
        self.lock = threading.Lock()
        self.inflight: set = set()
        self.idle = threading.Event()
        self.thread: threading.Thread | None = None

    def _emit(self, rid, when: str) -> None:
        log = self.svc.reqlog
        if not log.enabled:
            return
        figures = self.svc.hardware_now()
        if figures:
            log.add(rid, "gpu", {"when": when, **figures})

    def enter(self, rid) -> None:
        with self.lock:
            self.inflight.add(rid)
            if self.thread is None:
                self.idle.clear()
                self.thread = threading.Thread(target=self._loop, daemon=True, name="reqlog-gpu")
                self.thread.start()
        self._emit(rid, "start")

    def leave(self, rid) -> None:
        self._emit(rid, "end")
        with self.lock:
            self.inflight.discard(rid)
            if not self.inflight:
                self.idle.set()

    def _loop(self) -> None:
        while True:
            woke = self.idle.wait(self.every)
            with self.lock:
                if not self.inflight:
                    self.thread = None
                    return
                if woke:                                             # a new request came before the thread ended
                    self.idle.clear()
                    continue
                busy = sorted(self.inflight)
            log = self.svc.reqlog
            figures = self.svc.hardware_now() if log.enabled else None
            if figures:
                log.add(None, "gpu", {"when": "tick", "requests": busy, **figures})


class RequestTrace:
    """What the request log records for one request of the three APIs. The server's handler creates it, the engine
    loop (Service.run) reports the text and the timings through the thread's request context, and the structured
    retry loop reports its tries. With the log off every call returns at once."""

    def __init__(self, svc, endpoint: str, method: str, client: str):
        self.svc, self.log = svc, svc.reqlog
        self.on = self.log.enabled
        self.endpoint, self.method, self.client = endpoint, method, client
        self.id = self.log.new_request() if self.on else None
        self.t0 = time.monotonic()
        self.attempt: int | None = None                   # the structured try being generated
        self.stream = False
        self.logged_request = self.logged_cancel = self.ended = False
        self.first_s: float | None = None                 # seconds from the pass's start to its first piece
        self.pass_t0 = self.t0
        self.finish: dict | None = None
        self.prompt_tokens: int | None = None
        self.streamed: set = set()                        # tool calls whose pieces were already logged
        self.tries = 0
        self.watched = False                              # the hardware ticker knows this request

    # --- what came in
    def request(self, req: dict | None = None, raw: bytes | None = None) -> None:
        """The request event: its parameters and the body as it was received (`raw` when it was not valid JSON)."""
        if not self.on or self.logged_request:
            return
        self.logged_request = True
        data = {"endpoint": self.endpoint, "method": self.method, "client": self.client, "stream": False,
                "params": {}}
        text = ""
        if isinstance(req, dict):
            data["params"] = request_params(req)
            data["stream"] = data["params"]["stream"]
            self.stream = data["stream"]
            text = json.dumps(req, indent=2, ensure_ascii=False, default=str)
        elif raw is not None:
            data["unparsed"] = True
            text = raw.decode("utf-8", "replace")
        self.log.add(self.id, "request", data, text)
        self.watched = True
        self.svc.gpu_ticker.enter(self.id)

    # --- what the model wrote (called from Service.run)
    def event(self, ev) -> None:
        if not self.on:
            return
        if ev.kind in ("reasoning", "content") and ev.text:
            self._first()
            self.log.piece(self.id, ev.kind, ev.text, self.attempt)
        elif ev.kind == "tool_start":
            self._first()
            self.streamed.add(ev.call.id)
            self.log.piece(self.id, "content", f"\n[tool call: {ev.call.name}] ", self.attempt)
        elif ev.kind == "tool_args" and ev.text:
            self.log.piece(self.id, "content", ev.text, self.attempt)
        elif ev.kind == "tool_call" and ev.call.id not in self.streamed:
            self._first()
            self.log.piece(self.id, "content", f"\n[tool call: {ev.call.name}] "
                           + json.dumps(ev.call.arguments, ensure_ascii=False), self.attempt)

    def _first(self) -> None:
        if self.first_s is None:
            self.first_s = round(time.monotonic() - self.pass_t0, 3)
            data = {"attempt": self.attempt, "first_token_s": self.first_s, "prompt_tokens": self.prompt_tokens}
            self.log.add(self.id, "timing", {k: v for k, v in data.items() if v is not None})

    def run_started(self, prompt_tokens: int) -> None:
        """A generation pass begins (one per structured try)."""
        self.prompt_tokens, self.first_s, self.pass_t0 = prompt_tokens, None, time.monotonic()
        self.tries += 1

    def run_done(self, done: dict) -> None:
        """A generation pass ended: the engine's figures, kept for the request's finish event."""
        if not self.on:
            return
        t = done.get("timings") or {}
        usage = {"prompt_tokens": self.prompt_tokens, "completion_tokens": done.get("completion_tokens"),
                 "cached_tokens": done.get("reused") or 0, "reasoning_tokens": done.get("reasoning_tokens")}
        timing = {"attempt": self.attempt, "prompt_tokens": self.prompt_tokens, "cached_tokens": usage["cached_tokens"],
                  "prefill_tokens": t.get("prompt_n"),
                  "prefill_s": round(t["prompt_ms"] / 1000, 3) if t.get("prompt_ms") is not None else None,
                  "first_token_s": self.first_s, "tokens_per_s": t.get("predicted_per_second"),
                  "completion_tokens": done.get("completion_tokens")}
        self.log.add(self.id, "timing", {k: v for k, v in timing.items() if v is not None})
        self.finish = {"finish_reason": done.get("finish"), "usage": usage, "timings": t or None}
        if done.get("finish") == "cancel":
            self.cancel("the request was cancelled")

    # --- what happened around it
    def note(self, kind: str, data: dict | None = None, text: str = "") -> None:
        """A structured try's outcome ("attempt") or the repair message sent for the next one ("repair")."""
        if self.on:
            self.log.add(self.id, kind, data, text)

    def cancel(self, reason: str) -> None:
        if self.on and not self.logged_cancel:
            self.logged_cancel = True
            self.log.add(self.id, "cancel", {"reason": reason})

    def error(self, kind: str, message: str) -> None:
        if self.on:
            self.request()
            self.log.add(self.id, "error", {"type": kind, "message": message})

    def error_body(self, status: int | None, body) -> None:
        """An error the server sent, as {"error": {...}}; `status` is its HTTP status (None: sent inside a stream)."""
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            self.error(str(err.get("type") or err.get("code") or "error"),
                       (f"{status}: " if status else "") + str(err.get("message")))

    def end(self) -> None:
        """The request is over: its finish event (always the last one of the request) and the closing hardware figures."""
        if not self.on or self.ended:
            return
        self.ended = True
        self.request()
        done = self.finish or {}
        data = {"finish_reason": done.get("finish_reason") or ("cancel" if self.logged_cancel else None), "usage": done.get("usage"), "timings": done.get("timings"),
                "duration_s": round(time.monotonic() - self.t0, 3)}
        if self.tries > 1:
            data["attempts"] = self.tries
        if self.watched:
            self.svc.gpu_ticker.leave(self.id)
        self.log.add(self.id, "finish", {k: v for k, v in data.items() if v is not None or k == "finish_reason"})
