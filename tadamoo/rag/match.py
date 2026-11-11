"""Finding a knowledge archive from the words a person or a model uses for it: by name, in any case and with or without accents, never by a guess
between two."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Sequence

_WORDS = re.compile(r"[^\w]+", re.UNICODE)
SHOWN = 8                          # the most candidates an ambiguous answer names


def fold(text: Any) -> str:
    """Lower-case, accents removed, every run of other characters one space."""
    parts = unicodedata.normalize("NFKD", str(text or "")).casefold()
    return _WORDS.sub(" ", "".join(c for c in parts if not unicodedata.combining(c))).strip()


def _stages(rows: Sequence[Dict[str, Any]], q: str, raw: str) -> List[List[Dict[str, Any]]]:
    names = [(r, fold(r.get("name"))) for r in rows]
    words = [w for w in q.split() if len(w) > 1]
    return [
        [r for r, n in names if n == q or r.get("id") == raw],                                           # the name (or the id) as it is
        [r for r, n in names if n.startswith(q)],                                                      # the start of a name
        [r for r, n in names if q in n],                                                               # a part of a name
        [r for r, n in names if len(n) >= 3 and n in q],                                               # a name inside the words given
        [r for r in rows if words and all(w in fold(f"{r.get('name')} {r.get('description')}").split() for w in words)],   # the words, in the name or description
    ]


def find(rows: Sequence[Dict[str, Any]], query: Any) -> Dict[str, Any]:
    """`{status: "one", archive}`, `{status: "many", candidates}` when the first rule that matches anything matches several (nothing is chosen),
    or `{status: "none"}`. Rows are archive summaries (`id`, `name`, `description`, and whatever else the caller keeps)."""
    q = fold(query)
    raw = str(query or "").strip().lower()
    if not q:
        return {"status": "none", "candidates": []}
    for hits in _stages(rows, q, raw):
        if len(hits) == 1:
            return {"status": "one", "archive": hits[0]}
        if hits:
            return {"status": "many", "candidates": hits[:SHOWN]}
    return {"status": "none", "candidates": []}
