"""PlanInsightCard: the compact summary of a plan that System 1 reads to judge incoming events.

It holds what an event could threaten (bookings, constraints of the people involved), not the plan's
whole history. Before anything leaves this machine it goes through Pseudonymizer and an allow-list of
fields; the user decides both. By default the whole card goes, as the validated experiment sent it, because
the fields about the participants' health, allergies and personal constraints are what lets an event that matters only through them be
recognised; the user can take any field out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

CARD_FIELDS = ("titolo", "date", "destinazione", "partecipanti", "logistica_e_prenotazioni",
               "profilo_e_vincoli_partecipanti")
PERSONAL_FIELDS = ("profilo_e_vincoli_partecipanti",)                       # health, allergies, personal constraints: the settings say so where they are chosen
DEFAULT_SHARED_FIELDS = tuple(f for f in CARD_FIELDS if f not in PERSONAL_FIELDS)   # what goes when the user has not chosen: no health, allergies or personal constraints
DERIVED_FIELDS = ("titolo", "date", "logistica_e_prenotazioni")      # what a plan's own data gives a card, and what follows the plan while the person has not written it
INTERVIEW_FIELDS = {"occasional": ("date", "destinazione", "partecipanti"), "constant": ()}      # what a card needs to judge events against a plan
PRONOUNS = {"io", "me", "noi", "tu", "lui", "lei", "voi", "loro", "i", "we"}
MIN_NAME_PART = 3
# words that appear in a participant's entry without being a name ("il mio amico Gianni"): never replaced on their own
COMMON_WORDS = frozenset("""
    il lo la le gli uno una del dello della dei degli delle con per non che sua suo tua tuo mia mio miei amico amica amici amiche
    collega nonno nonna madre padre mamma papa papà figlio figlia fratello sorella zio zia cugino cugina marito moglie
    fidanzato fidanzata compagno compagna signor signora sig dott dottor dottoressa prof professor professoressa
    the and for with friend colleague mother father brother sister wife husband partner mister miss
    organizzatore organizzatrice ospite
""".split())


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
        return card

    def people(self) -> List[str]:
        """Names a pseudonymizer should replace: participants and anyone with a profile entry."""
        names = list(self.partecipanti) + list(self.profilo_e_vincoli_partecipanti)
        cleaned = [re.sub(r"\s*\(.*?\)\s*", "", n).strip() for n in names]
        return list(dict.fromkeys(n for n in cleaned if len(n) >= 3 and n.lower() not in PRONOUNS))


def derive_card(dag: Any, profile: Any = None) -> PlanInsightCard:
    """The card a plan's own data gives: its title, its dates (from the profile) and its steps with their state. The destination, the
    participants and what limits them are not in a plan's data: they stay empty until the person says them."""
    steps: Dict[str, str] = {}
    for node in dag.nodes.values():
        base = " ".join(str(node.title).split())[:120] or node.id
        key, n = base, 2
        while key in steps:
            key, n = f"{base} ({n})", n + 1
        steps[key] = str(node.status) + ("" if str(getattr(node.assignee, "value", node.assignee)) == "agent" else f", for {getattr(node.assignee, 'value', node.assignee)}")
    starts, ends = getattr(profile, "starts_on", ""), getattr(profile, "ends_on", "")
    return PlanInsightCard(titolo=dag.title, date=(f"{starts} - {ends}" if starts and ends else starts or ends), logistica_e_prenotazioni=steps)


def card_gaps(card: PlanInsightCard, target_type: str) -> List[str]:
    """The fields of a card that are empty and that events cannot be judged without (a trip needs its dates, its place and who goes)."""
    return [f for f in INTERVIEW_FIELDS.get(target_type, ()) if not getattr(card, f)]


