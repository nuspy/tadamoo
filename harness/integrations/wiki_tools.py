"""Agent tools over wikibridge: search, read a page, fetch ranked passages.

Everything wikibridge returns can come from imported web pages, so the tools are marked untrusted: the loop
fences their results as data and counts them against the Rule of Two. When the service cannot be reached
the tools say so in plain words (and what to check) instead of returning nothing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from harness.tools.registry import EffectClass, ToolRegistry

from .wikibridge_client import WikiBridgeClient


def register_wiki_tools(registry: ToolRegistry, client: WikiBridgeClient, default_project: Optional[str] = None) -> None:
    def project_of(project: Optional[str]) -> str:
        if project:
            return project
        if default_project:
            return default_project
        slugs = client.list_projects()
        if not slugs:
            raise RuntimeError(client.last_error or "wikibridge has no projects")
        return slugs[0]

    def unreachable() -> RuntimeError:
        return RuntimeError(f"{client.last_error or 'wikibridge did not answer'}. Check that wikibridge is running and its address "
                            "and key are set (BASALT_WIKIBRIDGE_URL, BASALT_WIKIBRIDGE_KEY).")

    def wiki_search(query: str, project: str = "", limit: int = 5) -> List[Dict[str, Any]]:
        hits = client.search(project_of(project or None), query, limit=max(1, min(int(limit), 20)))
        if not hits and client.last_error:
            raise unreachable()
        return hits or [{"note": "No page matches."}]

    def wiki_page(path: str, project: str = "") -> str:
        body = client.get_page(project_of(project or None), path)
        if body is None:
            raise RuntimeError(client.last_error or f"The page '{path}' was not found or is not visible.")
        return body

    def wiki_context(query: str, project: str = "", max_chars: int = 6000) -> Dict[str, Any]:
        ctx = client.rag_context(project_of(project or None), query, max_chars=max(500, min(int(max_chars), 30000)))
        if not ctx["blocks"] and client.last_error:
            raise unreachable()
        return {"blocks": [{"path": b.get("path"), "title": b.get("title"), "text": b.get("text")} for b in ctx["blocks"]],
                "truncated": ctx.get("truncated", False)}

    prop = {"project": {"type": "string", "description": "Project slug; empty uses the default"}}
    registry.register("wiki_search", "Searches the knowledge wiki and lists the matching pages.",
                      {"type": "object", "properties": {"query": {"type": "string"}, **prop, "limit": {"type": "integer", "default": 5}},
                       "required": ["query"]}, EffectClass.PURE, wiki_search, untrusted=True)
    registry.register("wiki_page", "Reads one page of the knowledge wiki.",
                      {"type": "object", "properties": {"path": {"type": "string", "description": "e.g. entities/diana.md"}, **prop},
                       "required": ["path"]}, EffectClass.PURE, wiki_page, untrusted=True)
    registry.register("wiki_context", "Fetches the passages of the knowledge wiki that best answer a question.",
                      {"type": "object", "properties": {"query": {"type": "string"}, **prop, "max_chars": {"type": "integer", "default": 6000}},
                       "required": ["query"]}, EffectClass.PURE, wiki_context, untrusted=True)
