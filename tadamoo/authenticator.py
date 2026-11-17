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


def scan_image(data: bytes) -> List[str]:
    """The text of every QR code in a picture (a screenshot, a photo, a frame of the camera), in reading order; needs the `zxing-cpp` package."""
    try:
        import io

        import zxingcpp
        from PIL import Image
    except ImportError:
        raise AuthenticatorError("reading a QR code from a picture needs the 'zxing-cpp' and 'Pillow' packages") from None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        raise AuthenticatorError("this is not a picture this reader can open") from None
    found = zxingcpp.read_barcodes(image.convert("RGB"), formats=zxingcpp.BarcodeFormat.QRCode)
    return [b.text for b in sorted(found, key=lambda b: (b.position.top_left.y, b.position.top_left.x)) if b.text]


def read_many(texts: List[str]) -> Migration:
    """Every link found (the batches of an export, or several links) as one list of accounts; an export that lacks a batch is said."""
    batches: List[Migration] = []
    singles: List[Account] = []
    for t in texts:
        m = read(t)
        if t.strip().lower().startswith("otpauth-migration://"):
            batches.append(m)
        else:
            singles.extend(m.accounts)
    joined = merge_batches(batches) if batches else Migration()
    if batches and joined is None:
        got = sorted(b.batch_index + 1 for b in batches)
        raise AuthenticatorError(f"the export has {max(b.batch_size for b in batches)} parts and only {got} were read: read the other ones too")
    accounts = (joined.accounts if joined else []) + singles
    if not accounts:
        raise AuthenticatorError("no account was found")
    return Migration(accounts)


# The sites that the codes of a known issuer are used on, as a starting point the person confirms: the vault gives a code only to a site its entry names.
KNOWN_SITES = {
    "google": ["accounts.google.com", "google.com"], "github": ["github.com"], "microsoft": ["login.microsoftonline.com", "login.live.com", "microsoft.com"],
    "amazon": ["amazon.com", "amazon.it", "amazon.de", "amazon.co.uk"], "dropbox": ["dropbox.com"], "paypal": ["paypal.com"], "facebook": ["facebook.com"],
    "instagram": ["instagram.com"], "twitter": ["x.com", "twitter.com"], "x": ["x.com", "twitter.com"], "linkedin": ["linkedin.com"], "apple": ["appleid.apple.com", "apple.com"],
    "cloudflare": ["cloudflare.com", "dash.cloudflare.com"], "gitlab": ["gitlab.com"], "stripe": ["stripe.com", "dashboard.stripe.com"], "slack": ["slack.com"],
    "discord": ["discord.com"], "binance": ["binance.com"], "coinbase": ["coinbase.com"], "kraken": ["kraken.com"],
}


def suggested_hosts(account: Account) -> List[str]:
    """The sites to propose for an account: from its issuer when it is a well known one."""
    key = re.sub(r"[^a-z0-9]", "", account.issuer.lower())
    return list(KNOWN_SITES.get(key, KNOWN_SITES.get(re.sub(r"[^a-z0-9]", "", account.issuer.lower().split(" ")[0]), [])))


def preview(migration: Migration) -> List[dict]:
    """What the person sees before anything is kept: the label, the algorithm and the sites proposed; never the secret. HOTP accounts are marked as not usable."""
    return [{"index": i, "label": a.label, "issuer": a.issuer, "name": a.name, "algorithm": a.algorithm, "digits": a.digits, "period": a.period,
             "usable": a.kind == "totp", "why_not": "" if a.kind == "totp" else "it counts events (HOTP), the vault makes codes by the clock", "hosts": suggested_hosts(a)}
            for i, a in enumerate(migration.accounts)]


def _entry_id(account: Account, taken: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_\-]+", "-", f"{account.issuer or account.name or 'account'}-{account.name}" if account.issuer and account.name else (account.issuer or account.name or "account")).strip("-")[:34] or "account"
    out, n = base, 2
    while out in taken:
        out = f"{base[:36]}-{n}"
        n += 1
    return out


