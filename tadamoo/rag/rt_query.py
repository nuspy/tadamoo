"""The search query a real-time function builds from other people's words (what a participant of a meeting or the caller of a call said).

Those words are untrusted: they could steer the search or carry orders for a model. The query is only ever used as search text (words and an embedding): it
reaches no tool and no prompt. Still it is made plain and short here: look-alike and invisible characters are folded, links, addresses, code and markup are
dropped, sentences that address an assistant are dropped (`inject_screen.detect`), and what is left is cut to the last few hundred characters, the part that
says what is being talked about now."""
from __future__ import annotations

import re
from typing import Tuple

from tadamoo import inject_screen, untrusted

QUERY_CHARS = 300
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_TAGS = re.compile(r"<[^>\n]{0,200}>")
_URL = re.compile(r"\b(?:https?://|www\.)\S+|\b[\w.+-]+@[\w-]+\.[\w.-]+\b", re.I)
_SPLIT = re.compile(r"(?<=[.!?;。！？])\s+|\n+")
_SPACE = re.compile(r"\s+")
_MARKS = re.compile(r"\[/?" + untrusted.TAG + r"\]|" + re.escape(untrusted.REMOVED_TOKEN))      # what `scrub` puts where a boundary tag or a control token was


def clean(text: str, cap: int = QUERY_CHARS) -> Tuple[str, int]:
    """(the query, how many sentences were dropped as orders to an assistant). Empty when nothing usable is left."""
    if not isinstance(text, str) or not text.strip():
        return "", 0
    folded, _ = untrusted.fold(text[:4000])
    folded = _MARKS.sub(" ", untrusted.scrub(folded))
    folded = _TAGS.sub(" ", _URL.sub(" ", _CODE.sub(" ", folded)))
    kept, dropped = [], 0
    for sentence in _SPLIT.split(folded):
        sentence = _SPACE.sub(" ", sentence).strip()
        if not sentence:
            continue
        if inject_screen.detect(sentence):
            dropped += 1
            continue
        kept.append(sentence)
    out = ""
    for sentence in reversed(kept):                         # the newest words first, whole sentences while they fit
        candidate = (sentence + " " + out).strip()
        if len(candidate) > cap:
            if not out:
                out = sentence[-cap:].strip()
            break
        out = candidate
    return out, dropped
