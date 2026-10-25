"""Secrets broker: holds credentials out of model reach and sanitizes outputs.

The model only ever writes placeholders such as {{secret:github_token}}; the value is substituted at
the execution boundary and masked again in anything that flows back. A secret may name the hosts it
can be sent to; a command that would send it anywhere else is refused. This is a check of what the
command says, not network isolation: a command that builds its address at run time needs a firewall.
"""
from __future__ import annotations

import base64
import os
import re
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional
from urllib.parse import quote, quote_plus

PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")
URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>|]+")
# a site written without a scheme, as `curl evil.example/x` or `scp f user@evil.example:/x` do
BARE_HOST = re.compile(r"(?<![\w.\-/\\])((?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,24}|\d{1,3}(?:\.\d{1,3}){3})(?::\d{1,5})?(?=[:/?#\\\s\"'<>|;&)]|$)", re.I)
FILE_EXTENSIONS = frozenset("txt json xml html htm csv tsv log py js mjs ts sh md yml yaml ini cfg conf zip tar gz tgz bz2 xz bin exe dll pdf png jpg jpeg gif "
                            "svg webp out tmp bak ps1 bat cmd sql db sqlite so o a c h cpp hpp rs go java toml lock pem key crt cer".split())
MIN_SECRET_LENGTH = 4
NETWORK_TOOLS = {"curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod", "nc", "ncat", "netcat",
                 "ssh", "scp", "sftp", "ftp", "telnet", "certutil", "bitsadmin"}


def shapes(value: str) -> set:
    """The forms a value can come back in after a command has changed it: reversed, in another case, encoded. Anything cleverer than these
    (a cipher, a split in pieces) cannot be recognised in text; only keeping the model away from the value stops that."""
    out = {value}
    if len(value) < MIN_SECRET_LENGTH:
        return out
    raw = value.encode("utf-8")
    out |= {value[::-1], value.lower(), value.upper(), quote(value, safe=""), quote_plus(value), raw.hex(), raw.hex().upper()}
    for encoded in (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()):
        out |= {encoded, encoded.rstrip("=")}
    return {s for s in out if len(s) >= MIN_SECRET_LENGTH}


_DIGITS_RUN = re.compile(r"\d(?:[ \-.]?\d){11,22}")


def mask_digit_runs(text: str, numbers: Dict[str, str]) -> str:
    """Hides a card number however a page or a command spaced it: `numbers` maps the bare digits to the label that replaces them."""
    if not numbers:
        return text

    def one(m: "re.Match[str]") -> str:
        bare = re.sub(r"\D", "", m.group(0))
        for digits, label in numbers.items():
            if digits in bare:
                return label
        return m.group(0)

    return _DIGITS_RUN.sub(one, text)


class SecretDestinationError(PermissionError):
    """A command would send a secret somewhere it is not allowed to go."""


def _authority_hosts(rest: str) -> set:
    """Every host the part after `://` can mean. Parsers disagree on a backslash and on where the user name ends, so each reading counts."""
    out = set()
    for cut in (re.split(r"[/?#]", rest, 1)[0], re.split(r"[/\\?#]", rest, 1)[0]):
        for hostport in {cut.rsplit("@", 1)[-1], cut.split("@", 1)[-1]}:
            host = hostport.strip()
            if host.startswith("["):
                host = host[1:].split("]", 1)[0]
            else:
                host = re.sub(r":\d*$", "", host)
            if host:
                out.add(host.lower())
    return out


def url_hosts(text: str) -> List[str]:
    hosts: set = set()
    for u in URL.findall(text):
        hosts |= _authority_hosts(u.split("://", 1)[1])
    return sorted(hosts)


def command_hosts(text: str, network_tools: Iterable[str] = ()) -> List[str]:
    """The sites a command names: its URLs and, when it runs a network tool, also bare names and addresses."""
    hosts = set(url_hosts(text))
    if list(network_tools):
        for m in BARE_HOST.finditer(URL.sub(" ", text)):
            name = m.group(1).lower()
            if name.rsplit(".", 1)[-1] in FILE_EXTENSIONS and not name.replace(".", "").isdigit():
                continue                                    # a file name, not a site
            hosts.add(name)
    return sorted(hosts)


class SecretsBroker:
    def __init__(self, secrets: Optional[Dict[str, str]] = None,
                 allowed_hosts: Optional[Mapping[str, Iterable[str]]] = None) -> None:
        self._secrets: Dict[str, str] = dict(secrets or {})
        self._hosts: Dict[str, FrozenSet[str]] = {k: frozenset(h.lower() for h in v)
                                                  for k, v in (allowed_hosts or {}).items()}
        self.vault: Any = None                          # the person's vault (harness.vault.Vault), when there is one
        self.ask_code: Optional[Callable[[str], str]] = None   # how to ask the person for a second-factor code

    @classmethod
    def from_environment(cls, names: Iterable[str], allowed_hosts: Optional[Mapping[str, Iterable[str]]] = None) -> "SecretsBroker":
        """Loads only the named variables; nothing else in the environment is reachable."""
        return cls({n: os.environ[n] for n in names if os.environ.get(n)}, allowed_hosts)

    def set_secret(self, key: str, value: str, allowed_hosts: Optional[Iterable[str]] = None) -> None:
        self._secrets[key] = value
        if allowed_hosts is not None:
            self._hosts[key] = frozenset(h.lower() for h in allowed_hosts)

    def attach_vault(self, vault: Any, ask_code: Optional[Callable[[str], str]] = None) -> None:
        self.vault, self.ask_code = vault, ask_code

    def names(self) -> List[str]:
        return sorted(self._secrets)

    def sensitive(self) -> bool:
        """Whether this conversation can reach a secret: it decides the Rule of Two."""
        return bool(self._secrets) or bool(self.vault is not None and self.vault.sensitive())

    def has_references(self, text: str) -> bool:
        return bool(self.references(text)) or bool(self.vault is not None and self.vault.references(text))

    def get_secret(self, key: str) -> Optional[str]:
        return self._secrets.get(key)

    def mask_secrets(self, text: str) -> str:
        """Replaces secret values with masked placeholders, longest values first."""
        pairs = [(s, f"[REDACTED:{k}]") for k, v in self._secrets.items() if v and len(v) >= MIN_SECRET_LENGTH for s in shapes(v)]
        masked = text
        for value, label in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
            masked = masked.replace(value, label)
        return self.vault.mask(masked) if self.vault is not None else masked

    def references(self, text: str) -> List[str]:
        """Names of the secrets a text asks for, known or not."""
        return PLACEHOLDER.findall(text)

    def check_destinations(self, text: str, hosts: Iterable[str], network_tools: Iterable[str] = ()) -> None:
        """Raises SecretDestinationError if a secret in `text` would go to a host it may not reach."""
        hosts, tools = list(hosts), list(network_tools)
        if self.vault is not None:
            self.vault.check_destinations(text, hosts, tools)
        for name in self.references(text):
            if name not in self._secrets:
                continue
            allowed = self._hosts.get(name)
            if allowed is None:
                if hosts or tools:
                    raise SecretDestinationError(
                        f"Secret '{name}' is for local use only; it cannot be put in a command that reaches the network")
            else:
                if tools and not hosts:
                    raise SecretDestinationError(
                        f"Secret '{name}' may only go to {', '.join(sorted(allowed))}; this command runs a network tool but names no site, so where it would go cannot be checked")
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

        out = PLACEHOLDER.sub(repl, text)
        return self.vault.resolve(out, hosts=destinations, ask_code=self.ask_code) if self.vault is not None else out
