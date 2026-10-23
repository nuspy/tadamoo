"""Payments broker (level 4): the hard cap and the waivers live here, never in a prompt.

The model can only ask for a payment. The broker decides from a signed state file and a hash-chained
ledger that it re-reads on every request, so a revocation made elsewhere (the GUI) takes effect at
once, the running total survives a restart, and any file that fails verification stops all payments.
No connector moves money yet: an authorization is a recorded decision.

Limits chosen here, pending the owner's decision: the amount written in the declaration is the cap
per payment; a second, cumulative cap is also required; a waiver lasts 30 days unless shorter.
Files in the data folder are protected from edits by anything but their owner; an agent running as the
same OS user could still reach them, which only a separate OS user fully prevents.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness.audit import AuditLedger
from harness.waiver.cards import PaymentSystem
from harness.waiver.dialogs import DIALOG_TEXTS
from harness.waiver.dossier import DossierStore
from harness.waiver.keystore import load_or_create_key
from harness.waiver.phrase import normalize, parse_amount, verify_phrase

DAY = 86400.0
MAX_CENTS = 100_000_000_00        # no single request above 100 million units


def to_cents(amount: Any) -> int:
    """An amount as whole cents. Rejects bool, NaN, infinity, zero, negatives and more than two decimals."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float, str, Decimal)):
        raise ValueError(f"the amount must be a number, got {amount!r}")
    if isinstance(amount, float) and not math.isfinite(amount):
        raise ValueError("the amount must be a finite number")
    try:
        value = Decimal(str(amount).strip())
    except InvalidOperation:
        raise ValueError(f"the amount must be a number, got {amount!r}") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("the amount must be positive and finite")
    cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError("the amount cannot have more than two decimals")
    if cents > MAX_CENTS:
        raise ValueError("the amount is unreasonably large")
    return int(cents)


def money(cents: int, currency: str) -> str:
    return f"{Decimal(cents) / 100:.2f} {currency.upper()}"


