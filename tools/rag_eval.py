"""Measures how well the knowledge archives find the right passage for a question: words (BM25), meaning (cosine), the two merged by rank fusion,
meaning with the small word share the search uses, and the whole search (with the reranker when there is one).

  python tools/rag_eval.py --archive <archive id> --questions questions.json [--rag-dir DIR] [--url http://127.0.0.1:7242] [--json report.json]
  python tools/rag_eval.py --archive <id> --questions q.json --fake            # a deterministic stand-in embedder and reranker: no model, no GPU (for CI)

The questions file is {"questions": [{"passage": P, "question": "..."}]} where P names the right passage of the archive: a number (its position in the
archive's passage list), "<document id>:<passage number>", or text the passage holds. A question whose passage is not found is listed and skipped. The
archive's own vectors are used (made by the embedder of the settings, so index the archive first); with --fake the passages are embedded in memory and the
archive is not touched. A real run borrows the GPU from the server at --url like the program does and needs the server to be running.

For each method it prints Recall at 1, 5 and 10 and the mean reciprocal rank, over all questions and by the language of the passage (a guess from the
script and the stop words: en, fr, zh, other), and the time per question."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tadamoo.rag import search as ranking  # noqa: E402
from tadamoo.rag.embed import normalize  # noqa: E402
from tadamoo.rag.query import RETRIEVAL, run  # noqa: E402
from tadamoo.rag.store import Archive, RagStore, searchable  # noqa: E402

KS = (1, 5, 10)
FUSE_DEPTH = 50
EN = {"the", "of", "and", "to", "in", "is", "that", "for", "with", "as", "by", "an", "are", "this", "which"}
FR = {"le", "la", "les", "de", "des", "du", "et", "un", "une", "est", "que", "pour", "dans", "par", "sur", "au", "aux", "ce"}
CJK = re.compile(r"[㐀-鿿]")
WORD = re.compile(r"[a-z]+")


def guess_language(text: str) -> str:
    """`zh` for text that is mostly Chinese characters, `en` or `fr` for Latin text that has more of that language's stop words, else `other`."""
    letters = [c for c in text if c.isalpha()]
    if letters and len(CJK.findall(text)) > 0.3 * len(letters):
        return "zh"
    words = WORD.findall(text.lower())
    en, fr = sum(w in EN for w in words), sum(w in FR for w in words)
    return "en" if en > fr and en >= 3 else "fr" if fr > en and fr >= 3 else "other"


def metrics(ranks: Sequence[Optional[int]]) -> Dict[str, float]:
    """Recall at 1, 5 and 10 and the mean reciprocal rank of 0-based ranks (None: not found; a rank of 50 or more counts as not found for the MRR)."""
    n = len(ranks)
    if not n:
        return {"n": 0, **{f"R@{k}": 0.0 for k in KS}, "MRR": 0.0}
    out: Dict[str, float] = {"n": n}
    for k in KS:
        out[f"R@{k}"] = round(sum(r is not None and r < k for r in ranks) / n, 3)
    out["MRR"] = round(sum(1.0 / (r + 1) for r in ranks if r is not None and r < FUSE_DEPTH) / n, 3)
    return out


def rank_of(scores: np.ndarray, target: int, floor: Optional[float] = None) -> Optional[int]:
    """The 0-based rank of passage `target` when passages are ordered by `scores` (ties by position); None when its score is not above `floor`."""
    mine = scores[target]
    if floor is not None and not mine > floor:
        return None
    return int((scores > mine).sum() + (scores[:target] == mine).sum())


def resolve(archive: Archive, ref: Any) -> Optional[int]:
    """The position in the archive's passage list of the passage a question names, or None."""
    chunks = archive.chunks
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref if 0 <= ref < len(chunks) else None
    if not isinstance(ref, str) or not ref.strip():
        return None
    m = re.fullmatch(r"(d[0-9a-f]{8}):(\d+)", ref.strip())
    if m:
        return next((i for i, c in enumerate(chunks) if c["doc"] == m.group(1) and c["n"] == int(m.group(2))), None)
    flat = " ".join(ref.split())
    return next((i for i, c in enumerate(chunks) if flat and flat in " ".join(c["text"].split())), None)


