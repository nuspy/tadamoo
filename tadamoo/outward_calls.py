"""Tells an observer that a tool which sends something out of this PC is about to run.

The agent loop calls `note` once per such call. Who listens is set by the host: `add_observer` in the same process, or
`report_to` for an agent started by the server, which posts each call to the server's security monitor. Nothing here
depends on the server's code, and a listener that fails never changes what the tool does."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List

TARGET_CHARS = 2000
_observers: List[Callable[[float, str, str], None]] = []


def add_observer(fn: Callable[[float, str, str], None]) -> None:
    if fn not in _observers:
        _observers.append(fn)


def target_of(arguments: Any) -> str:
    """The text of the call's arguments that can name an address: every string value, joined."""
    if not isinstance(arguments, dict):
        return ""
    parts = [v for v in arguments.values() if isinstance(v, str)]
    return " ".join(parts)[:TARGET_CHARS]


def note(tool: str, arguments: Any) -> None:
    if not _observers:
        return
    ts, target = time.time(), target_of(arguments)
    for fn in list(_observers):
        try:
            fn(ts, tool, target)
        except Exception:
            pass


def report_to(endpoint: str, api_key: str | None) -> None:
    """Posts each call to <endpoint>/api/netwatch/tool-calls in the background, as the server's other clients do."""
    base = endpoint.rstrip("/")

    def send(ts: float, tool: str, target: str) -> None:
        def post() -> None:
            headers: Dict[str, str] = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            try:
                from tadamoo import control_secret
                headers.update(control_secret.header_for(base))
                req = urllib.request.Request(base + "/api/netwatch/tool-calls", data=json.dumps({"ts": ts, "tool": tool, "target": target}).encode("utf-8"),
                                             headers=headers, method="POST")
                urllib.request.urlopen(req, timeout=5).close()
            except Exception:
                pass
        threading.Thread(target=post, daemon=True, name="tool-call-report").start()

    add_observer(send)
