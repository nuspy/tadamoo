"""VRAM for a heavy job, with the local model put to sleep when lending is not enough and the person allows it. Standard library only.

    with HeavyLease("video", 24000, "a clip for the newscast", minutes=5) as lease:
        if lease.gpu_ok:
            ...                                # lease.swapped says whether the conversations run on a provider meanwhile
        else:
            ...                                # lease.reason: not_enough; lease.swap_reason: why the model could not sleep (not_allowed, no_provider, timeout, ...)

It asks the server for the VRAM first (tadamoo/vram_lease.py). Only when the answer is `not_enough` does it ask the local model to step aside
(`/api/heavy/begin`: the person answers on a card, or has allowed it for this kind of job), then asks for the VRAM again. `release` gives the VRAM back and
then lets the model come back. A job that runs real GPU kernels beside the loaded engine (`exclusive`) goes the other way round: the model steps aside
first, because an engine that lent its cache to such a job can fail on its next request; only when the person does not allow that does it borrow anyway,
and `risky` says so. Nothing here raises for a missing server."""
from __future__ import annotations

import http.client
import importlib.util
import threading
from pathlib import Path

try:
    from tadamoo.vram_lease import Transport, VramLease, http_transport
except ImportError:                                # a process that does not have the package reads the sibling file by its path
    _spec = importlib.util.spec_from_file_location("tadamoo_vram_lease", Path(__file__).with_name("vram_lease.py"))
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    Transport, VramLease, http_transport = _module.Transport, _module.VramLease, _module.http_transport

BASE = "/api/heavy"
ASK_TIMEOUT_S = 700.0                              # the person may take their time answering the card


class HeavyLease:
    def __init__(self, owner: str, mib: int, purpose: str = "", minutes: float = 5, kind: str | None = None, ask_wait_s: float = 600.0, wait_s: float = 0,
                 exclusive: bool = False, transport: Transport | None = None, base_url: str | None = None, token: str = "", control_secret: str = "") -> None:
        self.owner, self.mib, self.purpose, self.minutes, self.kind, self.ask_wait_s = owner, mib, purpose, minutes, kind or owner, ask_wait_s
        self.wait_s, self.exclusive, self.risky = wait_s, exclusive, False
        self._call = transport or http_transport(base_url, token, control_secret, timeout=ASK_TIMEOUT_S)
        self._vram = transport or http_transport(base_url, token, control_secret, timeout=wait_s + 135.0)
        self.lease: VramLease | None = None
        self.swap_id: str | None = None
        self.swap_hold_s = 90.0
        self.reason = self.message = self.message_it = self.swap_reason = ""
        self._stop = threading.Event()

    @property
    def gpu_ok(self) -> bool:
        return self.lease is not None and self.lease.gpu_ok

    @property
    def swapped(self) -> bool:
        return self.swap_id is not None

    def _ask(self) -> VramLease:
        return VramLease(self.owner, self.mib, self.purpose, wait_s=self.wait_s, transport=self._vram).acquire()

    def _begin(self) -> tuple:
        """(the swap, "") when the model stepped aside, else (None, the refusal's reason and the server's words)."""
        try:
            status, out = self._call("POST", BASE + "/begin", {"kind": self.kind, "purpose": self.purpose, "minutes": self.minutes, "wait_s": self.ask_wait_s})
        except (OSError, http.client.HTTPException, ValueError) as e:
            return None, ("no_engine", f"the server does not answer ({e.__class__.__name__})", "")
        swap = out.get("swap") if isinstance(out.get("swap"), dict) else None
        if status != 200 or not out.get("ok") or not swap:
            return None, (str(out.get("reason") or "not_allowed"), str(out.get("message") or ""), str(out.get("message_it") or ""))
        self.swap_id, self.swap_hold_s = swap["id"], float(swap.get("hold_s") or 90)
        threading.Thread(target=self._keep, name="heavy-swap-keepalive", daemon=True).start()
        return swap, ("", "", "")

    def acquire(self) -> "HeavyLease":
        if self.exclusive:
            return self._acquire_exclusive()
        lease = self._ask()
        if lease.gpu_ok or lease.reason != "not_enough":
            return self._done(lease)
        swap, (why, en, it) = self._begin()
        if swap is None:
            self.swap_reason = why                                                     # the reason stays `not_enough`: that is what the job lacks
            return self._done(lease, "", f"{lease.message} The local model could not step aside: {en or why}.".strip(),
                              f"{lease.message_it} Il modello locale non si è potuto fare da parte: {it or why}.".strip())
        again = self._ask()
        if not again.gpu_ok:
            self._end()
        return self._done(again)

    def _acquire_exclusive(self) -> "HeavyLease":
        swap, (why, en, it) = self._begin()
        if swap is not None:
            lease = self._ask()
            if not lease.gpu_ok:
                self._end()
            return self._done(lease)
        lease = self._ask()                                                            # nothing local to step aside, or the person did not allow it
        if why != "no_local_engine":
            self.swap_reason = why
            if lease.gpu_ok:
                self.risky = True
                lease.message = f"The VRAM was borrowed without the model stepping aside ({en or why}): the model may stop and restart after this job."
                lease.message_it = f"La VRAM è stata presa senza che il modello si facesse da parte ({it or why}): il modello potrebbe fermarsi e ripartire dopo il lavoro."
        return self._done(lease)

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
