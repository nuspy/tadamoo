"""The check on an answer before it is handed over: what the person asked for, measured.

A request has a *card*: the constraints it states that can be measured (a number of words, a language, a format, a word to
leave out) and the ones that cannot (a tone, an audience). The card is made by rules, with no call to a model. When the
agent has an answer, `Pipeline.verify` measures what can be measured and, in the full mode and only when the card has
qualitative constraints, asks a judge (`harness/llm/judge.py`) one yes/no question for each. The agent loop gives a failed
answer **one** second try with the list of what was not respected, then says what is still unmet.

Modes: `off` checks nothing, `light` measures only, `full` measures and judges.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

MODES = ("off", "light", "full")
MAX_JUDGED = 3                                      # qualitative constraints judged for one answer
NUMBER = r"(\d{1,6})"

WORDS = r"parol[ae]|words?"
_MAX = r"(?:in\s+(?:massimo|al\s+massimo)?|entro|al\s+massimo|massimo|max\.?|non\s+più\s+di|meno\s+di|fino\s+a|in\s+meno\s+di|under|at\s+most|no\s+more\s+than|within|up\s+to)"
_MIN = r"(?:almeno|minimo|min\.?|non\s+meno\s+di|più\s+di|at\s+least|minimum|more\s+than)"
_EXACT = r"(?:esattamente|precisamente|exactly)"

LANGUAGES = {
    "it": ("italiano", "italian", "in italiano"),
    "en": ("inglese", "english", "in inglese"),
    "es": ("spagnolo", "spanish"),
    "fr": ("francese", "french"),
    "de": ("tedesco", "german"),
}
STOP = {
    "it": {"il", "lo", "la", "gli", "le", "di", "che", "è", "per", "una", "un", "con", "non", "sono", "del", "della", "nel", "dei", "come", "anche"},
    "en": {"the", "of", "and", "to", "is", "in", "that", "for", "with", "this", "are", "was", "it", "as", "on", "be", "you"},
    "es": {"el", "los", "las", "de", "que", "y", "en", "un", "una", "es", "para", "con", "por", "del", "se"},
    "fr": {"le", "les", "des", "de", "et", "est", "que", "pour", "une", "un", "dans", "avec", "pas", "du"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "mit", "ein", "eine", "für", "den", "zu", "von", "auf"},
}
QUALITATIVE = [
    (r"\btono\s+(?:più\s+)?\w+", "tone"), (r"\b(?:in\s+modo|in\s+maniera|in\s+stile|con\s+(?:un\s+)?(?:stile|linguaggio))\s+[\w\s]{3,30}", "style"),
    (r"\b(?:per|adatto\s+a|rivolto\s+a)\s+(?:un\s+|una\s+|dei\s+)?(?:bambin\w+|principiant\w+|espert\w+|ragazz\w+|non\s+esperti|esperti|manager|clienti)\b", "audience"),
    (r"\bsenza\s+(?:ripetere|ripetizioni|giri\s+di\s+parole|tecnicismi|gergo)\b[\w\s]{0,20}", "style"),
    (r"\b(?:formal[ei]|informal[ei]|amichevole|ironic\w+|professionale|semplice|chiaro|neutro)\b", "tone"),
    (r"\b(?:in\s+a|in\s+an|with\s+a)\s+(?:formal|casual|friendly|neutral|professional|simple)\s+(?:tone|style|way)\b", "tone"),
    (r"\b(?:for|to)\s+(?:a\s+)?(?:child\w*|beginners?|experts?|managers?|customers?)\b", "audience"),
    (r"\bwithout\s+(?:repeating|jargon|repetition)\b[\w\s]{0,20}", "style"),
]


@dataclass
class Constraint:
    kind: str                 # max_words, min_words, exact_words, max_chars, max_lines, items, language, json, forbidden, qualitative
    value: Any
    text: str                 # how it is told to the person and to the model

    def to_json(self) -> Dict[str, Any]:
        return {"kind": self.kind, "value": self.value, "text": self.text}


@dataclass
class Card:
    request: str
    constraints: List[Constraint] = field(default_factory=list)

    @property
    def measurable(self) -> List[Constraint]:
        return [c for c in self.constraints if c.kind != "qualitative"]

    @property
    def qualitative(self) -> List[Constraint]:
        return [c for c in self.constraints if c.kind == "qualitative"]

    def to_json(self) -> Dict[str, Any]:
        return {"constraints": [c.to_json() for c in self.constraints]}


@dataclass
class Verdict:
    mode: str
    violations: List[str] = field(default_factory=list)
    judge_calls: int = 0
    judged_unknown: int = 0
    seconds: float = 0.0
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations


# -- the card ----------------------------------------------------------------------------------------------


def extract_card(text: str) -> Card:
    """The constraints a request states, found by rules. A request without any gives an empty card and costs nothing more."""
    low = " ".join((text or "").lower().split())
    card = Card(request=text or "")
    seen = set()

    def add(kind: str, value: Any, msg: str) -> None:
        key = (kind, str(value))
        if key not in seen:
            seen.add(key)
            card.constraints.append(Constraint(kind, value, msg))

    for m in re.finditer(_EXACT + r"\s+" + NUMBER + r"\s+(?:" + WORDS + ")", low):
        add("exact_words", int(m.group(1)), f"exactly {m.group(1)} words")
    for m in re.finditer(_MAX + r"\s+" + NUMBER + r"\s+(?:" + WORDS + ")", low):
        if not re.search(_EXACT + r"\s+" + m.group(1), low):
            add("max_words", int(m.group(1)), f"at most {m.group(1)} words")
    for m in re.finditer(_MIN + r"\s+" + NUMBER + r"\s+(?:" + WORDS + ")", low):
        add("min_words", int(m.group(1)), f"at least {m.group(1)} words")
    for m in re.finditer(_MAX + r"\s+" + NUMBER + r"\s+(?:caratteri|characters|chars)", low):
        add("max_chars", int(m.group(1)), f"at most {m.group(1)} characters")
    for m in re.finditer(_MAX + r"\s+" + NUMBER + r"\s+(?:righe|linee|lines|frasi|sentences)", low):
        add("max_lines", int(m.group(1)), f"at most {m.group(1)} lines or sentences")
    for m in re.finditer(r"(?:elenco\s+(?:puntato|numerato)|lista\s+(?:puntata|numerata)|bullet(?:ed)?\s+list|numbered\s+list)\s+(?:di|di\s+esattamente|of|with)?\s*" + NUMBER, low):
        add("items", int(m.group(1)), f"a list of exactly {m.group(1)} items")
    for m in re.finditer(NUMBER + r"\s+(?:punti|voci|elementi|bullet|bullets|points|items)\b", low):
        if re.search(r"elenc|lista|list|bullet|punti", low):
            add("items", int(m.group(1)), f"a list of exactly {m.group(1)} items")
    for code, names in LANGUAGES.items():
        if any(re.search(r"(?:\bin\s+|\brispondi\s+in\s+|\banswer\s+in\s+|\bwrite\s+in\s+)" + re.escape(n.replace("in ", "")) + r"\b", low) for n in names):
            add("language", code, f"written in {names[1].capitalize()}")
            break
    if re.search(r"\b(?:solo|only|in\s+formato|as|format(?:ted)?\s+as|come)\s+json\b", low):
        add("json", True, "valid JSON")
    for m in re.finditer(r"\b(?:senza\s+(?:usare\s+)?(?:la\s+parola|il\s+termine)|without\s+(?:using\s+)?the\s+word)\s+[\"“'«]?([\w'-]{2,30})", low):
        add("forbidden", m.group(1), f"without the word “{m.group(1)}”")
    for pattern, tag in QUALITATIVE:
        m = re.search(pattern, low)
        if m and len(card.qualitative) < MAX_JUDGED:
            phrase = m.group(0).strip()
            if not any(phrase in c.value or c.value in phrase for c in card.qualitative):
                add("qualitative", phrase, f"{tag}: {phrase}")
    return card


# -- measuring -----------------------------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    m = re.match(r"\s*```[\w-]*\n(.*?)\n```\s*$", text, re.S)
    return m.group(1) if m else text


def _words(text: str) -> int:
    return len(re.findall(r"[\w'’-]+", re.sub(r"```.*?```", " ", text, flags=re.S)))


def _language(text: str) -> str:
    words = re.findall(r"[a-zàèéìòùäöüßçñ']+", text.lower())
    if len(words) < 6:
        return ""
    scores = {code: sum(1 for w in words if w in stops) for code, stops in STOP.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 and list(scores.values()).count(scores[best]) == 1 else ""


def _items(text: str) -> int:
    return len(re.findall(r"^\s*(?:[-*•]|\d+[.)])\s+\S", text, flags=re.M))


def measure(card: Card, answer: str) -> List[str]:
    """What the answer does not respect among the measurable constraints, as short sentences (empty: all respected)."""
    out: List[str] = []
    body = answer or ""
    for c in card.measurable:
        if c.kind == "max_words" and _words(body) > c.value:
            out.append(f"It has {_words(body)} words; at most {c.value} were asked.")
        elif c.kind == "min_words" and _words(body) < c.value:
            out.append(f"It has {_words(body)} words; at least {c.value} were asked.")
        elif c.kind == "exact_words" and _words(body) != c.value:
            out.append(f"It has {_words(body)} words; exactly {c.value} were asked.")
        elif c.kind == "max_chars" and len(body) > c.value:
            out.append(f"It has {len(body)} characters; at most {c.value} were asked.")
        elif c.kind == "max_lines":
            lines = len([ln for ln in body.splitlines() if ln.strip()])
            sentences = len(re.findall(r"[.!?](?:\s|$)", body))
            if min(lines, max(sentences, 1)) > c.value:
                out.append(f"It is longer than {c.value} lines or sentences.")
        elif c.kind == "items" and _items(body) != c.value:
            out.append(f"It has {_items(body)} list items; exactly {c.value} were asked.")
        elif c.kind == "language":
            found = _language(body)
            if found and found != c.value:
                out.append(f"It is not written in the language asked ({c.text}).")
        elif c.kind == "json":
            try:
                json.loads(_strip_fences(body).strip())
            except ValueError:
                out.append("It is not valid JSON.")
        elif c.kind == "forbidden" and re.search(r"\b" + re.escape(c.value) + r"\b", body.lower()):
            out.append(f"It uses the word “{c.value}”, which was to be left out.")
    return out


class Pipeline:
    """Checks answers against the card of the request. `judge(question, options, context)` returns one option name or None."""

    def __init__(self, mode: str = "full", judge: Optional[Callable[[str, Dict[str, str], str], Optional[str]]] = None) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.mode, self.judge = mode, judge

    def card(self, request: str) -> Card:
        return extract_card(request) if self.mode != "off" else Card(request or "")

    def verify(self, card: Card, answer: str, judged: bool = True) -> Verdict:
        started = time.monotonic()
        verdict = Verdict(self.mode)
        if self.mode == "off" or not card.constraints:
            return verdict
        verdict.checked = len(card.measurable)
        verdict.violations = measure(card, answer)
        if self.mode == "full" and judged and self.judge and card.qualitative and not verdict.violations:
            for c in card.qualitative[:MAX_JUDGED]:
                verdict.judge_calls += 1
                answer_ = self.judge("Does the answer respect this requirement of the request?",
                                     {"yes": "it respects it", "no": "it does not respect it"},
                                     f"Requirement: {c.value}\nAnswer to check:\n{(answer or '')[:3000]}")
                if answer_ == "no":
                    verdict.violations.append(f"It does not respect this requirement: {c.value}.")
                elif answer_ is None:
                    verdict.judged_unknown += 1
        verdict.seconds = round(time.monotonic() - started, 3)
        return verdict


def feedback(violations: List[str]) -> str:
    """The message that asks for the second try: what was not respected, and nothing that could be taken as a new task."""
    lines = "\n".join(f"- {v}" for v in violations)
    return ("Check of your last answer against the request: it does not respect these points.\n" + lines +
            "\nWrite the answer again so that every point is respected, and reply with the corrected answer only.")
