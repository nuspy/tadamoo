"""What the person keeps for payments and bookings: the vault (cards, personal data, logins, secrets) and the payment systems with their limits.

The list shows what is set, with the last four digits of a card; the owner can show or copy a value when editing, and an edit replaces a field or leaves it.
The three dialogs of a waiver are the ones the dossier records; the declaration is checked here, in the server, not only on the page.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from harness.browser import BrowserPrefs, find_browser
from harness.payments_broker import PaymentsBroker
from harness.vault import CARD_KINDS, KINDS, SECOND_FACTORS, Vault, VaultError
from harness.vault_import import parse_logins
from harness.waiver import DIALOG_TEXTS, PaymentSystem, parse_amount, required_phrase
from harness.waiver.cards import CREDENTIAL_KINDS, VAULT_KINDS
from harness.waiver.dossier import DossierUnavailable


class WalletError(ValueError):
    pass


class WalletApi:
    def __init__(self, vault: Callable[[], Vault], payments: Callable[[], PaymentsBroker], browser: BrowserPrefs) -> None:
        self._vault, self._payments, self._browser = vault, payments, browser

    # -- what the page shows ---------------------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kinds": KINDS, "second_factors": list(SECOND_FACTORS), "card_kinds": list(CARD_KINDS),
                               "credential_kinds": sorted(CREDENTIAL_KINDS), "texts": DIALOG_TEXTS, "entries": [], "systems": [], "ledger": [],
                               "problems": [], "browser": {"found": bool(find_browser()), **self._browser.read()}}
        try:
            out["entries"] = self._vault().entries()
        except VaultError as e:
            out["problems"].append(str(e))
        try:
            broker = self._payments()
            active = {w["system"]: w for w in broker.status()}
            out["systems"] = [{**s, "waiver": active.get(s["id"])} for s in broker.systems()]
            out["ledger"] = broker.recent(30)
        except PermissionError as e:
            out["problems"].append(str(e))
        return out

    def set_browser(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Whether the agent has a browser in the conversations it starts from now, and whether the browser shows a window."""
        if not isinstance(req.get("enabled"), bool) or not isinstance(req.get("headless"), bool) or not isinstance(req.get("private", False), bool):
            raise WalletError("send enabled and headless as true or false")
        return {"browser": {"found": bool(find_browser()), **self._browser.write(req["enabled"], req["headless"], req.get("private", False))}}

    # -- the vault -------------------------------------------------------------------------------

    def put_entry(self, req: Dict[str, Any]) -> Dict[str, Any]:
        fields = req.get("fields") or {}
        if not isinstance(fields, dict):
            raise WalletError("send the fields as an object")
        hosts = req.get("hosts")
        if isinstance(hosts, str):
            hosts = [h for h in hosts.replace(",", " ").split() if h]
        try:
            return self._vault().put(str(req.get("id", "")), kind=str(req.get("kind", "")), label=str(req.get("label") or ""), fields=fields,
                                     hosts=hosts, second_factor=req.get("second_factor"), card_kind=req.get("card_kind"),
                                     currency=req.get("currency"), guard=req.get("guard") if isinstance(req.get("guard"), bool) else None)
        except VaultError as e:
            raise WalletError(str(e)) from None

    def reveal(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """A value, for the owner to read or copy in their own page (a password manager shows what it keeps)."""
        try:
            return {"value": self._vault().reveal(str(req.get("id", "")), str(req.get("field", "")))}
        except VaultError as e:
            raise WalletError(str(e)) from None

    def import_logins(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """The logins of a browser's or a password manager's CSV export, each sent only to its own site."""
        text = req.get("csv")
        if not isinstance(text, str) or not text.strip():
            raise WalletError("send the text of the exported file")
        vault = self._vault()
        try:
            parsed = parse_logins(text, vault.login_facts())
            made = vault.put_many(parsed["entries"])
        except (ValueError, VaultError) as e:
            raise WalletError(str(e)) from None
        return {"added": len(made["added"]), "refused": made["refused"][:20], "skipped": parsed["skipped"]}

    def delete_entry(self, entry_id: Any) -> Dict[str, Any]:
        entry_id = str(entry_id or "")
        for s in self._payments().systems():
            if s.get("vault_entry") == entry_id:
                raise WalletError(f"the payment system '{s['id']}' uses this entry: remove the system first")
        return {"deleted": self._vault().delete(entry_id)}

    # -- the payment systems ---------------------------------------------------------------------

    def put_system(self, req: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(req.get("credential_kind") or "")
        entry = str(req.get("vault_entry") or "")
        if kind in VAULT_KINDS:
            try:
                found = self._vault().describe(entry)
            except VaultError:
                raise WalletError(f"there is no vault entry '{entry}'") from None
            if found["kind"] != ("card" if kind == "vault_card" else "login"):
                raise WalletError(f"a {kind.replace('vault_', '')} system needs a {'card' if kind == 'vault_card' else 'login'} entry")
        system = PaymentSystem(system_id=str(req.get("id") or ""), name=str(req.get("name") or ""), kind=str(req.get("kind") or "payment system"),
                               credential_kind=kind, currency=str(req.get("currency") or ""), provider_terms_url=str(req.get("terms_url") or ""),
                               provider_terms_checked_on=str(req.get("terms_checked_on") or ""),
                               revoke_instructions=str(req.get("how_to_revoke") or ""), notes=str(req.get("notes") or ""), vault_entry=entry)
        self._payments().register_system(system)
        return {"system": system.system_id}

    def remove_system(self, system_id: Any) -> Dict[str, Any]:
        return {"removed": self._payments().remove_system(str(system_id or ""))}

    # -- the waiver ------------------------------------------------------------------------------

    def phrase(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """The declaration to type or paste, for this name and amount, so the person can copy it."""
        lang = req.get("lang") if req.get("lang") in ("it", "en") else "it"
        name, amount = str(req.get("name") or "").strip(), str(req.get("amount") or "").strip()
        if not name or not amount:
            raise WalletError("write your name and the amount first")
        parse_amount(amount)
        return {"phrase": required_phrase(name, amount, lang), "lang": lang}

    def grant(self, req: Dict[str, Any]) -> Dict[str, Any]:
        names = req.get("retyped_names") if isinstance(req.get("retyped_names"), dict) else {}
        intervals = req.get("typing_intervals_ms")
        try:
            waiver = self._payments().grant_waiver(
                str(req.get("system_id") or ""), user_name=str(req.get("user_name") or ""), typed_text=str(req.get("typed_text") or ""),
                per_payment=str(req.get("per_payment") or ""), cumulative=str(req.get("cumulative") or ""),
                read_to_end=req.get("read_to_end") is True, release_ack=req.get("release_ack") is True,
                retyped_names={str(k): str(v) for k, v in names.items()}, pasted=req.get("pasted") is True,
                ttl_days=float(req.get("ttl_days") or 30), lang=req.get("lang") if req.get("lang") in ("it", "en") else "it",
                typing_intervals_ms=[int(x) for x in intervals][:2000] if isinstance(intervals, list) else None)
        except DossierUnavailable as e:
            raise WalletError(str(e)) from None
        except KeyError as e:
            raise WalletError(str(e.args[0]) if e.args else "unknown payment system") from None
        return {"waiver": {k: waiver[k] for k in ("waiver_id", "system_id", "granted_at", "expires_at")}}

    def revoke(self, system_id: Any) -> Dict[str, Any]:
        """Ends the waiver of one system, or of every system when none is named: the kill switch."""
        return {"ended": self._payments().revoke(str(system_id) if system_id else None)}
