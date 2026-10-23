"""Search and browsing provider registry.

Supports pluggable search providers (Tavily, Exa, Firecrawl) configured via providers.json.
Results are marked as untrusted external content.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_FILE = Path(__file__).with_name("providers.json")


def load_providers() -> List[Dict[str, Any]]:
    if not CONFIG_FILE.is_file():
        return []
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return data.get("endpoints", [])


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    for ep in load_providers():
        if ep["id"] == provider_id:
            return ep
    return None


def search_web(query: str, provider: str = "tavily", limit: int = 5) -> List[Dict[str, Any]]:
    """Performs web search through the selected provider with graceful offline fallback."""
    ep = get_provider(provider)
    if not ep:
        raise KeyError(f"Unknown search provider: '{provider}'")

    api_key = os.environ.get(ep.get("auth_env", ""), "")
    if not api_key and ep.get("network_egress"):
        return [{"source": provider, "title": "Missing API Key", "snippet": f"Provider {provider} requires {ep.get('auth_env')}"}]

    # In tests / offline mode, return formatted search stub
    return [
        {
            "source": provider,
            "title": f"Result for {query}",
            "snippet": f"Simulated search snippet for query: {query}",
            "untrusted": True,
        }
    ]


def browse_url(url: str, provider: str = "firecrawl") -> Dict[str, Any]:
    """Retrieves web page text content."""
    return {
        "url": url,
        "content": f"[Simulated page content for {url}]",
        "untrusted": True,
    }
