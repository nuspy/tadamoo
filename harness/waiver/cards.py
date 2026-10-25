"""The card of a payment system: what level 4 turns off and what stays, checked before any waiver."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

SYSTEM_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,40}$")
# what Basalt pays with. The first three are credentials a provider issued with limits of their own; the last two are the person's own
# entries in the vault (a card of any kind, or a login with the second factor the person chose there), which carry no limit but the cap.
CREDENTIAL_KINDS = {"delegated_card", "delegated_token", "mandate", "vault_card", "vault_login"}
VAULT_KINDS = {"vault_card", "vault_login"}
BASALT_PROTECTIONS_OFF = ["confirmation of each payment, below the cap"]
PROTECTIONS_THAT_STAY = ["provider authentication and limits", "hard cap enforced outside the model", "audit ledger",
                         "kill switch"]


@dataclass
class PaymentSystem:
    system_id: str
    name: str
    kind: str                         # e.g. virtual card, wallet, marketplace account
    credential_kind: str              # what Basalt holds: only delegated credentials are allowed
    currency: str
    provider_terms_url: str = ""
    provider_terms_checked_on: str = ""   # date the user checked that the provider allows automatic agents
    revoke_instructions: str = ""     # the step to take on the provider's side
    notes: str = ""
    vault_entry: str = ""             # for a vault credential: the entry that holds it

    def problems(self) -> List[str]:
        out: List[str] = []
        if not SYSTEM_ID.match(self.system_id):
            out.append("the system id must be 2-41 lowercase letters, digits, - or _")
        if not self.name.strip():
            out.append("the system needs a name")
        if self.credential_kind not in CREDENTIAL_KINDS:
            out.append("choose what Basalt pays with: " + ", ".join(sorted(CREDENTIAL_KINDS)))
        elif self.credential_kind in VAULT_KINDS and not self.vault_entry.strip():
            out.append("name the vault entry that holds the card or the login")
        if not re.fullmatch(r"[A-Za-z]{3}", self.currency):
            out.append("the currency must be a three-letter code")
        if not self.provider_terms_url.strip() or not self.provider_terms_checked_on.strip():
            out.append("check the provider's terms and record the link and the date")
        if not self.revoke_instructions.strip():
            out.append("say how to revoke on the provider's side")
        return out

    def card(self) -> Dict[str, Any]:
        """What the first dialog shows, and what the dossier keeps."""
        return {
            "id": self.system_id, "name": self.name, "kind": self.kind, "credential": self.credential_kind,
            "currency": self.currency.upper(), "basalt_protections_off": BASALT_PROTECTIONS_OFF,
            "protections_that_stay": PROTECTIONS_THAT_STAY, "terms_url": self.provider_terms_url,
            "terms_checked_on": self.provider_terms_checked_on, "how_to_revoke": self.revoke_instructions,
            "vault_entry": self.vault_entry,
            "own_credential": self.credential_kind in VAULT_KINDS,       # nothing but the cap stands between a page the agent reads and this money
        }
