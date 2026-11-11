"""The knowledge archives: a folder of the user data, `rag/<id>/`, for each.

    meta.json         name, description, the project it belongs to (a key, tadamoo/rag/project.py, and its name as it was when the archive was made), the documents, how many passages, the embedding model
                      of the vectors with their size and version (`embed_model` empty: text only)
    chunks.jsonl      one passage per line: {"doc", "n", "text", "path", "page_start", "page_end", "kind", "section", "flag"?}
    sections.jsonl    one section per line, to expand a hit to the part of the document it came from: {"doc", "id", "parent", "path", "page_start", "page_end", "text"}
    embeddings.npy    float16, one L2-normalized row per passage (read back as float32 and normalized again), present only when every passage has one
    blocks/<doc>.json.gz   the blocks read from each document, before cleaning, so an archive can be cut again without the original or a new reading

The files are written to a temporary name and renamed; `meta.json` goes last. Nothing outside `rag/` is reachable: an archive id is a plain
slug, a document is named by its own id, and a path given to read is refused when it climbs or is inside the data folder."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.rag import search as ranking
from tadamoo.rag.embed import Embedder, EmbedderProvider, compatible, embed_all, no_embedder, normalize as unit_rows
from tadamoo.rag.errors import RagError
from tadamoo.rag.ingest import chunker, extraction_of, formats, normalize, read_document
from tadamoo.rag.project import is_key
from tadamoo.rag.settings import RagSettings

LIMITS = {"name": 60, "description": 500, "archives": 50, "documents": 1000, "document_bytes": 200 << 20, "document_chars": 40_000_000, "chunks": 200_000, "top_k": 20}
TOP_K = 4
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DOC_ID = re.compile(r"^d[0-9a-f]{8}$")
INCOMING = ".incoming"
INGEST = {"normalizer": normalize.VERSION, "chunker": chunker.VERSION}      # the versions that cut the passages of an archive: a change means cutting them again
_NAME_BAD = re.compile(r"[\x00-\x1f\\/]")


def slug_of(name: str) -> str:
    """A folder-name stem for an archive's name: lower-case ASCII letters, digits and hyphens."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:40].strip("-")


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


def searchable(chunk: Dict[str, Any]) -> str:
    """The text of a passage as it is searched and embedded: where it is in the document, then the passage."""
    return (chunk["path"] + "\n" + chunk["text"]) if chunk.get("path") else chunk["text"]


def _extraction(meta: Dict[str, Any]) -> str:
    """How the documents of an archive were read: `docling`, `basic`, `mixed`, or "" for an archive with no document."""
    kinds = {d.get("extraction", "basic") for d in meta["documents"]}
    return "" if not kinds else kinds.pop() if len(kinds) == 1 else "mixed"


def _copy(meta: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(meta))


