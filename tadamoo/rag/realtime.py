"""Searching the knowledge archives while somebody waits: a meeting, a call, the copilot, the avatar.

The search of `query.py` is made for a question that can take seconds: its embedder lives on a lease of the VRAM broker, which the voice functions
keep while they are loaded, and a cold worker needs a long time to start. Here the question is embedded by a small model on the CPU, kept warm for as
long as the conversation lasts, against a second set of vectors the archive carries for that model (`embeddings_rt.npy`, made in the background), and
the answer is hybrid like the usual one (cosine plus a small word share), never words alone and never reranked. A search has a budget: when the
hybrid answer is not there in time the words-only answer, which is ready in milliseconds, is returned with a marker that says so.

The usual search is used as it is (`query.run`): the second vectors are given to it as the vectors of a view of the archive."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from tadamoo.rag import query as q
from tadamoo.rag.embed import embed_all, normalize
from tadamoo.rag.embed_client import VERSION, EmbedService, Unavailable, venv_python
from tadamoo.rag.errors import RagError
from tadamoo.rag.store import RagStore, searchable

MODEL = "Qwen3-Embedding-0.6B"          # the small embedder: its folder name under rag/embedding
THREADS = 4                              # CPU threads of the question embedder: the engine's own CPU work keeps the rest
IDLE_S = 900.0                           # the warm worker is given back after this long without a question
VECTORS, META = "embeddings_rt.npy", "rt.json"
WORKER = Path(__file__).with_name("rt_worker.py")
TOP_K = 4

# What a search is allowed to take, by the function that asks (seconds). The copilot and the spoken reply are heard or read while the other person
# is still talking; the minutes are written once, afterwards.
BUDGET_S = {"copilot": 1.2, "meeting": 1.2, "call": 0.9, "avatar": 0.9, "minutes": 20.0}
DEFAULT_BUDGET_S = 1.2


# -- the second vectors of an archive ---------------------------------------------------------------------

def fingerprint(chunks: Sequence[Dict[str, Any]]) -> str:
    """Names the passages the vectors were made from: another cutting of the archive gives another one and the vectors are not used."""
    h = hashlib.blake2b(digest_size=12)
    h.update(str(len(chunks)).encode())
    for c in chunks[:: max(1, len(chunks) // 64)]:
        h.update(searchable(c).encode("utf-8", "replace"))
    return h.hexdigest()


def read_vectors(folder: Path, chunks: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """`{model, vectors}` of an archive folder when the second vectors are there and made from these passages, else None."""
    try:
        meta = json.loads((folder / META).read_text(encoding="utf-8"))
        found = np.load(folder / VECTORS, allow_pickle=False)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("fingerprint") != fingerprint(chunks) or meta.get("version") != VERSION:
        return None
    if found.ndim != 2 or found.shape[0] != len(chunks):
        return None
    return {"model": str(meta.get("model") or ""), "vectors": normalize(found)}


def write_vectors(folder: Path, chunks: Sequence[Dict[str, Any]], vectors: np.ndarray, model: str) -> None:
    """The vectors and the note that names them, each written to a temporary name and renamed; the note goes last."""
    data = np.asarray(vectors, dtype=np.float16)
    tmp = folder / (VECTORS + ".tmp")
    with open(tmp, "wb") as f:
        np.save(f, data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, folder / VECTORS)
    note = {"model": model, "version": VERSION, "dim": int(data.shape[1]), "passages": len(chunks), "fingerprint": fingerprint(chunks), "built": time.time()}
    tmp = folder / (META + ".tmp")
    tmp.write_text(json.dumps(note), encoding="utf-8")
    os.replace(tmp, folder / META)


class View:
    """An archive as the search sees it with the second vectors: the same passages, words and documents, the vectors of the small model."""

    def __init__(self, archive: Any, vectors: np.ndarray, model: str) -> None:
        self._archive = archive
        self.chunks, self.vectors = archive.chunks, vectors
        self.meta = {**archive.meta, "embed_model": model, "embed_version": VERSION, "embed_dim": int(vectors.shape[1])}

    def index(self):
        return self._archive.index()

    def document(self, doc_id: str):
        return self._archive.document(doc_id)


# -- the services -----------------------------------------------------------------------------------------

class NoLease:
    """A lease that is never granted: the worker it is given to runs on the CPU, whatever the broker could lend."""
    gpu_ok = False
    reason, message = "cpu_profile", "the real-time embedder runs on the CPU"
    granted_mib = 0
    lost = False

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def acquire(self) -> None:
        pass

    def release(self) -> None:
        pass


def _settings(model: str, idle_s: float) -> Callable[[], Dict[str, Any]]:
    return lambda: {"embed_enabled": True, "embed_model": model, "rerank": False, "idle_s": idle_s}


def query_service(models_dirs: Callable[[], Sequence[str]], model: str = MODEL, threads: int = THREADS, idle_s: float = IDLE_S, **kw: Any) -> EmbedService:
    """The warm CPU worker that embeds questions: no lease, a small model, a limited number of threads, kept for `idle_s` after the last question."""
    svc = EmbedService(_settings(model, idle_s), models_dirs, lease_factory=NoLease, **kw)
    python = venv_python(svc.venv_roots)
    if python:
        svc.argv = [python, "-X", "utf8", str(WORKER), str(int(threads))]
    return svc


def build_service(models_dirs: Callable[[], Sequence[str]], lease_url: Callable[[], str], model: str = MODEL, **kw: Any) -> EmbedService:
    """The worker that embeds the passages of an archive for the second vectors: the usual lease when the broker can lend one, else the CPU."""
    return EmbedService(_settings(model, 60.0), models_dirs, lease_url, **kw)


def build(store: RagStore, aid: str, embedder: Any, progress: Optional[Callable[[Dict[str, Any]], None]] = None, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    """Makes the second vectors of an archive; `{passages, model, seconds}`. Not made when the archive has no passages."""
    started = time.monotonic()
    archive = store.load(aid)
    if not archive.chunks:
        raise RagError("the archive has no passages")
    vectors = embed_all(embedder, [searchable(c) for c in archive.chunks], progress, cancel)
    write_vectors(store.root / aid, archive.chunks, vectors, embedder.model)
    return {"passages": len(archive.chunks), "model": embedder.model, "seconds": round(time.monotonic() - started, 1)}


def has_vectors(store: RagStore, aid: str) -> bool:
    try:
        return read_vectors(store.root / aid, store.load(aid).chunks) is not None
    except RagError:
        return False


# -- the search -------------------------------------------------------------------------------------------

class Searcher:
    """Real-time search over archives of a store. `embedder` is the provider of the small model's embedder (the query service's `provider`)."""

    def __init__(self, store: RagStore, embedder: Callable[[], Any], clock: Callable[[], float] = time.monotonic, warm_query: str = "warm") -> None:
        self.store, self.embedder, self.clock, self.warm_query = store, embedder, clock, warm_query
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rag-rt")
        self._running = 0
        self._lock = threading.Lock()

    def warm(self) -> bool:
        """Loads the worker and the model now (a meeting or a call is starting). False when there is no small model."""
        emb = self.embedder()
        if emb is None:
            return False
        try:
            emb.embed_query(self.warm_query)
            return True
        except Exception:                                 # noqa: BLE001 - the first real question tries again and says what failed
            return False

    def _views(self, ids: Sequence[str]):
        """(the archives as loaded, their views with the second vectors where there are some, the ids of those without)."""
        loaded, views, missing = [], [], []
        for aid in ids:
            try:
                a = self.store.load(aid)
            except RagError:
                continue
            loaded.append(a)
            got = read_vectors(self.store.root / aid, a.chunks)
            if got is not None:
                views.append(View(a, got["vectors"], got["model"]))
            else:
                views.append(a)
                missing.append(a.meta["id"])
        return loaded, views, missing

    def words(self, archives: Sequence[Any], query: str, top_k: int, min_share: float = 0.0) -> Dict[str, Any]:
        return q.run(list(archives), None, query, top_k, min_share, {"rerank": False})

    def search(self, ids: Sequence[str], query: str, top_k: int = TOP_K, budget_s: float = DEFAULT_BUDGET_S, min_share: float = 0.0) -> Dict[str, Any]:
        """The answer of `query.run` plus `profile` (`hybrid-small` or `words`), `elapsed_ms`, `timed_out`, and `marker`: "" or why the answer is words only."""
        t0 = self.clock()
        loaded, views, missing = self._views(ids)
        out: Dict[str, Any] = {"mode": "lexical", "stages": ["lexical"], "note": "", "reranked": False, "hits": [], "profile": "words", "timed_out": False, "marker": ""}
        if not loaded or not query.strip():
            return {**out, "elapsed_ms": 0}
        fallback = self.words(loaded, query, top_k, min_share)

        def done(result: Dict[str, Any], profile: str, marker: str, timed_out: bool = False) -> Dict[str, Any]:
            note = result.get("note", "")
            if marker:
                note = (note + " " if note else "") + marker
            return {**result, "note": note, "profile": profile, "marker": marker, "timed_out": timed_out, "elapsed_ms": int((self.clock() - t0) * 1000)}

        ready = [v for v in views if isinstance(v, View)]
        if not ready:
            return done(fallback, "words", "words only: these archives have no vectors for the real-time search yet")
        emb = self.embedder()
        if emb is None:
            return done(fallback, "words", "words only: the real-time embedder is not available")
        with self._lock:
            if self._running >= 2:                         # earlier questions are still in the worker: do not queue behind them
                return done(fallback, "words", "words only: the embedder is busy", True)
            self._running += 1

        def hybrid() -> Dict[str, Any]:
            try:
                return q.run(views, emb, query, top_k, min_share, {"rerank": False})
            finally:
                with self._lock:
                    self._running -= 1

        future = self._pool.submit(hybrid)
        try:
            result = future.result(timeout=max(0.05, budget_s - (self.clock() - t0)))
        except FutureTimeout:
            return done(fallback, "words", f"words only: the semantic search took more than {int(budget_s * 1000)} ms", True)
        except Exception as e:                            # noqa: BLE001 - the worker failed: say it and give the words
            return done(fallback, "words", f"words only: the semantic search failed ({str(e)[:100]})")
        marker = "" if result.get("mode") == "hybrid" else "words only: " + (result.get("note") or "no vectors matched")
        if missing and not marker:
            marker = f"{len(missing)} archive(s) without vectors for the real-time search were read by words"
        return done(result, "hybrid-small" if result.get("mode") == "hybrid" else "words", marker)


class Indexer:
    """Makes the second vectors, in the background, for the archives that real-time functions use and that have none (or have them for other passages)."""

    def __init__(self, store: RagStore, embedder: Callable[[], Any], rest: Callable[[], None] = lambda: None) -> None:
        self.store, self.embedder, self.rest = store, embedder, rest
        self.state: Dict[str, str] = {}                  # id -> building | queued | failed: <why>
        self._queue: List[str] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def ensure(self, ids: Sequence[str]) -> List[str]:
        """Queues the archives of `ids` that need vectors; returns the ids that are queued or being made now."""
        with self._lock:
            for aid in ids:
                if aid in self._queue or self.state.get(aid) in ("building", "queued") or has_vectors(self.store, aid):
                    continue
                if str(self.state.get(aid, "")).startswith("failed") and self.state.get(aid) != "failed: retry":
                    continue                                 # a failure is not tried again until the archive or the models change (`forget`)
                self._queue.append(aid)
                self.state[aid] = "queued"
            if self._queue and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, name="rag-rt-index", daemon=True)
                self._thread.start()
            return [a for a in ids if self.state.get(a) in ("queued", "building")]

    def forget(self, aid: str) -> None:
        with self._lock:
            self.state.pop(aid, None)

    def status(self, ids: Sequence[str]) -> Dict[str, str]:
        """For each id: `ready`, `building`, `queued`, `missing` or `failed: <why>`."""
        out = {}
        for aid in ids:
            s = self.state.get(aid)
            out[aid] = s if s else ("ready" if has_vectors(self.store, aid) else "missing")
        return out

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                aid = self._queue.pop(0)
                self.state[aid] = "building"
            try:
                emb = self.embedder()
                if emb is None:
                    raise RagError("the small embedding model is not there")
                build(self.store, aid, emb)
                with self._lock:
                    self.state.pop(aid, None)
            except Exception as e:                           # noqa: BLE001 - kept as the state; the search says words only meanwhile
                with self._lock:
                    self.state[aid] = "failed: " + str(e)[:120]
            finally:
                self.rest()
