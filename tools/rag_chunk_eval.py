"""Compares ways of cutting the documents of an archive into passages, on the same questions, the same embedder and reranker, the same fusion settings.

  python tools/rag_chunk_eval.py --archive <id> --questions q.json --variants flat,struct-350-600,sem-free [--facts f.json --plans p.json] [--rerank flat,struct-350-600]
                                 [--cache DIR] [--url http://127.0.0.1:7232] [--json out.json]

The archive is only read (its saved blocks and passages); every variant is built in memory, its vectors are kept in --cache (a text is embedded once, whichever
variant holds it). The questions are those of tools/rag_eval.py (`{"passage": "<doc id>:<n>", "question": ...}`); the evidence of a question is the text of the
passage it names in the archive, so a variant is judged by whether its own passages carry that text, not by a passage number: a passage `holds` the evidence
when it contains at least `HOLD` of the evidence's text (pieces of 30 characters), `part` at least `PART`. A question is `split` in a variant when no single passage holds it.

Variants: `flat` (1000 characters, 150 overlap, the first chunking), `struct-<target>-<hard>` (tokens; the structural cut), `sem-valleys` (structural 350/600 with
the semantic splitter inside long paragraphs), `sem-valleys-merge`, `sem-free` (cut at valleys with no size target), `sem-free-nomerge`, `struct-merge`
(structural 350/600 and small neighbours that are close in meaning joined), `parent-nbr` (match 250/400 passages, give the passage and its neighbours),
`parent-section` (match 250/400 passages, give their section). For a parent variant a hit is the text handed to the model, and several hits with the same parent count once.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tadamoo.rag.embed import normalize as unit_rows  # noqa: E402
from tadamoo.rag.ingest import chunker, normalize, semantic  # noqa: E402
from tadamoo.rag.language import guess_language  # noqa: E402
from tadamoo.rag.query import RETRIEVAL, run  # noqa: E402
from tadamoo.rag.store import Archive, RagStore, searchable  # noqa: E402

HOLD, PART = 0.8, 0.3
KS = (1, 5, 10)
CJK = re.compile(r"[㐀-鿿]")
PARENT_CHARS = 8000
STRUCT = re.compile(r"^struct-(\d+)-(\d+)$")


_NORM = re.compile(r"[^\w]+")
_WINDOW, _STRIDE = 30, 15
_norms: Dict[str, str] = {}


def norm(text: str) -> str:
    """A text as letters and digits only, lower case: the same text cut or joined in another way reads the same."""
    got = _norms.get(text)
    if got is None:
        got = _norms[text] = _NORM.sub("", text.lower())
    return got


def windows(text: str) -> List[str]:
    """Pieces of 30 characters of the normalized text, every 15: a passage holds the evidence when it holds its pieces (a passage that shares only common words holds none)."""
    n = norm(text)
    return [n[i:i + _WINDOW] for i in range(0, max(1, len(n) - _WINDOW + 1), _STRIDE)]


# -- the first chunking -------------------------------------------------------------------------------------------------------------

def flat_chunks(text: str, size: int = 1000, overlap: int = 150) -> List[str]:
    """Paragraphs gathered to about `size` characters, each passage starting with the tail of the one before."""
    units: List[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n", text.strip())):
        while len(para) > size:
            window = para[:size]
            ends = [m.end() for m in re.finditer(r"[.!?;:]\s", window)]
            space = window.rfind(" ")
            cut = ends[-1] if ends and ends[-1] > size // 2 else (space + 1 if space > size // 2 else size)
            units.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            units.append(para)
    out, cur = [], ""
    for unit in units:
        if cur and len(cur) + len(unit) + 2 > size:
            out.append(cur)
            tail = cur[-overlap:]
            space = tail.find(" ")
            tail = tail[space + 1:] if 0 <= space < len(tail) - 1 else tail
            cur = (tail + "\n\n" + unit) if tail else unit
        else:
            cur = (cur + "\n\n" + unit) if cur else unit
    if cur:
        out.append(cur)
    return out


# -- the embedder with a memory --------------------------------------------------------------------------------------------------------

class Memory:
    """Vectors by the hash of the text (kept on disk), the time the real embedder took, and the same for questions; the reranker passes through."""

    def __init__(self, emb: Any, folder: Path) -> None:
        self.emb, self.folder = emb, folder
        folder.mkdir(parents=True, exist_ok=True)
        self.model, self.version = emb.model, getattr(emb, "version", "")
        self.file = folder / "vectors.npz"
        self.vectors: Dict[str, np.ndarray] = {}
        self.stats: Dict[str, float] = {"seconds": 0.0, "texts": 0, "chars": 0}
        if self.file.is_file():
            data = np.load(self.file, allow_pickle=False)
            self.vectors = {k: data[k] for k in data.files}

    @staticmethod
    def key(text: str) -> str:
        return hashlib.blake2b(text.encode("utf-8"), digest_size=10).hexdigest()

    def embed(self, texts: Sequence[str], label: str = "") -> np.ndarray:
        missing = [t for t in dict.fromkeys(texts) if self.key(t) not in self.vectors]
        t0 = time.perf_counter()
        for i in range(0, len(missing), 256):
            part = missing[i:i + 256]
            got = np.asarray(self.emb.embed(part), dtype=np.float32)
            for t, v in zip(part, unit_rows(got)):
                self.vectors[self.key(t)] = v.astype(np.float16)
            np.savez(self.file, **self.vectors)
            print(f"  embedded {min(i + 256, len(missing))}/{len(missing)} for {label}", flush=True)
        self.stats["seconds"] += time.perf_counter() - t0
        self.stats["texts"] += len(missing)
        self.stats["chars"] += sum(len(t) for t in missing)
        return np.vstack([self.vectors[self.key(t)].astype(np.float32) for t in texts]) if texts else np.zeros((0, 1), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        key = "q:" + self.key(text)
        if key not in self.vectors:
            self.vectors[key] = unit_rows(np.asarray(self.emb.embed_query(text), dtype=np.float32).reshape(1, -1))[0].astype(np.float16)
        return self.vectors[key].astype(np.float32)

    def rerank(self, query: str, passages: Sequence[str]) -> List[float]:
        return self.emb.rerank(query, passages)


# -- the blocks and the variants -----------------------------------------------------------------------------------------------------------

def load_blocks(folder: Path) -> Tuple[Dict[str, str], Dict[str, List[Dict[str, Any]]]]:
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    names = {d["id"]: d["name"] for d in meta["documents"]}
    blocks = {}
    for doc in names:
        raw = json.loads(gzip.decompress((folder / "blocks" / f"{doc}.json.gz").read_bytes()).decode("utf-8"))
        blocks[doc], _ = normalize.normalize_blocks(raw)
    return names, blocks


def rows_of(doc: str, built: Dict[str, Any], start: int = 0) -> List[Dict[str, Any]]:
    return [{"doc": doc, "n": start + n, "text": c["text"], "path": c["path"], "page_start": c["page_start"], "page_end": c["page_end"], "kind": c["kind"],
             "section": f"{doc}.{c['section']}"} for n, c in enumerate(built["chunks"])]


def merge_close(chunks: List[Dict[str, Any]], vectors: np.ndarray, soft_min: int = 120, tau: float = 0.55, hard: int = 600) -> List[Dict[str, Any]]:
    """Adjacent passages of one section, one of them under `soft_min` tokens, joined when their vectors are close (cosine at least `tau`) and the sum is under `hard`."""
    out: List[Dict[str, Any]] = []
    sums: List[np.ndarray] = []
    for c, v in zip(chunks, vectors):
        if out and out[-1]["section"] == c["section"] and out[-1]["kind"] == c["kind"] == "text" and (chunker.tokens(c["text"]) < soft_min or chunker.tokens(out[-1]["text"]) < soft_min) \
                and chunker.tokens(out[-1]["text"]) + chunker.tokens(c["text"]) <= hard and float(sums[-1] @ v / (np.linalg.norm(sums[-1]) * np.linalg.norm(v) or 1.0)) >= tau:
            out[-1] = {**out[-1], "text": out[-1]["text"] + "\n\n" + c["text"], "page_end": max(out[-1]["page_end"], c["page_end"])}
            sums[-1] = sums[-1] + v
        else:
            out.append(dict(c))
            sums.append(v.copy())
    for n, c in enumerate(out):
        c["n"] = n
    return out


def build(name: str, names: Dict[str, str], blocks: Dict[str, List[Dict[str, Any]]], memory: Memory) -> Dict[str, Any]:
    """`{chunks, sections, parents}` of a variant; `parents` maps a passage index to the text handed to the model when it differs from the passage."""
    t0 = time.perf_counter()
    chunks: List[Dict[str, Any]] = []
    sections: Dict[str, str] = {}
    embed = lambda texts: memory.embed(texts, name)             # noqa: E731
    for doc, bl in blocks.items():
        title = Path(names[doc]).stem
        if name == "flat":
            text = "\n\n".join(b["text"] for b in bl if b.get("type") != "page_break" and b.get("text"))
            marks = []
            pos = 0
            for b in bl:
                if b.get("type") != "page_break" and b.get("text"):
                    marks.append((pos, int(b.get("page") or 0)))
                    pos += len(b["text"]) + 2
            at = 0
            for n, c in enumerate(flat_chunks(text)):
                found = text.find(c[:80], max(0, at - 300))
                found = found if found >= 0 else at
                at = found
                page = [p for q, p in marks if q <= found][-1:] or [0]
                chunks.append({"doc": doc, "n": n, "text": c, "path": title, "page_start": page[0], "page_end": page[0], "kind": "text", "section": f"{doc}.s0"})
            continue
        base = name
        if name in ("parent-nbr", "parent-section"):
            base = "struct-250-400"
        m = STRUCT.match(base)
        if m:
            built = chunker.chunk_blocks(bl, title, target=int(m.group(1)), hard=int(m.group(2)))
        elif base in ("sem-valleys", "sem-valleys-merge"):
            built = chunker.chunk_blocks(bl, title, valleys=semantic.sentence_valleys(embed))
        elif base == "struct-merge":
            built = chunker.chunk_blocks(bl, title)
        elif base in ("sem-free", "sem-free-nomerge"):
            built = semantic.semantic_chunks(bl, title, embed, **({"merge_tau": 2.0} if base == "sem-free-nomerge" else {}))
        else:
            raise SystemExit(f"unknown variant {name}")
        rows = rows_of(doc, built)
        if base in ("sem-valleys-merge", "struct-merge"):
            rows = merge_close(rows, memory.embed([searchable(c) for c in rows], name))
        chunks += rows
        sections.update({f"{doc}.{s['id']}": s["text"] for s in built["sections"]})
    for i, c in enumerate(chunks):
        c["i"] = i
    parents: Dict[int, str] = {}
    if name == "parent-nbr":
        for i, c in enumerate(chunks):
            near = [x["text"] for x in chunks[max(0, i - 1):i + 2] if x["doc"] == c["doc"]]
            parents[i] = "\n\n".join(near)
    elif name == "parent-section":
        for i, c in enumerate(chunks):
            parents[i] = sections.get(c["section"], c["text"])[:PARENT_CHARS]
    return {"chunks": chunks, "parents": parents, "build_s": time.perf_counter() - t0}


# -- the questions ------------------------------------------------------------------------------------------------------------------------

def evidence_of(store: RagStore, aid: str, questions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The questions with the text of the passage each names in the archive, its document and its language."""
    archive = store.load(aid)
    by_key = {(c["doc"], c["n"]): c for c in archive.chunks}
    out = []
    for q in questions:
        doc, _, n = str(q["passage"]).partition(":")
        c = by_key.get((doc, int(n))) if n.isdigit() else None
        if c is not None and q.get("question"):
            out.append({"doc": doc, "question": q["question"], "text": c["text"], "windows": windows(c["text"]), "lang": guess_language(c["text"])})
    return out


