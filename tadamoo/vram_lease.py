"""Borrowing VRAM from TaDaMoo's server for a heavy job, from a process that does not import the server (the voice service, the knowledge workers).

    with VramLease("docling", 3000, "reading a PDF") as lease:
        device = "cuda" if lease.gpu_ok else "cpu"
        ...                                    # the hold is extended while the block runs

A lease that could not be had (the model cannot lend, no server, a refusal) has `gpu_ok` False and `reason` set: the caller uses the CPU, or
waits and asks again when the reason is `busy`. Nothing here raises for a missing server. Standard library only."""
from __future__ import annotations

import http.client
import json
import os
import threading
from typing import Callable
from urllib.parse import urlsplit

DEFAULT_URL = "http://127.0.0.1:7242"
TIMEOUT_S = 130.0                                  # asking for VRAM can take as long as the engine takes to give the cache up
CONTROL_HEADER = "X-Tadamoo-Control"
LEASE = "/api/vram/lease"

Transport = Callable[[str, str, "dict | None"], "tuple[int, dict]"]


def http_transport(base_url: str | None = None, token: str = "", control_secret: str = "", timeout: float = TIMEOUT_S) -> Transport:
    """A transport over HTTP to the server on this PC. `token` is the voice service's bearer token, `control_secret` the server's own."""
    parts = urlsplit(base_url or os.environ.get("TADAMOO_URL") or DEFAULT_URL)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if control_secret:
            headers[CONTROL_HEADER] = control_secret
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            r = conn.getresponse()
            raw = r.read(1 << 20)
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            return r.status, data if isinstance(data, dict) else {}
        finally:
            conn.close()

    return call


class VramLease:
    def __init__(self, owner: str, mib: int, purpose: str = "", hold_s: int | None = None, wait_s: float = 0, keepalive: bool = True,
                 transport: Transport | None = None, base_url: str | None = None, token: str = "", control_secret: str = "") -> None:
        self.owner, self.mib, self.purpose, self.hold_s, self.wait_s, self.keepalive = owner, mib, purpose, hold_s, wait_s, keepalive
        self._call = transport or http_transport(base_url, token, control_secret)
        self.id: str | None = None
        self.granted_mib = 0
        self.reason = ""                           # why there is no GPU for the caller ("" when there is)
        self.message = ""
        self.message_it = ""
        self.lost = False                          # the server forgot the lease (it expired): the VRAM may be taken back
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def gpu_ok(self) -> bool:
        return self.id is not None and self.granted_mib > 0 and not self.lost

    def acquire(self) -> "VramLease":
        body = {"owner": self.owner, "mib": self.mib, "purpose": self.purpose, "wait_s": self.wait_s}
        if self.hold_s is not None:
            body["hold_s"] = self.hold_s
        try:
            status, out = self._call("POST", LEASE, body)
        except (OSError, http.client.HTTPException, ValueError) as e:
            return self._refused("no_engine", f"the server does not answer ({e.__class__.__name__})")
        lease = out.get("lease") if isinstance(out.get("lease"), dict) else None
        if status == 200 and out.get("ok") and lease and isinstance(lease.get("id"), str):
            self.id, self.granted_mib = lease["id"], int(lease.get("granted_mib") or 0)
            self.hold_s = int(lease.get("hold_s") or self.hold_s or 60)
            if self.keepalive:
                self._thread = threading.Thread(target=self._keep, name="vram-lease-keepalive", daemon=True)
                self._thread.start()
            return self
        err = out.get("error") if isinstance(out.get("error"), dict) else {}
        return self._refused(str(out.get("reason") or err.get("code") or "unavailable"), str(out.get("message") or err.get("message") or ""),
                             str(out.get("message_it") or ""))

    def _refused(self, reason: str, message: str, message_it: str = "") -> "VramLease":
        self.id, self.granted_mib, self.reason, self.message, self.message_it = None, 0, reason, message, message_it
        return self

    def touch(self) -> bool:
        if self.id is None:
            return False
        try:
            status, _ = self._call("POST", f"{LEASE}/{self.id}/touch", None)
        except (OSError, http.client.HTTPException, ValueError):
            return False
        if status == 404:
            self.lost = True
        return status == 200

    def _keep(self) -> None:
        while not self._stop.wait(max(1.0, (self.hold_s or 60) / 3)):
            if not self.touch() and self.lost:
                return

    def release(self) -> None:
        self._stop.set()
        lease_id, self.id = self.id, None
        if lease_id is None:
            return
        try:
            self._call("POST", f"{LEASE}/{lease_id}/release", None)
        except (OSError, http.client.HTTPException, ValueError):
            pass                                   # the server gives it back when the hold passes

    def __enter__(self) -> "VramLease":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
