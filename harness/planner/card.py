"""PlanInsightCard: the compact summary of a plan that System 1 reads to judge incoming events.

It holds what an event could threaten (bookings, constraints of the people involved), not the plan's
whole history. Before anything leaves this machine it goes through Pseudonymizer and an allow-list of
fields; the user decides both.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

CARD_FIELDS = ("titolo", "date", "destinazione", "partecipanti", "logistica_e_prenotazioni",
               "profilo_e_vincoli_partecipanti")
MAX_CARD_BYTES = 12_000
PRONOUNS = {"io", "me", "noi", "tu", "lui", "lei", "voi", "loro", "i", "we"}


@dataclass
class PlanInsightCard:
    titolo: str
    date: str = ""
    destinazione: str = ""
    partecipanti: List[str] = field(default_factory=list)
    logistica_e_prenotazioni: Dict[str, str] = field(default_factory=dict)
    profilo_e_vincoli_partecipanti: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in CARD_FIELDS}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanInsightCard":
        if not isinstance(data, dict) or not str(data.get("titolo", "")).strip():
            raise ValueError("a card needs a title")
        card = cls(
            titolo=str(data["titolo"]), date=str(data.get("date", "")), destinazione=str(data.get("destinazione", "")),
            partecipanti=[str(p) for p in data.get("partecipanti", [])],
            logistica_e_prenotazioni={str(k): str(v) for k, v in dict(data.get("logistica_e_prenotazioni", {})).items()},
            profilo_e_vincoli_partecipanti={str(k): str(v) for k, v in dict(data.get("profilo_e_vincoli_partecipanti", {})).items()},
        )
        if len(json.dumps(card.to_dict(), ensure_ascii=False).encode("utf-8")) > MAX_CARD_BYTES:
            raise ValueError(f"the card is larger than {MAX_CARD_BYTES} bytes; summarize it")
        return card

    def people(self) -> List[str]:
        """Names a pseudonymizer should replace: participants and anyone with a profile entry."""
        names = list(self.partecipanti) + list(self.profilo_e_vincoli_partecipanti)
        cleaned = [re.sub(r"\s*\(.*?\)\s*", "", n).strip() for n in names]
        return list(dict.fromkeys(n for n in cleaned if len(n) >= 3 and n.lower() not in PRONOUNS))


class Pseudonymizer:
    """Replaces people's names with stable labels (Persona 1, Persona 2, ...) in the card and in events.

    Labels follow the order the names are given in; matching is whole-word, case-insensitive, and tries
    longer names first so "Anna Maria" is not turned into "Persona 1 Maria"."""

    def __init__(self, names: Iterable[str], label: str = "Persona") -> None:
        unique = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
        self.mapping = {n: f"{label} {i + 1}" for i, n in enumerate(unique)}
        self._lookup = {n.lower(): v for n, v in self.mapping.items()}
        longest_first = sorted(unique, key=lambda n: (-len(n), n))
        self._pattern = (re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in longest_first) + r")(?!\w)", re.IGNORECASE)
                         if unique else None)

    def apply(self, text: str) -> str:
        return self._pattern.sub(lambda m: self._lookup[m.group(0).lower()], text) if self._pattern else text

    def apply_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.apply(value)
        if isinstance(value, list):
            return [self.apply_value(v) for v in value]
        if isinstance(value, dict):
            return {self.apply(str(k)): self.apply_value(v) for k, v in value.items()}
        return value


def share_card(card: PlanInsightCard, fields: Optional[Iterable[str]] = None, pseudonymize: bool = True) -> Dict[str, Any]:
    """The part of a card that may leave this machine: only the allowed fields, with names replaced."""
    allowed = list(fields) if fields is not None else list(CARD_FIELDS)
    unknown = [f for f in allowed if f not in CARD_FIELDS]
    if unknown:
        raise ValueError(f"unknown card fields: {', '.join(unknown)}")
    data = {k: v for k, v in card.to_dict().items() if k in allowed}
    if pseudonymize:
        return Pseudonymizer(card.people()).apply_value(data)
    return data
