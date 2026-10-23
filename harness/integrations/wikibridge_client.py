"""Client for the local wikibridge knowledge service (port 8775).

Allows the agent to search and retrieve project-specific documentation and syntheses.
Gracefully degrades when the service is offline without throwing unhandled errors.
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional


class WikiBridgeClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8775", timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_available(self) -> bool:
        """Returns True if the wikibridge server is reachable."""
        try:
            req = urllib.request.Request(f"{self.base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_projects(self) -> List[str]:
        """Lists available knowledge projects from wikibridge."""
        try:
            req = urllib.request.Request(f"{self.base_url}/api/projects", method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("projects", [])
        except Exception:
            return []

    def search(self, project_slug: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Queries the FTS5 knowledge base of a wikibridge project."""
        url = f"{self.base_url}/api/projects/{urllib.parse.quote(project_slug)}/search?q={urllib.parse.quote(query)}&limit={limit}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("results", [])
        except Exception:
            return []

    def get_document(self, project_slug: str, doc_path: str) -> Optional[str]:
        """Fetches the markdown content of a wiki entity or synthesis."""
        url = f"{self.base_url}/api/projects/{urllib.parse.quote(project_slug)}/doc/{urllib.parse.quote(doc_path)}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("content")
        except Exception:
            return None
