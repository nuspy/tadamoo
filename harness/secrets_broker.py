"""Secrets broker: holds credentials out of model reach and sanitizes outputs.

The model only ever writes placeholders such as {{secret:github_token}}; the value is
substituted at the execution boundary and masked again in anything that flows back.
"""
from __future__ import annotations

import os
import re
from typing import Dict, Iterable, Optional

PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")
MIN_SECRET_LENGTH = 4


class SecretsBroker:
    def __init__(self, secrets: Optional[Dict[str, str]] = None) -> None:
        self._secrets: Dict[str, str] = dict(secrets or {})

    @classmethod
    def from_environment(cls, names: Iterable[str]) -> "SecretsBroker":
        """Loads only the named variables; nothing else in the environment is reachable."""
        return cls({n: os.environ[n] for n in names if os.environ.get(n)})

    def set_secret(self, key: str, value: str) -> None:
        self._secrets[key] = value

    def names(self) -> list[str]:
        return sorted(self._secrets)

    def get_secret(self, key: str) -> Optional[str]:
        return self._secrets.get(key)

    def mask_secrets(self, text: str) -> str:
        """Replaces secret values with masked placeholders, longest values first."""
        masked = text
        for k, v in sorted(self._secrets.items(), key=lambda kv: len(kv[1]), reverse=True):
            if v and len(v) >= MIN_SECRET_LENGTH:
                masked = masked.replace(v, f"[REDACTED:{k}]")
        return masked

    def resolve_tokens(self, text: str) -> str:
        """Substitutes {{secret:NAME}} with the stored value; unknown names are left as written."""
        def repl(match: "re.Match[str]") -> str:
            val = self.get_secret(match.group(1))
            return val if val is not None else match.group(0)

        return PLACEHOLDER.sub(repl, text)

    def references(self, text: str) -> list[str]:
        """Names of the secrets a text asks for, known or not."""
        return PLACEHOLDER.findall(text)
