"""Asking the archives: words always, vectors when an archive has some made by the embedder in use.

With vectors a passage scores its cosine with the question plus a small share of its word score (`lexical_weight` times the BM25 score over the best BM25
score of the question): the words break ties and find a rare name, the vectors decide the order. Rank fusion of the two lists was measured to be worse
across languages and is not used. The best twenty are judged again by a reranker when there is one, then the first `top_k` are given, passages with the same
text counted once. A passage that is too far from the question (`min_cosine`, `min_rerank`) is not given at all. An archive with no vectors in the same
search is searched by words and comes after the others."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.rag import search as ranking
from tadamoo.rag.embed import Embedder, compatible, embed_query
from tadamoo.rag.ingest import meta as facts
from tadamoo.rag.store import searchable

POOL = 20                         # passages the reranker judges
KEEP = 100                        # candidates kept from one archive before the best are taken
NATIVE_FROM = 1000                # passages from which an archive's database does the word and vector search itself; below, the application does it (the same answer, no round trip)
NATIVE_K = 300                    # nearest vectors asked of the database (ten times as many when a filter narrows the passages)
WORDS_K = 1000                    # word hits asked of the database
RETRIEVAL = {"lexical_weight": 0.05, "min_cosine": 0.3, "min_rerank": 0.2, "rerank": True}
Candidate = Tuple[int, float, int, int, Optional[float], Optional[float]]            # (tier, -score, archive, passage, bm25, cosine)


def _fingerprint(text: str) -> bytes:
    return hashlib.blake2b(" ".join(text.lower().split()).encode("utf-8"), digest_size=12).digest()


def _native(archive: Any, what: str) -> bool:
    """Whether the database that holds `archive` answers its `what` search (`words` or `vectors`) itself."""
    backend = getattr(archive, "backend", None)
    return backend is not None and len(archive.chunks) >= NATIVE_FROM and bool(getattr(backend, "native_" + what, False))


def _native_cosines(archive: Any, q: np.ndarray, wide: bool) -> Optional[np.ndarray]:
    """The cosine of the passages the database finds nearest to `q` (-1 for the others), or None when the vectors are not of `q`'s size."""
    aid, slot = archive.meta["id"], getattr(archive, "slot", "main")
    note = archive.backend.vector_note(aid, slot)
    if note is None or note["dims"] != q.shape[0]:
        return None
    cos = np.full(len(archive.chunks), -1.0, dtype=np.float32)
    for i, c in archive.backend.vector_search(aid, q, NATIVE_K * (10 if wide else 1), slot):
        if 0 <= i < cos.shape[0]:
            cos[i] = c
    return cos


FIGURES_PER_HIT = 3


