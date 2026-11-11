"""VRAM for a heavy job, with the local model put to sleep when lending is not enough and the person allows it. Standard library only.

    with HeavyLease("video", 24000, "a clip for the newscast", minutes=5) as lease:
        if lease.gpu_ok:
            ...                                # lease.swapped says whether the conversations run on a provider meanwhile
        else:
            ...                                # lease.reason: not_enough, not_allowed, no_provider, timeout, ...

It asks the server for the VRAM first (tadamoo/vram_lease.py). Only when the answer is `not_enough` does it ask to sleep the local model
(`/api/heavy/begin`: the person answers on a card, or has allowed it for this kind of job), then asks for the VRAM again. `release` gives the VRAM back and
then lets the model wake. Nothing here raises for a missing server."""
from __future__ import annotations

import http.client
import threading

from tadamoo.vram_lease import Transport, VramLease, http_transport

BASE = "/api/heavy"
ASK_TIMEOUT_S = 700.0                              # the person may take their time answering the card


class HeavyLease:
    def __init__(self, owner: str, mib: int, purpose: str = "", minutes: float = 5, kind: str | None = None, ask_wait_s: float = 600.0,
                 transport: Transport | None = None, base_url: str | None = None, token: str = "", control_secret: str = "") -> None:
        self.owner, self.mib, self.purpose, self.minutes, self.kind, self.ask_wait_s = owner, mib, purpose, minutes, kind or owner, ask_wait_s
        self._call = transport or http_transport(base_url, token, control_secret, timeout=ASK_TIMEOUT_S)
        self._vram = transport or http_transport(base_url, token, control_secret)
        self.lease: VramLease | None = None
        self.swap_id: str | None = None
        self.swap_hold_s = 90.0
        self.reason = self.message = self.message_it = ""
        self._stop = threading.Event()

    @property
    def gpu_ok(self) -> bool:
        return self.lease is not None and self.lease.gpu_ok

    @property
    def swapped(self) -> bool:
        return self.swap_id is not None

    def _ask(self) -> VramLease:
        return VramLease(self.owner, self.mib, self.purpose, transport=self._vram).acquire()

    def acquire(self) -> "HeavyLease":
        lease = self._ask()
        if lease.gpu_ok or lease.reason != "not_enough":
            return self._done(lease)
        try:
            status, out = self._call("POST", BASE + "/begin", {"kind": self.kind, "purpose": self.purpose, "minutes": self.minutes, "wait_s": self.ask_wait_s})
        except (OSError, http.client.HTTPException, ValueError) as e:
            return self._done(lease, "no_engine", f"the server does not answer ({e.__class__.__name__})")
        swap = out.get("swap") if isinstance(out.get("swap"), dict) else None
        if status != 200 or not out.get("ok") or not swap:
            return self._done(lease, str(out.get("reason") or "not_allowed"), str(out.get("message") or ""), str(out.get("message_it") or ""))
        self.swap_id, self.swap_hold_s = swap["id"], float(swap.get("hold_s") or 90)
        threading.Thread(target=self._keep, name="heavy-swap-keepalive", daemon=True).start()
        again = self._ask()
        if again.gpu_ok:
            return self._done(again)
        self._end()
        return self._done(again)

    def _done(self, lease: VramLease, reason: str = "", message: str = "", message_it: str = "") -> "HeavyLease":
        self.lease = lease
        self.reason, self.message, self.message_it = (reason or lease.reason), (message or lease.message), (message_it or lease.message_it)
        return self

    def _keep(self) -> None:
        while not self._stop.wait(max(1.0, self.swap_hold_s / 3)):
            try:
                self._call("POST", f"{BASE}/{self.swap_id}/touch", None)
            except (OSError, http.client.HTTPException, ValueError):
                pass

    def _end(self) -> None:
        self._stop.set()
        swap_id, self.swap_id = self.swap_id, None
        if swap_id is not None:
            try:
                self._call("POST", f"{BASE}/{swap_id}/end", None)
            except (OSError, http.client.HTTPException, ValueError):
                pass                               # the server ends it when the hold passes

    def release(self) -> None:
        if self.lease is not None:
            self.lease.release()                   # the VRAM first: the model wakes into it
        self._end()

    def __enter__(self) -> "HeavyLease":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
