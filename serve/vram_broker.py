"""The VRAM broker: components that need VRAM only now and then borrow it from the engine's expert cache, and give it back.

The engine can shrink its expert cache so that N MiB stay free (`POST /v1/vram`, needs --vram-elastic). The broker puts a lease around that:
one holder at a time, the others wait their turn or are refused; the reserve is asked for the borrower's size on top of the reserve in force,
the free VRAM is read afterwards (the engine's word is not enough), and the reserve goes back when the lease ends, is not touched for its
hold time, or the server stops. The broker never raises into a caller: it answers with a result.

    acquire(owner, mib, purpose, hold_s, wait_s)  {"ok": True, "lease": {...}} or {"ok": False, "reason": <code>, "message", "message_it"}
    touch(id), release(id), state()

Refusal codes: no_engine (nothing here holds VRAM), unavailable (the engine cannot lend: the caller uses the CPU), busy (another lease, or a
request the engine is still running), not_enough (even with the whole cache lent), engine_died, disabled, bad_request, not_found."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MIB = 1 << 20
HOLD_DEFAULT_S = 60
HOLD_RANGE_S = (5, 3600)
WAIT_MAX_S = 3600
MIB_MAX = 1 << 20
RESTORE_TRIES = 120                                # attempts, one per tick, to put the reserve back
TICK_S = 1.0                                       # how often the timer looks for expired leases
MESSAGES = {
    "no_engine": ("No program holds VRAM here.", "Nessun programma tiene VRAM qui."),
    "unavailable": ("The model cannot lend VRAM (start the engine with --vram-elastic); use the CPU.",
                    "Il modello non può prestare VRAM (avvia il motore con --vram-elastic): usa la CPU."),
    "busy": ("Another job holds the VRAM; try again when it has finished.", "Un altro lavoro tiene la VRAM: riprova quando ha finito."),
    "not_enough": ("There is not enough VRAM even with the whole expert cache lent.",
                   "La VRAM non basta nemmeno prestando tutta la cache degli esperti."),
    "engine_died": ("The model's engine ended while the VRAM was asked for.", "Il motore del modello si è fermato mentre si chiedeva la VRAM."),
    "disabled": ("The VRAM broker is turned off.", "Il gestore della VRAM è spento."),
    "bad_request": ("Send owner (text) and mib (a whole number above 0).", "Manda owner (testo) e mib (un intero maggiore di 0)."),
    "not_found": ("No such lease: it ended or expired.", "Nessun prestito con questo id: è finito o scaduto."),
}


def refusal(reason: str, **extra) -> dict:
    en, it = MESSAGES[reason]
    return {"ok": False, "reason": reason, "message": en, "message_it": it, **extra}


# ---------------------------------------------------------------------------------------- the persisted block
class BrokerPrefs:
    """{"enabled": true, "hold_s": 60} in one small file of the user data folder."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None

    def read(self) -> dict:
        data = {}
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        data = data if isinstance(data, dict) else {}
        hold = data.get("hold_s")
        ok = isinstance(hold, int) and not isinstance(hold, bool) and HOLD_RANGE_S[0] <= hold <= HOLD_RANGE_S[1]
        return {"enabled": data.get("enabled") is not False, "hold_s": hold if ok else HOLD_DEFAULT_S}

    def write(self, changes: dict) -> dict:
        """Saves the values given and keeps the others; ValueError names a wrong one."""
        now = self.read()
        if "enabled" in changes:
            if not isinstance(changes["enabled"], bool):
                raise ValueError("enabled is true or false")
            now["enabled"] = changes["enabled"]
        if "hold_s" in changes:
            v = changes["hold_s"]
            if isinstance(v, bool) or not isinstance(v, int) or not HOLD_RANGE_S[0] <= v <= HOLD_RANGE_S[1]:
                raise ValueError(f"hold_s is a whole number of seconds from {HOLD_RANGE_S[0]} to {HOLD_RANGE_S[1]}")
            now["hold_s"] = v
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(now, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        return now


# ---------------------------------------------------------------------------------------- the lease
@dataclass
class Lease:
    id: str
    owner: str
    mib: int
    purpose: str
    hold_s: int
    granted_mib: int
    lent_mib: int                                  # what the engine's reserve grew by (0: the VRAM was free already)
    expires_at: float
    started_at: float
    reserve_mib: int | None = None
    reply: dict = field(default_factory=dict)

    def public(self, now: float) -> dict:
        return {"id": self.id, "owner": self.owner, "mib": self.mib, "purpose": self.purpose, "hold_s": self.hold_s,
                "granted_mib": self.granted_mib, "lent_mib": self.lent_mib, "reserve_mib": self.reserve_mib,
                "expires_at": self.expires_at, "expires_in_s": max(0.0, round(self.expires_at - now, 1))}


def _engine_says_unavailable(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ("elastic", "cannot resize", "second gpu", "not segmented"))


def _engine_says_not_enough(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ("above", "exceed", "largest", "maximum", "cannot free", "not enough"))


class VramBroker:
    """`service` is the server's ModelService (its `vram(reserve_mib)`, `loaded()` and `vram_reserve`); `probe()` gives the free VRAM in MiB
    (None: unreadable); `start_reserve()` the reserve the engine started with; `clock` is monotonic seconds."""

    def __init__(self, service=None, probe=None, clock=time.monotonic, start_reserve=None, prefs: BrokerPrefs | None = None,
                 timer: bool = True) -> None:
        self.service, self.clock, self.prefs = service, clock, prefs or BrokerPrefs()
        self._probe = probe or self._default_probe
        self._start_reserve = start_reserve or self._default_start_reserve
        self._timer_on = timer
        self._cv = threading.Condition()
        self._lease: Lease | None = None
        self._queue: list[object] = []
        self._op = False                           # the engine is being asked for something: nobody else acts
        self._changed = False                      # the engine's reserve is not the one it had before the first lease
        self._saved: int | None = None             # that reserve (None: the start's)
        self._last_reply: dict = {}
        self._retries = 0
        self._timer: threading.Thread | None = None
        self._closed = False

    # ---- what is read from outside
    def _default_probe(self) -> int | None:
        try:
            from serve import loader
            free = loader.system_now(int(getattr(self.service, "gpu_index", 0) or 0)).get("vram_free")
            return None if free is None else int(free) // MIB
        except Exception:  # noqa: BLE001 - an unreadable GPU is None
            return None

    def _default_start_reserve(self) -> int | None:
        try:
            from serve import loader, loader_api
            return int(loader.effective(loader_api.file_values(self.service))["engine.vram-reserve-mib"])
        except Exception:  # noqa: BLE001 - no config: the base is unknown
            return None

    def _free(self) -> int | None:
        try:
            v = self._probe()
        except Exception:  # noqa: BLE001
            return None
        return None if v is None else int(v)

    def _loaded(self) -> bool:
        try:
            return bool(self.service.loaded())
        except Exception:  # noqa: BLE001
            return False

    # ---- acquiring
    def acquire(self, owner, mib, purpose: str = "", hold_s=None, wait_s=0) -> dict:
        try:
            return self._acquire(owner, mib, purpose, hold_s, wait_s)
        except Exception as e:  # noqa: BLE001 - the caller gets a result, never an exception
            return refusal("unavailable", detail=f"{e.__class__.__name__}: {e}")

    def _acquire(self, owner, mib, purpose, hold_s, wait_s) -> dict:
        if not isinstance(owner, str) or not owner.strip() or isinstance(mib, bool) or not isinstance(mib, int) or not 0 < mib <= MIB_MAX:
            return refusal("bad_request")
        cfg = self.prefs.read()
        if not cfg["enabled"]:
            return refusal("disabled")
        hold = cfg["hold_s"] if hold_s is None else hold_s
        if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not HOLD_RANGE_S[0] <= hold <= HOLD_RANGE_S[1]:
            return refusal("bad_request", detail=f"hold_s: {HOLD_RANGE_S[0]} to {HOLD_RANGE_S[1]} seconds")
        if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or not 0 <= wait_s <= WAIT_MAX_S:
            return refusal("bad_request", detail=f"wait_s: 0 to {WAIT_MAX_S} seconds")
        if self.service is None:
            return refusal("no_engine")
        ticket, deadline = object(), self.clock() + wait_s
        with self._cv:
            self._queue.append(ticket)
        taken = False
        try:
            while True:
                self.reap()
                with self._cv:
                    if self._closed:
                        return refusal("unavailable")
                    if self._queue[0] is ticket and self._lease is None and not self._op:
                        self._queue.pop(0)
                        self._op = taken = True
                        break
                    left = deadline - self.clock()
                    if left <= 0:
                        return refusal("busy", holder=self._lease.owner if self._lease else None)
                    self._cv.wait(min(left, 0.25))
        finally:
            if not taken:
                with self._cv:
                    if ticket in self._queue:
                        self._queue.remove(ticket)
                    self._cv.notify_all()
        try:
            out = self._grant(owner.strip(), mib, str(purpose or "")[:120], int(hold))
        finally:
            with self._cv:
                self._op = False
                self._cv.notify_all()
        if not out["ok"]:
            self._restore_if_idle()
        else:
            self._start_timer()
        return out

    def _base_reserve(self) -> int | None:
        saved = self._saved if self._changed else getattr(self.service, "vram_reserve", None)
        return saved if saved is not None else self._start_reserve()

    def _grant(self, owner, mib, purpose, hold) -> dict:
        free = self._free()
        base = self._base_reserve()
        loaded = self._loaded()
        if loaded and getattr(self.service, "loading", False):
            return refusal("busy", detail="the model is loading")
        if not loaded or (free is not None and base is not None and free >= mib + base):
            if free is None:
                return refusal("unavailable", detail="the free VRAM cannot be read")
            if free < mib:
                return refusal("not_enough", free_mib=free, needed_mib=mib)
            return self._make(owner, mib, purpose, hold, mib, 0, None, {})
        if not hasattr(self.service, "vram"):
            return refusal("no_engine")
        if base is None:
            return refusal("unavailable", detail="the reserve the engine started with is not known")
        target = base + mib
        first = not self._changed
        saved = getattr(self.service, "vram_reserve", None)
        try:
            reply = self.service.vram(target)
        except Exception as e:  # noqa: BLE001 - each kind of failure is a code
            return self._engine_error(e)
        if first:
            self._changed, self._saved = True, saved
        reply = reply if isinstance(reply, dict) else {}
        self._last_reply = reply
        now_free = self._free()
        if now_free is None and isinstance(reply.get("vram_free_mib"), int):
            now_free = reply["vram_free_mib"]
        if now_free is not None and now_free < mib:
            return refusal("not_enough", free_mib=now_free, needed_mib=mib, reply=reply)
        return self._make(owner, mib, purpose, hold, mib, target - base, target, reply)

    def _engine_error(self, e: Exception) -> dict:
        name, text = e.__class__.__name__, str(e)
        if name == "EngineDied":
            self._changed = False
            return refusal("engine_died", detail=text)
        if name == "ModelBusy":
            return refusal("busy", detail=text)
        if _engine_says_unavailable(text):
            return refusal("unavailable", detail=text)
        if _engine_says_not_enough(text):
            return refusal("not_enough", detail=text)
        return refusal("busy", detail=text)

    def _make(self, owner, mib, purpose, hold, granted, lent, reserve, reply) -> dict:
        now = self.clock()
        lease = Lease(uuid.uuid4().hex[:16], owner, mib, purpose, hold, granted, lent, now + hold, now, reserve, reply)
        with self._cv:
            self._lease = lease
        return {"ok": True, "lease": lease.public(now), "free_mib": self._free()}

    # ---- keeping and giving back
    def touch(self, lease_id) -> dict:
        with self._cv:
            lease = self._lease
            if lease is None or lease.id != lease_id or lease.expires_at <= self.clock():
                return refusal("not_found")
            lease.expires_at = self.clock() + lease.hold_s
            return {"ok": True, "lease": lease.public(self.clock())}

    def release(self, lease_id) -> dict:
        with self._cv:
            lease = self._lease
            if lease is None or lease.id != lease_id:
                return refusal("not_found")
            self._lease = None
            self._op = True
            self._cv.notify_all()
        self._after_end()
        return {"ok": True, "released": lease_id}

    def reap(self) -> None:
        """Ends a lease whose hold time passed without a touch."""
        with self._cv:
            lease = self._lease
            if lease is None or lease.expires_at > self.clock() or self._op:
                return
            self._lease = None
            self._op = True
        self._after_end()

    def _after_end(self) -> None:
        """The lease is gone and `_op` is held: put the reserve back unless someone waits for the VRAM."""
        try:
            self._restore_locked_op()
        finally:
            with self._cv:
                self._op = False
                self._cv.notify_all()

    def _restore_if_idle(self) -> None:
        with self._cv:
            if self._lease is not None or self._op or self._queue or not self._changed:
                return
            self._op = True
        self._after_end()

    def _restore_locked_op(self) -> None:
        with self._cv:
            if (self._queue and not self._closed) or not self._changed:
                return
            saved = self._saved
        svc = self.service
        try:
            svc.vram(saved)
        except Exception as e:  # noqa: BLE001
            name = e.__class__.__name__
            if name == "EngineDied":
                try:
                    svc.vram_reserve = saved              # the next engine starts with the reserve it had
                except Exception:  # noqa: BLE001
                    pass
            elif not _engine_says_unavailable(str(e)) and self._retries < RESTORE_TRIES and not self._closed:
                self._retries += 1                        # a request is still running: the timer tries again
                return
        with self._cv:
            self._changed = False
            self._retries = 0

    # ---- the timer
    def _start_timer(self) -> None:
        if not self._timer_on or (self._timer is not None and self._timer.is_alive()):
            return
        self._timer = threading.Thread(target=self._tick, name="vram-broker", daemon=True)
        self._timer.start()

    def _tick(self) -> None:
        while True:
            with self._cv:
                if self._closed or (self._lease is None and not self._changed and not self._op):
                    return
                self._cv.wait(TICK_S)
            self.reap()
            self._restore_if_idle()

    def shutdown(self) -> None:
        """Best effort at the server's end: the lease ends and the reserve goes back."""
        with self._cv:
            self._lease = None
            self._closed = True
            self._cv.notify_all()
            if self._op or not self._changed:
                return
            self._op = True
            self._queue.clear()
        self._after_end()

    # ---- what is shown
    def state(self) -> dict:
        self.reap()
        now = self.clock()
        cfg = self.prefs.read()
        with self._cv:
            lease = self._lease
            out = {"enabled": cfg["enabled"], "hold_s": cfg["hold_s"], "leases": [lease.public(now)] if lease else [],
                   "queue": len(self._queue), "lent_mib": lease.lent_mib if lease else 0,
                   "reserve_mib": getattr(self.service, "vram_reserve", None), "changed": self._changed, "last_reply": dict(self._last_reply)}
        loaded = self._loaded() if self.service is not None else False
        out["engine"] = "none" if self.service is None else "running" if loaded else "not_loaded"
        out["free_mib"] = self._free()
        return out


LEASE_ID = re.compile(r"[0-9a-f]{16}")
