"""The vault: cards, personal data, logins and secrets, kept encrypted on this PC.

The model never reads a value. It writes a placeholder such as {{vault:visa_trip.number}} and the value is put in at the moment a
tool uses it, only toward the sites the entry names, and masked again in everything that comes back. The file is encrypted with
AES-256-GCM under the device key, which Windows DPAPI wraps for this user. That protects the file from other users and from anyone
who copies it; code running as this same user can ask Windows for the key, which only a separate user for the agent would prevent.

What an entry may do is the person's choice: a login can have no second factor, ask the person for the code each time, or hold
the TOTP secret so that the code is made on its own. Whichever is chosen, the model only ever sees the placeholder.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from harness.secrets_broker import MIN_SECRET_LENGTH, SecretDestinationError
from harness.waiver.keystore import load_or_create_key

MAGIC = b"BVL1"
AAD = b"basalt-vault"
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,39}$")
FIELD = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PLACEHOLDER = re.compile(r"\{\{vault:([A-Za-z0-9][A-Za-z0-9_\-]{0,39})\.([a-z][a-z0-9_]{0,31})\}\}")
SECOND_FACTORS = ("none", "manual", "automatic")
CARD_KINDS = ("virtual", "real", "test")

# what the page offers to fill in for each kind; any other field name is allowed too
KINDS: Dict[str, List[str]] = {
    "card": ["holder", "number", "expiry", "cvc"],
    "identity": ["full_name", "birth_date", "address", "city", "postal_code", "country", "phone", "email", "document_number"],
    "login": ["username", "password", "totp_secret"],
    "secret": ["value"],
}
MAX_VALUE = 4096
MAX_ENTRIES = 200


class VaultError(Exception):
    """The vault cannot do what was asked; the message says what to change."""


def _aead():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise VaultError("the vault needs the 'cryptography' package to encrypt its file (pip install cryptography)") from None
    return AESGCM


def valid_code(value: Any) -> bool:
    """A one-time code typed by the person: a short run of letters, digits, spaces and dashes."""
    return isinstance(value, str) and 0 < len(value.strip()) <= 32 and all(c.isalnum() or c in " -" for c in value)


def totp_code(secret: str, at: float, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 with SHA-1, the form every authenticator app and bank uses."""
    try:
        raw = base64.b32decode(re.sub(r"[\s\-]", "", secret).upper() + "=" * (-len(re.sub(r"[\s\-]", "", secret)) % 8), casefold=True)
    except Exception:
        raise VaultError("the TOTP secret is not valid base32") from None
    digest = hmac.new(raw, struct.pack(">Q", int(at // step)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(number).zfill(digits)


def _clean_hosts(hosts: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for h in hosts or []:
        h = str(h).strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
        if h and h not in out:
            if not re.fullmatch(r"[a-z0-9]([a-z0-9.\-]*[a-z0-9])?", h):
                raise VaultError(f"'{h}' is not a site name")
            out.append(h)
    return out


class Vault:
    def __init__(self, directory: str | Path, key: Optional[bytes] = None, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(directory)
        self.path = self.dir / "vault.bin"
        self._key = key
        self.clock = clock
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._stamp: Any = None
        self._issued: List[tuple] = []                  # the codes handed out lately, so that an echo of one is masked too

    # -- the file --------------------------------------------------------------------------------

    def _device_key(self) -> bytes:
        if self._key is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._key = load_or_create_key(self.dir / "device.key")
        return self._key

    def _read(self) -> Dict[str, Dict[str, Any]]:
        """The entries; read again when the file changed, so an edit in the GUI reaches a running conversation."""
        with self._lock:
            try:
                st = self.path.stat()
            except FileNotFoundError:
                self._cache, self._stamp = {}, None
                return {}
            stamp = (st.st_mtime_ns, st.st_size)
            if self._cache is not None and stamp == self._stamp:
                return self._cache
            blob = self.path.read_bytes()
            if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 12 + 16:
                raise VaultError("the vault file is damaged: nothing was opened")
            nonce, body = blob[len(MAGIC):len(MAGIC) + 12], blob[len(MAGIC) + 12:]
            try:
                plain = _aead()(self._device_key()).decrypt(nonce, body, AAD)
            except VaultError:
                raise
            except Exception:
                raise VaultError("the vault could not be opened: it was made with another key or was changed") from None
            self._cache, self._stamp = json.loads(plain.decode("utf-8")), stamp
            return self._cache

    def _write(self, entries: Dict[str, Dict[str, Any]]) -> None:
        nonce = _secrets.token_bytes(12)
        blob = MAGIC + nonce + _aead()(self._device_key()).encrypt(nonce, json.dumps(entries, sort_keys=True).encode("utf-8"), AAD)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, self.path)
        self._cache, self._stamp = None, None

    # -- what the person edits -------------------------------------------------------------------

    def put(self, entry_id: str, *, kind: str, label: str = "", fields: Optional[Dict[str, Any]] = None, hosts: Optional[Iterable[Any]] = None,
            second_factor: Optional[str] = None, card_kind: Optional[str] = None, currency: Optional[str] = None) -> Dict[str, Any]:
        """Creates or changes an entry. A field sent as None keeps its old value and one sent as "" is removed; the others replace."""
        if not ID.match(entry_id or ""):
            raise VaultError("the name of an entry is 1 to 40 letters, digits, - or _")
        if kind not in KINDS:
            raise VaultError(f"the kind is one of: {', '.join(KINDS)}")
        with self._lock:
            entries = dict(self._read())
            old = entries.get(entry_id)
            if old and old["kind"] != kind:
                raise VaultError("an entry cannot change its kind: make a new one")
            if not old and len(entries) >= MAX_ENTRIES:
                raise VaultError("the vault holds up to %d entries" % MAX_ENTRIES)
            values = dict((old or {}).get("fields", {}))
            for name, value in (fields or {}).items():
                if not FIELD.match(str(name)):
                    raise VaultError(f"'{name}' is not a field name: lowercase letters, digits and _")
                if value is None:
                    continue
                if not isinstance(value, str) or len(value) > MAX_VALUE:
                    raise VaultError(f"{name}: a text of up to {MAX_VALUE} characters")
                if value == "":
                    values.pop(name, None)
                else:
                    values[name] = value
            entry = {"kind": kind, "label": (label if label else (old or {}).get("label", "")) or entry_id, "fields": values,
                     "hosts": _clean_hosts(hosts) if hosts is not None else (old or {}).get("hosts", [])}
            if kind == "login":
                level = second_factor if second_factor is not None else (old or {}).get("second_factor", "none")
                if level not in SECOND_FACTORS:
                    raise VaultError(f"the second factor is one of: {', '.join(SECOND_FACTORS)}")
                if level == "automatic" and not values.get("totp_secret"):
                    raise VaultError("to make the codes on its own the login needs its TOTP secret")
                if values.get("totp_secret"):
                    totp_code(values["totp_secret"], 0)                      # refuses a secret that is not base32
                entry["second_factor"] = level
            if kind == "card":
                ck = card_kind if card_kind is not None else (old or {}).get("card_kind", "virtual")
                if ck not in CARD_KINDS:
                    raise VaultError(f"the card is one of: {', '.join(CARD_KINDS)}")
                entry["card_kind"] = ck
                cur = (currency if currency is not None else (old or {}).get("currency", "")).strip().upper()
                if cur and not re.fullmatch(r"[A-Z]{3}", cur):
                    raise VaultError("the currency is a three-letter code")
                entry["currency"] = cur
                number = re.sub(r"[\s\-]", "", values.get("number", ""))
                if number and not (number.isdigit() and 12 <= len(number) <= 19):
                    raise VaultError("a card number is 12 to 19 digits")
            entries[entry_id] = entry
            self._write(entries)
            return self.describe(entry_id)

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            entries = dict(self._read())
            if entry_id not in entries:
                return False
            del entries[entry_id]
            self._write(entries)
            return True

    # -- what may be shown -----------------------------------------------------------------------

    @staticmethod
    def _hint(kind: str, name: str, value: str) -> str:
        if kind == "card" and name == "number":
            digits = re.sub(r"\D", "", value)
            return "•••• " + digits[-4:] if len(digits) >= 4 else "set"
        if kind == "identity":
            return value if len(value) <= 3 else value[:2] + "…"
        return "set"

    def describe(self, entry_id: str) -> Dict[str, Any]:
        """An entry without its values: which fields are set, and a hint (the last four digits of a card)."""
        e = self._read().get(entry_id)
        if e is None:
            raise VaultError(f"there is no entry '{entry_id}'")
        out = {"id": entry_id, "kind": e["kind"], "label": e["label"], "hosts": list(e["hosts"]),
               "fields": {n: self._hint(e["kind"], n, v) for n, v in sorted(e["fields"].items())}}
        for extra in ("second_factor", "card_kind", "currency"):
            if extra in e:
                out[extra] = e[extra]
        return out

    def entries(self) -> List[Dict[str, Any]]:
        return [self.describe(i) for i in sorted(self._read())]

    def note(self) -> str:
        """What the model is told: the names it may write in placeholders, never a value."""
        lines = []
        for e in self.entries():
            names = ", ".join(sorted(set(e["fields"]) | ({"totp"} if e.get("second_factor") in ("manual", "automatic") else set())))
            where = ", ".join(e["hosts"]) if e["hosts"] else "no site: local use only"
            lines.append(f"- {e['id']} ({e['kind']}: {e['label']}) fields: {names}; may be sent to: {where}")
        return ("Entries in the person's vault; write {{vault:ENTRY.FIELD}} where a value is needed and it is filled in for you "
                "(you never see it):\n" + "\n".join(lines)) if lines else ""

    def sensitive(self) -> bool:
        return bool(self._read())

    # -- at the boundary of a tool ---------------------------------------------------------------

    def references(self, text: str) -> List[str]:
        return [f"{i}.{f}" for i, f in PLACEHOLDER.findall(text)]

    def check_destinations(self, text: str, hosts: Iterable[str], network_tools: Iterable[str] = ()) -> None:
        hosts, tools, entries = list(hosts), list(network_tools), self._read()
        for entry_id, field in PLACEHOLDER.findall(text):
            e = entries.get(entry_id)
            if e is None:
                continue
            allowed = e["hosts"]
            if not allowed:
                if hosts or tools:
                    raise SecretDestinationError(f"'{entry_id}' is for local use only; it cannot be put in a command that reaches the network")
            else:
                bad = [h for h in hosts if h not in allowed and not any(h.endswith("." + a) for a in allowed)]
                if bad:
                    raise SecretDestinationError(f"'{entry_id}' may only be sent to {', '.join(allowed)}, not to {', '.join(bad)}")

    def resolve(self, text: str, hosts: Optional[Iterable[str]] = None, ask_code: Optional[Callable[[str], str]] = None) -> str:
        """Fills in the placeholders. With `hosts` (where the text goes) a value only goes to a site its entry names."""
        if hosts is not None:
            self.check_destinations(text, hosts)
        entries = self._read()

        def one(match: "re.Match[str]") -> str:
            entry_id, field = match.group(1), match.group(2)
            e = entries.get(entry_id)
            if e is None:
                return match.group(0)
            if field == "totp" and e["kind"] == "login":
                code = self._code(entry_id, e, ask_code)
                with self._lock:
                    self._issued = (self._issued + [(code, f"{entry_id}.totp")])[-20:]
                return code
            value = e["fields"].get(field)
            if value is None:
                raise VaultError(f"'{entry_id}' has no field '{field}'")
            return value

        return PLACEHOLDER.sub(one, text)

    def _code(self, entry_id: str, e: Dict[str, Any], ask_code: Optional[Callable[[str], str]]) -> str:
        level = e.get("second_factor", "none")
        if level == "automatic":
            return totp_code(e["fields"]["totp_secret"], self.clock())
        if level == "manual":
            if ask_code is None:
                raise VaultError(f"'{entry_id}' needs you to type its code, and there is no way to ask you here")
            code = (ask_code(e["label"]) or "").strip()
            if not code:
                raise VaultError("no code was given")
            return code
        raise VaultError(f"'{entry_id}' has no second factor: choose one in the vault")

    def mask(self, text: str) -> str:
        """Replaces the values in `text` by their names, longest first."""
        masked = text
        pairs = []
        for i, e in self._read().items():
            for n, v in e["fields"].items():
                shapes = {v, re.sub(r"[\s\-]", "", v)} if n == "number" else {v}       # a card number as typed and as bare digits
                pairs += [(s, f"[REDACTED:{i}.{n}]") for s in shapes if len(s) >= MIN_SECRET_LENGTH]
        pairs += [(c, f"[REDACTED:{n}]") for c, n in self._issued if len(c) >= MIN_SECRET_LENGTH]
        for value, label in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
            masked = masked.replace(value, label)
        return masked