def import_into_vault(vault, migration: Migration, choices: List[dict]) -> dict:
    """Keeps the chosen accounts in the vault as logins that make their own codes. `choices` is a list of {index, hosts, merge_into}: `hosts` are the sites the code is for (confirmed by the person),
    `merge_into` the name of a login already in the vault that gets the secret (its username and password stay). An account left out is not kept. Returns what was made, merged and refused."""
    taken = {e["id"] for e in vault.entries()}
    created: List[str] = []
    merged: List[str] = []
    refused: List[dict] = []
    for c in choices:
        try:
            account = migration.accounts[int(c["index"])]
        except (KeyError, ValueError, IndexError):
            refused.append({"label": str(c.get("index")), "why": "no such account in the export"})
            continue
        if account.kind != "totp":
            refused.append({"label": account.label, "why": "it counts events (HOTP), the vault makes codes by the clock"})
            continue
        meta = {"algorithm": account.algorithm, "digits": account.digits, "period": account.period}
        hosts = list(c.get("hosts") or [])
        if not hosts:
            refused.append({"label": account.label, "why": "name the sites its code is for: the vault gives a code only to a site its entry names"})
            continue
        try:
            if c.get("merge_into"):
                vault.put(c["merge_into"], kind="login", fields={"totp_secret": account.secret}, hosts=None, second_factor="automatic", totp=meta)
                merged.append(c["merge_into"])
            else:
                entry_id = _entry_id(account, taken)
                vault.put(entry_id, kind="login", label=account.label, fields={"username": account.name, "totp_secret": account.secret}, hosts=hosts, second_factor="automatic", totp=meta)
                taken.add(entry_id)
                created.append(entry_id)
        except Exception as exc:                                        # the vault's own refusal says why
            refused.append({"label": account.label, "why": str(exc)})
    return {"created": created, "merged": merged, "refused": refused}


def instructions(vault) -> str:
    """What the model reads when it loads the two-step capability: how a code is asked for and which logins can give one. Names and sites only, never a secret."""
    rows = []
    for e in (vault.entries() if vault is not None else []):
        if e.get("second_factor") in ("automatic", "manual"):
            how = "its code is made for you" if e["second_factor"] == "automatic" else "the person is asked to type its code"
            rows.append(f"- {e['id']} ({e['label']}): {how}; sites: {', '.join(e['hosts']) or 'any'}")
    listing = "\n".join(rows) if rows else "- none yet: the person adds them in Settings, under the vault (Import from Google Authenticator)."
    return ("When a page asks for a one-time code (after the password), write {{vault:ENTRY.totp}} in the code field with browser_type, where ENTRY is the login for that site. "
            "The code is made at that moment and valid for about 30 seconds: ask for it only when the field is in front of you, and type it at once. "
            "A code goes only to a site its entry names; on another site the request is refused. Never ask the person for a code that an entry makes by itself, "
            "and never try to read the secret behind it.\n\nLogins with a second factor:\n" + listing)


def progress(texts: List[str]) -> dict:
    """How far a scan has come: the parts of an export read so far out of its total, the accounts counted, and whether it can be used. A link that is not an authenticator link is ignored."""
    batches: List[Migration] = []
    singles = 0
    for t in texts:
        try:
            m = read(t)
        except AuthenticatorError:
            continue
        if t.strip().lower().startswith("otpauth-migration://"):
            batches.append(m)
        else:
            singles += len(m.accounts)
    out = {"parts": 0, "have": [], "accounts": singles, "complete": bool(singles)}
    if batches:
        ids = {b.batch_id for b in batches}
        out["parts"] = max(b.batch_size for b in batches)
        out["have"] = sorted({b.batch_index + 1 for b in batches})
        out["accounts"] += sum(len(b.accounts) for b in {b.batch_index: b for b in batches}.values())
        out["complete"] = len(ids) == 1 and len(out["have"]) >= out["parts"]
        out["mixed"] = len(ids) > 1
    return out
