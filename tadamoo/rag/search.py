"""Ranking passages: BM25 on words (always available) and cosine over embeddings (when there are any), merged by reciprocal rank."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from math import log
from typing import Dict, List, Sequence, Tuple

import numpy as np

K1, B = 1.5, 0.75
RRF_K = 60
MIN_COSINE = 0.2                  # a vector hit below this is too far from the question to count
_WORD = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> List[str]:
    """Lower-case words without accents, so `perché` and `perche` meet; one-letter words are left out."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return [w for w in _WORD.findall(plain) if len(w) > 1 or w.isdigit()]


class Bm25:
    """The word index of one set of passages."""

    def __init__(self, texts: Sequence[str]) -> None:
        self.postings: Dict[str, List[Tuple[int, int]]] = {}
        self.lengths: List[int] = []
        for i, text in enumerate(texts):
            words = tokens(text)
            self.lengths.append(len(words))
            for word, tf in Counter(words).items():
                self.postings.setdefault(word, []).append((i, tf))


def _idf(indexes: Sequence[Bm25], term: str) -> float:
    n = sum(len(ix.lengths) for ix in indexes)
    df = sum(len(ix.postings.get(term, ())) for ix in indexes)
    return log(1 + (n - df + 0.5) / (df + 0.5)) if df else 0.0


def information(indexes: Sequence[Bm25], query: str) -> float:
    """What a passage that held every word of `query` found in the archives would score, roughly: the words' weights added up. A passage's score
    over this is the share of the question it answers."""
    return sum(_idf(indexes, t) for t in dict.fromkeys(tokens(query)))


def bm25(indexes: Sequence[Bm25], query: str) -> List[Dict[int, float]]:
    """For each index, the score of every passage that holds a word of `query`. The statistics are those of all the indexes together, so scores
    from different archives can be compared."""
    terms = list(dict.fromkeys(tokens(query)))
    n = sum(len(ix.lengths) for ix in indexes)
    total = sum(sum(ix.lengths) for ix in indexes)
    scores: List[Dict[int, float]] = [{} for _ in indexes]
    if not n or not terms:
        return scores
    avg = max(total / n, 1.0)
    for term in terms:
        idf = _idf(indexes, term)
        if not idf:
            continue
        for k, ix in enumerate(indexes):
            for i, tf in ix.postings.get(term, ()):
                scores[k][i] = scores[k].get(i, 0.0) + idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * ix.lengths[i] / avg))
    return scores


def cosine(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """The cosine of each normalized row of `matrix` with the (normalized) `query` vector."""
    return matrix @ query


def fuse(rankings: Sequence[Sequence[Tuple[int, int]]]) -> Dict[Tuple[int, int], float]:
    """Reciprocal rank fusion: each passage scores the sum of 1/(60 + position) over the rankings that hold it. A passage is (archive, index)."""
    out: Dict[Tuple[int, int], float] = {}
    for ranking in rankings:
        for pos, key in enumerate(ranking, 1):
            out[key] = out.get(key, 0.0) + 1.0 / (RRF_K + pos)
    return out
