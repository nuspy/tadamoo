"""The amount a payment page asks for, read from the page's text.

A card is released into a form only when the amount the page shows is no more than the amount the person's limits approved. This reads
what the page shows, not what its form will send: a page that shows one amount and charges another is outside what text can reveal.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP"}
CODES = ("EUR", "USD", "GBP", "CHF", "CAD", "AUD", "SEK", "NOK", "DKK", "PLN", "CZK", "JPY")
_NUM = r"\d{1,3}(?:[.,'   ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_CUR = "|".join(["€", r"\$", "£"] + list(CODES))
_BEFORE = re.compile(rf"(?P<cur>{_CUR})\s?(?P<num>{_NUM})", re.I)
_AFTER = re.compile(rf"(?P<num>{_NUM})\s?(?P<cur>{_CUR})(?![A-Za-z])", re.I)
# a label that says this amount is what will be charged, not a part of it ("subtotal", "shipping")
_TOTAL = re.compile(r"(?<!sub)(?<!sub-)(?<!sub )\b(total|totale|amount due|to pay|pay now|da pagare|importo|gesamt|montant|totaal|order total)\b", re.I)


def cents_of(number: str) -> Optional[int]:
    """'1.234,50', '1,234.50', '12,5', '30' -> whole cents; None when it cannot be read."""
    n = re.sub(r"[  ' ]", "", number)
    last = max(n.rfind("."), n.rfind(","))
    if last >= 0 and len(n) - last - 1 in (1, 2):
        whole, frac = n[:last], n[last + 1:]
    else:
        whole, frac = n, ""
    whole = re.sub(r"[.,]", "", whole)
    if not whole.isdigit() or (frac and not frac.isdigit()):
        return None
    return int(whole) * 100 + int((frac + "00")[:2])


def amounts(text: str) -> List[Tuple[int, str, bool]]:
    """(cents, currency code, labelled as the total) for every amount in the text."""
    found: List[Tuple[int, str, bool]] = []
    for line in text.splitlines():
        seen = set()
        for pattern in (_BEFORE, _AFTER):
            for m in pattern.finditer(line):
                if m.span("num") in seen:
                    continue
                seen.add(m.span("num"))
                cents = cents_of(m.group("num"))
                if cents is None:
                    continue
                cur = m.group("cur").upper()
                code = SYMBOLS.get(cur, cur)
                near = line[max(0, m.start() - 60):m.end() + 25]
                found.append((cents, code, bool(_TOTAL.search(near))))
    return found


def payable(text: str, currency: str) -> Tuple[Optional[int], str]:
    """The amount the page asks for in `currency`, in cents, and '' ; or None and why it cannot be told. The largest amount marked as the
    total if the page marks any, else the largest amount in the currency."""
    found = amounts(text)
    mine = [a for a in found if a[1] == currency.upper()]
    if not mine:
        other = sorted({a[1] for a in found})
        return None, (f"the page shows amounts in {', '.join(other)}, not in {currency.upper()}" if other
                      else f"the page shows no amount in {currency.upper()}")
    marked = [a for a in mine if a[2]]
    return max(marked or mine)[0], ""
