"""Adding documents to an archive in a batch, and saying how an archive stands: what the agent's knowledge tools (and anything else) ask of the server.

    POST /api/rag/archives/<id>/add      {items: [text, ...]}   a job. Each item is a file on this PC, a folder on this PC, or an http(s) address:
                                          a file is read and added; a folder becomes a source of the archive and is read (its new files too, and again by `knowledge_update`);
                                          an address is downloaded into the archive's download folder (`<rag folder>/downloads/<archive>/`, robots.txt and the
                                          politeness settings of the sources apply) and the downloaded file is read and added.
    GET  /api/rag/archives/<id>/status   documents, passages, how it is searched, the database, the descriptions, the sources, and the jobs that run or just ended

The reading, cutting, embedding and the one-run-per-archive guard are the ones every other way of adding a document uses (tadamoo/rag/store.py, tadamoo/rag/sources)."""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import unquote, urlsplit

from tadamoo.rag.errors import RagError
from tadamoo.rag.sources import web
from tadamoo.rag.sources.model import SourceError, doc_name

ITEMS_MAX = 200
ITEM_CHARS = 1000
DOWNLOADS = "downloads"


class RagAddApi:
    def __init__(self, syncing: Any, jobs: Any) -> None:
        self.sync, self.jobs = syncing.sync, jobs

    @property
    def store(self) -> Any:
        return self.sync.store

    # -- the routes -----------------------------------------------------------------------------------------------

    def route_get(self, parts: Sequence[str]) -> Optional[Dict[str, Any]]:
        if len(parts) == 3 and parts[0] == "archives" and parts[2] == "status":
            return self.status(parts[1])
        return None

    def route_post(self, parts: Sequence[str], req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if len(parts) == 3 and parts[0] == "archives" and parts[2] == "add":
            return self.add(parts[1], req)
        return None

    # -- status ---------------------------------------------------------------------------------------------------

    def status(self, aid: str) -> Dict[str, Any]:
        archive = self.store.load(aid)
        row = self.store.get(aid)
        backend = self.store._backend(aid)
        info = backend.info()
        sources = self.sync.sources(aid)
        docs = [{"name": d["name"], "pages": d.get("pages") or None, "passages": d.get("chunks", 0), "figures": d.get("figures", 0), "from": (d.get("origin") or {}).get("ref", "")}
                for d in archive.meta["documents"]]
        jobs = sorted(self.jobs.list(aid), key=lambda j: j["started"], reverse=True)[:6]
        return {"archive": {k: row[k] for k in ("id", "name", "description", "project", "documents", "chunks", "mode", "embed_model", "reindex", "rechunk", "flagged", "extraction")},
                "documents": docs[:60], "more_documents": max(0, len(docs) - 60),
                "database": {"backend": info["backend"], "vectors": info["vectors"], "words": info["words"], "label": info.get("label", "")},
                "describing": self.store.enrichment(aid),
                "sources": [{"id": s["id"], "kind": s["kind"], "label": s["label"]} for s in sources["sources"]], "last_update": (sources["last"] or {}).get("summary", ""),
                "running": [{"job": j["id"], "what": j["label"], "stage": j["stage"], "page": j["page"], "pages": j["pages"]} for j in jobs if j["state"] == "running"],
                "recent": [{"what": j["label"], "state": j["state"], "error": j["error"]} for j in jobs if j["state"] != "running"][:3]}

    # -- adding ---------------------------------------------------------------------------------------------------

    def add(self, aid: str, req: Dict[str, Any]) -> Dict[str, Any]:
        items = req.get("items")
        if not isinstance(items, list) or not items or len(items) > ITEMS_MAX or any(not isinstance(i, str) or not i.strip() or len(i) > ITEM_CHARS for i in items):
            raise RagError(f"items: a list of 1 to {ITEMS_MAX} files, folders or http(s) addresses")
        items = list(dict.fromkeys(i.strip() for i in items))
        name = self.store.load(aid).meta["name"]
        if self.sync.running(aid):
            raise RagError("a check or an update of this archive is already running", "busy")

        def work(progress: Callable[[Dict[str, Any]], None], cancel: Any) -> Dict[str, Any]:
            results: List[Dict[str, Any]] = []
            for n, item in enumerate(items):
                if cancel.is_set():
                    raise RagError("the adding was cancelled", "cancelled")
                progress({"event": "stage", "stage": item[:80]})
                progress({"page": n, "pages": len(items)})
                results.append(self._one(aid, item, progress, cancel))
            progress({"page": len(items), "pages": len(items)})
            counts: Dict[str, int] = {}
            for r in results:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            row = self.store.get(aid)
            return {"archive": name, "results": results, "counts": counts, "documents": row["documents"], "chunks": row["chunks"], "mode": row["mode"]}
        return {"job": self.jobs.start("add to " + name, work, aid)}

    def _one(self, aid: str, item: str, progress: Callable[[Dict[str, Any]], None], cancel: Any) -> Dict[str, Any]:
        try:
            if re.match(r"^https?://", item, re.I):
                return self._url(aid, item, progress, cancel)
            if Path(item).is_dir():
                return self._folder(aid, item, progress, cancel)
            return self._file(aid, item, progress, cancel)
        except SourceError as e:
            return {"item": item, "status": "failed", "note": str(e)[:300]}
        except RagError as e:
            if e.code == "cancelled":
                raise
            text = str(e)
            return {"item": item, "status": "duplicate" if "already in the archive" in text else "failed", "note": text[:300]}
        except OSError as e:
            return {"item": item, "status": "failed", "note": f"{type(e).__name__}: {str(e)[:200]}"}

    def _added(self, aid: str, item: str, out: Dict[str, Any], origin: Dict[str, Any]) -> Dict[str, Any]:
        doc = out["document"]
        self.store.annotate_document(aid, doc["id"], {"origin": origin})
        return {"item": item, "status": "added", "document": doc["name"], "passages": doc["chunks"], "pages": doc.get("pages") or None, "figures": doc.get("figures", 0),
                "embedded": out["embedded"], **({"note": out["note"]} if out.get("note") else {})}

    def _file(self, aid: str, item: str, progress: Any, cancel: Any) -> Dict[str, Any]:
        out = self.store.add_path(aid, item, progress, cancel)
        return self._added(aid, item, out, {"kind": "file", "ref": str(Path(item)), "version": ""})

    def _url(self, aid: str, url: str, progress: Any, cancel: Any) -> Dict[str, Any]:
        address = web.clean_address(url)
        folder = self.store.root / DOWNLOADS / aid
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / (".part-" + secrets.token_hex(4))
        try:
            got = self.sync.ctx.http.get(address, {}, robots=True, to=part)
            ext = web._extension(got.headers.get("content-type", ""), got.url, self.store.formats())
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", unquote(Path(urlsplit(got.url).path).stem) or urlsplit(got.url).hostname or "page").strip("-")[:60] or "page"
        target = folder / f"{stem}-{got.sha256[:8]}{ext}"
        if target.exists():
            part.unlink(missing_ok=True)
        else:
            os.replace(part, target)
        try:
            out = self.store.add_file(aid, doc_name(stem, ext), target, progress, cancel)
        except BaseException as e:
            if not (isinstance(e, RagError) and "already in the archive" in str(e)):
                target.unlink(missing_ok=True)                                     # a file that could not be read is not kept
            raise
        return {**self._added(aid, url, out, {"kind": "url", "ref": got.url, "version": (got.headers.get("last-modified") or got.headers.get("etag") or "")[:60]}),
                "saved": str(target)}

    def _folder(self, aid: str, folder: str, progress: Any, cancel: Any) -> Dict[str, Any]:
        config = self.sync.providers["folder"].validate({"path": folder, "recursive": True})
        known = next((s for s in self.sync.sources(aid)["sources"] if s["kind"] == "folder" and s["config"] == config), None)
        source = known or self.sync.add_source(aid, "folder", config)
        report = self.sync.update(aid, include_new=True, progress=progress, cancel=cancel)
        mine = [i for i in report["items"] if i.get("source") == source["id"]]
        failed = [{"document": i["name"], "note": str(i.get("detail", ""))[:160]} for i in mine if i["state"] in ("failed", "error")][:10]
        return {"item": folder, "status": "added", "source": source["label"], "summary": report["summary"], "counts": report["counts"], **({"failed": failed} if failed else {}),
                "note": "the folder is now a source of the archive: knowledge_update reads again what changes in it"}
