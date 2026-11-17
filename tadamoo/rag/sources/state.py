"""What an archive remembers about where its documents come from, in the archive's database (one row, written whole): the sources, which document is which item of which
source and the stamp it had when it was read, the result of the last check, the versions kept of replaced documents (their saved readings, kept as blobs), and what the
person should look at (`attention`). The meta of the archive is touched only when a document's content changes, so a check never makes the archive reload."""
from __future__ import annotations

import gzip
import json
import threading
from typing import Any, Dict, List, Optional

VERSIONS = "version"                 # the kind of blob that holds a kept reading, named `<document>/<n>`
SOURCES_MAX = 10
ITEMS_KEPT = 300                     # items of the last check that are kept (the ones that are not unchanged)
HISTORY_KEPT = 30


def blank() -> Dict[str, Any]:
    return {"version": 1, "sources": [], "links": {}, "acknowledged": {}, "nodes": {}, "excluded": {}, "check": {}, "last": {}, "attention": {"t": 0.0, "signature": "", "counts": {}}, "history": [], "versions": {}}


class State:
    """The sources record of one archive, kept in `backend` under the archive's id."""

    def __init__(self, backend: Any, aid: str) -> None:
        self.backend, self.aid = backend, aid
        self._lock = threading.RLock()

    def read(self) -> Dict[str, Any]:
        data = self.backend.read_sources(self.aid)
        out = blank()
        if isinstance(data, dict):
            out.update({k: data[k] for k in out if k in data and type(data[k]) is type(out[k])})
        return out

    def write(self, data: Dict[str, Any]) -> None:
        self.backend.write_sources(self.aid, data)

    def change(self, fn: Any) -> Dict[str, Any]:
        """Reads, applies `fn(data)` and writes, one writer at a time."""
        with self._lock:
            data = self.read()
            fn(data)
            self.write(data)
            return data

    # -- versions of replaced documents ------------------------------------------------------------

    def keep_version(self, data: Dict[str, Any], doc: Dict[str, Any], blocks: Optional[bytes], source_version: str, now: float, keep: int) -> Optional[Dict[str, Any]]:
        """Keeps the saved reading `blocks` (the bytes the store holds) of `doc` aside as its version `n` and records it; None when versions are not kept or there is nothing to keep."""
        if keep <= 0 or not blocks:
            return None
        rows = data["versions"].setdefault(doc["id"], [])
        n = max((r["n"] for r in rows), default=0) + 1
        self.backend.put_blob(self.aid, VERSIONS, f"{doc['id']}/{n}", blocks)
        entry = {"n": n, "at": now, "name": doc.get("name", ""), "sha256": doc.get("sha256", ""), "chunks": doc.get("chunks", 0), "chars": doc.get("chars", 0),
                 "pages": doc.get("pages", 0), "bytes": doc.get("bytes", 0), "version": source_version, "extraction": doc.get("extraction", ""), "coverage": doc.get("coverage")}
        rows.append(entry)
        return entry

    def drop_version(self, doc_id: str, n: int) -> None:
        self.backend.delete_blobs(self.aid, VERSIONS, key=f"{doc_id}/{n}")

    def prune(self, data: Dict[str, Any], keep: int, days: int, now: float) -> int:
        """Drops the versions beyond the `keep` newest of each document, and those older than `days` (0: no age limit); the number dropped."""
        dropped = 0
        for doc_id, rows in list(data["versions"].items()):
            rows.sort(key=lambda r: r["n"])
            old = [r for r in rows[:-keep]] if keep > 0 else list(rows)
            if days > 0:
                old += [r for r in rows if now - float(r.get("at") or 0) > days * 86400 and r not in old]
            for r in old:
                self.drop_version(doc_id, r["n"])
                rows.remove(r)
                dropped += 1
            if not rows:
                data["versions"].pop(doc_id)
        return dropped

    def version_blocks(self, doc_id: str, n: int) -> List[Dict[str, Any]]:
        try:
            raw = self.backend.get_blob(self.aid, VERSIONS, f"{doc_id}/{n}")
            if raw is None:
                raise ValueError("missing")
            return json.loads(gzip.decompress(raw).decode("utf-8"))
        except (OSError, ValueError):
            raise ValueError("the saved version is missing") from None

    def forget_document(self, data: Dict[str, Any], doc_id: str) -> None:
        data["links"].pop(doc_id, None)
        data["acknowledged"].pop(doc_id, None)
        if data["versions"].pop(doc_id, None) is not None:
            self.backend.delete_blobs(self.aid, VERSIONS, prefix=doc_id + "/")
