"""A word to the model when it reads, file by file, a document that is already in a knowledge archive of the conversation: the archive finds a fact across the whole
document and gives any page at once, without a new OCR. Said once when the document is first read and once more after several reads of it, never more than a few
times in all, so it stays a hint."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Tuple

from tadamoo.rag import card
from tadamoo.rag.errors import RagError
from tadamoo.rag.recall import ProjectRecall
from tadamoo.rag.store import RagStore

READ_TOOLS = ("read_document", "read_file")
AGAIN_AFTER = 4                    # reads of one document after which it is said once more
MOST = 6                           # hints in one conversation


def read_hint(store: RagStore, recall: ProjectRecall) -> Callable[[str, Dict[str, Any]], str]:
    """A function `(tool, arguments) -> note` for the results of file reads."""
    reads: Dict[str, int] = {}
    cache: Dict[str, Any] = {"key": None, "docs": {}}
    told = [0]

    def known() -> Dict[str, Tuple[str, int]]:
        key = (store.stamp(), tuple(recall.attached), recall.project)
        if cache["key"] != key:
            docs: Dict[str, Tuple[str, int]] = {}
            for a in store.list():
                if a["id"] in set(recall.ids()):
                    try:
                        for d in store.documents(a["id"]):
                            docs.setdefault(d["name"].lower(), (a["name"], int(d.get("pages") or 0)))
                    except RagError:
                        continue
            cache.update(key=key, docs=docs)
        return cache["docs"]

    def hint(tool: str, args: Dict[str, Any]) -> str:
        path = args.get("path")
        if tool not in READ_TOOLS or not isinstance(path, str) or told[0] >= MOST:
            return ""
        name = Path(path.strip()).name.lower()
        mine = known().get(name)
        if mine is None:
            return ""
        reads[name] = reads.get(name, 0) + 1
        if reads[name] not in (1, AGAIN_AFTER):
            return ""
        told[0] += 1
        archive, pages = mine
        label = card.plain(Path(path.strip()).name, card.NAME_CHARS)
        if reads[name] == 1:
            return (f"[harness] {label} is also in the knowledge archive \"{card.plain(archive, card.NAME_CHARS)}\"" + (f" ({pages} pages)" if pages else "")
                    + ", already read and indexed. To find a specific fact, number or claim in it, `knowledge_search` (with `document`) finds the page across the whole document, "
                    "and `knowledge_read` gives any pages with no OCR wait. Keep reading it here only for a page you already know you need.")
        return (f"[harness] You have read {label} {reads[name]} times in pieces: for the rest of what you need from it, one `knowledge_search` for each point (with `document`) "
                "is quicker than more pages.")
    return hint
