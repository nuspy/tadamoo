"""Client for the local wikibridge knowledge service.

Contract (wikibridge server, default port 8775; the key header is optional):
  GET  /health                          -> {"ok": true, ...}
  GET  /projects                        -> [{"slug", "name", ...}]
  GET  /search?project=&q=&limit=       -> {"project", "hits": [{"path", ...}]}
  GET  /wiki/page?project=&path=        -> {"path", "frontmatter", "body"}
  POST /rag/context {project, query, limit, max_chars, participants}
                                        -> {"blocks": [{"path", "title", "text", ...}], "truncated", "chars"}
The address and the key are configuration (TADAMOO_WIKIBRIDGE_URL, TADAMOO_WIKIBRIDGE_KEY); without a
reachable service every method returns an empty result and `last_error` says why.
Page text can come from imported web sources, so callers must treat it as untrusted data.
"""
from __future__ import annotations

import os
import posixpath
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from ._http import ServiceError, request_json

DEFAULT_URL = "http://127.0.0.1:8775"


class WikiBridgeClient:
    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 3.0, api_key: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self.last_error: Optional[str] = None

    @classmethod
    def from_environment(cls, url: Optional[str] = None, **kwargs: Any) -> "WikiBridgeClient":
        """The address is `url` when given, else TADAMOO_WIKIBRIDGE_URL, else the usual local one; the key is TADAMOO_WIKIBRIDGE_KEY."""
        return cls(
            base_url=url or os.environ.get("TADAMOO_WIKIBRIDGE_URL", DEFAULT_URL),
            api_key=os.environ.get("TADAMOO_WIKIBRIDGE_KEY") or None,
            **kwargs,
        )

    def _call(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        headers = {"X-Wikibridge-Key": self.api_key} if self.api_key else {}
        try:
            data = request_json(method, self.base_url + path, payload, headers, self.timeout)
        except ServiceError as e:
            reason = str(e).partition(": ")[2] or str(e)
            self.last_error = f"wikibridge {e.kind} at {self.base_url}: {reason}"
            raise
        self.last_error = None
        return data

    def status(self) -> str:
        """"ok", "offline", "unauthorized" or "error"."""
        try:
            data = self._call("GET", "/health")
        except ServiceError as e:
            return e.kind if e.kind in ("offline", "unauthorized") else "error"
        return "ok" if isinstance(data, dict) and data.get("ok") else "error"

    def is_available(self) -> bool:
        return self.status() == "ok"

    def projects(self) -> List[Dict[str, Any]]:
        try:
            data = self._call("GET", "/projects")
        except ServiceError:
            return []
        return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []

    def list_projects(self) -> List[str]:
        return [p["slug"] for p in self.projects() if p.get("slug")]

    def search(self, project_slug: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        qs = urllib.parse.urlencode({"project": project_slug, "q": query, "limit": int(limit)})
        try:
            data = self._call("GET", f"/search?{qs}")
        except ServiceError:
            return []
        hits = data.get("hits") if isinstance(data, dict) else None
        return hits if isinstance(hits, list) else []

    def get_page(self, project_slug: str, doc_path: str) -> Optional[str]:
        """Markdown body of a wiki page; None when absent, hidden or unreachable."""
        path = posixpath.normpath(doc_path.replace("\\", "/")) if doc_path.strip() else ""
        if not path or path == "." or path.startswith("/") or path == ".." or path.startswith("../") or re.match(r"^[A-Za-z]:", path):
            self.last_error = "wikibridge page path must be relative and inside the wiki"
            return None
        qs = urllib.parse.urlencode({"project": project_slug, "path": path})
        try:
            data = self._call("GET", f"/wiki/page?{qs}")
        except ServiceError:
            return None
        body = data.get("body") if isinstance(data, dict) else None
        return body if isinstance(body, str) else None

    get_document = get_page

    def rag_context(
        self,
        project_slug: str,
        query: str,
        limit: int = 8,
        max_chars: int = 12000,
        participants: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Relevant chunks for a question, already filtered by the service's access rules."""
        payload: Dict[str, Any] = {"project": project_slug, "query": query, "limit": limit, "max_chars": max_chars}
        if participants:
            payload["participants"] = participants
        try:
            data = self._call("POST", "/rag/context", payload)
        except ServiceError:
            return {"blocks": [], "truncated": False, "chars": 0}
        if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
            self.last_error = "wikibridge bad_response"
            return {"blocks": [], "truncated": False, "chars": 0}
        return data
