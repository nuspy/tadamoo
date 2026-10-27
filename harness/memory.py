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
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
MAX_MEMORY_CHARS = 2000
MAX_MEMORIES = 5000
CHARS_PER_TOKEN = 3.5
MAX_QUERY_TERMS = 24
PREFIX_FROM = 4                    # a query word this long also matches the words that begin with it (build: builds, building)
CANDIDATES = 200
MAX_FULL_TERMS = 3                 # a request of this many words or fewer is answered by a note that has them all, whatever their weight
_CATEGORY = re.compile(r"[^a-z0-9_\-]+")
# a word, with the _ - . that join the parts of an identifier (sm_120, e-mail, v1.2.3) kept inside it
_WORD = re.compile(r"[^\W_]+(?:[_.\-][^\W_]+)*")
_PART = re.compile(r"[^\W_]+")

# Words that say nothing about a subject, in English and Italian, as they are after accents are folded away.
STOP_WORDS = frozenset(
    """a about above after again all also am an and any are as at be because been before being below between both but by can could did do does
    doing down during each few for from further had has have having he her here hers him his how i if in into is it its itself just me more most my
    no nor not now of off on once only or other our out over own same she should so some such than that the their them then there these they this
    those through to too under until up very was we were what when where which while who whom why will with would you your yours please like
    ad agli ai al alla alle allo anche che chi ci come con contro cui da dal dalla dalle dallo dei del della delle dello dentro di dove e ed era erano
    essere fra gli ha hai hanno ho i il in io la le lei lo loro lui ma mi mia mie miei mio ne nei nel nella nelle nello noi non nostro o per perche
    piu qual quale quali quando quanto quella quelle quelli quello questa queste questi questo se sei si sia siamo sono su sua sue sui sul sulla sulle
    suo suoi tra tu tua tue tuo tuoi un una uno vi voi vostro cosa fare fai fa puo puoi potresti vorrei voglio dimmi dammi""".split())


def _fold(text: str) -> str:
    """Lower case without accents, the way the full-text index sees words."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def canonical_workspace(workspace: str | Path) -> Path:
    """The folder that names a project. A linked git work tree (a conversation's own copy of a repository) is the same project as the
    folder it was made from, so it maps to the same place below the repository's main work tree; any other folder is itself."""
    here = Path(workspace).resolve()
    for folder in (here, *here.parents):
        marker = folder / ".git"
        if marker.is_dir():
            return here
        if marker.is_file():
            try:
                pointer = marker.read_text(encoding="utf-8", errors="replace").strip()
                if not pointer.startswith("gitdir:"):
                    return here
                gitdir = (folder / pointer[len("gitdir:"):].strip()).resolve()
                common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
            except (OSError, ValueError):
                return here
            if common.name != ".git" or not common.parent.is_dir():
                return here
            return (common.parent / here.relative_to(folder)).resolve()
    return here


def default_db_path(workspace: str | Path) -> Path:
    """Where the memory of a workspace is kept: <user data>/memory/<hash of the project folder>.db. The conversations that work in
    copies of one repository share the memory of the repository."""
    try:
        from serve.userdata import user_data_dir
        base = user_data_dir()
    except Exception:  # the harness also runs without the server package
        base = Path.home() / ".basalt"
    digest = hashlib.sha1(str(canonical_workspace(workspace)).lower().encode("utf-8")).hexdigest()[:16]
    return base / "memory" / f"{digest}.db"


def query_terms(query: str) -> List[str]:
    """The words of a request that say something: identifiers stay whole (sm_120, e-mail), the common words of English and Italian go."""
    seen: Dict[str, None] = {}
    for word in _WORD.findall(_fold(query)):
        if len(word) >= 2 and word not in STOP_WORDS and word.upper() not in _OPERATORS:
            seen.setdefault(word)
    return list(seen)[:MAX_QUERY_TERMS]


def _specific(term: str) -> bool:
    """Whether a word names something in particular: an identifier with parts, or letters mixed with digits."""
    return len(_PART.findall(term)) > 1 or (any(c.isdigit() for c in term) and any(c.isalpha() for c in term))


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


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

    @staticmethod
    def _expression(term: str) -> str:
        return _phrase(term) + ("*" if len(term) >= PREFIX_FROM else "")

    def ranked(self, query: str) -> List[Dict[str, Any]]:
        """The memories that share a word with the query, best first. A shared word scores up to 1 point, fewer the more notes have it,
        and 2 times that when it names something specific (sm_120, e-mail, gpt4); the points of a note are its `score`, and `covers` says
        it has every word of the query. Common words of English and Italian are not words of the query."""
        terms = query_terms(query)
        if not terms:
            return []
        with self._lock:
            try:
                rows = self._match(" OR ".join(self._expression(t) for t in terms), CANDIDATES)
                total = self.count()
                weights = {}
                for t in terms:
                    n = self.conn.execute("SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?", (self._expression(t),)).fetchone()[0]
                    weights[t] = max(0.0, 1 - (n - 1) / max(1, total)) * (2 if _specific(t) else 1)
            except sqlite3.OperationalError:
                return []
        parts = {t: _PART.findall(t) for t in terms}
        out = []
        for position, row in enumerate(rows):
            tokens = _PART.findall(_fold(row["content"]))
            hit = [t for t in terms if self._occurs(parts[t], tokens, len(t) >= PREFIX_FROM)]
            if hit:
                out.append({**row, "score": sum(weights[t] for t in hit), "covers": len(hit) == len(terms), "_position": position})
        out.sort(key=lambda r: (-r["score"], r["_position"]))
        for r in out:
            del r["_position"]
        return out

    @staticmethod
    def _occurs(words: List[str], tokens: List[str], prefix: bool) -> bool:
        """Whether the words of a term follow each other in the tokens of a note (the last one may be the start of a word)."""
        n = len(words)
        for i in range(len(tokens) - n + 1):
            if tokens[i:i + n - 1] == words[:-1] and (tokens[i + n - 1].startswith(words[-1]) if prefix else tokens[i + n - 1] == words[-1]):
                return True
        return False

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """The memories that share words with the query, best match first."""
        return self.ranked(query)[:max(0, limit)]

    def relevant(self, query: str, limit: int = 4, min_score: float = 1.5, budget_tokens: int = 600) -> List[Dict[str, Any]]:
        """The notes worth putting before the model for this request: those whose shared words score `min_score` points or more (or that
        have every word of a request of three or fewer), at most `limit` of them, and as many as fit in `budget_tokens`."""
        out: List[Dict[str, Any]] = []
        used = 0
        few = len(query_terms(query)) <= MAX_FULL_TERMS
        for note in self.ranked(query):
            if len(out) >= limit:
                break
            if min_score > 0 and note["score"] < min_score and not (few and note["covers"]):
                continue
            cost = estimate_tokens(note["content"])
            if used + cost > budget_tokens:
                if out:
                    break
                note = {**note, "content": note["content"][:int(budget_tokens * CHARS_PER_TOKEN)] + " [...]"}
                cost = budget_tokens
            used += cost
            out.append(note)
        return out

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
