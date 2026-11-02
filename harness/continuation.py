"""Joining the pieces of a reply that the output limit cut and that was asked to go on.

A continuation is a new reply, so it never begins with the space that separates it from the word before, and it may repeat a little of
what came before. The cut reply is therefore sent back only up to its last whitespace (`cut_point`): the model writes the unfinished word
again as the first word of its new reply. `join_continuation` puts the whole cut text and the new reply together without a doubled or
missing space, without a second paragraph break and without the words the model repeated. The Chat page has the same two functions in
`serve/web/chat.js` (`cutPoint`, `joinCut`); `tests/test_continuation.py` runs one table of cases through both.
"""
from __future__ import annotations

import re

WORD_MAX = 48               # an unfinished last word longer than this is kept as it is (a URL, a long identifier)
OVERLAP_MIN = 6             # characters a repeated start must have to be taken for one
OVERLAP_MAX = 400
_LAST_WORD = re.compile(r"\s(\S+)\Z")


def cut_point(text: str) -> int:
    """Where a cut reply is sent back up to: the end of its last whitespace. A reply that ends in whitespace is kept whole."""
    m = _LAST_WORD.search(text)
    return m.start(1) if m and len(m.group(1)) <= WORD_MAX else len(text)


def _overlap(text: str, body: str) -> int:
    """How many characters at the start of `body` repeat the end of `text`: whole words, except that the last of them may be the word the
    cut left unfinished (then `body` may go on past it). 0 when nothing repeats."""
    base = text.rstrip()[-OVERLAP_MAX:]
    open_end = len(text.rstrip()) == len(text)                     # the cut fell inside a word
    for k in range(min(len(base), len(body), OVERLAP_MAX), OVERLAP_MIN - 1, -1):
        if base.endswith(body[:k]) and (len(base) == k or base[-k - 1].isspace()) and (open_end or k == len(body) or body[k].isspace()):
            return k
    return 0


def join_continuation(text: str, new: str) -> str:
    """The cut reply `text` followed by `new`, the reply that goes on from it."""
    if not new.strip():
        return text
    if not text:
        return new
    lead = new[:len(new) - len(new.lstrip())]
    body = new[len(lead):]
    k = _overlap(text, body)
    if k:                                                          # the model began a little early: those words are already written
        return text.rstrip() + body[k:] if body[k:].strip() else text
    kept = text[:cut_point(text)]
    tail = kept[len(kept.rstrip()):]
    if tail and lead:                                              # whitespace on both sides of the seam: one break, never two
        newlines = (tail + lead).count("\n")
        return kept.rstrip() + ("\n" * min(2, newlines) if newlines else " ") + body
    return kept + lead + body
