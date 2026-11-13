"""The file format the archives had before they were kept in a database, as an import source only: a folder `rag/<id>/` with

    meta.json  chunks.jsonl  sections.jsonl  embeddings.npy  blocks/<doc>.json.gz  images/<doc>/  figures/<doc>.json  enrich.jsonl  sources.json  versions/<doc>/<n>.json.gz
    embeddings_rt.npy + rt.json (the second vectors)

`import_archive` copies all of it into a backend and says what it copied; it never changes or deletes the folder."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from tadamoo.rag.backends.base import MAIN, RT, Backend
from tadamoo.rag.errors import RagError

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue                                  # a half-written last line costs one entry
                    if isinstance(row, dict):
                        out.append(row)
    except OSError:
        pass
    return out


def _unit(matrix: np.ndarray) -> np.ndarray:
    data = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(data, axis=1, keepdims=True)
    return data / np.where(norms == 0, 1.0, norms)


def legacy_folders(root: str | Path) -> List[Path]:
    """The folders under `root` that are archives in the old format."""
    base = Path(root)
    if not base.is_dir():
        return []
    return [p for p in sorted(base.iterdir()) if p.is_dir() and ID.match(p.name) and (p / "meta.json").is_file()]


def read_meta(folder: Path) -> Dict[str, Any]:
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RagError(f"{folder.name}: meta.json cannot be read") from None
    if not isinstance(meta, dict) or not isinstance(meta.get("documents"), list) or meta.get("id") != folder.name:
        raise RagError(f"{folder.name}: meta.json is not an archive's")
    return meta


def import_archive(folder: str | Path, backend: Backend, replace: bool = False) -> Dict[str, Any]:
    """Copies the archive in `folder` into `backend` and returns the counts of what was copied. An archive that is already there is refused unless `replace`."""
    folder = Path(folder)
    meta = read_meta(folder)
    aid = meta["id"]
    if backend.revision(aid) is not None:
        if not replace:
            raise RagError(f"{aid} is already in the database: import it with replace to copy it again")
        backend.delete_archive(aid)
    chunks = _jsonl(folder / "chunks.jsonl")
    sections = {r["id"]: r for r in _jsonl(folder / "sections.jsonl") if "id" in r}
    report: Dict[str, Any] = {"archive": aid, "name": meta.get("name", ""), "documents": len(meta["documents"]), "chunks": len(chunks), "sections": len(sections), "vectors": 0,
                              "blocks": 0, "images": 0, "figures": 0, "enrichment": 0, "versions": 0, "second_vectors": 0, "notes": []}
    vectors: Optional[np.ndarray] = None
    if meta.get("embed_model") and (folder / "embeddings.npy").is_file():
        found = np.load(folder / "embeddings.npy", allow_pickle=False)
        if found.ndim == 2 and found.shape[0] == len(chunks):
            vectors = _unit(found)
            report["vectors"] = int(found.shape[0])
        else:
            report["notes"].append("the vectors do not match the passages and were not copied: index the archive again")
    try:
        _copy_rest(folder, backend, meta, chunks, sections, vectors, report)
    except BaseException:
        backend.delete_archive(aid)                                  # a copy that stopped half way leaves nothing
        raise
    return report


def _copy_rest(folder: Path, backend: Backend, meta: Dict[str, Any], chunks: List[Dict[str, Any]], sections: Dict[str, Dict[str, Any]], vectors: Optional[np.ndarray],
               report: Dict[str, Any]) -> None:
    aid = meta["id"]
    backend.commit(aid, meta, chunks, sections, vectors, str(meta.get("embed_model") or ""), str(meta.get("embed_version") or ""))
    for p in sorted((folder / "blocks").glob("*.json.gz")):
        backend.put_blob(aid, "blocks", p.name[:-len(".json.gz")], p.read_bytes())
        report["blocks"] += 1
    for sub in sorted((folder / "images").glob("*")) if (folder / "images").is_dir() else []:
        for p in sorted(sub.glob("*")):
            if p.is_file():
                backend.put_blob(aid, "image", f"{sub.name}/{p.name}", p.read_bytes())
                report["images"] += 1
    for p in sorted((folder / "figures").glob("*.json")) if (folder / "figures").is_dir() else []:
        try:
            records = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report["notes"].append(f"figures/{p.name} could not be read")
            continue
        if isinstance(records, list):
            backend.write_figures(aid, p.stem, [r for r in records if isinstance(r, dict)])
            report["figures"] += len(records)
    entries = [r for r in _jsonl(folder / "enrich.jsonl") if "k" in r]
    if entries:
        backend.enrich_add(aid, entries)
        report["enrichment"] = len(entries)
    try:
        sources = json.loads((folder / "sources.json").read_text(encoding="utf-8"))
        if isinstance(sources, dict):
            backend.write_sources(aid, sources)
    except (OSError, ValueError):
        pass
    for doc in sorted((folder / "versions").glob("*")) if (folder / "versions").is_dir() else []:
        for p in sorted(doc.glob("*.json.gz")):
            backend.put_blob(aid, "version", f"{doc.name}/{p.name[:-len('.json.gz')]}", p.read_bytes())
            report["versions"] += 1
    if (folder / "embeddings_rt.npy").is_file() and (folder / "rt.json").is_file():
        try:
            note = json.loads((folder / "rt.json").read_text(encoding="utf-8"))
            rows = np.load(folder / "embeddings_rt.npy", allow_pickle=False)
            backend.put_vectors(aid, RT, _unit(rows), str(note.get("model") or ""), str(note.get("version") or ""), {k: v for k, v in note.items() if k not in ("model", "version")})
            report["second_vectors"] = int(rows.shape[0])
        except (OSError, ValueError):
            report["notes"].append("the second vectors could not be read")
    stored = backend.counts(aid)
    if stored["chunks"] != len(chunks) or stored["documents"] != len(meta["documents"]):
        raise RagError(f"{aid}: the database holds {stored['chunks']} passages and {stored['documents']} documents, the folder had {len(chunks)} and {len(meta['documents'])}")
