"""Knowledge for the agent: a fixed reference block (CAG) or searched passages (RAG), and which one to use.

CAG puts a document set at the start of the context, byte-stable, so the engine's prefix checkpoint is reused
on every turn; it costs a full re-read whenever the set changes and it crowds the context. RAG leaves the
context small and fetches only what a question needs, as untrusted data at the tail. The size at which CAG
stops paying is a choice to tune with measurements (python -m harness.eval --set knowledge), not a fact: the
default below is a starting point.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.memory import MemoryStore
from harness.tools.registry import EffectClass, ToolRegistry

CAG_MAX_FRACTION = 0.25          # starting point: reference text may take a quarter of the prompt budget
CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def cag_prefix(docs: Dict[str, str], budget_tokens: Optional[int] = None) -> str:
    """The documents as one reference block, in path order so the same set always gives the same bytes.

    Raises ValueError if it would not fit `budget_tokens`."""
    parts = [f"## {path}\n{text.strip()}" for path, text in sorted(docs.items())]
    block = "\n\n".join(parts)
    if budget_tokens is not None and estimate_tokens(block) > budget_tokens:
        raise ValueError(f"the reference documents need about {estimate_tokens(block)} tokens, more than the {budget_tokens} allowed")
    return block


def fingerprint(docs: Dict[str, str]) -> str:
    """Changes when any document does: a changed CAG block means the whole prefix is read again."""
    h = hashlib.sha256()
    for path, text in sorted(docs.items()):
        h.update(path.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    return h.hexdigest()[:16]


def choose_strategy(docs: Dict[str, str], budget_tokens: int, volatile: bool = False, max_fraction: float = CAG_MAX_FRACTION) -> str:
    """"none" for no documents, "cag" for a small stable set, else "rag"."""
    if not docs:
        return "none"
    size = estimate_tokens(cag_prefix(docs))
    if volatile or size > budget_tokens * max_fraction:
        return "rag"
    return "cag"


class LocalKnowledge:
    """A small searchable copy of a document set, in SQLite full-text search, for RAG without any service."""

    def __init__(self, docs: Dict[str, str], chunk_chars: int = 900) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="basalt-kb-", ignore_cleanup_errors=True)
        self.store = MemoryStore(Path(self._dir.name) / "kb.db")
        for path, text in sorted(docs.items()):
            for i, chunk in enumerate(self._chunks(text, chunk_chars)):
                self.store.add_memory(f"[{path}#{i + 1}] {chunk}", category=path)

    @staticmethod
    def _chunks(text: str, size: int) -> List[str]:
        out, cur = [], ""
        for para in re.split(r"\n\s*\n", text.strip()):
            if cur and len(cur) + len(para) > size:
                out.append(cur)
                cur = ""
            cur = (cur + "\n\n" + para).strip()
        if cur:
            out.append(cur)
        return out or [""]

    def search(self, query: str, limit: int = 4) -> List[str]:
        return [row["content"] for row in self.store.search(query, limit=limit)]

    def close(self) -> None:
        self.store.close()
        self._dir.cleanup()


def register_local_search(registry: ToolRegistry, kb: LocalKnowledge) -> None:
    """`kb_search`: passages of the reference documents, marked untrusted like any retrieved text."""
    registry.register(
        "kb_search", "Searches the reference documents and returns the best passages.",
        {"type": "object", "properties": {"query": {"type": "string", "description": "Words to look for"}}, "required": ["query"]},
        EffectClass.PURE, lambda query: kb.search(query) or ["No passage matches."], untrusted=True)
