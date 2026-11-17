"""Adding to an archive from a request in words, over the API: a web search whose results the technical session judges, the same search widened by the links of the pages
found, and the import of the results the person picked. The work is tadamoo/rag/discover.py.

Routes, under /api/rag (this PC only, as the rest):

  POST archives/<id>/discover          {query, limit?}: {job}; its result is {query, items: [{id, title, url, snippet, via, relevant, reason}]}
  POST archives/<id>/discover/more     {query, items}: {job}; the result is {items} that are new, from the links of the pages already found
  POST archives/<id>/discover/import   {urls, depth?}: each address becomes a web source of the archive and the archive is read; {added, skipped, job}"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.rag import RagError, discover

ITEMS_MAX = 200
IMPORT_MAX = 200


def web_search() -> Callable[[str, int], List[Dict[str, Any]]]:
    """The search the person chose in Settings > Web search, else DuckDuckGo; an error when web search is switched off on this PC."""
    if str(os.environ.get("TADAMOO_WEB_SEARCH", "")).lower() == "off":
        raise RagError("web search is switched off on this PC")
    from tadamoo.tools.search import duckduckgo_search
    try:
        from tadamoo import websearch
        prefs = websearch.Prefs()
        chosen = websearch.search_backend(prefs.read()["search"], prefs, websearch.Keys())
    except Exception:                                   # unreadable settings are the default engine
        chosen = None
    return chosen or duckduckgo_search


class RagDiscoverApi:
    def __init__(self, syncing: Any, jobs: Any, describer: Optional[Callable[[], Any]] = None, find: Optional[Callable[[], Callable[[str, int], List[Dict[str, Any]]]]] = None) -> None:
        """`syncing` is the RagSyncApi (its sources and its refresh), `describer` gives the model client of the technical session (None: results are offered unjudged), `find`
        gives the search function."""
        self.syncing, self.jobs, self.describer = syncing, jobs, describer
        self.find = find or web_search

    def _client(self) -> Any:
        try:
            return self.describer() if self.describer else None
        except Exception:                               # no session to ask: the results are offered unjudged
            return None

    def _items(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list) or len(value) > ITEMS_MAX:
            raise RagError(f"items: a list of at most {ITEMS_MAX} results")
        out = []
        for i in value:
            if not isinstance(i, dict) or not isinstance(i.get("url"), str) or not i["url"].startswith(("http://", "https://")):
                raise RagError("items: each result has a web address")
            out.append({"title": str(i.get("title") or "")[:200], "url": i["url"][:2000], "snippet": str(i.get("snippet") or "")[:500], "via": str(i.get("via") or "")[:80],
                        "relevant": i.get("relevant") if isinstance(i.get("relevant"), bool) else None, "reason": str(i.get("reason") or "")[:120]})
        return out

    def _job(self, label: str, aid: str, work: Callable[[Any, Any], Dict[str, Any]]) -> Dict[str, Any]:
        name = self.syncing.store.load(aid).meta["name"]
        return {"job": self.jobs.start(f"{label} {name}", work, aid)}

    def route_post(self, parts: Sequence[str], req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if len(parts) < 3 or parts[0] != "archives" or parts[2] != "discover" or len(parts) > 4:
            return None
        aid, action = parts[1], (parts[3] if len(parts) == 4 else "")
        query = str(req.get("query") or "").strip()[:600]
        if action == "":
            if not query:
                raise RagError("query: write what to look for")
            limit = req.get("limit", 10)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= discover.RESULTS_MAX:
                raise RagError(f"limit: a whole number from 1 to {discover.RESULTS_MAX}")
            find, client = self.find(), self._client()

            def run(progress: Any, cancel: Any) -> Dict[str, Any]:
                progress({"event": "stage", "stage": "searching the web"})
                return {"query": query, "items": discover.search(find, client, query, limit, cancel.is_set)}
            return self._job("look for", aid, run)
        if action == "more":
            items, client = self._items(req.get("items")), self._client()
            http = self.syncing.sync.ctx.http

            def fetch(url: str) -> Optional[bytes]:
                got = http.get(url, robots=True, max_bytes=2_000_000)
                return got.body if "html" in (got.headers.get("content-type") or "html").lower() else None

            def widen(progress: Any, cancel: Any) -> Dict[str, Any]:
                progress({"event": "stage", "stage": "reading the pages found"})
                return {"items": discover.extend(fetch, client, query, items, cancel.is_set)}
            return self._job("widen the search for", aid, widen)
        if action == "import":
            urls = req.get("urls")
            if not isinstance(urls, list) or not urls or len(urls) > IMPORT_MAX or any(not isinstance(u, str) for u in urls):
                raise RagError(f"urls: a list of 1 to {IMPORT_MAX} web addresses")
            depth = req.get("depth", 0)
            added, skipped = 0, []
            for url in dict.fromkeys(urls):
                try:
                    self.syncing.sync.add_source(aid, "url", {"url": url, "depth": depth})
                    added += 1
                except RagError as e:
                    skipped.append({"url": url, "why": str(e)})
            out: Dict[str, Any] = {"added": added, "skipped": skipped}
            if added:
                out.update(self.syncing.route_post(["archives", aid, "refresh"], {"include_new": True}) or {})
            return out
        return None
