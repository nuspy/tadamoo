"""Secrets broker: stores credentials out of model reach and sanitizes outputs."""
from __future__ import annotations

import os
import re
from typing import Dict, Optional


class SecretsBroker:
    def __init__(self, secrets: Optional[Dict[str, str]] = None) -> None:
        self._secrets: Dict[str, str] = dict(secrets or {})

    def set_secret(self, key: str, value: str) -> None:
        self._secrets[key] = value

    def get_secret(self, key: str) -> Optional[str]:
        return self._secrets.get(key) or os.environ.get(key)

    def mask_secrets(self, text: str) -> str:
        """Replaces secret values with masked placeholders to prevent leaks."""
        masked = text
        for k, v in self._secrets.items():
            if v and len(v) >= 4:
                masked = masked.replace(v, f"[REDACTED:{k}]")
        return masked

    def resolve_tokens(self, text: str) -> str:
        """Resolves references like $SECRET:NAME into actual secret values at execution boundaries."""
        def repl(match):
            key = match.group(1)
            val = self.get_secret(key)
            return val if val is not None else match.group(0)

        return re.sub(r"\$SECRET:([A-Za-z0-9_]+)", repl, text)
