"""The request log: a bounded, thread-safe record of what the server received, what the model wrote and what happened
in between, for the web page's LOGS tab and its saved file.

Events are appended with rising ids; streamed pieces (reasoning, answer) are gathered for a moment and stored as one
event, so a long reasoning is a few thousand events and not a million. The memory budget counts characters; when it
is exceeded the oldest events go and the count of what went is kept, so a reader is told what is missing.

Kinds (the `kind` field), with their `data` and `text`:
  request    data: endpoint, method, client, stream, params{model, temperature, ... max_tokens ...}; text: raw body
  reasoning  text: reasoning pieces as they were produced            data: attempt (structured requests)
  content    text: answer pieces                                      data: attempt
  attempt    a structured try that ended: data: attempt, ok, error, finish_reason, discarded
  repair     the message that asks for the next try: text; data: attempt, path, repeated
  timing     data: prefill_tokens, prefill_s, prompt_tokens, cached_tokens, first_token_s, tokens_per_s
  finish     data: finish_reason, usage, timings, duration_s
  cancel     data: reason
  error      data: type, message
  gpu        data: the hardware figures at that moment (memory, use, temperature)
  engine     data/text: loads, unloads, stopped repetitions, cache events
  server     text: a line the server printed
"""
from __future__ import annotations

import collections
import json
import threading
import time

DEFAULT_MAX_MIB = 32
EVENT_OVERHEAD = 96             # characters counted for an event beyond its text and data
GATHER_S = 0.4                  # how long streamed pieces are gathered before they become an event
GATHER_CHARS = 4096
DELTA_KINDS = ("reasoning", "content")


def _weight(event: dict) -> int:
    data = event.get("data")
    return EVENT_OVERHEAD + len(event.get("text") or "") + (len(json.dumps(data, ensure_ascii=False, default=str))
                                                            if data else 0)


class RequestLog:
    def __init__(self, max_chars: int = DEFAULT_MAX_MIB << 20, clock=time.time, enabled: bool = True):
        self.lock = threading.Lock()
        self.max_chars = max(1 << 16, int(max_chars))
        self.clock = clock
        self.enabled = enabled
        self.events: collections.deque = collections.deque()
        self.chars = 0
        self.last = 0                                   # id of the newest event
        self.epoch = 0                                  # changes when the log is cleared
        self.dropped_events = 0                         # discarded for the memory budget since the last clear
        self.dropped_chars = 0
        self.serial = 0
        self.pending: dict[tuple, dict] = {}            # (req, kind, attempt) -> a gathering event

    def new_request(self) -> str:
        with self.lock:
            self.serial += 1
            return f"r{self.serial}"

    def _store(self, event: dict) -> int:
        self.last += 1
        event["id"] = self.last
        self.events.append(event)
        self.chars += _weight(event)
        while self.chars > self.max_chars and len(self.events) > 1:
            old = self.events.popleft()
            weight = _weight(old)
            self.chars -= weight
            self.dropped_events += 1
            self.dropped_chars += weight
        return self.last

    def _flush(self, req=None, older_than: float | None = None) -> None:
        now = self.clock()
        for key in [k for k, p in self.pending.items()
                    if (req is None or k[0] == req) and (older_than is None or now - p["_since"] >= older_than)]:
            piece = self.pending.pop(key)
            piece.pop("_since")
            self._store(piece)

    def add(self, req, kind: str, data: dict | None = None, text: str = "") -> int | None:
        """Store one event of request `req` (None: not about one request); the id, or None when the log is off."""
        if not self.enabled:
            return None
        with self.lock:
            self._flush(req)                            # gathered pieces come before whatever follows them
            event = {"t": round(self.clock(), 3), "req": req, "kind": kind}
            if data:
                event["data"] = data
            if text:
                event["text"] = text
            return self._store(event)

    def piece(self, req, kind: str, text: str, attempt: int | None = None) -> None:
        """A streamed piece of reasoning or answer: gathered with its neighbours into one event."""
        if not self.enabled or not text:
            return
        key = (req, kind, attempt)
        with self.lock:
            for other in [k for k in self.pending if k[0] == req and k != key]:
                self._flush_key(other)
            cur = self.pending.get(key)
            if cur is None:
                cur = {"t": round(self.clock(), 3), "req": req, "kind": kind, "text": "", "_since": self.clock()}
                if attempt is not None:
                    cur["data"] = {"attempt": attempt}
                self.pending[key] = cur
            cur["text"] += text
            if len(cur["text"]) >= GATHER_CHARS or self.clock() - cur["_since"] >= GATHER_S:
                self._flush_key(key)

    def _flush_key(self, key) -> None:
        piece = self.pending.pop(key, None)
        if piece is not None:
            piece.pop("_since")
            self._store(piece)

    def flush(self, req=None) -> None:
        with self.lock:
            self._flush(req)

    def since(self, after: int = 0, max_chars: int = 2 << 20) -> dict:
        """The events with an id above `after`, oldest first, the newest ones only when more than `max_chars` of
        them wait (`skipped` says how many were left out of the answer)."""
        with self.lock:
            self._flush(older_than=GATHER_S)
            fresh = [e for e in self.events if e["id"] > after]
            skipped, total = 0, 0
            keep = []
            for event in reversed(fresh):
                total += _weight(event)
                if total > max_chars and keep:
                    skipped = len(fresh) - len(keep)
                    break
                keep.append(event)
            keep.reverse()
            return {"enabled": self.enabled, "epoch": self.epoch, "last": self.last, "events": [dict(e) for e in keep],
                    "skipped": skipped, "dropped": {"events": self.dropped_events, "chars": self.dropped_chars},
                    "capacity_chars": self.max_chars, "held_chars": self.chars}

    def clear(self) -> None:
        with self.lock:
            self.events.clear()
            self.pending.clear()
            self.chars = self.dropped_events = self.dropped_chars = 0
            self.epoch += 1

    def snapshot(self) -> list[dict]:
        """Every kept event (gathered pieces included), for the saved file."""
        with self.lock:
            self._flush()
            return [dict(e) for e in self.events]

    def meta(self) -> dict:
        with self.lock:
            return {"epoch": self.epoch, "dropped_events": self.dropped_events, "dropped_chars": self.dropped_chars,
                    "capacity_chars": self.max_chars}


def clock_text(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def event_text(event: dict) -> str:
    """One event as readable text: a header line, then its data as indented JSON and its text as it is."""
    head = f"[{clock_text(event['t'])}] {event.get('req') or '-':<5} {event['kind'].upper()}"
    lines = [head]
    if event.get("data"):
        lines.append(json.dumps(event["data"], ensure_ascii=False, indent=2, default=str))
    if event.get("text"):
        lines.append(event["text"])
    return "\n".join(lines)


def export_text(log: RequestLog):
    """The saved file as readable text, in pieces."""
    meta = log.meta()
    yield f"# Request log, saved {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    if meta["dropped_events"]:
        yield (f"# {meta['dropped_events']} older events ({meta['dropped_chars']} characters) were discarded to stay "
               f"within the memory budget of {meta['capacity_chars']} characters.\n")
    yield "\n"
    for event in log.snapshot():
        yield event_text(event) + "\n\n"


def export_jsonl(log: RequestLog):
    """The saved file as JSON lines: a header record, then one event per line."""
    yield json.dumps({"kind": "log", "saved": round(time.time(), 3), **log.meta()}, ensure_ascii=False) + "\n"
    for event in log.snapshot():
        yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
