"""The messages of every conversation as immutable nodes.

A node is one message with the ids of the node it follows (`parents`: the first is the one it continues, any more are branches it
merges). Its id is a hash of the message and its parents, so the same message after the same history is the same node: branches
that share a start share its nodes, and nothing is copied when a conversation forks. A conversation is a pointer to one node (its
leaf); its messages are the chain of parents from there. Nodes are only ever added; `keep` drops the ones no conversation reaches.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

CHARS_PER_TOKEN = 3.5            # the harness's own estimate (tadamoo/context.py)


def node_id(parents: List[str], message: Dict[str, Any]) -> str:
    body = json.dumps({"parents": parents, "message": message}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def message_tokens(message: Dict[str, Any]) -> int:
    chars = len(str(message.get("content") or "")) + (len(json.dumps(message["tool_calls"])) if message.get("tool_calls") else 0)
    return int(chars / CHARS_PER_TOKEN)


class NodeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                n = json.loads(line)
            except ValueError:
                continue                                         # a torn last line is skipped
            if isinstance(n, dict) and isinstance(n.get("id"), str) and isinstance(n.get("message"), dict) and isinstance(n.get("parents"), list):
                self._nodes[n["id"]] = n

    def __len__(self) -> int:
        return len(self._nodes)

    def get(self, nid: Optional[str]) -> Optional[Dict[str, Any]]:
        return self._nodes.get(nid) if nid else None

    def add(self, message: Dict[str, Any], parents: Iterable[str] = ()) -> str:
        """One node after `parents`; the same message after the same parents is found, not added again."""
        parents = list(parents)
        missing = [p for p in parents if p not in self._nodes]
        if missing:
            raise KeyError(f"unknown parent node {missing[0]}")
        nid = node_id(parents, message)
        with self._lock:
            if nid not in self._nodes:
                node = {"id": nid, "parents": parents, "message": message}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(node, ensure_ascii=False, separators=(",", ":")) + "\n")
                self._nodes[nid] = node
        return nid

    def ingest(self, messages: Iterable[Dict[str, Any]], parent: Optional[str] = None) -> Optional[str]:
        """The messages one after another, each continuing the one before; the id of the last (`parent` when there are none)."""
        leaf = parent
        for m in messages:
            leaf = self.add(m, [leaf] if leaf else [])
        return leaf

    def path_to(self, leaf: Optional[str]) -> List[str]:
        """Node ids from the first message to `leaf`, following the first parent."""
        out: List[str] = []
        seen: set = set()
        while leaf and leaf in self._nodes and leaf not in seen:
            seen.add(leaf)
            out.append(leaf)
            parents = self._nodes[leaf]["parents"]
            leaf = parents[0] if parents else None
        return out[::-1]

    def chain(self, leaf: Optional[str]) -> List[Dict[str, Any]]:
        return [dict(self._nodes[n]["message"]) for n in self.path_to(leaf)]

    def common_ancestor(self, a: Optional[str], b: Optional[str]) -> Optional[str]:
        """The last node both chains pass through."""
        found = None
        for x, y in zip(self.path_to(a), self.path_to(b)):
            if x != y:
                break
            found = x
        return found

    def tokens(self, leaf: Optional[str]) -> int:
        return sum(message_tokens(self._nodes[n]["message"]) for n in self.path_to(leaf))

    def keep(self, leaves: Iterable[str]) -> int:
        """Drops every node that is not on a chain from one of `leaves` (or a merged parent of one); returns how many went."""
        reach: set = set()
        stack = [n for n in leaves if n in self._nodes]
        while stack:
            n = stack.pop()
            if n in reach:
                continue
            reach.add(n)
            stack.extend(p for p in self._nodes[n]["parents"] if p in self._nodes)
        with self._lock:
            gone = len(self._nodes) - len(reach)
            if not gone:
                return 0
            self._nodes = {n: v for n, v in self._nodes.items() if n in reach}
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".nodes.")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for node in self._nodes.values():
                    f.write(json.dumps(node, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(tmp, str(self.path))
            return gone

    def all(self) -> List[Dict[str, Any]]:
        return list(self._nodes.values())
