"""Search and browsing provider registry.

Providers are listed in providers.json and become usable only when a backend is connected for
them (see register_backend); a call to an unconnected provider is an error, never made-up data.
Everything a backend returns is marked untrusted.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

CONFIG_FILE = Path(__file__).with_name("providers.json")

SearchFn = Callable[[str, int], List[Dict[str, Any]]]
BrowseFn = Callable[[str], Dict[str, Any]]
_SEARCH_BACKENDS: Dict[str, SearchFn] = {}
_BROWSE_BACKENDS: Dict[str, BrowseFn] = {}


class ProviderUnavailable(RuntimeError):
    """The provider is unknown, has no backend connected, or lacks its credential."""


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


def register_backend(provider_id: str, search: Optional[SearchFn] = None, browse: Optional[BrowseFn] = None) -> None:
    if search is not None:
        _SEARCH_BACKENDS[provider_id] = search
    if browse is not None:
        _BROWSE_BACKENDS[provider_id] = browse


def clear_backends() -> None:
    _SEARCH_BACKENDS.clear()
    _BROWSE_BACKENDS.clear()


def _require(provider_id: str, backends: Dict[str, Any], what: str) -> Any:
    ep = get_provider(provider_id)
    if not ep:
        raise ProviderUnavailable(f"unknown provider '{provider_id}'")
    if provider_id not in backends:
        raise ProviderUnavailable(f"provider '{provider_id}' is not connected for {what}")
    if ep.get("network_egress") and ep.get("auth_env") and not os.environ.get(ep["auth_env"]):
        raise ProviderUnavailable(f"provider '{provider_id}' needs {ep['auth_env']}")
    return backends[provider_id]


def search_web(query: str, provider: str = "tavily", limit: int = 5) -> List[Dict[str, Any]]:
    backend = _require(provider, _SEARCH_BACKENDS, "search")
    results = backend(query, max(1, min(int(limit), 20)))
    return [{**r, "source": provider, "untrusted": True} for r in results]


def check_public_url(url: str) -> str:
    """Rejects non-http(s) URLs and hosts that resolve to this machine or a private network."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http(s) URLs with a host can be browsed")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {parts.hostname}") from e
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            raise ValueError("URLs on this machine or a private network are not browsable")
    return url


def browse_url(url: str, provider: str = "firecrawl") -> Dict[str, Any]:
    backend = _require(provider, _BROWSE_BACKENDS, "browsing")
    check_public_url(url)
    page = backend(url)
    return {**page, "url": url, "source": provider, "untrusted": True}
