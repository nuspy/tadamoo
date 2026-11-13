"""What a passage is as it is searched: shared by the store and the database backends."""
from __future__ import annotations

from typing import Any, Dict


def searchable(chunk: Dict[str, Any]) -> str:
    """The text of a passage as it is searched and embedded: where it is in the document, what a model said it is about and the questions it answers (when the
    archive was enriched, tadamoo/rag/enrich.py), then the passage."""
    lead = [chunk["path"]] if chunk.get("path") else []
    if chunk.get("summary"):
        lead.append(chunk["summary"])
    lead += list(chunk.get("questions") or [])
    return ("\n".join(lead) + "\n" + chunk["text"]) if lead else chunk["text"]
