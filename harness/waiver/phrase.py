"""The declaration a user types to waive confirmation of automatic payments (level 4).

The text is the one the user dictated; it is kept in both languages and compared after normalizing
spaces, letter case and typographic quotes. The amount written in it must equal the amount chosen in
the form, and a pasted declaration is recorded as pasted (it is allowed).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

PHRASES = {
    "it": ("Io {name} hackero la applicazione sotto la mia responsabilità di quanto accade, autorizzando il pagamento "
           "automatico senza mia conferma fino a {amount} massimo."),
    "en": ("I {name} hack the application under my own responsibility for whatever happens, authorizing automatic "
           "payment without my confirmation up to {amount} maximum."),
}
CURRENCY_WORDS = {"€": "EUR", "eur": "EUR", "euro": "EUR", "$": "USD", "usd": "USD", "dollari": "USD", "dollars": "USD",
                  "£": "GBP", "gbp": "GBP", "sterline": "GBP", "pounds": "GBP"}
_AMOUNT = re.compile(r"^\s*(?:(?P<pre>[€$£])\s*)?(?P<num>\d{1,9}(?:[.,]\d{1,2})?)\s*(?P<post>[€$£]|[A-Za-z]{3,8})?\s*$")


def normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def required_phrase(name: str, amount_text: str, lang: str = "it") -> str:
    if lang not in PHRASES:
        raise ValueError(f"language must be one of {sorted(PHRASES)}")
    return PHRASES[lang].format(name=name.strip(), amount=amount_text.strip())


def parse_amount(text: str) -> Tuple[Decimal, Optional[str]]:
    """(value, currency code if the text names one). Only plain amounts such as 50, 50.00, 50,5 or €50 EUR."""
    m = _AMOUNT.match(text)
    if not m:
        raise ValueError("write the amount as a plain number, for example 50 or 50.00 EUR")
    token = (m.group("pre") or m.group("post") or "").lower()
    currency = CURRENCY_WORDS.get(token) if token else None
    if token and currency is None:
        raise ValueError(f"unknown currency '{token}'")
    try:
        value = Decimal(m.group("num").replace(",", "."))
    except InvalidOperation:
        raise ValueError("invalid amount") from None
    if value <= 0:
        raise ValueError("the amount must be greater than zero")
    return value, currency


def verify_phrase(typed: str, name: str, amount: Decimal, currency: str) -> Tuple[str, Decimal]:
    """Checks a typed or pasted declaration against the form. Returns (language, amount written in it).

    Raises ValueError saying what does not match."""
    if not name.strip() or len(name.split()) < 2:
        raise ValueError("write first and last name as on your identity document")
    typed_n = normalize(typed)
    for lang, template in PHRASES.items():
        head, tail = template.split("{amount}")
        head_n = normalize(head.format(name=name.strip()))
        tail_n = normalize(tail)
        if typed_n.startswith(head_n) and typed_n.endswith(tail_n):
            written = typed_n[len(head_n):len(typed_n) - len(tail_n)].strip()
            if not written:
                raise ValueError("the amount is missing from the declaration")
            value, written_currency = parse_amount(written)
            if value != amount:
                raise ValueError("the amount in the declaration is not the amount you chose")
            if written_currency and written_currency != currency.upper():
                raise ValueError("the currency in the declaration is not the one you chose")
            return lang, value
    raise ValueError("the declaration does not match the required text")
