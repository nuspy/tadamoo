"""Payments broker (level 4): the hard cap and the waivers live here, never in a prompt.

The model can only ask for a payment. The broker decides from a signed state file and a hash-chained
ledger that it re-reads on every request, so a revocation made elsewhere (the GUI) takes effect at
once, the running total survives a restart, and any file that fails verification stops all payments.
No connector moves money: an authorization is a recorded decision, and what lets a payment happen is the
card the vault then releases to the site that was named, for a few uses, while that decision still stands.

The amount written in the declaration is the cap per payment; a second, cumulative cap is also required;
a waiver lasts 30 days unless shorter, and may name the only sites it covers. The caps are the person's
own numbers, to any amount. The amount is checked here, when the payment is asked for; the form that
charges the card is the merchant's, so the card's own limit is what bounds what that form can take.
Files in the data folder are protected from edits by anything but their owner; an agent running as the
same OS user could still reach them, which only a separate OS user fully prevents.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from harness.audit import AuditLedger
from harness.waiver.cards import PaymentSystem
from harness.waiver.dialogs import DIALOG_TEXTS
from harness.waiver.dossier import DossierStore
from harness.waiver.keystore import load_or_create_key
from harness.waiver.phrase import normalize, parse_amount, verify_phrase

DAY = 86400.0
DIGITS = 60


@contextlib.contextmanager
def _file_lock(path: Path):
    """One writer at a time across processes: the GUI's server and the conversations' own processes all write the same state and ledger."""
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


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
    if value.adjusted() >= DIGITS:
        raise ValueError(f"the amount has more than {DIGITS} digits")          # only so that a number cannot be made to fill the memory
    with localcontext() as ctx:
        ctx.prec = 2 * DIGITS                                                  # the amount is the person's: no digit is rounded off
        cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError("the amount cannot have more than two decimals")
    return int(cents)


def site_host(site: Any) -> str:
    """The host of a site written as an address or as a bare name; '' when it names none."""
    text = str(site or "").strip().lower()
    if not text:
        return ""
    host = urlparse(text if "://" in text else "//" + text).hostname or ""
    return host.removeprefix("www.") if host.count(".") > 1 else host


def clean_sites(sites: Any) -> List[str]:
    out: List[str] = []
    for s in sites:
        host = site_host(s)
        if str(s).strip() and not host:
            raise ValueError(f"'{s}' is not a site")
        if host and host not in out:
            out.append(host)
    return out


def site_ok(site: str, allowed: List[str]) -> bool:
    """Whether `site` is one of `allowed` or a part of one (a shop's checkout is on a subdomain of its site)."""
    host = site_host(site)
    return bool(host) and any(host == a or host.endswith("." + a) for a in allowed)


def money(cents: int, currency: str) -> str:
    with localcontext() as ctx:
        ctx.prec = 100                                   # a limit is the person's to set, however large: every digit is kept
        return f"{Decimal(cents) / 100:.2f} {currency.upper()}"


class PaymentsBroker:
    def __init__(
        self,
        data_dir: str | Path,
        key: Optional[bytes] = None,
        notify: Optional[Callable[[Dict[str, Any]], None]] = None,
        clock: Callable[[], float] = time.time,
        app_root: Optional[Path] = None,
        network_time: Optional[Callable[[], Optional[float]]] = None,
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
        self.network_time = network_time                  # how far the clock is from a network time server, for the dossier; none in tests
        self._rlock = threading.RLock()
        self._lock_path = self.dir / "payments.lock"

    @contextlib.contextmanager
    def _lock(self):
        """This process's lock and the file lock, and the ledger read again from disk, which another process may have extended."""
        with self._rlock, _file_lock(self._lock_path):
            self.ledger = AuditLedger(self.ledger.path)
            yield

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
        with self._lock():
            state = self._load_state()
            card = {**system.card(), "currency": system.currency.upper()}
            before = state["systems"].get(system.system_id)
            changed = before is not None and any(before.get(k) != card.get(k) for k in ("currency", "credential", "vault_entry"))
            if changed:                                       # a waiver was granted for another currency, credential or card
                now = self.clock()
                for w in state["waivers"]:
                    if w["system_id"] == system.system_id and not w.get("revoked_at"):
                        w["revoked_at"], w["revoked_reason"] = now, "system changed"
            state["systems"][system.system_id] = card
            self._save_state(state)
            self.ledger.record("user", "system_registered", {"system": system.system_id})
            if changed:
                self.ledger.record("user", "waiver_revoked", {"system": system.system_id, "count": 0, "reason": "system changed"})

    def system_card(self, system_id: str) -> Dict[str, Any]:
        state = self._load_state()
        if system_id not in state["systems"]:
            raise KeyError(f"unknown payment system '{system_id}'")
        return dict(state["systems"][system_id])

    def systems(self) -> List[Dict[str, Any]]:
        return [dict(card, id=sid) for sid, card in sorted(self._load_state()["systems"].items())]

    def remove_system(self, system_id: str) -> bool:
        """Forgets a system; its waivers end with it."""
        with self._lock():
            state = self._load_state()
            if system_id not in state["systems"]:
                return False
            now = self.clock()
            for w in state["waivers"]:
                if w["system_id"] == system_id and not w.get("revoked_at"):
                    w["revoked_at"], w["revoked_reason"] = now, "system removed"
            del state["systems"][system_id]
            self._save_state(state)
            self.ledger.record("user", "system_removed", {"system": system_id})
            return True

    def recent(self, count: int = 50) -> List[Dict[str, Any]]:
        """The last entries of the ledger, for the person to read: what was authorized, refused or granted."""
        self._verified_ledger()
        keep = ("payment_authorized", "payment_confirmed", "payment_refused", "waiver_granted", "waiver_revoked", "system_registered", "system_removed")
        rows = [r for r in self.ledger.records if r.get("action") in keep]
        return [{"index": r["index"], "at": r["timestamp"], "action": r["action"], "details": {k: v for k, v in r["details"].items() if k != "dossier_sha256"}}
                for r in rows[-count:]][::-1]

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
        sites: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        with self._lock():
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
            allowed_sites = clean_sites(sites or [])

            now = self.clock()
            record = self.dossiers.build(
                card=system, user_name=user_name, phrase_text=typed_text, phrase_lang=phrase_lang, amount_text=per_payment,
                per_payment=money(per_cents, currency), cumulative=money(cum_cents, currency), currency=currency,
                pasted=pasted, dialog_texts=DIALOG_TEXTS[lang], retyped_names=retyped_names,
                typing_intervals_ms=typing_intervals_ms, screenshots=screenshots, app_root=self.app_root,
                network_offset_s=self.network_time() if self.network_time else None,
                now=datetime.datetime.fromtimestamp(now, datetime.timezone.utc),
            )
            ref = self.dossiers.seal(record)

            for w in state["waivers"]:
                if w["system_id"] == system_id and not w.get("revoked_at"):
                    w["revoked_at"], w["revoked_reason"] = now, "superseded"
            waiver = {"waiver_id": f"wv_{secrets.token_hex(6)}", "system_id": system_id, "user": user_name,
                      "currency": currency, "per_payment_cents": per_cents, "cumulative_cents": cum_cents,
                      "granted_at": now, "expires_at": now + ttl_days * DAY, "dossier_id": ref.dossier_id,
                      "revoked_at": None, "sites": allowed_sites}
            state["waivers"].append(waiver)
            self._save_state(state)
            self.ledger.record("user", "waiver_granted", {
                "waiver_id": waiver["waiver_id"], "system": system_id, "per_payment_cents": per_cents,
                "cumulative_cents": cum_cents, "currency": currency, "dossier_id": ref.dossier_id,
                "dossier_sha256": ref.sha256, "pasted": pasted, "sites": allowed_sites})
            return dict(waiver)

    def revoke(self, system_id: Optional[str] = None, reason: str = "user") -> int:
        """Ends the active waiver of one system, or of all; payments stop at once. Returns how many ended."""
        with self._lock():
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
            # recorded even when no waiver was active: a payment the person confirmed themselves is stopped by this too
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
                            "remaining": money(w["cumulative_cents"] - spent, cur), "expires_at": w["expires_at"],
                            "sites": list(w.get("sites") or [])})
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
        site: str = "",
    ) -> Dict[str, Any]:
        """Decides one payment request. Raises ValueError for a malformed request and PermissionError when
        it is refused; a refused request that `confirm` approves is recorded as confirmed by the user.
        `site` is the website that will take the payment; a waiver that names its sites covers only those.
        The question to the person is asked with no lock held, so that the kill switch works while it is open."""
        cents = to_cents(amount)
        if not recipient.strip():
            raise ValueError("a payment needs a recipient")
        host = site_host(site)
        with self._lock():
            state = self._load_state()
            self._verified_ledger()
            system = state["systems"].get(system_id)
            if system is None:
                raise PermissionError(f"Unknown payment system '{system_id}'")
            if currency.upper() != system["currency"]:
                raise PermissionError(f"This system pays in {system['currency']}, not {currency.upper()}")
            request = {"system": system_id, "amount": money(cents, system["currency"]), "recipient": recipient,
                       "reason": reason, "site": host}
            waiver = self._active(state, system_id)
            refusal = "no active waiver for this payment system"
            if waiver:
                spent = self._spent_cents(waiver["waiver_id"])
                sites = list(waiver.get("sites") or [])
                if sites and not host:
                    refusal = f"this waiver covers only {', '.join(sites)}: say which site takes the payment"
                elif sites and not site_ok(host, sites):
                    refusal = f"{host} is not one of the sites this waiver covers ({', '.join(sites)})"
                elif cents > waiver["per_payment_cents"]:
                    refusal = f"over the cap of {money(waiver['per_payment_cents'], system['currency'])} per payment"
                elif spent + cents > waiver["cumulative_cents"]:
                    refusal = f"over the cumulative cap, {money(waiver['cumulative_cents'] - spent, system['currency'])} left"
                else:
                    payment_id = "pay_" + secrets.token_hex(8)
                    entry = self.ledger.record("broker", "payment_authorized", {
                        **request, "amount_cents": cents, "waiver_id": waiver["waiver_id"], "payment_id": payment_id})
                    result = {**request, "mode": "waiver", "status": "authorized", "ledger_index": entry["index"], "payment_id": payment_id,
                              "remaining": money(waiver["cumulative_cents"] - spent - cents, system["currency"])}
                    self._notify(result)
                    return result
        if confirm is not None and confirm(request):
            with self._lock():
                self._verified_ledger()
                payment_id = "pay_" + secrets.token_hex(8)
                entry = self.ledger.record("broker", "payment_confirmed", {**request, "amount_cents": cents, "payment_id": payment_id})
            result = {**request, "mode": "confirmed", "status": "authorized", "ledger_index": entry["index"], "payment_id": payment_id}
            self._notify(result)
            return result
        with self._lock():
            self._verified_ledger()
            self.ledger.record("broker", "payment_refused", {**request, "why": refusal})
        raise PermissionError(f"Payment refused: {refusal}")

    def still_valid(self, payment_id: str) -> bool:
        """Whether the decision that let a payment through still stands: it is on the ledger, its waiver is still in force and nothing was
        revoked or removed since. The vault asks this each time it releases a card, so the kill switch reaches a conversation that is running."""
        try:
            with self._lock():
                self._verified_ledger()
                state = self._load_state()
                rows = self.ledger.records
                found = next((r for r in rows if r.get("details", {}).get("payment_id") == payment_id
                              and r.get("action") in ("payment_authorized", "payment_confirmed")), None)
                if found is None:
                    return False
                system_id = found["details"].get("system")
                for r in rows[found["index"] + 1:]:
                    if r.get("action") in ("waiver_revoked", "system_removed") and r["details"].get("system") in (system_id, "*"):
                        return False
                if found["action"] == "payment_authorized":
                    w = next((x for x in state["waivers"] if x["waiver_id"] == found["details"].get("waiver_id")), None)
                    return bool(w) and not w.get("revoked_at") and self.clock() < w["expires_at"]
                return True
        except (PermissionError, OSError, ValueError, KeyError):
            return False                                   # a damaged state stops every release too

    def note(self, actor: str, action: str, details: Dict[str, Any]) -> None:
        """A line of the ledger for something the person did, such as reading a value in the vault."""
        with self._lock():
            self._verified_ledger()
            self.ledger.record(actor, action, details)

    def _notify(self, result: Dict[str, Any]) -> None:
        if self.notify:
            try:
                self.notify(result)
            except Exception:
                pass                                       # telling the user must never undo a recorded decision


