"""The avatar loads when it is asked for, not with the Loader's Load.

    POST /api/voice/avatar/ensure {wait_s?}   this PC, or a phone with its key: 200 {ok: true, state: "loaded", lent_mib, lease}, or
                                              409/502/503 {ok: false, reason, message, message_it}; nothing is left half loaded

What it does, with the avatar set to load on request (the default; `at_load` makes it only load the avatar, with no lease and no idle unload):
the VRAM broker lends the avatar's size (its measured or catalogue size, else 3000 MiB) through the voice functions' one shared lease
(serve/voice_lend.py), only the avatar function is loaded, and while it is used the lease is kept. Used means a phone on the voice
gateway, a viewer of the preview stream, a running voice session with the avatar, or the virtual camera running. After the idle time
(Settings: `idle_unload_s`, 5 minutes by default) the avatar is unloaded and the lease returned, so the model's expert cache refills.
Everything the voice service says is read from its own routes."""
from __future__ import annotations

import json
import threading
import time

from serve import models, voice_lend, voice_loader, voice_service, voice_settings

PATH = "/api/voice/avatar/ensure"
TICK_S = 5.0
WAIT_MAX_S = 120.0
BODY_MAX = 1 << 10
TEXT = {
    "avatar_off": ("The avatar is turned off: turn it on in Models > Voice services.", "L'avatar è spento: accendilo in Modelli > Servizi vocali."),
    "not_installed": ("The avatar model is not installed.", "Il modello dell'avatar non è installato."),
    "voice_service_down": ("The voice service is not running.", "Il servizio vocale non è attivo."),
    "busy": ("The voice functions are being changed or the model is loading: try again in a moment.",
             "Le funzioni vocali si stanno cambiando o il modello si sta caricando: riprova tra poco."),
    "load_failed": ("The avatar did not load.", "L'avatar non si è caricato."),
}


def _fail(status: int, reason: str, message: str | None = None, message_it: str | None = None, **extra) -> tuple[int, dict]:
    en, it = TEXT.get(reason, ("", ""))
    return status, {"ok": False, "reason": reason, "message": message or en, "message_it": message_it or it, **extra}


