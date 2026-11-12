"""What an archive holds, in a few words the model can plan with: its documents with their pages and languages. Told in the `[state]` note (tadamoo/capstate.py), so the
model knows which documents the archive covers before it decides to read files one page at a time.

File names are the person's, and may come from anywhere: only plain characters are kept, and they are cut short."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from tadamoo.rag.language import guess_language
from tadamoo.rag.store import RagStore

DOCS_SHOWN = 10                    # documents named for one archive; the rest is counted
NAME_CHARS = 40
DESCRIPTION_CHARS = 60
SUMMARY_CHARS = 110                # what a model said a document is about, in the note
SEARCH = {"hybrid": "search by meaning and by words", "text": "search by words only"}
_PLAIN = re.compile(r"[^\w .()+-]")


def plain(text: Any, limit: int) -> str:
    """`text` as one line of plain characters, at most `limit` long."""
    return _PLAIN.sub("", " ".join(str(text or "").split()))[:limit].strip()


def languages(store: RagStore, aid: str) -> Dict[str, str]:
    """The language of each document of an archive (`en`, `fr`, `it`, `de`, `es`, `zh`, or "" when it cannot be told), by document id."""
    out: Dict[str, str] = {}
    for doc, texts in store.samples(aid).items():
        said = guess_language("\n".join(texts))
        out[doc] = "" if said == "other" else said
    return out


def documents(store: RagStore, aid: str) -> List[Dict[str, Any]]:
    """`{id, name, pages, language}` for each document of an archive, in the order they were added."""
    langs = languages(store, aid)
    return [{"id": d["id"], "name": d["name"], "pages": int(d.get("pages") or 0), "language": langs.get(d["id"], ""), "summary": plain(d.get("summary"), SUMMARY_CHARS), "figures": int(d.get("figures") or 0)}
            for d in store.documents(aid)]


def line(store: RagStore, row: Dict[str, Any], where: str = "") -> str:
    """One archive for the `[state]` note: `Name (7 documents, 1126 pages, hybrid search; a.pdf 27 p. zh, b.pdf 20 p. fr, ...; its description)`."""
    docs = documents(store, row["id"])
    pages = sum(d["pages"] for d in docs)
    shown = [f"{plain(d['name'], NAME_CHARS)}" + (f" {d['pages']} p." if d["pages"] else "") + (f" {d['language']}" if d["language"] else "") + (f" {d['figures']} fig." if d["figures"] else "") + (f" ({d['summary']})" if d["summary"] else "") for d in docs[:DOCS_SHOWN]]
    if len(docs) > DOCS_SHOWN:
        shown.append(f"and {len(docs) - DOCS_SHOWN} more")
    said = plain(row.get("description"), DESCRIPTION_CHARS)
    head = (where + "; " if where else "") + f"{row['documents']} documents" + (f", {pages} pages" if pages else "") + f", {SEARCH.get(row['mode'], row['mode'])}"
    return f"{plain(row['name'], NAME_CHARS)} ({head}" + (f"; {', '.join(shown)}" if shown else "") + (f"; {said}" if said else "") + ")"
