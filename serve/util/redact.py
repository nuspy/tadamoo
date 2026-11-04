"""Hiding what the person wants hidden in a file that leaves the log: secrets by default, personal data or nothing when they choose.

Secrets are recognised by what they look like (private keys, the token forms of the common services, passwords and keys assigned to a name,
card numbers that pass the check digit, a password inside an address) and by value (the server's key, whatever the vault holds).
Personal data is hidden only when asked: e-mail addresses, phone numbers, IBANs, tax codes, and the words the person lists.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, List, Optional

from tadamoo.rule_of_two import _TOKEN_SHAPES

HIDDEN = "[HIDDEN]"
MAX_WORDS = 20
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_BEARER = re.compile(r"(?i)\b(authorization\s*[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+)[A-Za-z0-9._~+/=\-]{8,}")
_ASSIGNED = re.compile(r"(?i)\b((?:[\w.\-]*(?:api[_\-]?key|apikey|secret|token|passw(?:or)?d|pwd|access[_\-]?key|private[_\-]?key|credential)[\w.\-]*)[\"']?\s*[:=]\s*[\"']?)"
                       r"(?!\[HIDDEN\]|\[REDACTED)([^\s\"',;&<>]{6,})")
_BEARER_ALONE = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=\-]{16,}")
_SECRET_KEY = re.compile(r"(?i)api[_\-]?key|secret|token|passw|pwd|authorization|credential|private[_\-]?key|cookie")      # a field of structured data that holds one
_URL_PASSWORD = re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]+:)[^\s/@]+(@)")
_DIGITS = re.compile(r"(?<![\w.])\d(?:[ \-]?\d){12,18}(?![\w])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w.])(?:\+|00)\d[\d \-().]{7,16}\d|(?<![\w.:])\d{3}[ \-.]?\d{3,4}[ \-.]?\d{3,4}(?![\w.:])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b")
_TAX_CODE = re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", re.I)
PERSONAL = {"emails": _EMAIL, "phones": _PHONE, "iban": _IBAN, "taxcode": _TAX_CODE}
LEVELS = ("none", "secrets", "personal")          # none: only the words listed; personal: also e-mail addresses, phone numbers, IBANs and tax codes


def luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if flip:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
        flip = not flip
    return total % 10 == 0


def _card(match: "re.Match[str]") -> str:
    bare = re.sub(r"\D", "", match.group(0))
    return HIDDEN if 13 <= len(bare) <= 19 and luhn(bare) else match.group(0)


def clean_words(raw: Any) -> List[str]:
    """The words the person asked to hide: at most MAX_WORDS, 2 to 60 characters each, from a comma-separated text."""
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    words = [w.strip() for w in items if 2 <= len(str(w).strip()) <= 60]
    return list(dict.fromkeys(words))[:MAX_WORDS]


class ExportMask:
    def __init__(self, level: str = "secrets", words: Iterable[str] = (), known: Iterable[str] = (), vault_mask: Optional[Callable[[str], str]] = None) -> None:
        if level not in LEVELS:
            raise ValueError(f"the mask level is one of: {', '.join(LEVELS)}")
        self.level = level
        self.words = [re.compile(re.escape(w), re.I) for w in clean_words(list(words))]
        self.known = sorted({k for k in known if k and len(k) >= 4}, key=len, reverse=True)
        self.vault_mask = vault_mask

    def text(self, text: str) -> str:
        if self.level == "none":
            for pattern in self.words:
                text = pattern.sub(HIDDEN, text)
            return text
        if self.vault_mask is not None:
            try:
                text = self.vault_mask(text)
            except Exception:
                pass                                       # a vault that cannot be opened hides nothing more; the shapes below still do
        for value in self.known:
            text = text.replace(value, HIDDEN)
        text = _PEM.sub(HIDDEN, text)
        text = _BEARER.sub(lambda m: m.group(1) + HIDDEN, text)
        text = _BEARER_ALONE.sub(lambda m: m.group(1) + HIDDEN, text)
        text = _TOKEN_SHAPES.sub(HIDDEN, text)
        text = _URL_PASSWORD.sub(lambda m: m.group(1) + HIDDEN + m.group(2), text)
        text = _ASSIGNED.sub(lambda m: m.group(1) + HIDDEN, text)
        text = _DIGITS.sub(_card, text)
        if self.level == "personal":
            for pattern in PERSONAL.values():
                text = pattern.sub(HIDDEN, text)
        for pattern in self.words:
            text = pattern.sub(HIDDEN, text)
        return text

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            if self.level == "none":
                return {k: self.value(v) for k, v in value.items()}
            return {k: HIDDEN if isinstance(v, str) and v and isinstance(k, str) and _SECRET_KEY.search(k) else self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value

    __call__ = text
