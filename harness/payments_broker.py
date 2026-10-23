"""Payments broker (Level 4): enforces strict spending limits and waiver compliance."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional
from harness.audit import AuditLedger


class PaymentsBroker:
    def __init__(
        self,
        max_limit_usd: float = 50.0,
        audit_ledger: Optional[AuditLedger] = None,
    ) -> None:
        self.max_limit_usd = max_limit_usd
        self.spent_usd: float = 0.0
        self.waiver_accepted: bool = False
        self.revoked: bool = False
        self.audit = audit_ledger

    def accept_waiver(self, confirmation_phrase: str) -> bool:
        """User must explicitly type the required confirmation string."""
        required = "I ACCEPT FULL RESPONSIBILITY FOR AUTONOMOUS PAYMENTS"
        if confirmation_phrase.strip() == required:
            self.waiver_accepted = True
            if self.audit:
                self.audit.record("user", "waiver_accepted", {"limit_usd": self.max_limit_usd})
            return True
        return False

    def revoke(self) -> None:
        """Immediately halts all payments permanently. Preserves spent amount record."""
        self.revoked = True
        self.waiver_accepted = False
        if self.audit:
            self.audit.record("user", "payments_revoked", {"spent_usd": self.spent_usd})

    def authorize_transaction(self, amount_usd: float, recipient: str, reason: str) -> Dict[str, Any]:
        """Validates and authorizes payment against hard limits."""
        if self.revoked:
            raise PermissionError("Payments have been revoked")
        if not self.waiver_accepted:
            raise PermissionError("Level 4 payment waiver has not been accepted by user")
        if amount_usd <= 0:
            raise ValueError(f"Transaction amount must be positive, got {amount_usd}")

        new_total = self.spent_usd + amount_usd
        if new_total > self.max_limit_usd:
            raise PermissionError(
                f"Transaction of ${amount_usd:.2f} exceeds remaining budget (${self.max_limit_usd - self.spent_usd:.2f})"
            )

        self.spent_usd = new_total
        tx_record = {
            "status": "authorized",
            "amount_usd": amount_usd,
            "recipient": recipient,
            "reason": reason,
            "cumulative_spent": self.spent_usd,
            "remaining_budget": self.max_limit_usd - self.spent_usd,
            "timestamp": time.time(),
        }
        if self.audit:
            self.audit.record("payments_broker", "authorize_payment", tx_record)

        return tx_record
