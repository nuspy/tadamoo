"""Placing, answering and ending calls through the policy (call_policy.py).

`CallService` is what the routes (/call, /hangup, /transport, /external/*) and the GSM and Android incoming paths use. It asks the policy,
takes the one call slot, opens the audit, hands the call to the transport router and wraps the call's brain so that the call is closed in the
audit when the transport says it ended. The transport router is made on first use: the service opens no serial port or phone link until the
person has turned telephony on and a call needs it.

Whatever the other side says is untrusted text. The brain of a call is a fresh direct brain: no tools, no files, no TaDaMoo agent."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .call_policy import AuditError, CallRecord, Policy, incoming_greeting

log = logging.getLogger("callbridge.calls")

CALL_TRANSPORTS = ("gsm", "android")


class GuardedBrain:
    """The brain of one call: the given brain, the call's record, and the opening line when the transport does not speak it itself."""

    def __init__(self, inner, record: CallRecord, opening: str = "") -> None:
        self._inner = inner
        self.record = record
        self.opening = opening

    def respond_stream(self, text: str, source: str = "remote"):
        self.record.turns += 1
        if self.opening:
            line, self.opening = self.opening, ""
            yield line
            return
        yield from self._inner.respond_stream(text, source)

    def call_ended(self) -> None:
        try:
            ended = getattr(self._inner, "call_ended", None)
            if callable(ended):
                ended()
        finally:
            self.record.end()

    def __getattr__(self, name):
        return getattr(self._inner, name)


@dataclass
class IncomingPlan:
    brain: GuardedBrain
    opening: str


class CallService:
    def __init__(self, cfg, bridge, policy: Policy, brain_factory=None, router_factory=None) -> None:
        self.cfg, self.bridge, self.policy = cfg, bridge, policy
        self._brain_factory = brain_factory or self._direct_brain
        self._router_factory = router_factory or self._make_router
        self._router = None
        self._lock = threading.RLock()
        self._watching = False
        if bridge is not None:
            bridge.gate = self

    def _direct_brain(self):
        from .brains import DirectBrain
        return DirectBrain(self.cfg)

    def _make_router(self):
        from .transport import CallRouter
        router = CallRouter(self.cfg, self.bridge, self._brain_factory, gate=self)
        router.setup()
        return router

    def router(self):
        with self._lock:
            if self._router is None:
                self._router = self._router_factory()
            return self._router

    def sync(self, eager: bool = False) -> None:
        """Brings the transports in line with the settings: telephony off closes what is open; incoming auto-answer on (or `eager`) opens
        the modem to listen and watches the phone."""
        try:
            if not self.policy.telephony_on():
                self.close()
                return
            if eager or self.policy.auto_answer:
                self.router()
            if self.policy.auto_answer and self.bridge is not None and (self.cfg.call_transport or "").lower() in ("auto", "android"):
                with self._lock:
                    if not self._watching:
                        self._watching = True
                        self.bridge.start_incoming_watcher()
        except Exception as e:  # noqa: BLE001 - the transport being absent must not stop the service
            log.error("telephony: %s", e)

    def close(self) -> None:
        with self._lock:
            router, self._router = self._router, None
        modem = getattr(router, "_gsm_modem", None)
        if modem is not None:
            try:
                modem.close()
            except Exception as e:  # noqa: BLE001
                log.warning("telephony: closing the modem: %s", e)

    # -- outgoing
    def place(self, origin: str, number: str, *, greeting: str = "", voice=None, lang=None, transport=None, approval_id: str = "",
              brain_for=None) -> tuple[int, dict]:
        """(HTTP status, body). Never retries: whatever happens, one placement is one attempt. `brain_for(decision)` makes the brain of the call
        once the policy has agreed (a program-driven call); without it the call talks to a fresh direct brain."""
        if transport not in (None, "", *CALL_TRANSPORTS):
            return 400, {"error": "the transport is gsm or android", "code": "transport"}
        decision = self.policy.authorize(origin, number, greeting, lang or self.cfg.default_lang, approval_id)
        if not decision.ok:
            if decision.status != 202:
                self.policy.refuse("outgoing", origin, number, decision.code, transport or "")
            return decision.status, decision.body()
        try:
            rec = self.policy.begin("outgoing", origin, decision.number, decision.approval, decision.disclosure, transport or "")
        except AuditError as e:
            log.error("telephony: %s", e)
            return 500, {"error": "the call cannot be recorded, so it is not placed", "code": "audit_unavailable"}
        if rec is None:
            self.policy.refuse("outgoing", origin, decision.number, "busy", transport or "")
            return 409, {"error": "a call is already running", "code": "busy"}
        inner = brain_for(decision) if brain_for is not None else self._brain_factory()
        guard = GuardedBrain(inner, rec, decision.greeting if brain_for is None else "")
        try:
            result = self.router().place_call(decision.number, voice=voice, lang=lang, greeting=decision.greeting, force=transport or None, brain=guard)
        except Exception as e:  # noqa: BLE001
            log.error("telephony: the call was not placed: %s", e)
            rec.end("error")
            return 500, {"error": "the call was not placed", "code": "place_failed"}
        if result.get("status") != "calling":
            rec.end("failed")
            return 502, result
        rec.transport = result.get("transport") or ""
        if rec.transport == "gsm":
            guard.opening = ""                          # the GSM session says the greeting itself
        return 200, result

    def hangup(self) -> dict:
        rec = self.policy.active()
        if rec is not None:
            rec.hangup_requested = True
        with self._lock:
            router = self._router
        return router.hangup() if router is not None else {"status": "ok"}

    # -- incoming
    def incoming(self, number: str | None, transport: str, explicit: bool = False) -> IncomingPlan | None:
        """Whether the AI answers a call that rings, and with what opening. None leaves it ringing (and says why in the audit).
        `explicit`: the person asked to answer this call, so auto-answer being off does not matter."""
        p = self.policy
        number = number or ""
        if not p.telephony_on():
            return None
        if not p.auto_answer and not explicit:
            p.refuse("incoming", "caller", number, "auto_answer_off", transport)
            return None
        disclosure = "on" if p.disclosure_on else "off"
        try:
            rec = p.begin("incoming", "caller", number, "explicit" if explicit else "auto_answer", disclosure, transport)
        except AuditError as e:
            log.error("telephony: %s", e)
            return None
        if rec is None:
            p.refuse("incoming", "caller", number, "busy", transport)
            return None
        opening = incoming_greeting(p.who, self.cfg.default_lang) if p.disclosure_on else "Pronto?"
        guard = GuardedBrain(self._brain_factory(), rec, opening if transport == "android" else "")
        return IncomingPlan(guard, opening)

    # -- state
    def transport_status(self, refresh: bool = False) -> dict:
        if not self.policy.telephony_on():
            return {"selected": None, "mode": "off"}
        router = self.router()
        if refresh:
            router.refresh()
        return router.status()

    def summary(self) -> dict:
        """What /status says about telephony. Reads settings only: no device is touched."""
        p = self.policy
        return {"transport": (self.cfg.call_transport or "off").lower(), "outgoing_approval": p.approval_mode, "ai_disclosure_on_start": p.disclosure_on,
                "incoming_auto_answer": p.auto_answer, "busy": p.active() is not None}
