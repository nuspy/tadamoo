"""Persistent long-term memory store using SQLite FTS5."""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self.conn:
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
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    id UNINDEXED,
                    category,
                    content
                )
            """)
            # Triggers to keep FTS in sync
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
        mem_id = f"mem_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self.conn:
            self.conn.execute(
                "INSERT INTO memories (id, category, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (mem_id, category, content.strip(), now, now),
            )
        return mem_id

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        # Clean query for FTS5 syntax
        clean_q = "".join(c for c in query if c.isalnum() or c.isspace()).strip()
        if not clean_q:
            return []

        # Use prefix match on tokens
        tokens = [f"{token}*" for token in clean_q.split() if token]
        fts_query = " ".join(tokens)

        try:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT m.id, m.category, m.content, m.created_at
                FROM memories_fts f
                JOIN memories m ON f.id = m.id
                WHERE memories_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit))
            return [dict(row) for row in cur.fetchall()]
        except sqlite3.OperationalError:
            # Fallback to LIKE if FTS expression has syntax issues
            cur = self.conn.cursor()
            cur.execute("""
                SELECT id, category, content, created_at
                FROM memories
                WHERE content LIKE ?
                LIMIT ?
            """, (f"%{clean_q}%", limit))
            return [dict(row) for row in cur.fetchall()]

    def delete_memory(self, memory_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def close(self) -> None:
        self.conn.close()
