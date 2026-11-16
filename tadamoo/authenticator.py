"""The accounts of an authenticator app (Google Authenticator and the apps that use its format) read into plain values, so that the vault can keep them.

Three forms are read: the export link of Google Authenticator (`otpauth-migration://offline?data=...`, a protobuf in base64, in batches when there are many accounts), the usual
`otpauth://totp/...` link, and a bare base32 secret. The code that makes the numbers is the vault's `totp_code` (RFC 6238); this module only reads and checks.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

ALGORITHMS = {0: "SHA1", 1: "SHA1", 2: "SHA256", 3: "SHA512", 4: "MD5"}
DIGITS = {0: 6, 1: 6, 2: 8}
KINDS = {0: "totp", 1: "hotp", 2: "totp"}          # in the export: 1 is HOTP and 2 is TOTP (0 is not said: TOTP)
NAMES = {"SHA1", "SHA256", "SHA512"}


class AuthenticatorError(ValueError):
    pass


@dataclass(frozen=True)
class Account:
    secret: str                                     # base32, upper case, no padding
    name: str = ""
    issuer: str = ""
    algorithm: str = "SHA1"
    digits: int = 6
    period: int = 30
    kind: str = "totp"                              # "hotp" (a counter) is read but not used: the vault makes time codes
    counter: int = 0

    @property
    def label(self) -> str:
        return f"{self.issuer}: {self.name}" if self.issuer and self.name else (self.issuer or self.name or "account")


@dataclass
class Migration:
    accounts: List[Account] = field(default_factory=list)
    batch_size: int = 1
    batch_index: int = 0
    batch_id: int = 0


def _varint(data: bytes, i: int) -> Tuple[int, int]:
    n = shift = 0
    while True:
        if i >= len(data) or shift > 63:
            raise AuthenticatorError("the export link is cut short or damaged")
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7


def _fields(data: bytes):
    """(number, wire type, value) of each field of a protobuf message; a length-delimited value is bytes, a varint is an int."""
    i = 0
    while i < len(data):
        key, i = _varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(data, i)
        elif wire == 2:
            size, i = _varint(data, i)
            if i + size > len(data):
                raise AuthenticatorError("the export link is cut short or damaged")
            value, i = data[i:i + size], i + size
        elif wire == 1:
            value, i = data[i:i + 8], i + 8
        elif wire == 5:
            value, i = data[i:i + 4], i + 4
        else:
            raise AuthenticatorError("the export link holds something this reader does not know")
        yield number, wire, value


def base32(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def clean_secret(text: str) -> str:
    """A base32 secret as typed (spaces, dashes and lower case allowed), checked and given back in the plain form."""
    plain = re.sub(r"[\s\-]", "", text or "").upper().rstrip("=")
    if not plain or not re.fullmatch(r"[A-Z2-7]+", plain):
        raise AuthenticatorError("the secret is not base32 (letters A-Z and digits 2-7)")
    try:
        if not base64.b32decode(plain + "=" * (-len(plain) % 8)):
            raise AuthenticatorError("the secret is empty")
    except binascii.Error:
        raise AuthenticatorError("the secret is not base32") from None
    return plain


def parse_migration(link: str) -> Migration:
    """The export link of Google Authenticator: `otpauth-migration://offline?data=<base64 of a protobuf>`."""
    parts = urlsplit((link or "").strip())
    if parts.scheme != "otpauth-migration":
        raise AuthenticatorError("this is not an authenticator export link (otpauth-migration://...)")
    data = parse_qs(parts.query).get("data")
    if not data:
        raise AuthenticatorError("the export link has no data")
    text = unquote(data[0]).replace(" ", "+")                                      # a plus sign turned into a space by a form
    try:
        blob = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_" if ("-" in text or "_" in text) else None, validate=True)
    except (binascii.Error, ValueError):
        raise AuthenticatorError("the export link is not base64") from None
    out = Migration()
    for number, wire, value in _fields(blob):
        if number == 1 and wire == 2:
            out.accounts.append(_account(value))
        elif number == 3 and wire == 0:
            out.batch_size = int(value)
        elif number == 4 and wire == 0:
            out.batch_index = int(value)
        elif number == 5 and wire == 0:
            out.batch_id = int(value)
    if not out.accounts:
        raise AuthenticatorError("the export link holds no account")
    return out


def _account(blob: bytes) -> Account:
    raw, name, issuer, algorithm, digits, kind, counter = b"", "", "", 1, 1, 2, 0
    for number, wire, value in _fields(blob):
        if number == 1 and wire == 2:
            raw = bytes(value)
        elif number == 2 and wire == 2:
            name = bytes(value).decode("utf-8", "replace")
        elif number == 3 and wire == 2:
            issuer = bytes(value).decode("utf-8", "replace")
        elif number == 4 and wire == 0:
            algorithm = int(value)
        elif number == 5 and wire == 0:
            digits = int(value)
        elif number == 6 and wire == 0:
            kind = int(value)
        elif number == 7 and wire == 0:
            counter = int(value)
    if not raw:
        raise AuthenticatorError("an account of the export has no secret")
    algo = ALGORITHMS.get(algorithm, "")
    if algo not in NAMES:
        raise AuthenticatorError(f"an account of the export uses an algorithm this vault does not make ({ALGORITHMS.get(algorithm, algorithm)})")
    if ":" in name and not issuer:
        issuer, name = name.split(":", 1)
    return Account(secret=base32(raw), name=name.strip(), issuer=issuer.strip(), algorithm=algo, digits=DIGITS.get(digits, 6), kind=KINDS.get(kind, "totp"), counter=counter)


def parse_otpauth(link: str) -> Account:
    """The usual `otpauth://totp/Issuer:name?secret=...&issuer=...&algorithm=SHA1&digits=6&period=30`."""
    parts = urlsplit((link or "").strip())
    if parts.scheme != "otpauth" or parts.netloc.lower() not in ("totp", "hotp"):
        raise AuthenticatorError("this is not an authenticator link (otpauth://totp/...)")
    query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items() if v}
    label = unquote(parts.path.lstrip("/"))
    issuer, name = (label.split(":", 1) + [""])[:2] if ":" in label else ("", label)
    issuer = query.get("issuer", issuer).strip()
    algorithm = query.get("algorithm", "SHA1").upper()
    if algorithm not in NAMES:
        raise AuthenticatorError(f"the algorithm {algorithm} is not one this vault makes")
    try:
        digits, period = int(query.get("digits", "6")), int(query.get("period", "30"))
    except ValueError:
        raise AuthenticatorError("digits and period must be numbers") from None
    if digits not in (6, 7, 8) or not 5 <= period <= 300:
        raise AuthenticatorError("digits must be 6, 7 or 8 and the period from 5 to 300 seconds")
    return Account(secret=clean_secret(query.get("secret", "")), name=name.strip(), issuer=issuer, algorithm=algorithm, digits=digits, period=period, kind=parts.netloc.lower(),
                   counter=int(query.get("counter", "0") or 0))


def read(text: str) -> Migration:
    """Whatever the person pasted: an export link (many accounts), an `otpauth://` link (one), or a bare secret (one, with no name)."""
    t = (text or "").strip()
    if t.lower().startswith("otpauth-migration://"):
        return parse_migration(t)
    if t.lower().startswith("otpauth://"):
        return Migration([parse_otpauth(t)])
    if "://" in t:
        raise AuthenticatorError("this is not an authenticator link (otpauth-migration://... or otpauth://...)")
    return Migration([Account(secret=clean_secret(t))])


def merge_batches(batches: List[Migration]) -> Optional[Migration]:
    """The batches of one export (the same batch id) as one list, in the order of their index; None if one of them is missing."""
    if not batches:
        return None
    ids = {b.batch_id for b in batches}
    if len(ids) != 1:
        raise AuthenticatorError("these links come from different exports")
    size = max(b.batch_size for b in batches)
    got = {b.batch_index for b in batches}
    if len(got) < size:
        return None
    done = sorted(({b.batch_index: b for b in batches}).values(), key=lambda b: b.batch_index)
    return Migration([a for b in done for a in b.accounts], size, 0, batches[0].batch_id)