class FakeEmbedder:
    """Deterministic and model-free: a text becomes its hashed words, so passages that share words are near; the reranker is the share of the question's
    words a passage holds."""
    model, version, device, batch = "fake-hash", "fake-1", "cpu", 64
    DIM = 256

    @staticmethod
    def _words(text: str) -> List[str]:
        return ranking.tokens(text)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.DIM), dtype=np.float32)
        for r, text in enumerate(texts):
            for w in self._words(text):
                out[r, int.from_bytes(hashlib.blake2b(w.encode("utf-8"), digest_size=4).digest(), "big") % self.DIM] += 1.0
        return out

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([text])[0]

    def rerank(self, query: str, passages: Sequence[str]) -> List[float]:
        q = set(self._words(query))
        return [len(q & set(self._words(p))) / max(1, len(q)) for p in passages]


def evaluate(archive: Archive, embedder: Any, questions: Sequence[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None, rerank: bool = True,
             clock: Callable[[], float] = time.perf_counter) -> Dict[str, Any]:
    """The report: for each method the metrics over all questions and by language, and the seconds per question. The archive must hold vectors of `embedder`."""
    cfg = {**RETRIEVAL, **(cfg or {}), "rerank": rerank}
    weight = float(cfg["lexical_weight"])
    index = archive.index()
    langs = [guess_language(c["text"]) for c in archive.chunks]
    rows: Dict[str, List[Optional[int]]] = {"bm25": [], "dense": [], "fused": [], "dense+words": [], "search": []}
    seconds = {k: 0.0 for k in rows}
    targets: List[int] = []
    skipped: List[str] = []
    for q in questions:
        target = resolve(archive, q.get("passage"))
        text = q.get("question")
        if target is None or not isinstance(text, str) or not text.strip():
            skipped.append(str(q.get("passage"))[:60])
            continue
        targets.append(target)
        t0 = clock()
        lex = np.zeros(len(archive.chunks), dtype=np.float32)
        for i, v in ranking.bm25([index], text)[0].items():
            lex[i] = v
        seconds["bm25"] += clock() - t0
        rows["bm25"].append(rank_of(lex, target, 0.0))
        t0 = clock()
        qv = normalize(np.asarray(embedder.embed_query(text), dtype=np.float32).reshape(1, -1))[0]
        cos = archive.vectors @ qv
        seconds["dense"] += clock() - t0
        rows["dense"].append(rank_of(cos, target))
        t0 = clock()
        top = lambda s: [(0, int(i)) for i in np.argsort(-s)[:FUSE_DEPTH]]                              # noqa: E731
        fused = ranking.fuse([top(lex), top(cos)])
        order = [i for (_, i), _ in sorted(fused.items(), key=lambda kv: -kv[1])]
        seconds["fused"] += clock() - t0
        rows["fused"].append(order.index(target) if target in order else None)
        t0 = clock()
        blend = cos + weight * lex / (lex.max() or 1.0)
        seconds["dense+words"] += clock() - t0
        rows["dense+words"].append(rank_of(blend, target))
        t0 = clock()
        got = run([archive], embedder, text, max(KS), 0.0, cfg)
        seconds["search"] += clock() - t0
        rows["search"].append(_position(archive, got, target))
    count = max(1, len(targets))
    report: Dict[str, Any] = {"questions": len(targets), "skipped": skipped, "settings": {k: cfg[k] for k in ("lexical_weight", "min_cosine", "min_rerank", "rerank")},
                              "methods": {}}
    for name, ranks in rows.items():
        by = {}
        for lang in sorted(set(langs[t] for t in targets)):
            sel = [r for r, t in zip(ranks, targets) if langs[t] == lang]
            by[lang] = metrics(sel)
        report["methods"][name] = {"all": metrics(ranks), "by_language": by, "ms_per_question": round(1000 * seconds[name] / count, 2)}
    return report


def _position(archive: Archive, got: Dict[str, Any], target: int) -> Optional[int]:
    """The rank of passage `target` among the hits of a search (by document and passage number), None when it was not given."""
    want = (archive.chunks[target]["doc"], archive.chunks[target]["n"])
    for pos, h in enumerate(got["hits"]):
        if (h["doc_id"], h["chunk"]) == want:
            return pos
    return None


def table(report: Dict[str, Any]) -> str:
    lines = [f"{report['questions']} questions" + (f", {len(report['skipped'])} skipped (passage not found)" if report["skipped"] else "")
             + "; settings " + ", ".join(f"{k}={v}" for k, v in report["settings"].items())]
    head = f"{'method':<14}{'group':<8}{'n':>5}{'R@1':>8}{'R@5':>8}{'R@10':>8}{'MRR':>8}{'ms/q':>9}"
    lines.append(head)
    for name, row in report["methods"].items():
        groups = [("all", row["all"])] + list(row["by_language"].items())
        for n, (group, m) in enumerate(groups):
            lines.append(f"{name if n == 0 else '':<14}{group:<8}{m['n']:>5}{m['R@1']:>8.3f}{m['R@5']:>8.3f}{m['R@10']:>8.3f}{m['MRR']:>8.3f}"
                         + (f"{row['ms_per_question']:>9.1f}" if n == 0 else ""))
    return "\n".join(lines)


def load_questions(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise SystemExit('the questions file is {"questions": [{"passage": ..., "question": ...}]}')
    return [r for r in rows if isinstance(r, dict)]


def real_embedder(store: RagStore, url: str) -> Any:
    from serve import models
    from tadamoo.rag.embed_client import EmbedService
    service = EmbedService(store.settings.read, models.dirs, lambda: url)
    emb = service.provider()
    if emb is None:
        raise SystemExit("no embedder: " + (service.info().get("message") or "turned off"))
    return emb


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive", required=True, help="the id of the archive")
    ap.add_argument("--questions", required=True, type=Path)
    ap.add_argument("--rag-dir", type=Path, default=None, help="the folder of the archives (default: rag/ in the user data folder)")
    ap.add_argument("--url", default="http://127.0.0.1:7242", help="the server that lends the GPU")
    ap.add_argument("--fake", action="store_true", help="a deterministic stand-in embedder and reranker")
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--weight", type=float, help="lexical_weight, instead of the setting")
    ap.add_argument("--min-cosine", type=float)
    ap.add_argument("--min-rerank", type=float)
    ap.add_argument("--json", type=Path, help="write the report here")
    args = ap.parse_args(argv)
    rag_dir = args.rag_dir
    if rag_dir is None:
        from serve import userdata
        rag_dir = userdata.path("rag")
    store = RagStore(rag_dir)
    archive = store.load(args.archive)
    cfg = {k: v for k, v in store.settings.read().items() if k in RETRIEVAL}
    for key, value in (("lexical_weight", args.weight), ("min_cosine", args.min_cosine), ("min_rerank", args.min_rerank)):
        if value is not None:
            cfg[key] = value
    if args.fake:
        emb: Any = FakeEmbedder()
        archive = Archive(archive.meta, archive.chunks, normalize(emb.embed([searchable(c) for c in archive.chunks])), archive.sections)
        archive.meta = {**archive.meta, "embed_model": emb.model, "embed_version": emb.version}
    else:
        emb = real_embedder(store, args.url)
        from tadamoo.rag.embed import compatible
        if archive.vectors is None or not compatible(archive.meta, emb):
            raise SystemExit("the archive has no vectors of the model in use: index it again in the Knowledge tab")
    report = evaluate(archive, emb, load_questions(args.questions), cfg, rerank=not args.no_rerank)
    print(table(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
