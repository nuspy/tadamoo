"""Verifying an archive: is what it holds whole, and does it still match what it was made from. Nothing is changed.

    structure   the passages in the file are the ones `meta.json` counts, each document's count agrees, no passage belongs to an unknown document, the vectors
                have a row for every passage, every document's saved reading can be read back
    coverage    each document's pages against its passages, recomputed (a record that disagrees with the passages is a problem), the pages that came out empty or
                were skipped, the passages flagged
    sources     what a check finds (changed, new, gone, unreachable) and the documents that have no source, so cannot be compared with an original
    deep        the passages are cut again from the saved readings and compared with the stored ones; the files of local sources are hashed and their PDF page counts
                counted again

Each problem has a level: `error` (something is missing or wrong), `warn` (look at it), `info`."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


from tadamoo.rag.errors import RagError
from tadamoo.rag.sources import coverage
from tadamoo.rag.sources.local import sha_of

LEVELS = ("error", "warn", "info")


def pdf_pages(path: Path) -> Optional[int]:
    """The page count of a PDF, or None when no reader for it is installed or the file cannot be read."""
    try:
        import pypdf  # type: ignore
        return len(pypdf.PdfReader(str(path)).pages)
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import fitz  # type: ignore
        with fitz.open(str(path)) as doc:
            return len(doc)
    except Exception:
        return None


def _core(chunks: List[Dict[str, Any]]) -> List[str]:
    return [hashlib.sha1((c.get("path", "") + "\n" + c["text"]).encode("utf-8")).hexdigest() for c in chunks]


def verify(sync: Any, aid: Any, deep: bool = False, sources: bool = True, progress: Optional[Callable[[Dict[str, Any]], None]] = None,
           cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    store = sync.store
    tell = progress or (lambda event: None)
    archive, folder = store.load(aid), store.folder(aid)
    meta = archive.meta
    problems: List[Dict[str, Any]] = []

    def add(level: str, text: str, doc: Optional[Dict[str, Any]] = None) -> None:
        problems.append({"level": level, "doc": doc["id"] if doc else "", "name": doc["name"] if doc else "", "text": text})

    tell({"event": "stage", "stage": "checking"})
    # structure
    try:
        lines = sum(1 for line in open(folder / "chunks.jsonl", encoding="utf-8") if line.strip())
    except OSError:
        lines = 0
    if lines != meta["chunks"]:
        add("error", f"the file holds {lines:,} passages and the archive counts {meta['chunks']:,}: an update was cut short; update or index again")
    known = {d["id"] for d in meta["documents"]}
    orphans = sum(1 for line in open(folder / "chunks.jsonl", encoding="utf-8") if line.strip() and json.loads(line).get("doc") not in known) if lines else 0
    if orphans:
        add("error", f"{orphans:,} passages belong to a document that is not in the archive")
    per_doc: Dict[str, List[Dict[str, Any]]] = {}
    for c in archive.chunks:
        per_doc.setdefault(c["doc"], []).append(c)
    if meta.get("embed_model"):
        if archive.vectors is None:
            add("error", "the archive says it has vectors but they are missing or out of step with the passages: index it again")
        elif archive.vectors.shape[0] != len(archive.chunks):
            add("error", "the vectors and the passages are not the same number: index it again")
    blocks: Dict[str, List[Dict[str, Any]]] = {}
    for doc in meta["documents"]:
        if cancel is not None and cancel.is_set():
            raise RagError("the verify was cancelled", "cancelled")
        mine = per_doc.get(doc["id"], [])
        if len(mine) != doc.get("chunks"):
            add("error", f"the document counts {doc.get('chunks')} passages and the archive holds {len(mine)}", doc)
        try:
            blocks[doc["id"]] = store.blocks_of(aid, doc)
        except RagError:
            add("error", "the saved reading of this document is missing or damaged: add or update it again", doc)
        # coverage, recomputed from the passages
        fresh = coverage.of_document(doc, mine)
        stored = doc.get("coverage")
        if isinstance(stored, dict) and (stored.get("passages") != fresh["passages"] or stored.get("covered") != fresh["covered"]):
            add("error", "its coverage record does not match its passages: update it again", doc)
        for text in fresh["problems"]:
            add("error" if fresh["status"] == coverage.INCOMPLETE else "warn", text, doc)
        if fresh["flagged"]:
            add("info", f"{fresh['flagged']} passage{'s' if fresh['flagged'] > 1 else ''} read as instructions to an assistant (kept as data, marked)", doc)
    # sources
    data = sync.sources(aid)
    out_of_date = 0
    if sources and data["sources"]:
        tell({"event": "stage", "stage": "comparing"})
        items, errors, _ = sync._probe(aid, None, cancel)
        for it in items:
            if it["state"] == "unchanged":
                continue
            level = {"changed": "warn", "new": "info", "gone": "warn", "error": "error", "pinned": "info"}.get(it["state"], "info")
            doc = next((d for d in meta["documents"] if d["id"] == it["doc"]), None)
            if it["state"] in ("changed", "gone") and not it.get("acknowledged"):
                out_of_date += 1
            add(level, {"changed": "the source has a newer version (" + str(it.get("detail", "")) + ")", "new": "the source has an item the archive does not hold",
                        "gone": "the source no longer has this document (" + str(it.get("detail", "")) + ")", "error": "the source could not be read: " + str(it.get("detail", "")),
                        "pinned": "held at an older version on purpose"}.get(it["state"], it["state"]), doc or {"id": "", "name": it["name"]})
        for e in errors:
            if not any(it["state"] == "error" and it["source"] == e["source"] for it in items):
                add("error", f"{e['label']}: {e['error']}")
    unlinked = [d for d in meta["documents"] if d["id"] not in data["links"]]
    if unlinked and data["sources"]:
        add("info", f"{len(unlinked)} document{'s' if len(unlinked) > 1 else ''} came from no source: nothing to compare {'them' if len(unlinked) > 1 else 'it'} with")
    # deep
    if deep:
        tell({"event": "stage", "stage": "recutting"})
        same_cut = meta.get("ingest") == store.ingest()
        for doc in meta["documents"]:
            if cancel is not None and cancel.is_set():
                raise RagError("the verify was cancelled", "cancelled")
            if same_cut and doc["id"] in blocks:
                try:
                    made, _, _ = store.passages_of(doc["id"], doc["name"], blocks[doc["id"]])
                except RagError as e:
                    add("error", f"the saved reading no longer gives passages: {e}", doc)
                    continue
                if _core(made) != _core(per_doc.get(doc["id"], [])):
                    add("error", "its passages are not what its saved reading gives: index the archive again", doc)
            link = data["links"].get(doc["id"])
            src = next((s for s in data["sources"] if link and s["id"] == link["source"]), None)
            if src and src["kind"] in ("file", "folder"):
                path = Path(src["config"]["path"]) / link["key"] if src["kind"] == "folder" else Path(src["config"]["path"])
                if not path.is_file():
                    continue
                if sha_of(path) != doc.get("sha256"):
                    add("warn", "the file on disk is not the one that was read (its content differs)", doc)
                elif path.suffix.lower() == ".pdf" and doc.get("pages"):
                    pages = pdf_pages(path)
                    if pages is not None and pages != doc["pages"]:
                        add("error", f"the PDF has {pages} pages and the reading found {doc['pages']}", doc)
    problems.sort(key=lambda p: LEVELS.index(p["level"]))
    cov = coverage.of_archive([{**d, "coverage": coverage.of_document(d, per_doc.get(d["id"], []))} for d in meta["documents"]], [])     # recomputed: an older document has no record
    errors_n = sum(1 for p in problems if p["level"] == "error")
    return {"archive": aid, "name": meta["name"], "ok": errors_n == 0, "errors": errors_n, "warnings": sum(1 for p in problems if p["level"] == "warn"), "problems": problems[:200],
            "documents": len(meta["documents"]), "passages": len(archive.chunks), "coverage": cov, "out_of_date": out_of_date, "deep": deep, "sources": len(data["sources"])}
