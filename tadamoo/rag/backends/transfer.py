"""Copies an archive from one backend to another (a project's SQLite file to its PostgreSQL database, say): the meta, passages, sections, vectors of every slot, blobs, figures,
sources and enrichment. The source is only read."""
from __future__ import annotations

from typing import Any, Dict

from tadamoo.rag.backends.base import MAIN, RT, Backend
from tadamoo.rag.errors import RagError

BLOB_KINDS = ("blocks", "image", "version")


def copy_archive(src: Backend, dst: Backend, aid: str, replace: bool = False) -> Dict[str, Any]:
    """Copies archive `aid` from `src` into `dst` and returns the counts of what was copied. One already in `dst` is refused unless `replace`."""
    meta = src.read_meta(aid)
    if meta is None:
        raise RagError(f"{aid} is not in the source database")
    if dst.revision(aid) is not None:
        if not replace:
            raise RagError(f"{aid} is already in the destination database: copy it with replace to write it again")
        dst.delete_archive(aid)
    chunks = src.read_chunks(aid)
    report: Dict[str, Any] = {"archive": aid, "name": meta.get("name", ""), "documents": len(meta.get("documents") or []), "chunks": len(chunks), "vectors": 0, "second_vectors": 0,
                              "blobs": 0, "figures": 0, "enrichment": 0}
    try:
        main = src.read_vectors(aid, MAIN)
        note = src.vector_note(aid, MAIN) or {}
        dst.commit(aid, meta, chunks, src.read_sections(aid), main, str(note.get("model") or meta.get("embed_model") or ""), str(note.get("version") or meta.get("embed_version") or ""))
        report["vectors"] = int(main.shape[0]) if main is not None else 0
        rt = src.read_vectors(aid, RT)
        if rt is not None:
            n = src.vector_note(aid, RT) or {}
            dst.put_vectors(aid, RT, rt, str(n.get("model") or ""), str(n.get("version") or ""), n.get("note") if isinstance(n.get("note"), dict) else None)
            report["second_vectors"] = int(rt.shape[0])
        for kind in BLOB_KINDS:
            for key in src.list_blobs(aid, kind):
                data = src.get_blob(aid, kind, key)
                if data is not None:
                    dst.put_blob(aid, kind, key, data)
                    report["blobs"] += 1
        for d in meta.get("documents") or []:
            doc = d.get("id") if isinstance(d, dict) else d
            records = src.read_figures(aid, doc) if doc else []
            if records:
                dst.write_figures(aid, doc, records)
                report["figures"] += len(records)
        rows = list(src.enrich_read(aid).values())
        if rows:
            dst.enrich_add(aid, rows)
            report["enrichment"] = len(rows)
        sources = src.read_sources(aid)
        if sources:
            dst.write_sources(aid, sources)
        have, want = dst.counts(aid), src.counts(aid)
        if have["chunks"] != want["chunks"] or have["documents"] != want["documents"] or have["sections"] != want["sections"]:
            raise RagError(f"{aid}: the destination holds {have}, the source {want}")
    except BaseException:
        dst.delete_archive(aid)                                      # a copy that stopped half way leaves nothing
        raise
    return report