def coverage(ev: Dict[str, Any], text: str) -> float:
    have = norm(text)
    return sum(w in have for w in ev["windows"]) / max(1, len(ev["windows"]))


def evaluate(variant: Dict[str, Any], names: Dict[str, str], memory: Memory, evidence: Sequence[Dict[str, Any]], cfg: Dict[str, Any], rerank: bool,
             facts: Optional[Sequence[Dict[str, Any]]] = None, oracle: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    chunks = variant["chunks"]
    parents = variant["parents"]
    docs = [{"id": d, "name": n} for d, n in names.items()]
    vectors = memory.embed([searchable(c) for c in chunks], "evaluate")
    archive = Archive({"id": "x", "name": "x", "documents": docs, "embed_model": memory.model, "embed_version": memory.version}, chunks, vectors, {})
    cfg = {**cfg, "rerank": rerank}
    index = {(c["doc"], c["n"]): c["i"] for c in chunks}
    ranks: List[Optional[int]] = []
    split = 0
    seconds = 0.0
    for ev in evidence:
        mine = [c for c in chunks if c["doc"] == ev["doc"]]
        split += not any(coverage(ev, parents.get(c["i"], c["text"])) >= HOLD for c in mine)
        t0 = time.perf_counter()
        got = run([archive], memory, ev["question"], 10, 0.0, cfg)["hits"]
        seconds += time.perf_counter() - t0
        seen, rank = set(), None
        for h in got:
            text = parents.get(index[(h["doc_id"], h["chunk"])], h["text"])
            if text in seen:
                continue
            seen.add(text)
            if h["doc_id"] == ev["doc"] and coverage(ev, text) >= PART:
                rank = len(seen) - 1
                break
        ranks.append(rank)
    out: Dict[str, Any] = {"passages": len(chunks), "mean_tokens": round(statistics.mean(chunker.tokens(c["text"]) for c in chunks)), "max_tokens": max(chunker.tokens(c["text"]) for c in chunks),
                           "split": split, "questions": len(evidence), "seconds_per_question": round(seconds / max(1, len(evidence)), 2), "build_s": round(variant["build_s"], 1)}
    for label, sel in (("all", range(len(evidence))), *[(lang, [i for i, e in enumerate(evidence) if e["lang"] == lang]) for lang in ("en", "fr", "zh")]):
        r = [ranks[i] for i in sel]
        n = max(1, len(r))
        out[label] = {"n": len(r), **{f"R@{k}": round(sum(x is not None and x < k for x in r) / n, 3) for k in KS}, "MRR": round(sum(1 / (x + 1) for x in r if x is not None) / n, 3)}
    if facts and oracle:
        reached = {}
        for f in facts:
            if f["id"] not in oracle:
                continue
            hits = run([archive], memory, oracle[f["id"]], 5, 0.0, cfg)["hits"]
            reached[f["id"]] = any(h["document"].lower().startswith(str(f["doc"]).lower()) and any(h["page_start"] <= p <= max(h["page_end"], h["page_start"]) for p in f["true_pages"]) for h in hits)
        out["facts"] = reached
    return out


def table(report: Dict[str, Dict[str, Any]]) -> str:
    head = f"{'variant':<20}{'rr':<4}{'n':>6}{'mean':>6}{'max':>6}{'split':>6}{'R@1':>7}{'R@5':>7}{'R@10':>7}{'MRR':>7}{'en R@5':>8}{'fr R@5':>8}{'zh R@5':>8}{'build':>7}{'F11':>5}{'F13':>5}{'F14':>5}"
    lines = [head]
    for name, r in report.items():
        f = r.get("facts", {})
        lines.append(f"{name.split('|')[0]:<20}{('yes' if name.endswith('|rerank') else 'no'):<4}{r['passages']:>6}{r['mean_tokens']:>6}{r['max_tokens']:>6}{r['split']:>6}"
                     f"{r['all']['R@1']:>7.3f}{r['all']['R@5']:>7.3f}{r['all']['R@10']:>7.3f}{r['all']['MRR']:>7.3f}"
                     f"{r['en']['R@5']:>8.3f}{r['fr']['R@5']:>8.3f}{r['zh']['R@5']:>8.3f}{r['build_s']:>7.1f}"
                     + "".join(f"{('yes' if f.get(k) else '-') if k in f else '.':>5}" for k in ("F11", "F13", "F14")))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive", required=True)
    ap.add_argument("--questions", required=True, type=Path)
    ap.add_argument("--variants", required=True)
    ap.add_argument("--rerank", default="", help="variants that are also run with the reranker")
    ap.add_argument("--facts", type=Path)
    ap.add_argument("--plans", type=Path)
    ap.add_argument("--rag-dir", type=Path)
    ap.add_argument("--cache", type=Path, default=Path("rag_chunk_cache"))
    ap.add_argument("--url", default="http://127.0.0.1:7232")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    rag_dir = args.rag_dir
    if rag_dir is None:
        from serve import userdata
        rag_dir = userdata.path("rag")
    store = RagStore(rag_dir)
    sys.path.insert(0, str(Path(__file__).parent))
    import rag_eval
    memory = Memory(rag_eval.real_embedder(store, args.url), args.cache)
    names, blocks = load_blocks(Path(rag_dir) / args.archive)
    q = json.loads(args.questions.read_text(encoding="utf-8"))
    evidence = evidence_of(store, args.archive, q["questions"] if isinstance(q, dict) else q)
    cfg = {k: v for k, v in store.settings.read().items() if k in RETRIEVAL}
    facts = json.loads(args.facts.read_text(encoding="utf-8")) if args.facts else None
    oracle = (json.loads(args.plans.read_text(encoding="utf-8")).get("oracle") if args.plans else None)
    also = set(filter(None, args.rerank.split(",")))
    report: Dict[str, Dict[str, Any]] = {}
    for name in [v for v in args.variants.split(",") if v]:
        print(f"building {name}", flush=True)
        variant = build(name, names, blocks, memory)
        for rr in ((False, True) if name in also else (False,)):
            t0 = time.perf_counter()
            res = evaluate(variant, names, memory, evidence, cfg, rr, facts, oracle)
            res["eval_s"] = round(time.perf_counter() - t0, 1)
            report[name + ("|rerank" if rr else "")] = res
            print(table({name + ("|rerank" if rr else ""): res}).splitlines()[-1], flush=True)
    print()
    print(table(report))
    print(f"embedding: {memory.stats['texts']} texts, {memory.stats['chars']:,} characters, {memory.stats['seconds']:.0f} s in this run")
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