class PaymentsBroker:
    def __init__(
        self,
        data_dir: str | Path,
        key: Optional[bytes] = None,
        notify: Optional[Callable[[Dict[str, Any]], None]] = None,
        clock: Callable[[], float] = time.time,
        app_root: Optional[Path] = None,
    ) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._key = key or load_or_create_key(self.dir / "device.key")
        self.ledger = AuditLedger(self.dir / "payments.jsonl")
        self.dossiers = DossierStore(self.dir / "dossiers", self._key)
        self.state_path = self.dir / "payments_state.json"
        self.notify = notify
        self.clock = clock
        self.app_root = app_root
        self._lock = threading.RLock()

    # -- signed state ----------------------------------------------------------------------------

    def _sign(self, body: Dict[str, Any]) -> str:
        return hmac.new(self._key, json.dumps(body, sort_keys=True).encode("utf-8"), hashlib.sha256).hexdigest()

    def _load_state(self) -> Dict[str, Any]:
        """The verified state; raises PermissionError when the file was altered (payments then stop)."""
        if not self.state_path.is_file():
            return {"systems": {}, "waivers": []}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            body = {"systems": data["systems"], "waivers": data["waivers"]}
            ok = hmac.compare_digest(str(data.get("sig", "")), self._sign(body))
        except (OSError, ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            raise PermissionError("The payment state failed verification; all payments are stopped")
        return body

    def _save_state(self, body: Dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**body, "sig": self._sign(body)}, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _verified_ledger(self) -> None:
        self.ledger = AuditLedger(self.ledger.path)          # re-read what is on disk
        ok, why = self.ledger.verify_integrity()
        if not ok:
            raise PermissionError(f"The payment ledger failed verification ({why}); all payments are stopped")

    # -- systems and waivers ---------------------------------------------------------------------

    def register_system(self, system: PaymentSystem) -> None:
        problems = system.problems()
        if problems:
            raise ValueError("; ".join(problems))
        with self._lock:
            state = self._load_state()
            state["systems"][system.system_id] = {**system.card(), "currency": system.currency.upper()}
            self._save_state(state)
            self.ledger.record("user", "system_registered", {"system": system.system_id})

    def begin_waiver(self, system_id: str, lang: str = "it") -> Dict[str, Any]:
        """What the dialogs show: the system's card and the three texts."""
        state = self._load_state()
        if system_id not in state["systems"]:
            raise KeyError(f"unknown payment system '{system_id}'")
        return {"card": state["systems"][system_id], "texts": DIALOG_TEXTS[lang], "lang": lang}

    def grant_waiver(
        self,
        system_id: str,
        *,
        user_name: str,
        typed_text: str,
        per_payment: str,
        cumulative: str,
        read_to_end: bool,
        release_ack: bool,
        retyped_names: Dict[str, str],
        pasted: bool = False,
        ttl_days: float = 30.0,
        lang: str = "it",
        typing_intervals_ms: Optional[List[int]] = None,
        screenshots: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            state = self._load_state()
            system = state["systems"].get(system_id)
            if system is None:
                raise KeyError(f"unknown payment system '{system_id}'")
            currency = system["currency"]
            per_value, _ = parse_amount(per_payment)
            cum_value, _ = parse_amount(cumulative)
            per_cents, cum_cents = to_cents(per_value), to_cents(cum_value)
            if cum_cents < per_cents:
                raise ValueError("the cumulative cap cannot be below the cap per payment")
            if not 0 < ttl_days <= 365:
                raise ValueError("the waiver must last between a moment and 365 days")
            if not (read_to_end and release_ack):
                raise ValueError("read both dialogs to the end and accept the release first")
            for dialog in ("release", "acceptance"):
                if normalize(retyped_names.get(dialog, "")) != normalize(user_name):
                    raise ValueError(f"retype your name in the {dialog} dialog")
            phrase_lang, _ = verify_phrase(typed_text, user_name, per_value, currency)

            now = self.clock()
            record = self.dossiers.build(
                card=system, user_name=user_name, phrase_text=typed_text, phrase_lang=phrase_lang, amount_text=per_payment,
                per_payment=money(per_cents, currency), cumulative=money(cum_cents, currency), currency=currency,
                pasted=pasted, dialog_texts=DIALOG_TEXTS[lang], retyped_names=retyped_names,
                typing_intervals_ms=typing_intervals_ms, screenshots=screenshots, app_root=self.app_root,
                now=datetime.datetime.fromtimestamp(now, datetime.timezone.utc),
            )
            ref = self.dossiers.seal(record)

            for w in state["waivers"]:
                if w["system_id"] == system_id and not w.get("revoked_at"):
                    w["revoked_at"], w["revoked_reason"] = now, "superseded"
            waiver = {"waiver_id": f"wv_{secrets.token_hex(6)}", "system_id": system_id, "user": user_name,
                      "currency": currency, "per_payment_cents": per_cents, "cumulative_cents": cum_cents,
                      "granted_at": now, "expires_at": now + ttl_days * DAY, "dossier_id": ref.dossier_id,
                      "revoked_at": None}
            state["waivers"].append(waiver)
            self._save_state(state)
            self.ledger.record("user", "waiver_granted", {
                "waiver_id": waiver["waiver_id"], "system": system_id, "per_payment_cents": per_cents,
                "cumulative_cents": cum_cents, "currency": currency, "dossier_id": ref.dossier_id,
                "dossier_sha256": ref.sha256, "pasted": pasted})
            return dict(waiver)

    def revoke(self, system_id: Optional[str] = None, reason: str = "user") -> int:
        """Ends the active waiver of one system, or of all; payments stop at once. Returns how many ended."""
        with self._lock:
            try:
                state = self._load_state()
            except PermissionError:
                return 0                                   # a damaged state already stops every payment
            now, count = self.clock(), 0
            for w in state["waivers"]:
                if not w.get("revoked_at") and (system_id is None or w["system_id"] == system_id):
                    w["revoked_at"], w["revoked_reason"] = now, reason
                    count += 1
            if count:
                self._save_state(state)
                self.ledger.record("user", "waiver_revoked", {"system": system_id or "*", "count": count, "reason": reason})
            return count

    def _active(self, state: Dict[str, Any], system_id: str) -> Optional[Dict[str, Any]]:
        now = self.clock()
        for w in reversed(state["waivers"]):
            if w["system_id"] == system_id and not w.get("revoked_at") and now < w["expires_at"]:
                return w
        return None

    def _spent_cents(self, waiver_id: str) -> int:
        return sum(r["details"].get("amount_cents", 0) for r in self.ledger.records
                   if r.get("action") == "payment_authorized" and r["details"].get("waiver_id") == waiver_id)

    def status(self) -> List[Dict[str, Any]]:
        """The active waivers, for the permanent banner."""
        state = self._load_state()
        self._verified_ledger()
        out = []
        for system_id, system in state["systems"].items():
            w = self._active(state, system_id)
            if w:
                spent = self._spent_cents(w["waiver_id"])
                cur = w["currency"]
                out.append({"system": system_id, "name": system["name"], "per_payment": money(w["per_payment_cents"], cur),
                            "cumulative": money(w["cumulative_cents"], cur), "spent": money(spent, cur),
                            "remaining": money(w["cumulative_cents"] - spent, cur), "expires_at": w["expires_at"]})
        return out

    # -- the decision ----------------------------------------------------------------------------

    def authorize(
        self,
        system_id: str,
        amount: Any,
        currency: str,
        recipient: str,
        reason: str,
        confirm: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Dict[str, Any]:
        """Decides one payment request. Raises ValueError for a malformed request and PermissionError when
        it is refused; a refused request that `confirm` approves is recorded as confirmed by the user."""
        cents = to_cents(amount)
        if not recipient.strip():
            raise ValueError("a payment needs a recipient")
        with self._lock:
            state = self._load_state()
            self._verified_ledger()
            system = state["systems"].get(system_id)
            if system is None:
                raise PermissionError(f"Unknown payment system '{system_id}'")
            if currency.upper() != system["currency"]:
                raise PermissionError(f"This system pays in {system['currency']}, not {currency.upper()}")
            request = {"system": system_id, "amount": money(cents, system["currency"]), "recipient": recipient,
                       "reason": reason}
            waiver = self._active(state, system_id)
            refusal = "no active waiver for this payment system"
            if waiver:
                spent = self._spent_cents(waiver["waiver_id"])
                if cents > waiver["per_payment_cents"]:
                    refusal = f"over the cap of {money(waiver['per_payment_cents'], system['currency'])} per payment"
                elif spent + cents > waiver["cumulative_cents"]:
                    refusal = f"over the cumulative cap, {money(waiver['cumulative_cents'] - spent, system['currency'])} left"
                else:
                    entry = self.ledger.record("broker", "payment_authorized", {
                        **request, "amount_cents": cents, "waiver_id": waiver["waiver_id"]})
                    result = {**request, "mode": "waiver", "status": "authorized", "ledger_index": entry["index"],
                              "remaining": money(waiver["cumulative_cents"] - spent - cents, system["currency"])}
                    self._notify(result)
                    return result
            if confirm is not None and confirm(request):
                entry = self.ledger.record("broker", "payment_confirmed", {**request, "amount_cents": cents})
                result = {**request, "mode": "confirmed", "status": "authorized", "ledger_index": entry["index"]}
                self._notify(result)
                return result
            self.ledger.record("broker", "payment_refused", {**request, "why": refusal})
            raise PermissionError(f"Payment refused: {refusal}")

    def _notify(self, result: Dict[str, Any]) -> None:
        if self.notify:
            try:
                self.notify(result)
            except Exception:
                pass                                       # telling the user must never undo a recorded decision


def register_payment_tool(registry: Any, broker: PaymentsBroker, permissions: Any = None,
                          confirm: Optional[Callable[[Dict[str, Any]], bool]] = None) -> None:
    """Adds `request_payment`. The broker is the only gate: below a waiver's cap it decides alone, otherwise
    it asks `confirm`, and with no way to ask it refuses."""
    from harness.tools.registry import EffectClass

    def request_payment(system_id: str, amount: str, currency: str, recipient: str, reason: str) -> Dict[str, Any]:
        result = broker.authorize(system_id, amount, currency, recipient, reason, confirm=confirm)
        return {**result, "executed": False,
                "note": "Basalt authorized the amount; no payment connector is configured, so nothing was charged."}

    registry.register(
        "request_payment",
        "Asks to pay an amount through a registered payment system. It is approved only within the user's own limits.",
        {"type": "object", "properties": {
            "system_id": {"type": "string"}, "amount": {"type": "string", "description": "e.g. 12.50"},
            "currency": {"type": "string"}, "recipient": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["system_id", "amount", "currency", "recipient", "reason"]},
        EffectClass.IRREVERSIBLE, request_payment)
    if permissions is not None:
        permissions.add_rule("request_payment", "allow")
