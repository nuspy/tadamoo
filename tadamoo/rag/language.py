"""A guess of the language of a text from its script and its most common function words: enough to tell the model what language an archive's documents
are in, so it searches with their words. `other` when the text is too short or too mixed to say."""
from __future__ import annotations

import re

STOP = {
    "en": {"the", "of", "and", "to", "in", "is", "that", "for", "with", "as", "by", "an", "are", "this", "which"},
    "fr": {"le", "la", "les", "de", "des", "du", "et", "un", "une", "est", "que", "pour", "dans", "par", "sur", "au", "aux", "ce"},
    "it": {"il", "lo", "gli", "di", "del", "della", "dei", "delle", "che", "per", "con", "una", "sono", "nel", "nella", "alla", "dal", "più"},
    "de": {"der", "die", "das", "und", "ist", "von", "mit", "den", "dem", "des", "für", "auf", "nicht", "ein", "eine", "zu", "im"},
    "es": {"el", "los", "las", "de", "del", "y", "que", "en", "un", "una", "por", "con", "para", "es", "se", "al", "su"},
}
MIN_WORDS = 3                         # function words a text needs before it is called a language
CJK = re.compile(r"[㐀-鿿]")
WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def guess_language(text: str) -> str:
    """`zh` for text that is mostly Chinese characters, else the language (`en`, `fr`, `it`, `de`, `es`) whose function words are the most and at least
    `MIN_WORDS`, else `other`."""
    letters = [c for c in text if c.isalpha()]
    if letters and len(CJK.findall(text)) > 0.3 * len(letters):
        return "zh"
    words = WORD.findall(text.lower())
    counts = {lang: sum(w in stop for w in words) for lang, stop in STOP.items()}
    best = max(counts.values(), default=0)
    leaders = [lang for lang, n in counts.items() if n == best]
    return leaders[0] if best >= MIN_WORDS and len(leaders) == 1 else "other"
