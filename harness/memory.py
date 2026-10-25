"""Persistent long-term memory per project, in SQLite with full-text search (FTS5).

The database lives in the user's data folder, one file per workspace, never inside the repository.
Retrieved text is data from the past, not instructions: callers hand it to the model as untrusted.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
MAX_MEMORY_CHARS = 2000
MAX_MEMORIES = 5000
_CATEGORY = re.compile(r"[^a-z0-9_\-]+")


def default_db_path(workspace: str | Path) -> Path:
    """Where the memory of a workspace is kept: <user data>/memory/<hash of the workspace>.db."""
    try:
        from serve.userdata import user_data_dir
        base = user_data_dir()
    except Exception:  # the harness also runs without the server package
        base = Path.home() / ".basalt"
    digest = hashlib.sha1(str(Path(workspace).resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return base / "memory" / f"{digest}.db"


def _phrase(token: str) -> str:
    """One FTS5 phrase per token: quoting keeps identifiers such as sm_120 from being split into operators."""
    return '"' + token.replace('"', '""') + '"'


def fts_query(query: str, mode: str = "and") -> str:
    tokens = [t for t in query.split() if t and t.upper() not in _OPERATORS]
    if not tokens:
        return ""
    parts = [_phrase(t) + ("*" if i == len(tokens) - 1 else "") for i, t in enumerate(tokens)]
    return (" OR " if mode == "or" else " ").join(parts)


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(id UNINDEXED, category, content)
            """)
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(id, category, content) VALUES (new.id, new.category, new.content);
                END;
            """)
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_memories_ad AFTER DELETE ON memories BEGIN
                    DELETE FROM memories_fts WHERE id = old.id;
                END;
            """)
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_memories_au AFTER UPDATE ON memories BEGIN
                    DELETE FROM memories_fts WHERE id = old.id;
                    INSERT INTO memories_fts(id, category, content) VALUES (new.id, new.category, new.content);
                END;
            """)

    def add_memory(self, content: str, category: str = "general") -> str:
        text = content.strip()
        if not text:
            raise ValueError("a memory needs some text")
        mem_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO memories (id, category, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (mem_id, category, text, now, now),
            )
        return mem_id

    def find(self, content: str) -> Optional[str]:
        """The id of a memory with exactly this text, if there is one."""
        with self._lock:
            row = self.conn.execute("SELECT id FROM memories WHERE content = ?", (content.strip(),)).fetchone()
        return row["id"] if row else None

    def count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def update_memory(self, memory_id: str, content: str) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute("UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                                    (content.strip(), time.time(), memory_id))
            return cur.rowcount > 0

    def _match(self, expression: str, limit: int) -> List[Dict[str, Any]]:
        cur = self.conn.execute("""
            SELECT m.id, m.category, m.content, m.created_at
            FROM memories_fts f JOIN memories m ON f.id = m.id
            WHERE memories_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (expression, limit))
        return [dict(row) for row in cur.fetchall()]

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Memories matching every word of the query, else any of them, best match first."""
        with self._lock:
            for mode in ("and", "or"):
                expression = fts_query(query, mode)
                if not expression:
                    return []
                try:
                    rows = self._match(expression, limit)
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    return rows
                if len(query.split()) < 2:
                    break
            return []

    def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """The memories last written or changed, newest first."""
        with self._lock:
            rows = self.conn.execute("SELECT id, category, content, created_at FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def register_memory_tools(registry: Any, store: MemoryStore) -> None:
    """`remember`, `recall` and `forget`: the model keeps notes about this project for later conversations.

    What comes back from `recall` is marked untrusted like every other retrieved text: it is data from the past, not instructions."""
    from harness.tools.registry import EffectClass

    def remember(content: str, category: str = "general") -> Dict[str, Any]:
        text = content.strip()
        if not text:
            raise ValueError("a memory needs some text")
        if len(text) > MAX_MEMORY_CHARS:
            raise ValueError(f"a memory is a short note (at most {MAX_MEMORY_CHARS} characters); keep the point and leave the rest out")
        known = store.find(text)
        if known:
            return {"id": known, "saved": False, "note": "this note is already kept"}
        if store.count() >= MAX_MEMORIES:
            raise ValueError("the memory of this project is full; forget notes that no longer matter first")
        return {"id": store.add_memory(text, _CATEGORY.sub("-", category.strip().lower()).strip("-")[:32] or "general"), "saved": True}

    def recall(query: str, limit: int = 5) -> List[Dict[str, Any]]:
        return [{"id": m["id"], "category": m["category"], "content": m["content"]} for m in store.search(query, limit=max(1, min(int(limit), 20)))]

    def forget(id: str) -> Dict[str, Any]:
        return {"id": id, "forgotten": store.delete_memory(id)}

    registry.register(
        name="remember",
        description="Keeps a short note about this project (a decision, a convention, a fact that will matter later) for future conversations.",
        parameters={"type": "object", "properties": {
            "content": {"type": "string", "description": "The note, one or two sentences"},
            "category": {"type": "string", "description": "A word to group notes, e.g. build, decision, preference", "default": "general"}},
            "required": ["content"]},
        effect=EffectClass.REVERSIBLE, handler=remember)
    registry.register(
        name="recall",
        description="Searches the notes kept about this project.",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to look for"},
            "limit": {"type": "integer", "description": "How many notes", "default": 5}},
            "required": ["query"]},
        effect=EffectClass.PURE, handler=recall, untrusted=True)
    registry.register(
        name="forget",
        description="Deletes one kept note, by the id that remember or recall gave.",
        parameters={"type": "object", "properties": {"id": {"type": "string", "description": "The note's id"}}, "required": ["id"]},
        effect=EffectClass.REVERSIBLE, handler=forget)