class Archive:
    """One archive as loaded: its meta, its passages and its vectors, with the word index made when first needed."""

    def __init__(self, meta: Dict[str, Any], chunks: List[Dict[str, Any]], vectors: Optional[np.ndarray], sections: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.meta, self.chunks, self.vectors = meta, chunks, vectors
        self.sections: Dict[str, Dict[str, Any]] = sections or {}
        self._index: Optional[ranking.Bm25] = None

    def index(self) -> ranking.Bm25:
        if self._index is None:
            self._index = ranking.Bm25([searchable(c) for c in self.chunks])
        return self._index

    def document(self, doc_id: str) -> Dict[str, Any]:
        return next((d for d in self.meta["documents"] if d["id"] == doc_id), {"id": doc_id, "name": ""})


class RagStore:
    def __init__(self, root: str | Path, embedder: EmbedderProvider = no_embedder, forbidden: Sequence[str | Path] = (),
                 clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.settings = RagSettings(self.root)
        self.provider, self.clock = embedder, clock
        self.remote: Optional[Callable[[List[str], str, int, float], Dict[str, Any]]] = None      # a process with no embedder asks the server's store instead
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
            sections: Dict[str, Dict[str, Any]] = {}
            try:
                with open(folder / "sections.jsonl", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            row = json.loads(line)
                            if row.get("doc") in known:
                                sections[row["id"]] = row
            except FileNotFoundError:
                pass
            except (OSError, ValueError, KeyError):
                sections = {}
            vectors = None
            if meta.get("embed_model") and (folder / "embeddings.npy").is_file():
                try:
                    found = np.load(folder / "embeddings.npy", allow_pickle=False)
                    if found.ndim == 2 and found.shape[0] == len(chunks) == meta.get("chunks"):
                        vectors = unit_rows(found)
                except (OSError, ValueError):
                    vectors = None
            archive = Archive(meta, chunks, vectors, sections)
            self._cache[aid] = (stamp, archive)
            return archive

    def _commit(self, folder: Path, meta: Dict[str, Any], chunks: List[Dict[str, Any]], vectors: Optional[np.ndarray], model: str,
                sections: Optional[Dict[str, Dict[str, Any]]] = None, version: str = "") -> None:
        has = vectors is not None
        meta.update(chunks=len(chunks), embed_model=model if has else "", embed_dim=int(vectors.shape[1]) if has else 0, embed_version=version if has else "",
                    updated=self.clock(), ingest=dict(INGEST))
        self._put(folder / "chunks.jsonl", "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks))
        if sections is not None:
            self._put(folder / "sections.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in sections.values()))
        if vectors is not None:
            tmp = folder / "embeddings.npy.tmp"
            with open(tmp, "wb") as f:
                np.save(f, np.asarray(vectors, dtype=np.float16), allow_pickle=False)
            os.replace(tmp, folder / "embeddings.npy")
        else:
            (folder / "embeddings.npy").unlink(missing_ok=True)
        self._put_meta(folder, meta)

    # -- the archives ----------------------------------------------------------------------------

    def status(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        """How an archive is searched now: `hybrid` (words and the vectors of the embedder in use, same model and version) or `text`; `reindex` says that
        indexing it again would change that."""
        emb, model = self.embedder(), meta.get("embed_model", "")
        hybrid = compatible(meta, emb)
        return {"mode": "hybrid" if hybrid else "text", "embed_model": model, "reindex": bool(meta["chunks"]) and emb is not None and not hybrid,
                "rechunk": bool(meta["documents"]) and meta.get("ingest") != INGEST}

    def _summary(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": meta["id"], "name": meta["name"], "description": meta["description"], "project": meta.get("project", ""), "project_name": meta.get("project_name", ""), "cloud_ok": bool(meta.get("cloud_ok")), "created": meta["created"],
                "updated": meta["updated"], "documents": len(meta["documents"]), "chunks": meta["chunks"], "bytes": sum(d["bytes"] for d in meta["documents"]),
                "flagged": sum(d.get("flagged", 0) for d in meta["documents"]), "extraction": _extraction(meta), **self.status(meta)}

    def _metas(self, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """The meta of every readable archive (of one project, or of all with None), by name; a folder that is not an archive, or is damaged, is left out.
        An archive whose meta names no project belongs to none."""
        out = []
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir()):
                if folder.is_dir() and ID.match(folder.name):
                    try:
                        meta = self._read_meta(folder)
                    except RagError:
                        continue
                    if project is None or meta.get("project") == project:
                        out.append(meta)
        return sorted(out, key=lambda m: (m["name"].lower(), m["id"]))

    def list(self, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """The archives of `project` (every archive with None)."""
        return [self._summary(m) for m in self._metas(project)]

    def stamp(self) -> Tuple[Tuple[str, int], ...]:
        """What tells that an archive was made, changed or removed: the id and the modification time of each `meta.json` (one stat each)."""
        out = []
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir()):
                if ID.match(folder.name):
                    try:
                        out.append((folder.name, (folder / "meta.json").stat().st_mtime_ns))
                    except OSError:
                        continue
        return tuple(out)

    def ids(self, project: Optional[str]) -> List[str]:
        """The ids of the archives of `project` (every archive with None), read without looking at the embedder."""
        return [m["id"] for m in self._metas(project)] if project is None or is_key(project) else []

    def get(self, aid: Any) -> Dict[str, Any]:
        meta = self.load(aid).meta
        return {**self._summary(meta), "document_list": [dict(d) for d in meta["documents"]]}

    def create(self, name: Any, description: Any = "", project: Any = None, project_name: Any = "") -> Dict[str, Any]:
        name, description = _text(name, "name", LIMITS["name"], True), _text(description, "description", LIMITS["description"])
        if not is_key(project):
            raise RagError("project: the key of the project the archive belongs to")
        with self._lock:
            existing = self._metas(project)
            if len(existing) >= LIMITS["archives"]:
                raise RagError(f"at most {LIMITS['archives']} archives in a project")
            if any(m["name"].lower() == name.lower() for m in existing):
                raise RagError("there is already an archive with that name in this project")
            aid = f"{slug_of(name) or 'archive'}-{secrets.token_hex(3)}"
            now = self.clock()
            meta = {"id": aid, "name": name, "description": description, "project": project, "project_name": str(project_name or "").strip()[:LIMITS["name"]],
                    "cloud_ok": False, "created": now, "updated": now, "chunks": 0,
                    "embed_model": "", "embed_dim": 0, "embed_version": "", "ingest": dict(INGEST), "documents": []}
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
                if any(m["name"].lower() == name.lower() and m["id"] != aid for m in self._metas(meta.get("project") or "-")):
                    raise RagError("there is already an archive with that name in this project")
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

    def formats(self) -> List[str]:
        """The extensions that can be added now (more when Docling is set or found)."""
        return formats(self.settings.read())

    def _check_ext(self, name: str) -> str:
        ext = Path(name).suffix.lower()
        allowed = self.formats()
        if ext not in allowed:
            raise RagError(f"{ext or 'a file without an extension'} is not a supported format: use " + ", ".join(allowed))
        return ext

    def add_path(self, aid: Any, raw: Any, progress: Optional[Callable[[Dict[str, Any]], None]] = None, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        path = self.check_path(raw)
        return self.add_file(aid, path.name, path, progress, cancel)

    def stage_bytes(self, aid: Any, name: Any, data: bytes) -> Tuple[str, Path]:
        """The checked name and the temporary file that holds an upload until it is added (`add_file` with it; the caller removes it)."""
        self._dir(aid)
        name = self._doc_name(name)
        self._check_ext(name)
        if len(data) > LIMITS["document_bytes"]:
            raise RagError(f"a document is at most {LIMITS['document_bytes'] >> 20} MB")
        if data and any(d["sha256"] == hashlib.sha256(data).hexdigest() for d in self.load(aid).meta["documents"]):
            raise RagError("this file is already in the archive")
        incoming = self.root / INCOMING
        incoming.mkdir(parents=True, exist_ok=True)
        tmp = incoming / (secrets.token_hex(8) + Path(name).suffix.lower())
        tmp.write_bytes(data)
        return name, tmp

    def add_bytes(self, aid: Any, name: Any, data: bytes, progress: Optional[Callable[[Dict[str, Any]], None]] = None, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        name, tmp = self.stage_bytes(aid, name, data)
        try:
            return self.add_file(aid, name, tmp, progress, cancel)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _doc_name(name: Any) -> str:
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or _NAME_BAD.search(name) or name.strip() in (".", ".."):
            raise RagError("name: the name of a file, without folders, up to 200 characters")
        return name.strip()

    def _build(self, doc_id: str, name: str, raw_blocks: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, int]]:
        """The passages and the sections of a document from the blocks a reader gave: cleaned, cut, and each passage looked at for instructions
        to an assistant (`flag`). Also `{chars, flagged}` and what the cleaning dropped."""
        from tadamoo.inject_screen import detect
        blocks, stats = normalize.normalize_blocks(raw_blocks)
        blocks = [{**b, "flag": "instructions"} if b.get("type") != "page_break" and detect(b["text"]) else b for b in blocks]
        chars = sum(len(b["text"]) for b in blocks)
        if chars < 20:
            raise RagError("the file holds no text to search (a scanned page needs Docling to be read)")
        if chars > LIMITS["document_chars"]:
            raise RagError(f"the text is {chars:,} characters: at most {LIMITS['document_chars']:,} per document")
        built = chunker.chunk_blocks(blocks, Path(name).stem)
        chunks: List[Dict[str, Any]] = []
        flagged = 0
        for n, c in enumerate(built["chunks"]):
            row = {"doc": doc_id, "n": n, "text": c["text"], "path": c["path"], "page_start": c["page_start"], "page_end": c["page_end"], "kind": c["kind"],
                   "section": f"{doc_id}.{c['section']}"}
            if c.get("flag"):
                row["flag"] = c["flag"]
                flagged += 1
            chunks.append(row)
        sections = {f"{doc_id}.{r['id']}": {**r, "doc": doc_id, "id": f"{doc_id}.{r['id']}", "parent": f"{doc_id}.{r['parent']}" if r["parent"] else ""} for r in built["sections"]}
        return chunks, sections, {"chars": chars, "flagged": flagged, **stats}

    @staticmethod
    def _save_blocks(folder: Path, doc_id: str, blocks: List[Dict[str, Any]]) -> None:
        RagStore._put(folder / "blocks" / (doc_id + ".json.gz"), gzip.compress(json.dumps(blocks, ensure_ascii=False).encode("utf-8"), 6))

    @staticmethod
    def _load_blocks(folder: Path, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            return json.loads(gzip.decompress((folder / "blocks" / (doc["id"] + ".json.gz")).read_bytes()).decode("utf-8"))
        except (OSError, ValueError):
            raise RagError(f"the saved reading of {doc['name']} is missing: add the document again") from None

    def add_file(self, aid: Any, name: str, path: Path, progress: Optional[Callable[[Dict[str, Any]], None]] = None, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        """Reads the file at `path` as a document called `name`, cleans it, cuts it into passages and indexes it. Nothing is kept when it is refused or
        cancelled. `progress` is told of the stages and, for a long reading, of the pages."""
        self.load(aid)
        name = self._doc_name(name)
        self._check_ext(name)
        size = path.stat().st_size
        if not 0 < size <= LIMITS["document_bytes"]:
            raise RagError("the file is empty" if size == 0 else f"a document is at most {LIMITS['document_bytes'] >> 20} MB")
        tell = progress or (lambda event: None)
        sha = hashlib.sha256()
        with path.open("rb") as f:
            for part in iter(lambda: f.read(1 << 20), b""):
                sha.update(part)
        sha = sha.hexdigest()
        if any(d["sha256"] == sha for d in self.load(aid).meta["documents"]):
            raise RagError("this file is already in the archive")
        tell({"event": "stage", "stage": "reading"})
        reading = read_document(path, name, self.settings.read(), progress=progress, cancel=cancel)
        tell({"event": "stage", "stage": "cutting"})
        doc_id = "d" + secrets.token_hex(4)
        chunks, sections, stats = self._build(doc_id, name, reading["blocks"])
        if cancel is not None and cancel.is_set():
            raise RagError("the reading was cancelled", "cancelled")
        tell({"event": "stage", "stage": "indexing"})
        made = self._embed_new(self.load(aid), [searchable(c) for c in chunks], progress, cancel)        # the slow part, out of the lock: searches go on meanwhile
        with self._lock:
            archive = self.load(aid)
            meta = _copy(archive.meta)
            if len(meta["documents"]) >= LIMITS["documents"]:
                raise RagError(f"at most {LIMITS['documents']} documents in an archive")
            if any(d["sha256"] == sha for d in meta["documents"]):
                raise RagError("this file is already in the archive")
            if len(archive.chunks) + len(chunks) > LIMITS["chunks"]:
                raise RagError(f"the archive would hold more than {LIMITS['chunks']:,} passages")
            doc = {"id": doc_id, "name": name, "ext": Path(name).suffix.lower(), "bytes": size, "chars": stats["chars"], "chunks": len(chunks), "sha256": sha,
                   "added": self.clock(), "extraction": reading["extraction"], "pages": int(reading.get("pages") or 0), "flagged": stats["flagged"],
                   **({"skipped_pages": reading["skipped_pages"]} if reading.get("skipped_pages") else {})}
            folder = self._dir(aid)
            self._save_blocks(folder, doc_id, reading["blocks"])
            try:
                vectors, model, version, note = self._join(archive, made)
                meta["documents"].append(doc)
                self._commit(folder, meta, archive.chunks + chunks, vectors, model, {**archive.sections, **sections}, version)
            except BaseException:
                (folder / "blocks" / (doc_id + ".json.gz")).unlink(missing_ok=True)
                raise
            return {"document": doc, "embedded": vectors is not None, "note": "; ".join(x for x in [note] + list(reading.get("notes") or []) if x)}

    def _embed_new(self, archive: Archive, pieces: List[str], progress: Optional[Callable[[Dict[str, Any]], None]] = None,
                   cancel: Optional[threading.Event] = None) -> Tuple[Optional[np.ndarray], str, str, str]:
        """(vectors of `pieces`, their model, their version, a note) from the embedder in use: nothing when there is none, when the archive's own vectors are of
        another model or version (they are never mixed), or when it fails. A cancel raises RagError (`cancelled`)."""
        emb = self.embedder()
        if emb is None or not pieces:
            return None, "", "", ""
        if archive.chunks and not (archive.vectors is not None and compatible(archive.meta, emb)):
            return None, "", "", "some passages of the archive have no vectors: index it again to use them"
        try:
            return embed_all(emb, pieces, progress, cancel), emb.model, str(getattr(emb, "version", "")), ""
        except RagError:
            raise
        except Exception as e:
            return None, "", "", f"text search only: the embedder failed ({str(e)[:120]})"
        finally:
            getattr(emb, "rest", lambda: None)()                  # indexing is done: the GPU goes back before the next document is read

    @staticmethod
    def _join(archive: Archive, made: Tuple[Optional[np.ndarray], str, str, str]) -> Tuple[Optional[np.ndarray], str, str, str]:
        """(the archive's vectors with the new ones added, model, version, note), looked at again now that the archive may have changed since they were made."""
        fresh, model, version, note = made
        if fresh is None:
            return None, "", "", note
        if archive.chunks and (archive.vectors is None or archive.meta["embed_model"] != model or archive.meta.get("embed_version", "") != version
                               or archive.vectors.shape[1] != fresh.shape[1]):
            return None, "", "", "some passages of the archive have no vectors: index it again to use them"
        return (fresh if archive.vectors is None else np.vstack([archive.vectors, fresh])), model, version, ""

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
            sections = {k: v for k, v in archive.sections.items() if v.get("doc") != doc_id}
            self._commit(folder, meta, [archive.chunks[i] for i in keep], vectors, archive.meta["embed_model"], sections, archive.meta.get("embed_version", ""))
            (folder / "blocks" / (doc_id + ".json.gz")).unlink(missing_ok=True)
            return {"removed": True, "chunks": len(keep)}

    def reindex(self, aid: Any, progress: Optional[Callable[[Dict[str, Any]], None]] = None, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        """Cleans and cuts every document's saved reading into passages again (with the versions of the cleaning and the cutting now in use) and
        embeds them with the embedder in use, if there is one. Docling is not asked again. The archive stays searchable while it is embedded."""
        tell = progress or (lambda event: None)
        archive, folder = self.load(aid), self._dir(aid)
        meta = _copy(archive.meta)
        chunks: List[Dict[str, Any]] = []
        sections: Dict[str, Dict[str, Any]] = {}
        tell({"event": "stage", "stage": "cutting"})
        for doc in meta["documents"]:
            made, secs, stats = self._build(doc["id"], doc["name"], self._load_blocks(folder, doc))
            doc.update(chunks=len(made), chars=stats["chars"], flagged=stats["flagged"])
            chunks += made
            sections.update(secs)
        if len(chunks) > LIMITS["chunks"]:
            raise RagError(f"the archive would hold more than {LIMITS['chunks']:,} passages")
        tell({"event": "stage", "stage": "indexing"})
        vectors, model, version, note = self._embed_new(Archive(meta, [], None), [searchable(c) for c in chunks], progress, cancel)
        with self._lock:
            if self.load(aid).meta["updated"] != archive.meta["updated"]:
                raise RagError("the archive changed while it was indexed: index it again")
            self._commit(folder, meta, chunks, vectors, model, sections, version)
        return {"chunks": len(chunks), "embedded": vectors is not None, "note": note}

    def section(self, aid: Any, sid: Any) -> Dict[str, Any]:
        """The section a passage came from (`section` of a hit): its path, pages and text (capped), for a reader that wants more than the passage."""
        archive = self.load(aid)
        row = archive.sections.get(sid) if isinstance(sid, str) else None
        if row is None:
            raise RagError("there is no such section", "not_found")
        return {"id": row["id"], "path": row["path"], "page_start": row["page_start"], "page_end": row["page_end"], "text": row["text"], "flag": row.get("flag", ""),
                "document": archive.document(row["doc"])["name"], "archive": archive.meta["name"]}

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
        if self.remote is not None:
            try:
                return self.remote([a.meta["id"] for a in loaded], query.strip()[:2000], max(1, min(int(top_k), LIMITS["top_k"])), min_share)
            except Exception:                              # the server that holds the embedder does not answer: words alone
                pass
        return run(loaded, self.embedder(), query.strip()[:2000], max(1, min(int(top_k), LIMITS["top_k"])), min_share, self.settings.read())
