"""Searching through the server: the server holds the embedding model (a worker process on a lease of VRAM), so a process that runs beside it, such as
the agent harness, asks its `/api/rag/search` instead of starting a second model. Only a server on this PC is asked."""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

TIMEOUT_S = 180.0                  # the first question may wait for the model to load and for the VRAM to be lent
LOCAL = ("127.0.0.1", "localhost", "::1")


def server_search(endpoint: str, api_key: str = "", timeout: float = TIMEOUT_S, context: Callable[[], Dict[str, Any]] = lambda: {}) -> Optional[Callable[[List[str], str, int, float], Dict[str, Any]]]:
    """A function `(archive ids, query, top_k, min_share) -> the server's answer` for the store's `remote`, or None when `endpoint` is not this PC.
    `context()` says what the search is for (`{function, routed_to_cloud, ...}`, as for /api/rag/resolve): the server leaves out the archives that route may not carry."""
    parts = urlsplit(endpoint or "")
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in LOCAL:
        return None
    url = endpoint.rstrip("/") + "/api/rag/search"

    def search(ids: List[str], query: str, top_k: int, min_share: float, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json", **({"Authorization": "Bearer " + api_key} if api_key else {})}
        body = json.dumps({"archives": ids, "query": query, "top_k": top_k, "min_share": min_share, "strict": False, **({"filters": filters} if filters else {}), **({"context": context()} if context() else {})}).encode("utf-8")
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"), timeout=timeout) as r:
            out = json.loads(r.read())
        if not isinstance(out, dict) or not isinstance(out.get("hits"), list):
            raise ValueError("the server did not answer a search")
        return out
    return search
