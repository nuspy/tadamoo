"""The VRAM the voice functions borrow from the model's expert cache while the model keeps running.

The broker lends to one holder at a time (serve/vram_broker.py), so every borrowed function shares one lease, sized to their sum: a function
that joins makes the lease larger, one that leaves makes it smaller, and the last one that leaves gives it back. The lease is kept alive here
for as long as any function holds a share, so the expert cache stays small only while the functions are in memory.

    acquire({"tts": 330, "avatar": 4276}, wait_s)   the broker's answer for the new total: {"ok": True, "lease": {...}} or {"ok": False, "reason", ...}
    release("avatar")                               the share of those functions ends
    touch()                                         extend the hold; {"ok": False} when the lease is gone
    holds(function), sizes()                        what is borrowed"""
from __future__ import annotations

import threading

from serve import vram_api

OWNER = "voice"
FALLBACK_MIB = 3000                    # a function's VRAM when no figure is known
TICK_S = 15.0


class Lent:
    def __init__(self, svc, broker, tick_s: float = TICK_S, timer: bool = True) -> None:
        self.svc, self.broker, self.tick_s, self._timer_on = svc, broker, tick_s, timer
        self._lock = threading.RLock()
        self._sizes: dict[str, int] = {}
        self._lease: dict | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- what is borrowed
    def holds(self, function: str) -> bool:
        with self._lock:
            return function in self._sizes

    def sizes(self) -> dict:
        with self._lock:
            return dict(self._sizes)

    def _purpose(self, sizes: dict) -> str:
        return "voice: " + "+".join(sorted(sizes))

    # ---- borrowing and giving back
    def acquire(self, wanted: dict, wait_s: float = 0) -> dict:
        """Add (or resize) the shares of `wanted` ({function: MiB}); a refusal leaves the shares as they were."""
        with self._lock:
            sizes = {**self._sizes, **{f: int(m) for f, m in wanted.items() if int(m) > 0}}
            total = sum(sizes.values())
            if self._lease is not None and sum(self._sizes.values()) == total and set(sizes) == set(self._sizes):
                return {"ok": True, "lease": dict(self._lease)}
            old = self._lease
            if old is not None:
                self.broker.release(old["id"])
                self._lease = None
            got = self.broker.acquire(OWNER, total, self._purpose(sizes), wait_s=wait_s)
            if not got["ok"]:
                self._lease = self._again()                              # the shares as they were
                return got
            self._sizes, self._lease = sizes, got["lease"]
            self._start()
            return got

    def _again(self) -> dict | None:
        """The lease for the shares held now, asked for again after a failed resize (None when there are none or it is refused)."""
        if not self._sizes:
            return None
        got = self.broker.acquire(OWNER, sum(self._sizes.values()), self._purpose(self._sizes))
        if not got["ok"]:
            print(f"[server] warning: the VRAM lent to the voice functions could not be asked for again ({got['reason']})", flush=True)
        return got["lease"] if got["ok"] else None

    def release(self, *functions: str) -> None:
        with self._lock:
            if not any(f in self._sizes for f in functions):
                return
            for f in functions:
                self._sizes.pop(f, None)
            old, self._lease = self._lease, None
            if old is not None:
                self.broker.release(old["id"])
            if self._sizes:
                self._lease = self._again()

    def touch(self) -> dict:
        with self._lock:
            lease = self._lease
        if lease is None:
            return {"ok": False, "reason": "not_found"}
        got = self.broker.touch(lease["id"])
        if not got["ok"]:
            with self._lock:
                if self._lease is lease:
                    self._lease = None
        return got

    # ---- keeping it
    def _start(self) -> None:
        if not self._timer_on or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._keep, name="voice-lease", daemon=True)
        self._thread.start()

    def _keep(self) -> None:
        while not self._stop.wait(self.tick_s):
            with self._lock:
                if not self._sizes:
                    return
                lost = self._lease is None
            if not lost and self.touch()["ok"]:
                continue
            with self._lock:                                             # the lease is gone: ask for the same shares again
                if self._sizes:
                    self._lease = self._again()
                    if self._lease is None:
                        return

    def shutdown(self) -> None:
        self._stop.set()


def of(svc) -> Lent:
    """The server's one holder of the voice functions' lease, made on first use and kept on the service."""
    lent = getattr(svc, "voice_lend", None)
    if lent is None:
        lent = svc.voice_lend = Lent(svc, vram_api.broker_of(svc))
    return lent
