"""The knowledge archives: a folder of the user data, `rag/<id>/`, for each.

    meta.json         name, description, the documents, how many passages, the embedding model of the vectors (empty: text only)
    chunks.jsonl      one passage per line: {"doc", "n", "text"}
    embeddings.npy    float32, one L2-normalized row per passage, present only when every passage has one
    texts/<doc>.txt   the text read from each document, so an archive can be indexed again without the original

The files are written to a temporary name and renamed; `meta.json` goes last. Nothing outside `rag/` is reachable: an archive id is a plain
slug, a document is named by its own id, and a path given to read is refused when it climbs or is inside the data folder."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.personas import slug_of
from tadamoo.rag import search as ranking
from tadamoo.rag.chunking import chunk_text
from tadamoo.rag.embed import Embedder, EmbedderProvider, embed_all, no_embedder
from tadamoo.rag.errors import RagError
from tadamoo.rag.extract import check_name, extract

LIMITS = {"name": 60, "description": 500, "archives": 50, "documents": 200, "document_bytes": 25 << 20, "document_chars": 4_000_000, "chunks": 20_000, "top_k": 20}
TOP_K = 4
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DOC_ID = re.compile(r"^d[0-9a-f]{8}$")
INCOMING = ".incoming"
_NAME_BAD = re.compile(r"[\x00-\x1f\\/]")


def _text(value: Any, field: str, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise RagError(f"{field}: text")
    value = value.replace("\r\n", "\n").strip()
    if len(value) > limit:
        raise RagError(f"{field}: at most {limit} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise RagError(f"{field}: no control characters")
    if required and not value:
        raise RagError(f"{field}: it needs a value")
    return value


def _copy(meta: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(meta))


class Archive:
    """One archive as loaded: its meta, its passages and its vectors, with the word index made when first needed."""

    def __init__(self, meta: Dict[str, Any], chunks: List[Dict[str, Any]], vectors: Optional[np.ndarray]) -> None:
        self.meta, self.chunks, self.vectors = meta, chunks, vectors
        self._index: Optional[ranking.Bm25] = None

    def index(self) -> ranking.Bm25:
        if self._index is None:
            self._index = ranking.Bm25([c["text"] for c in self.chunks])
        return self._index

    def document(self, doc_id: str) -> Dict[str, Any]:
        return next((d for d in self.meta["documents"] if d["id"] == doc_id), {"id": doc_id, "name": ""})


class RagStore:
    def __init__(self, root: str | Path, embedder: EmbedderProvider = no_embedder, forbidden: Sequence[str | Path] = (),
                 clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.provider, self.clock = embedder, clock
        self.forbidden = [Path(p).resolve() for p in forbidden]
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[int, Archive]] = {}

    def embedder(self) -> Optional[Embedder]:
        try:
            return self.provider()
        except Exception:                                  # an embedder that cannot start means text search, not a failed request
            return None

    # -- where things are ------------------------------------------------------------------------

    def _dir(self, aid: Any) -> Path:
        if not isinstance(aid, str) or not ID.match(aid):
            raise RagError("there is no such archive", "not_found")
        path = (self.root / aid).resolve()
        if path.parent != self.root.resolve():
            raise RagError("there is no such archive", "not_found")
        return path

    @staticmethod
    def _put(path: Path, data: bytes | str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _put_meta(self, folder: Path, meta: Dict[str, Any]) -> None:
        self._put(folder / "meta.json", json.dumps(meta, indent=1, ensure_ascii=False))
        self._cache.pop(folder.name, None)

    @staticmethod
    def _read_meta(folder: Path) -> Dict[str, Any]:
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RagError("there is no such archive", "not_found") from None
        except (OSError, ValueError):
            raise RagError("the archive file could not be read") from None
        if not isinstance(meta, dict) or not isinstance(meta.get("documents"), list) or meta.get("id") != folder.name:
            raise RagError("the archive file is damaged")
        return meta

    def load(self, aid: Any) -> Archive:
        """The archive with its passages and vectors, kept in memory until it changes."""
        folder = self._dir(aid)
        with self._lock:
            meta_file = folder / "meta.json"
            stamp = meta_file.stat().st_mtime_ns if meta_file.is_file() else 0
            cached = self._cache.get(aid)
            if cached and cached[0] == stamp:
                return cached[1]
            meta = self._read_meta(folder)
            known = {d["id"] for d in meta["documents"]}
            chunks: List[Dict[str, Any]] = []
            try:
                with open(folder / "chunks.jsonl", encoding="utf-8") as f:
                    chunks = [c for c in (json.loads(line) for line in f if line.strip()) if c.get("doc") in known]
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                raise RagError("the passages of the archive could not be read") from None
            vectors = None
            if meta.get("embed_model") and (folder / "embeddings.npy").is_file():
                try:
                    found = np.load(folder / "embeddings.npy", allow_pickle=False)
                    if found.ndim == 2 and found.shape[0] == len(chunks) == meta.get("chunks"):
                        vectors = found.astype(np.float32)
                except (OSError, ValueError):
                    vectors = None
            archive = Archive(meta, chunks, vectors)
            self._cache[aid] = (stamp, archive)
            return archive

    def _commit(self, folder: Path, meta: Dict[str, Any], chunks: List[Dict[str, Any]], vectors: Optional[np.ndarray], model: str) -> None:
        meta.update(chunks=len(chunks), embed_model=model if vectors is not None else "", updated=self.clock())
        self._put(folder / "chunks.jsonl", "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks))
        if vectors is not None:
            tmp = folder / "embeddings.npy.tmp"
            with open(tmp, "wb") as f:
                np.save(f, vectors, allow_pickle=False)
            os.replace(tmp, folder / "embeddings.npy")
        else:
            (folder / "embeddings.npy").unlink(missing_ok=True)
        self._put_meta(folder, meta)

    # -- the archives ----------------------------------------------------------------------------

    def status(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        """How an archive is searched now: `hybrid` (words and the vectors of the embedder in use) or `text`; `reindex` says that indexing it again would change that."""
        emb, model = self.embedder(), meta.get("embed_model", "")
        hybrid = bool(model and emb is not None and emb.model == model)
        return {"mode": "hybrid" if hybrid else "text", "embed_model": model, "reindex": bool(meta["chunks"]) and emb is not None and not hybrid}

    def _summary(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": meta["id"], "name": meta["name"], "description": meta["description"], "cloud_ok": bool(meta.get("cloud_ok")), "created": meta["created"],
                "updated": meta["updated"], "documents": len(meta["documents"]), "chunks": meta["chunks"], "bytes": sum(d["bytes"] for d in meta["documents"]),
                **self.status(meta)}

    def list(self) -> List[Dict[str, Any]]:
        out = []
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir()):
                if folder.is_dir() and ID.match(folder.name):
                    try:
                        out.append(self._summary(self._read_meta(folder)))
                    except RagError:
                        continue                           # a folder that is not an archive, or is damaged, is left out
        return sorted(out, key=lambda a: (a["name"].lower(), a["id"]))

    def get(self, aid: Any) -> Dict[str, Any]:
        meta = self.load(aid).meta
        return {**self._summary(meta), "document_list": [dict(d) for d in meta["documents"]]}

    def create(self, name: Any, description: Any = "") -> Dict[str, Any]:
        name, description = _text(name, "name", LIMITS["name"], True), _text(description, "description", LIMITS["description"])
        with self._lock:
            existing = self.list()
            if len(existing) >= LIMITS["archives"]:
                raise RagError(f"at most {LIMITS['archives']} archives")
            if any(a["name"].lower() == name.lower() for a in existing):
                raise RagError("there is already an archive with that name")
            aid = f"{slug_of(name) or 'archive'}-{secrets.token_hex(3)}"
            now = self.clock()
            meta = {"id": aid, "name": name, "description": description, "cloud_ok": False, "created": now, "updated": now, "chunks": 0, "embed_model": "", "documents": []}
            self._put_meta(self._dir(aid), meta)
            return self.get(aid)

    def update(self, aid: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        extra = [k for k in data if k not in ("name", "description", "cloud_ok")]
        if extra:
            raise RagError("unknown field: " + ", ".join(sorted(map(str, extra))[:5]))
        with self._lock:
            folder, meta = self._dir(aid), _copy(self.load(aid).meta)
            if "name" in data:
                name = _text(data["name"], "name", LIMITS["name"], True)
                if any(a["name"].lower() == name.lower() and a["id"] != aid for a in self.list()):
                    raise RagError("there is already an archive with that name")
                meta["name"] = name
            if "description" in data:
                meta["description"] = _text(data["description"], "description", LIMITS["description"])
            if "cloud_ok" in data:
                if not isinstance(data["cloud_ok"], bool):
                    raise RagError("cloud_ok: true or false")
                meta["cloud_ok"] = data["cloud_ok"]
            meta["updated"] = self.clock()
            self._put_meta(folder, meta)
            return self.get(aid)

    def delete(self, aid: Any, confirm: Any) -> Dict[str, Any]:
        if confirm is not True:
            raise RagError("deleting an archive deletes its documents' text and index: it needs the confirmation")
        with self._lock:
            folder, meta = self._dir(aid), self.load(aid).meta
            shutil.rmtree(folder)
            self._cache.pop(aid, None)
            return {"deleted": True, "documents": len(meta["documents"])}

    # -- the documents ---------------------------------------------------------------------------

    def check_path(self, raw: Any) -> Path:
        """A file the person names on this PC: absolute, no `..`, a regular file, and not inside a forbidden folder (the data folder, where the vault is)."""
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise RagError("path: the full path of a file")
        path = Path(raw.strip())
        if not path.is_absolute() or ".." in path.parts:
            raise RagError("path: a full path without '..'")
        real = path.resolve()
        if not real.is_file():
            raise RagError("there is no such file")
        if any(real == root or root in real.parents for root in self.forbidden):
            raise RagError("that file is inside the TaDaMoo data folder and cannot be added")
        return real

    def add_path(self, aid: Any, raw: Any) -> Dict[str, Any]:
        path = self.check_path(raw)
        return self.add_file(aid, path.name, path)

    def add_bytes(self, aid: Any, name: Any, data: bytes) -> Dict[str, Any]:
        self._dir(aid)
        name = self._doc_name(name)
        check_name(name)
        if len(data) > LIMITS["document_bytes"]:
            raise RagError(f"a document is at most {LIMITS['document_bytes'] >> 20} MB")
        incoming = self.root / INCOMING
        incoming.mkdir(parents=True, exist_ok=True)
        tmp = incoming / (secrets.token_hex(8) + Path(name).suffix.lower())
        try:
            tmp.write_bytes(data)
            return self.add_file(aid, name, tmp)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _doc_name(name: Any) -> str:
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or _NAME_BAD.search(name) or name.strip() in (".", ".."):
            raise RagError("name: the name of a file, without folders, up to 200 characters")
        return name.strip()

    def add_file(self, aid: Any, name: str, path: Path) -> Dict[str, Any]:
        """Reads the file at `path` as a document called `name` and indexes it; nothing is kept when it is refused."""
        self.load(aid)
        name = self._doc_name(name)
        check_name(name)
        size = path.stat().st_size
        if not 0 < size <= LIMITS["document_bytes"]:
            raise RagError("the file is empty" if size == 0 else f"a document is at most {LIMITS['document_bytes'] >> 20} MB")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        text = extract(path, name)
        if len(text) > LIMITS["document_chars"]:
            raise RagError(f"the text is {len(text):,} characters: at most {LIMITS['document_chars']:,} per document")
        pieces = chunk_text(text)
        with self._lock:
            archive = self.load(aid)
            meta = _copy(archive.meta)
            if len(meta["documents"]) >= LIMITS["documents"]:
                raise RagError(f"at most {LIMITS['documents']} documents in an archive")
            if any(d["sha256"] == sha for d in meta["documents"]):
                raise RagError("this file is already in the archive")
            if len(archive.chunks) + len(pieces) > LIMITS["chunks"]:
                raise RagError(f"the archive would hold more than {LIMITS['chunks']:,} passages")
            doc = {"id": "d" + secrets.token_hex(4), "name": name, "ext": Path(name).suffix.lower(), "bytes": size, "chars": len(text), "chunks": len(pieces),
                   "sha256": sha, "added": self.clock()}
            folder = self._dir(aid)
            self._put(folder / "texts" / (doc["id"] + ".txt"), text)
            vectors, model, note = self._extend(archive, pieces)
            meta["documents"].append(doc)
            self._commit(folder, meta, archive.chunks + [{"doc": doc["id"], "n": i, "text": p} for i, p in enumerate(pieces)], vectors, model)
            return {"document": doc, "embedded": vectors is not None, "note": note}

    def _extend(self, archive: Archive, pieces: List[str]) -> Tuple[Optional[np.ndarray], str, str]:
        """(the archive's vectors with these passages added, their model, a note): vectors only when every passage has one."""
        emb = self.embedder()
        if emb is None or not pieces:
            return None, "", ""
        try:
            fresh = embed_all(emb, pieces)
        except Exception as e:
            return None, "", f"text search only: the embedder failed ({str(e)[:120]})"
        if archive.chunks and (archive.vectors is None or archive.meta["embed_model"] != emb.model or archive.vectors.shape[1] != fresh.shape[1]):
            return None, "", "some passages of the archive have no vectors: index it again to use them"
        return (fresh if archive.vectors is None else np.vstack([archive.vectors, fresh])), emb.model, ""

    def remove_document(self, aid: Any, doc_id: Any) -> Dict[str, Any]:
        if not isinstance(doc_id, str) or not DOC_ID.match(doc_id):
            raise RagError("there is no such document", "not_found")
        with self._lock:
            archive, folder = self.load(aid), self._dir(aid)
            meta = _copy(archive.meta)
            if not any(d["id"] == doc_id for d in meta["documents"]):
                raise RagError("there is no such document", "not_found")
            meta["documents"] = [d for d in meta["documents"] if d["id"] != doc_id]
            keep = [i for i, c in enumerate(archive.chunks) if c["doc"] != doc_id]
            vectors = archive.vectors[keep] if archive.vectors is not None and keep else None
            self._commit(folder, meta, [archive.chunks[i] for i in keep], vectors, archive.meta["embed_model"])
            (folder / "texts" / (doc_id + ".txt")).unlink(missing_ok=True)
            return {"removed": True, "chunks": len(keep)}

    def reindex(self, aid: Any) -> Dict[str, Any]:
        """Cuts every document's saved text into passages again and embeds them with the embedder in use, if there is one."""
        with self._lock:
            archive, folder = self.load(aid), self._dir(aid)
            meta = _copy(archive.meta)
            chunks: List[Dict[str, Any]] = []
            for doc in meta["documents"]:
                try:
                    text = (folder / "texts" / (doc["id"] + ".txt")).read_text(encoding="utf-8")
                except OSError:
                    raise RagError(f"the saved text of {doc['name']} is missing: add the document again") from None
                pieces = chunk_text(text)
                doc["chunks"] = len(pieces)
                chunks += [{"doc": doc["id"], "n": i, "text": p} for i, p in enumerate(pieces)]
            if len(chunks) > LIMITS["chunks"]:
                raise RagError(f"the archive would hold more than {LIMITS['chunks']:,} passages")
            vectors, model, note = self._extend(Archive(meta, [], None), [c["text"] for c in chunks])
            self._commit(folder, meta, chunks, vectors, model)
            return {"chunks": len(chunks), "embedded": vectors is not None, "note": note}

    # -- asking ----------------------------------------------------------------------------------

    def search(self, ids: Sequence[str], query: Any, top_k: int = TOP_K, min_share: float = 0.0, strict: bool = True) -> Dict[str, Any]:
        """The best passages of the archives `ids` for `query`. A words-only hit must answer at least `min_share` of the question (0: any hit).
        With `strict` False an archive that is gone is left out instead of refused."""
        from tadamoo.rag.query import run
        if not isinstance(query, str):
            raise RagError("query: the text of the question")
        loaded = []
        for aid in ids:
            try:
                loaded.append(self.load(aid))
            except RagError:
                if strict:
                    raise
        return run(loaded, self.embedder(), query.strip()[:2000], max(1, min(int(top_k), LIMITS["top_k"])), min_share)
