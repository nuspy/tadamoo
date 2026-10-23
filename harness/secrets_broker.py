"""Secrets broker: holds credentials out of model reach and sanitizes outputs.

The model only ever writes placeholders such as {{secret:github_token}}; the value is substituted at
the execution boundary and masked again in anything that flows back. A secret may name the hosts it
can be sent to; a command that would send it anywhere else is refused. This is a check of what the
command says, not network isolation: a command that builds its address at run time needs a firewall.
"""
from __future__ import annotations

import os
import re
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")
URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>|]+")
MIN_SECRET_LENGTH = 4
NETWORK_TOOLS = {"curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod", "nc", "ncat", "netcat",
                 "ssh", "scp", "sftp", "ftp", "telnet", "certutil", "bitsadmin"}


class SecretDestinationError(PermissionError):
    """A command would send a secret somewhere it is not allowed to go."""


def url_hosts(text: str) -> List[str]:
    return sorted({(urlparse(u).hostname or "").lower() for u in URL.findall(text)} - {""})


class SecretsBroker:
    def __init__(self, secrets: Optional[Dict[str, str]] = None,
                 allowed_hosts: Optional[Mapping[str, Iterable[str]]] = None) -> None:
        self._secrets: Dict[str, str] = dict(secrets or {})
        self._hosts: Dict[str, FrozenSet[str]] = {k: frozenset(h.lower() for h in v)
                                                  for k, v in (allowed_hosts or {}).items()}

    @classmethod
    def from_environment(cls, names: Iterable[str], allowed_hosts: Optional[Mapping[str, Iterable[str]]] = None) -> "SecretsBroker":
        """Loads only the named variables; nothing else in the environment is reachable."""
        return cls({n: os.environ[n] for n in names if os.environ.get(n)}, allowed_hosts)

    def set_secret(self, key: str, value: str, allowed_hosts: Optional[Iterable[str]] = None) -> None:
        self._secrets[key] = value
        if allowed_hosts is not None:
            self._hosts[key] = frozenset(h.lower() for h in allowed_hosts)

    def names(self) -> List[str]:
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

    def references(self, text: str) -> List[str]:
        """Names of the secrets a text asks for, known or not."""
        return PLACEHOLDER.findall(text)

    def check_destinations(self, text: str, hosts: Iterable[str], network_tools: Iterable[str] = ()) -> None:
        """Raises SecretDestinationError if a secret in `text` would go to a host it may not reach."""
        hosts, tools = list(hosts), list(network_tools)
        for name in self.references(text):
            if name not in self._secrets:
                continue
            allowed = self._hosts.get(name)
            if allowed is None:
                if hosts or tools:
                    raise SecretDestinationError(
                        f"Secret '{name}' is for local use only; it cannot be put in a command that reaches the network")
            else:
                bad = [h for h in hosts if h not in allowed]
                if bad:
                    raise SecretDestinationError(
                        f"Secret '{name}' may only be sent to {', '.join(sorted(allowed))}, not to {', '.join(bad)}")

    def resolve_tokens(self, text: str, destinations: Optional[Iterable[str]] = None) -> str:
        """Substitutes {{secret:NAME}} with the stored value; unknown names are left as written.

        With `destinations` (the hosts the text will reach) a secret is only filled in toward an allowed host."""
        if destinations is not None:
            self.check_destinations(text, destinations)

        def repl(match: "re.Match[str]") -> str:
            val = self.get_secret(match.group(1))
            return val if val is not None else match.group(0)

        return PLACEHOLDER.sub(repl, text)