def name_parts(name: str) -> List[str]:
    """The words of a full name that can stand for the person alone (first name, surname): 3 letters or more, not a common word."""
    words = [w.strip(".,;:'\"") for w in re.split(r"[\s\-]+", name)]
    if len(words) < 2:
        return []
    return [w for w in words if len(w) >= MIN_NAME_PART and w.isalpha() and w.lower() not in COMMON_WORDS and w.lower() not in PRONOUNS]


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w+])\+?\(?\d[\d ().\-]{5,}\d(?!\w)")
_DATE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")
PHONE_DIGITS = (8, 15)


_SENDER = re.compile(r"\A(Email|Telegram) from ([^:\n]{2,80}): ")


def sender_names(events: Iterable[str]) -> List[str]:
    """The people who sent the events, from the line the planner's own sources write ("Email from <name>: ...", "Telegram from <name>: ..."):
    a Telegram first name, or an e-mail display name of two or more words. A one-word e-mail sender may be a company, and is left as it is.
    A name written inside a message is not recognised: no rule finds those reliably."""
    out: List[str] = []
    for event in events:
        m = _SENDER.match(event)
        if not m:
            continue
        who = re.sub(r"\s+", " ", m.group(2)).strip()
        words = who.split(" ")
        if "@" in who or not all(w.replace("'", "").replace("-", "").isalpha() for w in words) or len(who) < 3:
            continue
        if who.lower() in PRONOUNS or who.lower() in COMMON_WORDS or who.lower() in ("someone", "unknown sender") or (m.group(1) == "Email" and len(words) < 2):
            continue
        out.append(who)
    return list(dict.fromkeys(out))


def scrub_contacts(text: str) -> str:
    """Takes e-mail addresses and phone numbers out of a text (a date is not mistaken for a number)."""
    def phone(m: "re.Match[str]") -> str:
        found = m.group(0).strip()
        digits = sum(c.isdigit() for c in found)
        first_group = re.split(r"\D+", found.lstrip("+("))[0]
        if (not PHONE_DIGITS[0] <= digits <= PHONE_DIGITS[1] or _DATE.fullmatch(found)
                or not (found.startswith(("+", "0", "(")) or len(first_group) >= 3)):          # "24 25 26 27" is a list, not a number
            return m.group(0)
        return "[telefono]"
    return _PHONE.sub(phone, _EMAIL.sub("[email]", text))


class Pseudonymizer:
    """Replaces people's names with stable labels (Persona 1, Persona 2, ...) in the card and in events.

    Labels follow the order the names are given in; matching is whole-word, case-insensitive, and tries
    longer names first so "Anna Maria" is not turned into "Persona 1 Maria". A full name ("Gianni Verdi") also has its
    first name and surname replaced on their own ("Gianni è ricoverato"), unless the word is a common one or is itself the
    whole name of someone else; a word that two people share goes to the one named first."""

    def __init__(self, names: Iterable[str], label: str = "Persona") -> None:
        unique = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
        self.mapping = {n: f"{label} {i + 1}" for i, n in enumerate(unique)}
        self._lookup = {n.lower(): v for n, v in self.mapping.items()}
        for name in unique:                                            # whole names win over parts of other names
            for part in name_parts(name):
                self._lookup.setdefault(part.lower(), self.mapping[name])
        longest_first = sorted(self._lookup, key=lambda n: (-len(n), n))
        self._pattern = (re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in longest_first) + r")(?!\w)", re.IGNORECASE)
                         if self._lookup else None)

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
    """The part of a card that may leave this machine: only the allowed fields, with names replaced and e-mail addresses and
    phone numbers taken out. With no list of fields the default ones go, which leave out the participants' health, allergies
    and personal constraints; a list names exactly what the user chose, those fields included."""
    allowed = list(fields) if fields is not None else list(DEFAULT_SHARED_FIELDS)
    unknown = [f for f in allowed if f not in CARD_FIELDS]
    if unknown:
        raise ValueError(f"unknown card fields: {', '.join(unknown)}")
    data = {k: v for k, v in card.to_dict().items() if k in allowed}
    if pseudonymize:
        return Pseudonymizer(card.people()).apply_value(_scrubbed(data))
    return data


def _scrubbed(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_contacts(value)
    if isinstance(value, list):
        return [_scrubbed(v) for v in value]
    if isinstance(value, dict):
        return {scrub_contacts(str(k)): _scrubbed(v) for k, v in value.items()}
    return value