def _figures_of(archive: Any, chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The figures that complete a passage: the ones of the same document and section, never more than a few. A figure passage has none: it is the figure."""
    if chunk.get("kind") == "figure":
        return []
    out = []
    for c in archive.chunks:
        if c.get("kind") == "figure" and c["doc"] == chunk["doc"] and c.get("section") == chunk.get("section"):
            out.append({"fig": c.get("fig", 0), "label": c.get("figure", ""), "caption": c.get("caption", "")[:200]})
            if len(out) >= FIGURES_PER_HIT:
                break
    return out


def _hit(archive: Any, i: int, lexical: Optional[float], semantic: Optional[float], rerank: Optional[float], score: float) -> Dict[str, Any]:
    chunk = archive.chunks[i]
    doc = archive.document(chunk["doc"])
    stages = [name for name, v in (("lexical", lexical), ("dense", semantic), ("rerank", rerank)) if v is not None]
    return {"archive_id": archive.meta["id"], "archive": archive.meta["name"], "doc_id": chunk["doc"], "document": doc["name"], "chunk": chunk["n"],
            "text": chunk["text"], "path": chunk.get("path", ""), "page_start": chunk.get("page_start", 0), "page_end": chunk.get("page_end", 0),
            "kind": chunk.get("kind", "text"), "section": chunk.get("section", ""), "facts": chunk.get("facts") or {}, "summary": chunk.get("summary", ""),
            "figure": chunk.get("figure", ""), "caption": chunk.get("caption", ""), "image": chunk.get("image", ""), "thumb": chunk.get("thumb", ""), "fig": chunk.get("fig", 0),
            "questions": chunk.get("questions") or [], "flag": chunk.get("flag", ""), "figures": _figures_of(archive, chunk), "score": round(score, 4),
            "lexical": None if lexical is None else round(lexical, 4), "semantic": None if semantic is None else round(semantic, 4),
            "rerank": None if rerank is None else round(rerank, 4), "past": bool(doc.get("past")), "stages": stages, "mode": "hybrid" if semantic is not None else "lexical"}


def _said(note: str, more: str) -> str:
    return (note + " " + more) if note else more


def _mask(archive: Any, filters: Dict[str, Any]) -> np.ndarray:
    """Which passages of an archive pass `filters` (tadamoo/rag/ingest/meta.py): `document` is a document id (or a list of them), the rest are read from the
    facts kept beside each passage and from its document's own facts."""
    infos = {d["id"]: d.get("info") or {} for d in archive.meta["documents"]}
    only = filters.get("document")
    only = {only} if isinstance(only, str) else set(only or ())
    rest = {k: v for k, v in filters.items() if k != "document"}
    return np.array([(not only or c["doc"] in only) and facts.matches(c.get("facts") or {}, infos.get(c["doc"], {}), c.get("kind", "text"), rest) for c in archive.chunks], dtype=bool)


def run(archives: Sequence[Any], embedder: Optional[Embedder], query: str, top_k: int, min_share: float = 0.0, cfg: Optional[Dict[str, Any]] = None,
        filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """{"mode", "stages", "note", "reranked", "hits"}: `mode` is `hybrid` when vectors took part and `lexical` when only words did. A hit's `score` is the
    rerank score, else the cosine plus the word share (vectors), else the BM25 score; it compares passages within one answer, not between answers."""
    cfg = {**RETRIEVAL, **(cfg or {})}
    out: Dict[str, Any] = {"mode": "lexical", "stages": ["lexical"], "note": "", "reranked": False, "hits": []}
    if not query or not archives:
        return out
    if min_share <= 0 and all(_native(a, "words") for a in archives):
        lex = [dict(a.backend.word_search(a.meta["id"], query, WORDS_K) or []) for a in archives]
        floor = 0.0
    else:
        indexes = [a.index() for a in archives]
        lex = ranking.bm25(indexes, query)
        floor = min_share * ranking.information(indexes, query)                 # the least a words-only hit may score
    masks = [_mask(a, filters) if filters else None for a in archives]
    for k, m in enumerate(masks):
        if m is not None:
            lex[k] = {i: v for i, v in lex[k].items() if m[i]}
    top_lex = max((v for per in lex for v in per.values()), default=0.0)
    ready = [k for k, a in enumerate(archives) if a.has_vectors and compatible(a.meta, embedder)]
    scored: Dict[int, np.ndarray] = {}
    if ready:
        try:
            q = embed_query(embedder, query)
            for k in ready:
                a = archives[k]
                got = _native_cosines(a, q, bool(filters)) if _native(a, "vectors") else (ranking.cosine(a.vectors, q) if a.vectors.shape[1] == q.shape[0] else None)
                if got is not None:
                    scored[k] = got
            for k, m in enumerate(masks):
                if m is not None and k in scored:
                    scored[k] = np.where(m, scored[k], -1.0).astype(np.float32)
        except Exception as e:
            out["note"] = f"text search only: the embedder failed ({str(e)[:120]})"
    weight, near_enough = float(cfg["lexical_weight"]), float(cfg["min_cosine"])
    cands: List[Candidate] = []
    for k, cos in scored.items():
        total = cos.copy()
        for i, v in lex[k].items():
            total[i] += weight * v / top_lex
        near = np.flatnonzero(cos >= near_enough)
        if near.size > KEEP:
            near = near[np.argpartition(-total[near], KEEP)[:KEEP]]
        cands += [(0, -float(total[i]), k, int(i), lex[k].get(int(i)), float(cos[i])) for i in near]
        cands += [(0, -weight * v / top_lex, k, i, v, None) for i, v in lex[k].items() if cos[i] < near_enough and v > 0 and v >= floor]      # a word match the vectors do not see
    for k, per in enumerate(lex):
        if k not in scored:
            cands += [(1, -v, k, i, v, None) for i, v in per.items() if v > 0 and v >= floor]
    cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    if scored:
        out["mode"], out["stages"] = "hybrid", ["lexical", "dense"]
        if len(scored) < len(archives):
            out["note"] = _said(out["note"], "some archives have no vectors of this model and were searched by words: index them again")
    rerank = getattr(embedder, "rerank", None) if scored and cfg["rerank"] else None
    pool: List[Candidate] = []
    seen = set()
    for c in cands:
        key = _fingerprint(archives[c[2]].chunks[c[3]]["text"])
        if key in seen:
            continue
        seen.add(key)
        pool.append(c)
        if len(pool) == max(top_k, POOL if rerank is not None else 0):
            break
    judged: Dict[Tuple[int, int], float] = {}
    if rerank is not None and pool:
        got = None
        try:
            got = rerank(query, [searchable(archives[c[2]].chunks[c[3]]) for c in pool])
        except Exception as e:
            out["note"] = _said(out["note"], f"not reranked: the reranker failed ({str(e)[:120]})")
        if got is not None and len(got) == len(pool):
            judged = {(c[2], c[3]): float(s) for c, s in zip(pool, got)}
            pool = sorted((c for c in pool if judged[(c[2], c[3])] >= float(cfg["min_rerank"])), key=lambda c: -judged[(c[2], c[3])])
            out["reranked"] = True
            out["stages"].append("rerank")
        elif got is None and "reranker failed" not in out["note"]:
            out["note"] = _said(out["note"], "not reranked: no GPU was lent for the reranker")
    for tier, neg, k, i, lexical, semantic in pool[:top_k]:
        r = judged.get((k, i))
        out["hits"].append(_hit(archives[k], i, lexical, semantic, r, r if r is not None else (-neg if tier == 0 else lexical)))
    return out
