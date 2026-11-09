"""The safety policy of phone calls: who may start one, how the call opens, which numbers are refused, and the audit of every call.

Every call the service places or answers goes through `Policy`. The settings come from the `Config` (set from the `telephony` section of voice.json).

Who started a call (`origin`):
  user     the person, by hand from the GUI (/call): no approval.
  agent    TaDaMoo's agent through its phone tools (/external/call with `X-Call-Origin: agent`): the agent asks the person itself when
           `outgoing_approval` is `manual`; the service records that and trusts the header, which only a holder of the service token can send.
  program  any other caller of /external/*: with `manual` the call waits for the person's approval (`Approvals`), with `auto` it is approved.
A call is never retried, one call runs at a time, a number that reaches an emergency service is refused for every origin, and the first
sentence of a call says the caller is an AI unless the person turned that off. The audit has no transcript and only the last three digits."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .util import is_valid_phone_number, normalize_phone_number

log = logging.getLogger("callbridge.policy")

APPROVAL_MODES = ("manual", "auto")
TRANSPORT_MODES = ("off", "auto", "gsm", "android")
ORIGINS = ("user", "agent", "program")
DISCLOSURE_MARKERS = ("intelligenza artificiale", "assistente virtuale", "artificial intelligence", "virtual assistant")
APPROVAL_TTL_S = 300.0
MAX_PENDING = 10
AUDIT_MAX_BYTES = 2 * 1024 * 1024

# The emergency and public-safety numbers of the countries this code can name (EU, UK, US/CA, AU/NZ, IN, CN/JP/KR, BR, ZA, PK and others).
EMERGENCY = frozenset({
    "112", "113", "114", "115", "117", "118", "1515", "911", "999", "000", "110", "119", "120", "122", "133", "144", "100", "101", "102", "108",
    "15", "17", "18", "190", "191", "192", "193", "194", "195", "196", "198", "091", "092", "060", "061", "997", "998", "995", "1122", "10111",
    "10177", "111", "116117", "155", "158", "150", "156", "166", "112112"})
_STRICT = re.compile(r"^\+?[0-9]{6,15}$")
_CALLEE_DIGITS = re.compile(r"[^0-9]")


class AuditError(OSError):
    """The audit file cannot be written."""


def digits_of(number: str) -> str:
    return _CALLEE_DIGITS.sub("", number or "")


def mask(number: str) -> str:
    """The number as the audit keeps it: its last three digits."""
    d = digits_of(number)
    return "***" + d[-3:] if d else ""


def is_emergency(number: str) -> bool:
    """True for an emergency number, alone or after a country code (+39 112, 0039112, +1 911)."""
    plain = normalize_phone_number(number or "")
    international = plain.startswith("+") or plain.startswith("00")
    d = digits_of(plain)
    if d in EMERGENCY:
        return True
    if plain.startswith("00"):
        d = d[2:]
        if d in EMERGENCY:
            return True
    if international and len(d) <= 9:
        return any(d.endswith(e) and len(d) <= len(e) + 3 for e in EMERGENCY if len(e) >= 3)
    return False


def has_disclosure(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in DISCLOSURE_MARKERS)


def _is_it(lang: str) -> bool:
    return not lang or lang.lower().startswith("it")


def disclosure_sentence(who: str, lang: str = "it") -> str:
    """One sentence that says the caller is an AI and for whom it calls."""
    who = (who or "").strip()
    if _is_it(lang):
        return f"Sono un assistente virtuale basato su intelligenza artificiale e chiamo per conto di {who or 'una persona'}."
    return f"I am a virtual assistant based on artificial intelligence, calling on behalf of {who or 'someone'}."


def outgoing_greeting(who: str, lang: str = "it", purpose: str = "") -> str:
    head = disclosure_sentence(who, lang)
    head = ("Buongiorno. " if _is_it(lang) else "Hello. ") + head
    purpose = (purpose or "").strip()
    return f"{head} {purpose}" if purpose else head


def incoming_greeting(who: str, lang: str = "it") -> str:
    who = (who or "").strip()
    if _is_it(lang):
        return f"Buongiorno, sono un assistente virtuale basato su intelligenza artificiale e rispondo per conto di {who or 'una persona'}. Come posso aiutarla?"
    return f"Hello, I am a virtual assistant based on artificial intelligence, answering on behalf of {who or 'someone'}. How can I help you?"


@dataclass
class Decision:
    ok: bool
    status: int = 200
    code: str = ""
    message: str = ""
    number: str = ""
    greeting: str = ""
    approval: str = ""              # not_needed | manual | auto
    disclosure: str = "on"          # on | off
    approval_id: str = ""

    def body(self) -> dict:
        out = {"error": self.message, "code": self.code}
        if self.approval_id:
            out["approval_id"] = self.approval_id
        return out


# ------------------------------------------------------------------------------------------------ audit
class CallAudit:
    """One JSON line per event in `path`: when, direction, transport, the number's last three digits, who started it, the approval, the
    disclosure, the duration and the outcome. Nothing that was said. The file is renamed to `<name>.1` when it grows past AUDIT_MAX_BYTES."""

    def __init__(self, path, clock=time.time) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self._lock = threading.Lock()

    def write(self, **fields) -> None:
        if self.path is None:
            raise AuditError("no audit file is set")
        row = {"ts": datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(timespec="seconds"), **fields}
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > AUDIT_MAX_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError as e:
            raise AuditError(str(e)) from e

    def tail(self, limit: int = 20) -> list[dict]:
        """The last `limit` rows, oldest first; unreadable lines are skipped."""
        if self.path is None:
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-max(1, int(limit)):]
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


# ------------------------------------------------------------------------------------------------ approvals
def _fingerprint(number: str, greeting: str) -> str:
    return hashlib.sha256(f"{number}\n{greeting}".encode("utf-8")).hexdigest()


@dataclass
class _Request:
    id: str
    number: str
    greeting: str
    fingerprint: str
    created: float
    state: str = "pending"          # pending | approved | denied | used


class Approvals:
    """Calls a program asked for that wait for the person. An approval is for one number and one greeting, is used once and expires."""

    def __init__(self, clock=time.monotonic, ttl_s: float = APPROVAL_TTL_S) -> None:
        self._clock, self._ttl = clock, ttl_s
        self._items: dict[str, _Request] = {}
        self._lock = threading.Lock()

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, r in self._items.items() if now - r.created > self._ttl]:
            del self._items[key]

    def create(self, number: str, greeting: str) -> str | None:
        """The id of a new request, or None when too many wait."""
        with self._lock:
            self._expire()
            if sum(1 for r in self._items.values() if r.state == "pending") >= MAX_PENDING:
                return None
            rid = secrets.token_hex(8)
            self._items[rid] = _Request(rid, number, greeting, _fingerprint(number, greeting), self._clock())
            return rid

    def pending(self) -> list[dict]:
        with self._lock:
            self._expire()
            return [{"id": r.id, "number": r.number, "greeting": r.greeting, "age_s": round(self._clock() - r.created)}
                    for r in self._items.values() if r.state == "pending"]

    def decide(self, rid: str, approve: bool) -> bool:
        with self._lock:
            self._expire()
            r = self._items.get(rid)
            if r is None or r.state != "pending":
                return False
            r.state = "approved" if approve else "denied"
            return True

    def consume(self, rid: str, number: str, greeting: str) -> str:
        """`ok` and the approval is used up; else `unknown`, `pending`, `denied`, `used` or `mismatch`."""
        with self._lock:
            self._expire()
            r = self._items.get(rid)
            if r is None:
                return "unknown"
            if r.state != "approved":
                return r.state
            if r.fingerprint != _fingerprint(number, greeting):
                return "mismatch"
            r.state = "used"
            return "ok"


# ------------------------------------------------------------------------------------------------ the policy
class CallRecord:
    """A call that holds the one slot, until `end`."""

    def __init__(self, policy: "Policy", cid: str, direction: str, origin: str, number: str, approval: str, disclosure: str) -> None:
        self._policy = policy
        self.id, self.direction, self.origin = cid, direction, origin
        self.number, self.approval, self.disclosure = number, approval, disclosure
        self.transport = ""
        self.turns = 0
        self.hangup_requested = False
        self.started = policy.monotonic()
        self.ended = False

    def end(self, outcome: str = "") -> bool:
        """Ends the call once: writes its row and frees the slot. False when it had already ended."""
        return self._policy._finish(self, outcome)


class Policy:
    def __init__(self, cfg, audit: CallAudit | None = None, clock=time.time, monotonic=time.monotonic) -> None:
        self.cfg = cfg
        self.audit = audit or CallAudit(getattr(cfg, "call_audit_file", ""), clock)
        self.approvals = Approvals(monotonic)
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._active: CallRecord | None = None

    # -- settings, read live because the settings file can change
    @property
    def approval_mode(self) -> str:
        return self.cfg.outgoing_approval if self.cfg.outgoing_approval in APPROVAL_MODES else "manual"

    @property
    def disclosure_on(self) -> bool:
        return self.cfg.ai_disclosure_on_start is not False

    @property
    def auto_answer(self) -> bool:
        return self.cfg.incoming_auto_answer is True

    @property
    def who(self) -> str:
        return (self.cfg.call_on_behalf_of or "").strip()

    def telephony_on(self) -> bool:
        return (self.cfg.call_transport or "off").lower() != "off"

    # -- the decision for an outgoing call
    def authorize(self, origin: str, number: str, greeting: str = "", lang: str = "", approval_id: str = "") -> Decision:
        """Whether a call may be placed, with the number cleaned and the greeting it opens with. Nothing is placed or written here, except that
        a call that waits for approval is listed (`Approvals`)."""
        if origin not in ORIGINS:
            return Decision(False, 400, "origin", "unknown origin")
        if not self.telephony_on():
            return Decision(False, 409, "telephony_off", "telephony is off: turn it on in Settings > Voice")
        clean = normalize_phone_number((number or "").strip())
        if not clean:
            return Decision(False, 400, "number_missing", "the number is missing")
        if is_emergency(clean):
            return Decision(False, 403, "emergency_number", "an emergency number is never called by the assistant", number=clean)
        valid = is_valid_phone_number(clean) if origin == "user" else bool(_STRICT.match(clean))
        if not valid:
            return Decision(False, 400, "invalid_number", "the number is not valid", number=clean)

        greeting = (greeting or "").strip()
        on = self.disclosure_on
        if origin == "user":
            if on and not has_disclosure(greeting):
                greeting = f"{disclosure_sentence(self.who, lang)} {greeting}".strip()
            greeting = greeting or ("Pronto?" if _is_it(lang) else "Hello?")
        elif not greeting:
            return Decision(False, 400, "greeting_missing", "the greeting is missing: it is the first sentence of the call and the caller decides it", number=clean)
        elif on and not has_disclosure(greeting):
            return Decision(False, 400, "no_disclosure", "the first sentence must say that the caller is an AI", number=clean)
        disclosure = "on" if on else "off"

        if origin == "user":
            approval = "not_needed"
        elif self.approval_mode == "auto":
            approval = "auto"
        elif origin == "agent":
            approval = "manual"
        else:
            if not approval_id:
                rid = self.approvals.create(clean, greeting)
                if rid is None:
                    return Decision(False, 429, "approval_queue_full", "too many calls wait for approval", number=clean)
                return Decision(False, 202, "approval_required", "the call waits for the person's approval", number=clean, greeting=greeting, approval_id=rid)
            verdict = self.approvals.consume(approval_id, clean, greeting)
            if verdict != "ok":
                return Decision(False, 403, "approval_" + verdict, f"the approval is not valid ({verdict})", number=clean, approval_id=approval_id)
            approval = "manual"
        return Decision(True, number=clean, greeting=greeting, approval=approval, disclosure=disclosure)

    # -- the slot and the audit
    def begin(self, direction: str, origin: str, number: str, approval: str, disclosure: str, transport: str = "") -> CallRecord | None:
        """Takes the one call slot and writes the start row; None when a call is running. Raises AuditError when the row cannot be written:
        a call that cannot be recorded is not made."""
        with self._lock:
            held = self._active
            if held is not None and not held.ended:
                if self.monotonic() - held.started < self._stale_s():
                    return None
                self._finish_locked(held, "abandoned")
            rec = CallRecord(self, secrets.token_hex(6), direction, origin, number, approval, disclosure)
            rec.transport = transport
            self.audit.write(event="start", id=rec.id, direction=direction, transport=transport, number=mask(number), origin=origin,
                             approval=approval, disclosure=disclosure)
            self._active = rec
        if disclosure == "off":
            log.info("call %s: the AI disclosure is off by the person's choice", rec.id)
        return rec

    def _stale_s(self) -> float:
        return max(float(getattr(self.cfg, "gsm_max_call_s", 600.0)), 3600.0) + 120.0

    def active(self) -> CallRecord | None:
        with self._lock:
            return self._active if self._active is not None and not self._active.ended else None

    def refuse(self, direction: str, origin: str, number: str, reason: str, transport: str = "") -> None:
        """Writes the row of a call that was not placed or answered; a failure to write is only logged."""
        try:
            self.audit.write(event="refused", direction=direction, transport=transport, number=mask(number), origin=origin, reason=reason)
        except AuditError as e:
            log.error("audit: %s", e)

    def _finish(self, rec: CallRecord, outcome: str) -> bool:
        with self._lock:
            return self._finish_locked(rec, outcome)

    def _finish_locked(self, rec: CallRecord, outcome: str) -> bool:
        if rec.ended:
            return False
        rec.ended = True
        if not outcome:
            outcome = "hung_up" if rec.hangup_requested else ("completed" if rec.turns else "no_conversation")
        try:
            self.audit.write(event="end", id=rec.id, direction=rec.direction, transport=rec.transport, number=mask(rec.number), origin=rec.origin,
                             approval=rec.approval, disclosure=rec.disclosure, duration_s=round(self.monotonic() - rec.started, 1), outcome=outcome)
        except AuditError as e:
            log.error("audit: %s", e)
        if self._active is rec:
            self._active = None
        return True
