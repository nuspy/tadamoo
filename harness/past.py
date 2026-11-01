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
CARD_ROLE = "card"                 # the role of a conversation's card, kept as one extra piece of the conversation (turn 0)
CARD_CHARS = 900                   # most of a card a search hit shows
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
            self.conn.execute("""CREATE TABLE IF NOT EXISTS cards (
                session TEXT PRIMARY KEY, made REAL NOT NULL, covers REAL NOT NULL, source TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, saw TEXT NOT NULL DEFAULT '')""")
            self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
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
            self.conn.execute("DELETE FROM chunks WHERE session = ? AND role != ?", (session, CARD_ROLE))      # the card stays until a new one replaces it
            self.conn.executemany("INSERT INTO chunks (session, turn, seq, role, title, project, kind, routine, at, text) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def drop(self, session: str) -> int:
        """Everything kept of a conversation, its card included; returns how many pieces of text went."""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM cards WHERE session = ?", (session,))
            gone = self.conn.execute("DELETE FROM chunks WHERE session = ?", (session,)).rowcount
            return gone

    def count(self) -> int:
        """The pieces of text kept (cards are not counted)."""
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM chunks WHERE role != ?", (CARD_ROLE,)).fetchone()[0])

    # -- cards: what a conversation is about, in the words a person may use later (harness/cards.py) ------------------

    def info(self, session: str) -> Optional[Dict[str, Any]]:
        """Where a conversation is from and when it was last changed, or None when it has no text kept."""
        with self._lock:
            row = self.conn.execute("SELECT title, project, kind, routine, MAX(at) AS at FROM chunks WHERE session = ? AND role != ? GROUP BY session",
                                    (session, CARD_ROLE)).fetchone()
        return dict(row) if row else None

    def set_card(self, session: str, text: str, source: str, saw: str = "", failed: bool = False) -> bool:
        """Keeps `text` as the card of a conversation, replacing its old card; false when the conversation has no text kept (it was dropped meanwhile).
        `source` says who wrote it ("model" or "fallback"); a card written after a failed attempt counts the try."""
        with self._lock, self.conn:
            row = self.conn.execute("SELECT title, project, kind, routine, MAX(at) AS at FROM chunks WHERE session = ? AND role != ? GROUP BY session",
                                    (session, CARD_ROLE)).fetchone()
            if row is None or not text.strip():
                return False
            old = self.conn.execute("SELECT tries FROM cards WHERE session = ?", (session,)).fetchone()
            tries = (int(old["tries"]) if old else 0) + (1 if failed else 0)
            self.conn.execute("DELETE FROM chunks WHERE session = ? AND role = ?", (session, CARD_ROLE))
            self.conn.execute("INSERT INTO chunks (session, turn, seq, role, title, project, kind, routine, at, text) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (session, 0, -1, CARD_ROLE, row["title"], row["project"], row["kind"], row["routine"], row["at"], text.strip()))
            self.conn.execute("INSERT OR REPLACE INTO cards (session, made, covers, source, tries, saw) VALUES (?,?,?,?,?,?)",
                              (session, time.time(), row["at"], source, tries, saw))
        return True

    def card_of(self, session: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("""SELECT c.text, k.made, k.covers, k.source, k.tries, k.saw FROM cards k JOIN chunks c ON c.session = k.session AND c.role = ?
                                       WHERE k.session = ?""", (CARD_ROLE, session)).fetchone()
        return dict(row) if row else None

    def cards_wanted(self, limit: int = 1, model: bool = True, tries: int = 3) -> List[str]:
        """Conversations whose card is missing, then those changed since their card, then (with `model`) those whose card was written by code
        and has not been tried `tries` times; newest first within each group."""
        with self._lock:
            rows = self.conn.execute("""SELECT t.session AS session, t.at AS at, k.source AS source, k.covers AS covers, COALESCE(k.tries, 0) AS tries
                                        FROM (SELECT session, MAX(at) AS at FROM chunks WHERE role != ? GROUP BY session) t
                                        LEFT JOIN cards k ON k.session = t.session ORDER BY t.at DESC""", (CARD_ROLE,)).fetchall()
        missing = [r["session"] for r in rows if r["source"] is None]
        changed = [r["session"] for r in rows if r["source"] is not None and r["at"] > r["covers"] + 1e-6]
        upgrade = [r["session"] for r in rows if model and r["source"] == "fallback" and r["tries"] < tries and r["at"] <= r["covers"] + 1e-6]
        return (missing + changed + upgrade)[:max(0, limit)]

    def card_counts(self) -> Dict[str, int]:
        """The conversations kept, those with a card that is up to date, and who wrote those."""
        with self._lock:
            rows = self.conn.execute("""SELECT k.source AS source, t.at AS at, k.covers AS covers
                                        FROM (SELECT session, MAX(at) AS at FROM chunks WHERE role != ? GROUP BY session) t
                                        LEFT JOIN cards k ON k.session = t.session""", (CARD_ROLE,)).fetchall()
        current = [r for r in rows if r["source"] is not None and r["at"] <= r["covers"] + 1e-6]
        return {"total": len(rows), "current": len(current), "model": sum(1 for r in current if r["source"] == "model"),
                "fallback": sum(1 for r in current if r["source"] == "fallback")}

    def meta_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def sessions(self) -> List[Dict[str, Any]]:
        """The conversations kept, newest first."""
        with self._lock:
            rows = self.conn.execute("""SELECT session, title, project, kind, routine, MAX(at) AS at, MAX(turn) AS turns, COUNT(*) AS pieces
                                        FROM chunks WHERE role != 'card' GROUP BY session ORDER BY at DESC""").fetchall()
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
        rows: List[sqlite3.Row] = []
        with self._lock:
            try:
                for clause in ("c.role = ?", "c.role != ?"):                         # the cards first, then the text
                    rows += self.conn.execute(f"""SELECT c.* FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE {' AND '.join(where)} AND {clause}
                                                  ORDER BY chunks_fts.rank LIMIT ?""", (*args, CARD_ROLE, CANDIDATES)).fetchall()
            except sqlite3.OperationalError:
                return []
        out, taken = [], {}
        for row in rows:
            if taken.get(row["session"], 0) >= PER_SESSION:
                continue
            taken[row["session"]] = taken.get(row["session"], 0) + 1
            card = row["role"] == CARD_ROLE
            out.append({"session": row["session"], "turn": max(1, row["turn"]), "role": row["role"], "title": row["title"], "project": row["project"], "kind": row["kind"],
                        "routine": row["routine"], "at": row["at"], "card": card, "snippet": row["text"][:CARD_CHARS] if card else self._snippet(row["text"], terms)})
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


def register_past_tools(registry: Any, index: PastIndex, local: Callable[[], bool] = lambda: True) -> None:
    """`search_past` and `open_past`: the model looks for what was said in the person's earlier conversations and reads it again as it was said.

    What comes back is untrusted data. It stays on this PC: when the request goes to another provider the tools answer with nothing."""
    from harness.tools.registry import EffectClass

    def day(stamp: Any) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(float(stamp))) if stamp else ""

    def search_past(query: str, days: int = 0, project: str = "", kind: str = "", limit: int = 5) -> Any:
        if not local():
            return {"hits": [], "note": "the earlier conversations stay on this PC and this request goes elsewhere"}
        since = time.time() - int(days) * 86400 if int(days or 0) > 0 else None
        hits = index.search(str(query), since=since, project=project or None, kind=kind if kind in KINDS else None, limit=max(1, min(int(limit), 20)))
        return {"hits": [{"conversation": h["session"], "turn": h["turn"], "title": h["title"], "project": h["project"], "kind": h["kind"], "routine": h["routine"],
                          "date": day(h["at"]), "who": h["role"], "card": h["card"], "text": h["snippet"]} for h in hits],
                "next": "open_past(conversation, first_turn, last_turn) reads the turns around a hit in full"}

    def open_past(conversation: str, first_turn: int = 1, last_turn: int = 0) -> Any:
        if not local():
            return {"messages": [], "note": "the earlier conversations stay on this PC and this request goes elsewhere"}
        got = index.open(str(conversation), int(first_turn), int(last_turn) if int(last_turn or 0) > 0 else None)
        return {"conversation": got["session"], "title": got["title"], "project": got["project"], "date": day(got["at"]),
                "messages": [{"turn": m["turn"], "who": m["role"], "text": m["text"]} for m in got["messages"]],
                "cut": got["more"]}

    registry.register(
        name="search_past",
        description=("Searches the person's earlier conversations (every chat, project and, where the person allowed it, routine) by words. Use it when they refer to something "
                     "said before, in this or another conversation. Each conversation also has a card (a hit with card true) that lists its topics, entities and the other names they "
                     "go by; a card comes first. The words used then may differ from yours: if a search finds nothing, search again with the other name, the abbreviation, the "
                     "other language (UE, EU, Unione Europea, European Union) or a synonym, and then open the conversation with open_past."),
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to look for"},
            "days": {"type": "integer", "description": "Only the last N days (0: any time)", "default": 0},
            "project": {"type": "string", "description": "Only this project's folder name", "default": ""},
            "kind": {"type": "string", "enum": ["", *KINDS], "default": ""},
            "limit": {"type": "integer", "default": 5}}, "required": ["query"]},
        effect=EffectClass.PURE, handler=search_past, untrusted=True)
    registry.register(
        name="open_past",
        description=("Reads turns of an earlier conversation as they were said, by the conversation id and turn numbers that search_past gave. For a hit that is a card, "
                     "read from turn 1: the card is a summary, the turns are what was said."),
        parameters={"type": "object", "properties": {
            "conversation": {"type": "string", "description": "The conversation id from search_past"},
            "first_turn": {"type": "integer", "default": 1},
            "last_turn": {"type": "integer", "description": "0: to the end", "default": 0}}, "required": ["conversation"]},
        effect=EffectClass.PURE, handler=open_past, untrusted=True)