class OnRequest:
    def __init__(self, svc, lend, clock=time.monotonic, tick_s: float = TICK_S, idle_s=voice_settings.avatar_idle_s) -> None:
        self.svc, self.lend, self.clock, self.tick_s, self.idle_s = svc, lend, clock, tick_s, idle_s
        self._ensure = threading.Lock()
        self._lock = threading.Lock()
        self._leased = False                                         # the avatar holds a share of the lease
        self._last_use = clock()
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- asking for the avatar
    def ensure(self, wait_s: float = 0) -> tuple[int, dict]:
        with self._ensure:
            try:
                return self._ensure_locked(wait_s)
            except models.Refused as e:
                err = e.body.get("error", {}) if isinstance(e.body, dict) else {}
                return _fail(e.status if e.status in (409, 503) else 502, str(err.get("code") or "load_failed"), err.get("message") or None)
            except Exception as e:  # noqa: BLE001 - the caller gets a reason, never a trace
                return _fail(502, "load_failed", f"The avatar did not load ({e.__class__.__name__}).")

    def _ensure_locked(self, wait_s: float) -> tuple[int, dict]:
        live = voice_loader.live_state()
        mine = (live["functions"].get("avatar") or {}).get("state")
        if mine == "loaded":
            self._used()
            if self._leased:
                self._start_watcher()
            return 200, {"ok": True, "state": "loaded", "lent_mib": 0, "lease": self._leased}
        if mine == "loading":
            return _fail(409, "busy")
        if not voice_settings.enabled("avatar"):
            return _fail(409, "avatar_off")
        item = next(i for i in voice_loader.plan(self.svc, None, live, only="avatar", lent=True)["items"] if i["id"] == "avatar")
        if item["action"] == "missing":
            return _fail(409, "not_installed", item.get("reason"))
        lend = voice_settings.load_mode("avatar") == "on_request" and item["memory"] == "vram" and item["device"] != "cpu"
        lease = None
        if lend:
            got = self.lend.acquire({"avatar": item["estimated_mib"] or voice_lend.FALLBACK_MIB}, wait_s=min(max(float(wait_s or 0), 0), WAIT_MAX_S))
            if not got["ok"]:
                return _fail(409, got["reason"], got["message"], got["message_it"], **{k: got[k] for k in ("free_mib", "needed_mib") if k in got})
            lease = got["lease"]
        try:
            job = voice_loader.apply_lent(self.svc, "avatar", wait=True).get("job") or {}
            problem = job.get("error") or ((job.get("report") or {}).get("avatar") or {}).get("error")
            loaded = ((voice_loader.live_state()["functions"].get("avatar") or {}).get("state") == "loaded")
        except BaseException:
            self._give_back(lease)
            raise
        if problem or not loaded:
            self._give_back(lease)
            return _fail(502, "load_failed", f"The avatar did not load: {problem}" if problem else None,
                         f"L'avatar non si è caricato: {problem}" if problem else None)
        with self._lock:
            self._leased = lease is not None
        self._used()
        if voice_settings.load_mode("avatar") == "on_request":
            self._start_watcher()
        return 200, {"ok": True, "state": "loaded", "lent_mib": lease["lent_mib"] if lease else 0, "lease": lease is not None}

    def _give_back(self, lease) -> None:
        if lease:
            self.lend.release("avatar")

    def _used(self) -> None:
        self._last_use = self.clock()

    # ---- keeping it while it is used
    def in_use(self) -> bool:
        """Whether anything is using the avatar now; a service that cannot be asked counts as not using it."""
        def get(route):
            try:
                code, body = voice_service.call("GET", route)
            except voice_service.ServiceDown:
                return {}
            return body if code == 200 and body else {}
        if (get("/gateway").get("clients") or 0) > 0:
            return True
        state = get("/avatar/state")
        outs = state.get("outputs") or {}
        if ((outs.get("preview") or {}).get("viewers") or 0) > 0 or (outs.get("camera") or {}).get("running"):
            return True
        session = get("/session/state")
        return bool(session.get("active") and session.get("avatar"))

    def _start_watcher(self) -> None:
        with self._lock:
            if self._watcher is not None and self._watcher.is_alive():
                return
            self._stop.clear()
            self._watcher = threading.Thread(target=self._watch, name="avatar-on-request", daemon=True)
            self._watcher.start()

    def _watch(self) -> None:
        while not self._stop.wait(self.tick_s):
            if self.step():
                return

    def step(self) -> bool:
        """One look: keep the lease, and unload the avatar once it has been idle long enough. True when the watching is over."""
        try:
            live = voice_loader.live_state()
        except models.Refused:
            self._end_lease()
            return True                                              # the service is gone, and its avatar with it
        if (live["functions"].get("avatar") or {}).get("state") != "loaded":
            self._end_lease()
            return True                                              # unloaded by hand
        with self._lock:
            lease = self._leased
        if lease and not self.lend.touch()["ok"]:
            return self.unload()                                     # the lease is lost: the VRAM may be lent to another job
        if self.in_use():
            self._used()
            return False
        if self.clock() - self._last_use >= self.idle_s():
            return self.unload()
        return False

    def unload(self) -> bool:
        """Unload the avatar, then give the VRAM back. False when the unload failed (the lease stays and the next look tries again)."""
        try:
            voice_loader.unload_lent(self.svc, "avatar", wait=True)
            still = (voice_loader.live_state()["functions"].get("avatar") or {}).get("state") == "loaded"
        except models.Refused as e:
            if e.body.get("error", {}).get("code") == "voice_service_down":
                still = False
            else:
                return False
        if still:
            return False
        self._end_lease()
        return True

    def _end_lease(self) -> None:
        with self._lock:
            lease, self._leased = self._leased, False
        if lease:
            self.lend.release("avatar")

    def shutdown(self) -> None:
        self._stop.set()


def of(svc) -> OnRequest:
    o = getattr(svc, "avatar_on_request", None)
    if o is None:
        o = svc.avatar_on_request = OnRequest(svc, voice_lend.of(svc))
    return o


def post(handler, svc) -> None:
    """The route: this PC, or a phone that came through the listener for phones with its key."""
    if not handler._local_or_device():
        handler._discard_body()
        return handler._json(403, {"error": {"type": "forbidden", "message": "the avatar is asked for from this PC or from a phone with its key"}})
    wait = 0
    try:
        req = json.loads(handler._body(BODY_MAX) or b"{}")
        wait = req.get("wait_s", 0) if isinstance(req, dict) else 0
    except Exception as e:  # noqa: BLE001 - BadBody carries its status; anything else is a bad body
        return handler._json(getattr(e, "status", 400), {"error": {"message": str(e) or "the body is not valid JSON"}})
    if isinstance(wait, bool) or not isinstance(wait, (int, float)):
        return handler._json(400, {"error": {"message": "wait_s is a number of seconds"}})
    status, body = of(svc).ensure(wait)
    handler._json(status, body)
