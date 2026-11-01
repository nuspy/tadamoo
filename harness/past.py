"""One index over the person's past conversations, whatever the chat, the project or the routine they came from.

A conversation is cut into pieces of about 500 tokens, each kept with where it came from (conversation, turn, project, kind, routine) and when. The index
holds the text itself, so a hit can be read again as it was said. What comes back is data from the past, not instructions: callers hand it to the model
as untrusted. The file lives in the user's data folder, never in a repository.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from harness.memory import CANDIDATES, CHARS_PER_TOKEN, PREFIX_FROM, _fold, _phrase, query_terms

CHUNK_TOKENS = 500
CHUNK_CHARS = int(CHUNK_TOKENS * CHARS_PER_TOKEN)
PER_SESSION = 2                    # most pieces one conversation gives to a search
SNIPPET_CHARS = 420
KINDS = ("chat", "agent", "routine")
_PARAGRAPH = re.compile(r"\n\s*\n")


def default_path() -> Path:
    try:
        from serve.userdata import user_data_dir
        base = user_data_dir()
    except Exception:  # the harness also runs without the server package
        base = Path.home() / ".basalt"
    return base / "memory" / "past.db"


def text_of(message: Dict[str, Any]) -> str:
    """The words of a message: a string, or the text parts of a list (images and tool calls are not words)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def pieces(text: str, size: int = CHUNK_CHARS) -> List[str]:
    """The text cut at paragraph ends into pieces of at most `size` characters; a paragraph longer than that is cut where it must be."""
    out: List[str] = []
    current = ""
    for para in (p.strip() for p in _PARAGRAPH.split(text)):
        while len(para) > size:
            if current:
                out.append(current)
                current = ""
            out.append(para[:size])
            para = para[size:]
        if para:
            if current and len(current) + len(para) + 2 > size:
                out.append(current)
                current = ""
            current = f"{current}\n\n{para}" if current else para
    if current:
        out.append(current)
    return out


class PastIndex:
    def __init__(self, db_path: str | Path | None = None, redact: Optional[Callable[[str], str]] = None) -> None:
        self.path = Path(db_path) if db_path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.redact = redact
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        with self._lock, self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, session TEXT NOT NULL, turn INTEGER NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,
                title TEXT NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL, routine TEXT NOT NULL, at REAL NOT NULL, text TEXT NOT NULL)""")
            self.conn.execute("CREATE INDEX IF NOT EXISTS chunks_session ON chunks(session, seq)")
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id')")
            self.conn.execute("""CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text); END""")
            self.conn.execute("""CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text); END""")

    def add_session(self, session: str, messages: Iterable[Dict[str, Any]], *, title: str = "", project: str = "", kind: str = "agent",
                    routine: str = "", at: Optional[float] = None) -> int:
        """Indexes what the person and the assistant said in a conversation, replacing what was kept of it before; returns how many pieces. A turn
        starts at each message of the person. Tool results and system messages are not indexed."""
        if kind not in KINDS:
            raise ValueError(f"kind: one of {', '.join(KINDS)}")
        stamp = time.time() if at is None else float(at)
        rows, turn, seq = [], 0, 0
        for message in messages:
            role = message.get("role") if isinstance(message, dict) else None
            if role not in ("user", "assistant"):
                continue
            text = text_of(message).strip()
            if not text:
                continue
            if role == "user":
                turn += 1
            if self.redact:
                text = self.redact(text)
            for part in pieces(text):
                rows.append((session, max(turn, 1), seq, role, title, project, kind, routine, stamp, part))
                seq += 1
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM chunks WHERE session = ?", (session,))
            self.conn.executemany("INSERT INTO chunks (session, turn, seq, role, title, project, kind, routine, at, text) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def drop(self, session: str) -> int:
        with self._lock, self.conn:
            return self.conn.execute("DELETE FROM chunks WHERE session = ?", (session,)).rowcount

    def count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def sessions(self) -> List[Dict[str, Any]]:
        """The conversations kept, newest first."""
        with self._lock:
            rows = self.conn.execute("""SELECT session, title, project, kind, routine, MAX(at) AS at, MAX(turn) AS turns, COUNT(*) AS pieces
                                        FROM chunks GROUP BY session ORDER BY at DESC""").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _expression(term: str) -> str:
        return _phrase(term) + ("*" if len(term) >= PREFIX_FROM else "")

    def search(self, query: str, *, since: Optional[float] = None, project: Optional[str] = None, kind: Optional[str] = None,
               routine: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """The pieces that share words with the query, best first, at most `PER_SESSION` from one conversation. Each says where it is from, when,
        and a short stretch of its text around the first shared word."""
        terms = query_terms(query)
        if not terms:
            return []
        where, args = ["chunks_fts MATCH ?"], [" OR ".join(self._expression(t) for t in terms)]
        for clause, value in (("c.at >= ?", since), ("c.project = ?", project), ("c.kind = ?", kind), ("c.routine = ?", routine)):
            if value is not None:
                where.append(clause)
                args.append(value)
        with self._lock:
            try:
                rows = self.conn.execute(f"""SELECT c.* FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE {' AND '.join(where)}
                                             ORDER BY chunks_fts.rank LIMIT ?""", (*args, CANDIDATES)).fetchall()
            except sqlite3.OperationalError:
                return []
        out, taken = [], {}
        for row in rows:
            if taken.get(row["session"], 0) >= PER_SESSION:
                continue
            taken[row["session"]] = taken.get(row["session"], 0) + 1
            out.append({"session": row["session"], "turn": row["turn"], "role": row["role"], "title": row["title"], "project": row["project"], "kind": row["kind"],
                        "routine": row["routine"], "at": row["at"], "snippet": self._snippet(row["text"], terms)})
            if len(out) >= max(1, limit):
                break
        return out

    @staticmethod
    def _snippet(text: str, terms: List[str]) -> str:
        folded = _fold(text)
        at = min((i for i in (folded.find(t) for t in terms) if i >= 0), default=0)
        start = max(0, at - SNIPPET_CHARS // 3)
        cut = text[start:start + SNIPPET_CHARS].strip()
        return ("[...] " if start else "") + cut + (" [...]" if start + SNIPPET_CHARS < len(text) else "")

    def open(self, session: str, first: int = 1, last: Optional[int] = None, max_chars: int = 12000) -> Dict[str, Any]:
        """The words of turns `first` to `last` of a conversation, as they were said. `more` says the text went past `max_chars` and was cut."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM chunks WHERE session = ? AND turn >= ? AND turn <= ? ORDER BY seq",
                                     (session, max(1, int(first)), int(last) if last is not None else 10 ** 9)).fetchall()
        out, used, more = [], 0, False
        for row in rows:
            if used + len(row["text"]) > max_chars and out:
                more = True
                break
            out.append({"turn": row["turn"], "role": row["role"], "text": row["text"][:max_chars]})
            used += len(row["text"])
        head = rows[0] if rows else None
        return {"session": session, "title": head["title"] if head else "", "project": head["project"] if head else "", "at": head["at"] if head else None,
                "messages": out, "more": more}

    def close(self) -> None:
        with self._lock:
            self.conn.close()
