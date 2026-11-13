"""What the model does with an archive once it knows what is in it: look a fact up (`search`, one or several queries, in one document or all) and read the pages
around what it found, or the outline of a long document (`read`). The answers are plain dicts for the tools in tadamoo/rag/tools.py; the passages and the pages
are text written by someone else, so the tools that carry them are marked untrusted and the flagged ones say so."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tadamoo.rag import card
from tadamoo.rag.ingest import meta as facts
from tadamoo.rag.errors import RagError
from tadamoo.rag.match import find
from tadamoo.rag.recall import ProjectRecall, citation, shown, spread
from tadamoo.rag.store import FLAG_NOTE, LIMITS, RagStore

TOOL_CHARS = 12000                 # the passages of one search hold at most this much text
LIMIT = 8                          # passages given when the caller does not say
PER_QUERY = 5                      # passages kept from each query of a search with several
MAX_QUERIES = 6
QUERY_CHARS = 300
OUTLINE_SHOWN = 80                 # sections an outline names; the rest is counted
READ_CHARS = 20000


def _candidates(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def scope(store: RagStore, recall: ProjectRecall, archive: str) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """The archive ids a lookup covers (the named archive, else the conversation's), or an answer that says why not. The archives the route may not carry are left out."""
    if archive:
        found = find(store.list(), archive)
        if found["status"] == "many":
            return [], {"status": "ambiguous", "candidates": [{"id": a["id"], "name": a["name"]} for a in found["candidates"]], "note": "several archives match: nothing was searched; ask the person which one"}
        if found["status"] == "none":
            return [], {"status": "none", "note": "no archive has that name: call knowledge_list"}
        ids = [found["archive"]["id"]]
    else:
        ids = recall.ids()
    return recall.permit(ids), None


def pick_document(store: RagStore, ids: Sequence[str], name: str) -> Tuple[Optional[Tuple[str, Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """`((archive id, document row), None)` for the document `name` names among the archives `ids`, or `(None, answer)` when there is none or several."""
    rows = []
    for aid in ids:
        try:
            rows += [{"id": f"{aid}|{d['id']}", "name": d["name"], "description": "", "doc": d, "aid": aid} for d in store.documents(aid)]
        except RagError:
            continue
    found = find(rows, name)
    if found["status"] == "many":
        return None, {"status": "ambiguous", "candidates": _candidates(found["candidates"]), "note": "several documents match: nothing was done; use a longer part of the name"}
    if found["status"] == "none":
        return None, {"status": "none", "note": "no document has that name in the archives searched: knowledge_list names the archives and the system note names their documents"}
    r = found["archive"]
    return (r["aid"], r["doc"]), None


def _queries(query: Any, queries: Any) -> List[str]:
    out: List[str] = []
    for q in ([query] if isinstance(query, str) else []) + (list(queries) if isinstance(queries, list) else []):
        if isinstance(q, str) and q.strip() and q.strip()[:QUERY_CHARS] not in out:
            out.append(q.strip()[:QUERY_CHARS])
    return out[:MAX_QUERIES]


def search(store: RagStore, recall: ProjectRecall, query: Any = "", queries: Any = None, archive: str = "", document: str = "", expand: bool = False,
           limit: Any = LIMIT, filters: Any = None) -> Dict[str, Any]:
    """`{status, searched, passages: [{cite, archive, document, page, section, score, text, for?, flagged?}], mode, note?}`."""
    asked = _queries(query, queries)
    if not asked:
        return {"status": "error", "note": "query: the words to look for (or queries: a list of them, one for each thing you need)"}
    ids, refused = scope(store, recall, archive if isinstance(archive, str) else "")
    if refused:
        return refused
    try:
        narrow = facts.clean_filters(filters)
    except RagError as e:
        return {"status": "error", "note": str(e)}
    if isinstance(document, str) and document.strip():
        only, refused = pick_document(store, ids, document)
        if refused:
            return refused
        ids, narrow = [only[0]], {**narrow, "document": only[1]["id"]}
    wanted = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1 else (LIMIT if len(asked) == 1 else PER_QUERY)       # passages for each query
    wanted = min(wanted, LIMITS["top_k"])
    found: List[List[Dict[str, Any]]] = []
    mode, notes = "lexical", []
    for q in asked:
        try:
            got = store.search(ids, q, top_k=wanted, strict=False, filters=narrow) if ids else {"hits": [], "mode": "lexical", "note": ""}
        except RagError as e:
            return {"status": "error", "note": str(e)}
        found.append(got["hits"])
        mode = "hybrid" if got.get("mode") == "hybrid" else mode
        if got.get("note") and got["note"] not in notes:
            notes.append(got["note"])
    drop = store.settings.read()["flagged"] == "drop"
    out: List[Dict[str, Any]] = []
    used = 0
    seen_chunks, seen_sections = set(), set()
    owner = {id(h): q for q, hits in zip(asked, found) for h in hits}
    for h in spread(found):
        key = (h["archive_id"], h["doc_id"], h["chunk"])
        if key in seen_chunks or (drop and h.get("flag")):
            continue
        seen_chunks.add(key)
        text = shown(h)
        if expand and h.get("section") and h["section"] not in seen_sections:        # small to big: the section the passage came from
            try:
                section = store.section(h["archive_id"], h["section"])
                if not (drop and section.get("flag")):
                    seen_sections.add(h["section"])
                    text = (FLAG_NOTE + "\n" if section.get("flag") else "") + section["text"]
            except RagError:
                pass
        if (used + len(text) > TOOL_CHARS and out) or len(out) >= wanted * len(asked):
            break
        row = {"cite": citation(h), "archive": h["archive"], "document": h["document"], "page": h.get("page_start") or None, "section": h.get("path", ""),
               "score": h["score"], "text": text}
        if h.get("kind") == "figure":                         # a figure: the handle to look at it, with its label and caption
            row["figure"] = {"id": f"{h['archive_id']}:{h['doc_id']}:{h.get('fig')}", "label": h.get("figure", ""), "caption": h.get("caption", "")}
        if h.get("summary"):
            row["summary"] = h["summary"]
        if h.get("facts"):
            row["facts"] = h["facts"]
        if len(asked) > 1:
            row["for"] = owner.get(id(h), "")
        if h.get("flag"):
            row["flagged"] = True
        out.append(row)
        used += len(text)
        recall.remember(h, text, shown(h), citation(h), h.get("summary") or "")
    answer: Dict[str, Any] = {"status": "ok", "searched": len(ids), "passages": out, "mode": mode}
    if notes:
        answer["note"] = "; ".join(notes)
    elif not out:
        answer["note"] = "no passage matches" + (" with those filters or in that document: try other terms or fewer filters, or read it with knowledge_read" if narrow else "")
    return answer


def _title_range(row: Dict[str, Any]) -> str:
    a, b = row["page_start"], row["page_end"]
    return f"{a}" if a == b or not b else f"{a}-{b}"


def read(store: RagStore, recall: ProjectRecall, document: Any, pages: Any = "", archive: str = "") -> Dict[str, Any]:
    """The pages of a document from the reading saved when it was added, or, without `pages`, its outline (sections with their pages)."""
    if not isinstance(document, str) or not document.strip():
        return {"status": "error", "note": "document: the name of a document in a knowledge archive"}
    ids, refused = scope(store, recall, archive if isinstance(archive, str) else "")
    if refused:
        return refused
    picked, refused = pick_document(store, ids, document)
    if refused:
        return refused
    aid, doc = picked
    try:
        if pages in (None, ""):
            rows = store.outline(aid, doc["id"])
            total = int(doc.get("pages") or 0)
            shown_rows = rows[:OUTLINE_SHOWN]
            out: Dict[str, Any] = {"status": "ok", "document": doc["name"], "pages_total": total or None, "chunks": doc.get("chunks"),
                                   "outline": [{"section": r["title"], "pages": _title_range(r), **({"summary": r["summary"]} if r.get("summary") else {})} for r in shown_rows],
                                   **({"summary": doc["summary"]} if doc.get("summary") else {}),
                                   "note": "ask for pages with knowledge_read(document, pages), for example \"" + (f"{rows[0]['page_start']}-{rows[0]['page_end']}" if rows else "1-10") + "\""}
            recall.remember({"archive_id": aid}, doc.get("summary") or "", *[r["summary"] for r in shown_rows if r.get("summary")])
            if len(rows) > OUTLINE_SHOWN:
                out["more_sections"] = len(rows) - OUTLINE_SHOWN
            return out
        got = store.read_pages(aid, doc["id"], pages, READ_CHARS)
        recall.remember({"archive_id": aid}, got.get("text") or "")
        return {"status": "ok", **got}
    except RagError as e:
        return {"status": "error", "note": str(e)}


def figure_id(text: Any) -> Optional[Tuple[str, str, int]]:
    """`(archive id, document id, figure number)` from the handle a figure hit carries (`<archive>:<document>:<n>`), or None."""
    parts = str(text or "").strip().split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    return parts[0], parts[1], int(parts[2])


def figure(store: RagStore, recall: ProjectRecall, id: Any, workspace: Optional[Path] = None) -> Dict[str, Any]:
    """The figure a hit named: its citation, caption, what a model said it shows, the words read inside it and what the document says of it, and, when the conversation
    has a working folder, a PNG copy of the image in `.tadamoo/artifacts/figures/` (`path`) that the model can look at (`analyze_image`), show in the answer (`show`) or put in
    a document. A figure of an archive this route may not carry is not given."""
    got = figure_id(id)
    if got is None:
        return {"status": "error", "note": "id: the `figure.id` of a knowledge_search result, such as `archive-id:d1a2b3c4:7`"}
    aid, doc_id, n = got
    if aid not in recall.ids():
        return {"status": "none", "note": "no archive of this conversation has that figure: search for it with knowledge_search first"}
    if aid not in recall.permit([aid]):
        return {"status": "withheld", "note": "this archive is not cleared for the model this conversation uses: its figures are not given"}
    try:
        found = store.figure(aid, doc_id, n)
    except RagError as e:
        return {"status": "none", "note": str(e)}
    row = found["passage"]
    page = int(row.get("page_start") or 0)
    label = row.get("figure") or "Figure"
    cite = citation({**row, "archive": found["archive"], "document": found["document"]})
    out: Dict[str, Any] = {"status": "ok", "cite": cite, "document": found["document"], "page": page or None, "label": label, "caption": row.get("caption", ""),
                           "text": row["text"], **({"description": row["summary"]} if row.get("summary") else {}), **({"questions": row["questions"]} if row.get("questions") else {})}
    credit = f"{row.get('caption') or label} (source: {found['document']}{', p. ' + str(page) if page else ''})"
    out["credit"] = credit
    if workspace is None:
        return {**out, "note": "there is no working folder to copy the image to"}
    try:
        from PIL import Image
        target = Path(workspace) / ".tadamoo" / "artifacts" / "figures" / f"{aid}-{doc_id}-{n}.png"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(io.BytesIO(found["image"])) as image:
                image.convert("RGB").save(target, "PNG", optimize=True)
        rel = target.relative_to(Path(workspace)).as_posix()
    except Exception as e:
        return {**out, "note": f"the image could not be copied ({type(e).__name__})"}
    return {**out, "path": rel, "show": f"show it in the answer with ![{credit}](tadamoo-path:{rel})",
            "look": "to see it yourself call analyze_image with this path and your question (a crop of a region gives more detail)"}


def documents_of(store: RagStore, ids: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
    """The documents of each archive for `knowledge_list`: name, pages, language."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for aid in ids:
        try:
            out[aid] = [{"name": d["name"], "pages": d["pages"] or None, "language": d["language"] or None} for d in card.documents(store, aid)]
        except RagError:
            out[aid] = []
    return out