def register_payment_tool(registry: Any, broker: PaymentsBroker, permissions: Any = None,
                          confirm: Optional[Callable[[Dict[str, Any]], bool]] = None,
                          on_authorized: Optional[Callable[[str, str, str], None]] = None) -> None:
    """Adds `request_payment`. The broker is the only gate: below a waiver's cap it decides alone, otherwise
    it asks `confirm`, and with no way to ask it refuses. `on_authorized(vault_entry, payment_id, site)` lets a guarded card
    out of the vault, to that site, for as long as the decision stands."""
    from harness.tools.registry import EffectClass

    def request_payment(system_id: str, amount: str, currency: str, recipient: str, reason: str, site: str = "") -> Dict[str, Any]:
        result = broker.authorize(system_id, amount, currency, recipient, reason, confirm=confirm, site=site)
        card = broker.system_card(system_id)
        entry = card.get("vault_entry") or ""
        out = {**result, "executed": False}
        if entry and card.get("credential") == "vault_card":
            if on_authorized:
                on_authorized(entry, result["payment_id"], result.get("site", ""))
            out["pay_with"] = {f: "{{vault:%s.%s}}" % (entry, f) for f in ("holder", "number", "expiry", "cvc")}
            out["note"] = ("Basalt approved this amount. Put the values above in the merchant's payment form exactly as written; they are filled in for you. "
                           "This tool charges nothing by itself: the payment is made when the form is sent.")
        elif entry and card.get("credential") == "vault_login":
            out["sign_in_with"] = {f: "{{vault:%s.%s}}" % (entry, f) for f in ("username", "password")}
            out["note"] = ("Basalt approved this amount. Sign in with the values above; if a code is asked, write {{vault:%s.totp}}. "
                           "This tool charges nothing by itself." % entry)
        else:
            out["note"] = "Basalt approved the amount; this payment system has no card or login in the vault, so nothing was charged."
        return out

    registry.register(
        "request_payment",
        "Asks to pay an amount through a registered payment system. It is approved only within the user's own limits.",
        {"type": "object", "properties": {
            "system_id": {"type": "string"}, "amount": {"type": "string", "description": "e.g. 12.50"},
            "currency": {"type": "string"}, "recipient": {"type": "string"}, "reason": {"type": "string"},
            "site": {"type": "string", "description": "the website that will take the payment, as its address; the card is released only to it"}},
         "required": ["system_id", "amount", "currency", "recipient", "reason"]},
        EffectClass.IRREVERSIBLE, request_payment)
    if permissions is not None:
        permissions.add_rule("request_payment", "allow")
