"""Asking the archives: words always, vectors when the archive has some made by the embedder in use, the two rankings merged by reciprocal rank."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.rag import search as ranking
from tadamoo.rag.embed import Embedder, embed_all

POOL = 5                          # candidates taken from each ranking, as a multiple of the answers wanted


def _hit(archive: Any, i: int, lexical: Optional[float], semantic: Optional[float], score: float, mode: str) -> Dict[str, Any]:
    chunk = archive.chunks[i]
    doc = archive.document(chunk["doc"])
    return {"archive_id": archive.meta["id"], "archive": archive.meta["name"], "doc_id": chunk["doc"], "document": doc["name"], "chunk": chunk["n"],
            "text": chunk["text"], "score": round(score, 4), "lexical": None if lexical is None else round(lexical, 4),
            "semantic": None if semantic is None else round(semantic, 4), "mode": mode}


def run(archives: Sequence[Any], embedder: Optional[Embedder], query: str, top_k: int, min_share: float = 0.0) -> Dict[str, Any]:
    """{"mode", "note", "hits"}: `mode` is `hybrid` when vectors took part and `lexical` when only words did. A hit's `score` is the BM25 score
    (words only) or the merged rank score (hybrid); it compares passages within one answer, not between answers."""
    out: Dict[str, Any] = {"mode": "lexical", "note": "", "hits": []}
    if not query or not archives:
        return out
    indexes = [a.index() for a in archives]
    lex = ranking.bm25(indexes, query)
    floor = min_share * ranking.information(indexes, query)             # the least a words-only hit may score
    pool = max(top_k * POOL, 20)
    lex_rank: List[Tuple[int, int]] = _ordered(lex)[:pool]
    dense: Dict[Tuple[int, int], float] = {}
    usable = [k for k, a in enumerate(archives) if a.vectors is not None and embedder is not None and a.meta["embed_model"] == embedder.model]
    if usable:
        try:
            q = embed_all(embedder, [query])[0]
            for k in usable:
                if archives[k].vectors.shape[1] != q.shape[0]:
                    continue
                for i, c in enumerate(ranking.cosine(archives[k].vectors, q)):
                    if c >= ranking.MIN_COSINE:
                        dense[(k, i)] = float(c)
        except Exception as e:
            dense = {}
            out["note"] = f"text search only: the embedder failed ({str(e)[:120]})"
    if not dense:
        for k, i in lex_rank:
            score = lex[k][i]
            if score <= 0 or score < floor:
                continue
            out["hits"].append(_hit(archives[k], i, score, None, score, "lexical"))
            if len(out["hits"]) == top_k:
                break
        return out
    dense_rank = [key for key, _ in sorted(dense.items(), key=lambda kv: (-kv[1], kv[0]))][:pool]
    fused = ranking.fuse([lex_rank, dense_rank])
    out["mode"] = "hybrid"
    for (k, i), score in sorted(fused.items(), key=lambda kv: (-kv[1], kv[0])):
        lexical, semantic = lex[k].get(i), dense.get((k, i))
        if semantic is None and (lexical is None or lexical < floor):
            continue
        out["hits"].append(_hit(archives[k], i, lexical, semantic, score, "hybrid" if k in usable else "lexical"))
        if len(out["hits"]) == top_k:
            break
    return out


def _ordered(scores: List[Dict[int, float]]) -> List[Tuple[int, int]]:
    flat = [(k, i, s) for k, per in enumerate(scores) for i, s in per.items()]
    return [(k, i) for k, i, _ in sorted(flat, key=lambda t: (-t[2], t[0], t[1]))]
