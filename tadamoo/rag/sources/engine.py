"""Keeping an archive up to date: `Sync` checks the sources of an archive for what changed and updates the documents that did.

    check     asks each source the cheapest question it can answer and says, per document: unchanged, changed, new, gone (not there any more: it is marked and the
              person is asked, nothing is deleted), error (the source could not be read: nothing is concluded), pinned (held at an older version on purpose)
    update    re-reads only the changed and new items through the ordinary ingestion (`read_document`, the cleaning and the cutting of the store), replaces the passages of
              a changed document in one commit with the vectors of the passages that did not change (found by their content hash) kept, so only new text is embedded
    verify    tadamoo/rag/sources/verify.py

A document that cannot be read keeps its old version and the report says why. A failed or cancelled update changes nothing that was not already committed whole."""
from __future__ import annotations

import contextlib
import hashlib
import shutil
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import numpy as np

from tadamoo.rag.errors import RagError
from tadamoo.rag.ingest import read_document
from tadamoo.rag.sources import coverage
from tadamoo.rag.sources.local import FileProvider, FolderProvider
from tadamoo.rag.sources.mediawiki import MediaWikiProvider
from tadamoo.rag.sources.model import KINDS, Fetched, Provider, Remote, SourceError
from tadamoo.rag.sources.net import Http
from tadamoo.rag.sources.settings import SyncSettings
from tadamoo.rag.sources.state import HISTORY_KEPT, ITEMS_KEPT, SOURCES_MAX, State
from tadamoo.rag.sources.web import UrlProvider
from tadamoo.rag.sources.wikibridge import WikiBridgeProvider
from tadamoo.rag.store import INCOMING, LIMITS, searchable

Progress = Callable[[Dict[str, Any]], None]
SAMPLES = 3                          # changed passages shown in a preview, each cut to SAMPLE_CHARS
SAMPLE_CHARS = 220
PENDING = ("changed", "new", "gone", "error")


@dataclass
class Context:
    http: Http
    settings: Callable[[], Dict[str, Any]]
    formats: Callable[[], List[str]]
    forbidden: List[Path]
    document_bytes: int
    stage: Path
    wiki_client: Callable[[str], Any]
    sleep: Callable[[float], None]


def _core(c: Dict[str, Any]) -> str:
    """What identifies a passage's content, whatever was added to it later (summaries, questions)."""
    return hashlib.sha1((c.get("path", "") + "\n" + c["text"]).encode("utf-8")).hexdigest()


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def stamp_text(stamp: Dict[str, Any]) -> str:
    """A stamp in words: a revision, a date, a size."""
    if not stamp:
        return ""
    if stamp.get("revid"):
        return f"revision {stamp['revid']}" + (f" of {str(stamp.get('timestamp'))[:10]}" if stamp.get("timestamp") else "")
    if stamp.get("content_version"):
        return f"version {stamp['content_version']}" + (f", {str(stamp['updated'])[:10]}" if stamp.get("updated") else "")
    if stamp.get("updated"):
        return str(stamp["updated"])[:19]
    if stamp.get("mtime_ns"):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp["mtime_ns"] / 1e9)) + f", {int(stamp.get('size') or 0):,} bytes"
    return str(stamp.get("last_modified") or stamp.get("etag") or str(stamp.get("sha256", ""))[:12])


class Sync:
    def __init__(self, store: Any, wiki_client: Optional[Callable[[str], Any]] = None, busy: Optional[Callable[[], str]] = None, http: Optional[Http] = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep, settings: Optional[SyncSettings] = None) -> None:
        """`busy()` says why a refresh must wait ("" when it need not): a heavy job holds the GPU. `wiki_client(address)` gives the client of the company wiki."""
        self.store, self.clock, self.sleep = store, clock, sleep
        self.settings = settings or SyncSettings(store.root)
        self.busy = busy or (lambda: "")
        if wiki_client is None:
            from tadamoo.integrations.wikibridge_client import WikiBridgeClient
            wiki_client = lambda address: WikiBridgeClient.from_environment(url=address or None, timeout=20.0)
        self.ctx = Context(http or Http(self.settings.read, sleep=sleep), self.settings.read, store.formats, list(store.forbidden), LIMITS["document_bytes"],
                           store.root / INCOMING / "refresh", wiki_client, sleep)
        self.providers: Dict[str, Provider] = {c.kind: c(self.ctx) for c in (FileProvider, FolderProvider, UrlProvider, MediaWikiProvider, WikiBridgeProvider)}
        self._running: set = set()
        self._guard = threading.Lock()

    # -- the sources of an archive ---------------------------------------------------------------

    def state(self, aid: Any) -> State:
        return State(self.store._backend(aid), aid)

    def sources(self, aid: Any) -> Dict[str, Any]:
        """What the archive remembers about its sources: the sources, the last check, the last update, what needs attention. Links of removed documents are dropped."""
        docs = {d["id"] for d in self.store.load(aid).meta["documents"]}
        data = self.state(aid).read()
        data["links"] = {k: v for k, v in data["links"].items() if k in docs}
        data["unlinked"] = len([d for d in docs if d not in data["links"]])
        return data

    def add_source(self, aid: Any, kind: Any, config: Any, label: Any = "") -> Dict[str, Any]:
        """Records a source. Nothing is read from it now: the next check or update finds what it holds."""
        self.store.load(aid)
        if kind not in KINDS or not isinstance(config, dict):
            raise RagError("kind: one of " + ", ".join(KINDS) + ", with its settings")
        clean = self.providers[kind].validate(config)
        text = str(label or "").strip()[:80] or self.providers[kind].label(clean)

        def add(data: Dict[str, Any]) -> None:
            if len(data["sources"]) >= SOURCES_MAX:
                raise RagError(f"at most {SOURCES_MAX} sources in an archive")
            if any(s["kind"] == kind and s["config"] == clean for s in data["sources"]):
                raise RagError("this source is already in the archive")
            data["sources"].append({"id": "s" + hashlib.sha1(f"{kind}{clean}{self.clock()}".encode()).hexdigest()[:7], "kind": kind, "label": text, "config": clean, "added": self.clock()})
        data = self.state(aid).change(add)
        return data["sources"][-1]

    def remove_source(self, aid: Any, sid: str, remove_documents: bool = False) -> Dict[str, Any]:
        """Stops following a source. Its documents stay as plain documents, or are removed too."""
        data = self.sources(aid)
        if not any(s["id"] == sid for s in data["sources"]):
            raise RagError("there is no such source", "not_found")
        mine = [d for d, link in data["links"].items() if link["source"] == sid]
        if remove_documents:
            for doc_id in mine:
                self.store.remove_document(aid, doc_id)

        def drop(d: Dict[str, Any]) -> None:
            d["sources"] = [s for s in d["sources"] if s["id"] != sid]
            for doc_id in mine:
                if remove_documents:
                    self.state(aid).forget_document(d, doc_id)
                else:
                    d["links"].pop(doc_id, None)
                    d["acknowledged"].pop(doc_id, None)
        self.state(aid).change(drop)
        return {"removed": sid, "documents": len(mine) if remove_documents else 0}

    # -- one run at a time per archive -----------------------------------------------------------

    @contextlib.contextmanager
    def _only(self, aid: str) -> Iterator[None]:
        with self._guard:
            if aid in self._running:
                raise RagError("a check or an update of this archive is already running", "busy")
            self._running.add(aid)
        try:
            yield
        finally:
            with self._guard:
                self._running.discard(aid)
            shutil.rmtree(self.ctx.stage, ignore_errors=True)

    def running(self, aid: str) -> bool:
        return aid in self._running

    def _wait(self, progress: Progress, cancel: Optional[threading.Event]) -> None:
        """A heavy job holds the GPU: wait for it (the wait is capped), seeing a cancel."""
        limit = float(self.settings.read()["wait_minutes"]) * 60
        waited, told = 0.0, False
        while True:
            why = self.busy()
            if not why:
                return
            if (cancel is not None and cancel.is_set()) or waited >= limit:
                raise RagError(f"not run now: {why}" + ("" if cancel is not None and cancel.is_set() else "; it will be tried again at the next time"), "busy")
            if not told:
                progress({"event": "stage", "stage": "waiting"})
                told = True
            self.sleep(2.0)
            waited += 2.0

    # -- looking ---------------------------------------------------------------------------------

    def _probe(self, aid: str, only: Optional[List[str]], cancel: Optional[threading.Event]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Remote]]:
        """(items, errors, remotes by item id): every item of every source, each with its state, judged from the recorded stamp. No content is downloaded unless the kind of
        source cannot tell otherwise (an address without validators)."""
        archive = self.store.load(aid)
        docs = {d["id"]: d for d in archive.meta["documents"]}
        data = self.sources(aid)
        items: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        remotes: Dict[str, Remote] = {}
        touched: Dict[str, Dict[str, Any]] = {}
        stop = (lambda: cancel is not None and cancel.is_set())
        for src in data["sources"]:
            if only and src["id"] not in only:
                continue
            prov = self.providers[src["kind"]]
            links = {link["key"]: (did, link) for did, link in data["links"].items() if link["source"] == src["id"]}
            try:
                remote_list = prov.list(src["config"], {k: v["stamp"] for k, (_, v) in links.items()}, stop)
            except SourceError as e:
                if e.kind == "cancelled":
                    raise RagError("the check was cancelled", "cancelled") from None
                errors.append({"source": src["id"], "label": src["label"], "kind": e.kind, "error": str(e)})
                for key, (did, _) in links.items():
                    items.append({"id": f"{src['id']}:{key}", "source": src["id"], "key": key, "doc": did, "name": docs[did]["name"], "state": "error", "detail": str(e)})
                if not links:                                                           # nothing was read from it yet: the source itself is what failed
                    items.append({"id": src["id"] + ":", "source": src["id"], "key": "", "doc": "", "name": src["label"], "state": "error", "detail": str(e)})
                continue
            seen = set()
            for r in remote_list:
                seen.add(r.key)
                iid = f"{src['id']}:{r.key}"
                found = links.get(r.key)
                base = {"id": iid, "source": src["id"], "key": r.key, "doc": found[0] if found else "", "name": r.name or (docs[found[0]]["name"] if found else r.key), "origin": r.origin}
                if r.missing:
                    if found:
                        items.append({**base, "name": docs[found[0]]["name"], "state": "gone", "detail": r.note or "not there now"})
                    else:
                        items.append({**base, "state": "error", "detail": r.note or "not there"})
                    continue
                remotes[iid] = r
                if found is None:
                    items.append({**base, "state": "new", "detail": stamp_text(r.stamp), "new": stamp_text(r.stamp)})
                    continue
                did, link = found
                if link.get("pinned"):                                                  # held at an older version: the source is, by the person's choice, ahead of it
                    items.append({**base, "state": "pinned", "old": link.get("version", ""), "new": stamp_text(r.stamp), "detail": "kept at an older version on purpose"})
                    continue
                if prov.same(link["stamp"], r.stamp):
                    items.append({**base, "state": "unchanged"})
                    continue
                if prov.confirm(src["config"], r, docs[did]) is False:                      # touched, same content: remember the new stamp, no re-reading
                    touched[did] = {"stamp": r.stamp}
                    items.append({**base, "state": "unchanged", "detail": "the marker moved but the content is the same"})
                    continue
                items.append({**base, "state": "changed", "old": stamp_text(link["stamp"]) or link.get("version", ""), "new": stamp_text(r.stamp),
                              "detail": f"{stamp_text(link['stamp'])} -> {stamp_text(r.stamp)}"})
            for key, (did, link) in links.items():
                if key not in seen:
                    items.append({"id": f"{src['id']}:{key}", "source": src["id"], "key": key, "doc": did, "name": docs[did]["name"], "state": "gone",
                                  "detail": "not listed by the source any more (deleted, renamed, or not visible now)"})
        if touched:
            def move(d: Dict[str, Any]) -> None:
                for did, change in touched.items():
                    if did in d["links"]:
                        d["links"][did]["stamp"] = change["stamp"]
            self.state(aid).change(move)
        for item in items:
            if item["state"] == "gone" and item["doc"] in data["acknowledged"]:
                item["acknowledged"] = True
        return items, errors, remotes

    @staticmethod
    def _counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
        c = Counter(i["state"] for i in items)
        for i in items:
            if i.get("acknowledged"):
                c["gone"] -= 1
                c["kept"] += 1
        return {k: v for k, v in sorted(c.items()) if v}

    @staticmethod
    def _summary(counts: Dict[str, int], mode: str) -> str:
        if not counts or set(counts) <= {"unchanged", "kept"}:
            return "nothing changed" if mode == "check" else "nothing to update"
        order = ("changed", "new", "would update", "would add", "updated", "added", "linked", "gone", "pinned", "failed", "error", "unchanged", "kept")
        return ", ".join(f"{counts[k]} {k}" for k in order if counts.get(k))

    def _remember(self, aid: str, report: Dict[str, Any], mode: str) -> None:
        """Keeps the report as the archive's last check (and last update), adds a line to the history and, when something is pending that was not before, moves the
        attention time the tab flashes for."""
        shown = [i for i in report["items"] if i["state"] != "unchanged" or i.get("detail")][:ITEMS_KEPT]
        pending = [i for i in report["items"] if (i["state"] in PENDING and not i.get("acknowledged")) or i["state"] == "failed"]
        signature = hashlib.sha1(repr(sorted((i["state"], i["id"], i.get("new", "") or i.get("detail", "")) for i in pending)).encode()).hexdigest() if pending else ""
        record = {"at": report["at"], "mode": mode, "counts": report["counts"], "summary": report["summary"], "items": shown, "errors": report["errors"]}

        def write(d: Dict[str, Any]) -> None:
            d["check"] = record
            if mode == "update":
                d["last"] = record
            d["history"] = (d["history"] + [{"at": report["at"], "mode": mode, "counts": report["counts"], "summary": report["summary"]}])[-HISTORY_KEPT:]
            att = d["attention"]
            if signature and signature != att.get("signature"):
                att["t"] = report["at"]
            att.update(signature=signature, counts={k: sum(1 for i in pending if i["state"] == k) for k in ("changed", "new", "gone", "error", "failed") if any(i["state"] == k for i in pending)})
        self.state(aid).change(write)

    def _report(self, aid: str, mode: str, items: List[Dict[str, Any]], errors: List[Dict[str, Any]], started: float, **more: Any) -> Dict[str, Any]:
        counts = self._counts(items)
        return {"archive": aid, "name": self.store.load(aid).meta["name"], "mode": mode, "at": self.clock(), "seconds": round(time.monotonic() - started, 2), "counts": counts,
                "summary": self._summary(counts, "check" if mode == "check" else "update"), "items": items, "errors": errors, **more}

    def check(self, aid: Any, only: Optional[List[str]] = None, progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        """What changed in the sources of the archive, without reading any document: `{counts, summary, items, errors}`. An archive with no source says so."""
        tell = progress or (lambda event: None)
        started = time.monotonic()
        with self._only(aid):
            if not self.sources(aid)["sources"]:
                raise RagError("this archive has no source to check: add one first")
            tell({"event": "stage", "stage": "checking"})
            items, errors, _ = self._probe(aid, only, cancel)
            report = self._report(aid, "check", items, errors, started)
            self._remember(aid, report, "check")
            return report

    # -- updating --------------------------------------------------------------------------------

    def update(self, aid: Any, only: Optional[List[str]] = None, include_new: bool = True, dry_run: bool = False, force: Optional[List[str]] = None,
               progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        """Reads again what changed and puts it in the archive. `only` limits it to items (ids from a check) or document ids; `force` lists pinned items to bring up to date;
        `dry_run` reads and cuts but changes nothing and says what would change (passages added, removed, kept, with a few examples)."""
        tell = progress or (lambda event: None)
        started = time.monotonic()
        with self._only(aid):
            if not self.sources(aid)["sources"]:
                raise RagError("this archive has no source to update from: add one first")
            if not dry_run:
                self._wait(tell, cancel)
            tell({"event": "stage", "stage": "checking"})
            items, errors, remotes = self._probe(aid, None, cancel)
            if not dry_run:
                self._mark_past(aid, items)
            force = force or []
            chosen = [i for i in items if (i["state"] in ("changed",) or (i["state"] == "new" and include_new) or (i["state"] == "pinned" and (i["id"] in force or i["doc"] in force)))
                      and (not only or i["id"] in only or i["doc"] in only)]
            deadline = time.monotonic() + float(self.settings.read()["max_run_minutes"]) * 60
            prepared: List[Dict[str, Any]] = []
            archive = self.store.load(aid)
            by_doc = {d["id"]: d for d in archive.meta["documents"]}
            old_chunks: Dict[str, List[Dict[str, Any]]] = {}
            for c in archive.chunks:
                old_chunks.setdefault(c["doc"], []).append(c)
            by_source = {src["id"]: src for src in self.sources(aid)["sources"]}
            stage = self.ctx.stage
            stage.mkdir(parents=True, exist_ok=True)
            link_updates: Dict[str, Dict[str, Any]] = {}
            for n, item in enumerate(chosen, 1):
                if cancel is not None and cancel.is_set():
                    raise RagError("the update was cancelled", "cancelled")
                if time.monotonic() > deadline:
                    item.update(state="failed", detail="the time limit of a refresh was reached: left for the next run")
                    continue
                tell({"event": "stage", "stage": "fetching"})
                tell({"event": "progress", "page": n, "pages": len(chosen)})
                try:
                    self._one(aid, item, remotes[item["id"]], by_source[item["source"]], by_doc, old_chunks, dry_run, prepared, link_updates, tell, cancel)
                except RagError as e:
                    if e.code == "cancelled" or (cancel is not None and cancel.is_set()):
                        raise
                    item.update(state="failed", detail=str(e))
                except SourceError as e:
                    item.update(state="failed", detail=str(e))
                except Exception as e:                                           # a reader's own failure is told, the old version stays
                    item.update(state="failed", detail=f"{type(e).__name__}: {str(e)[:160]}")
            made: Dict[str, Any] = {"embedded": 0, "reused": 0, "note": ""}
            if prepared and not dry_run:
                made = self._commit(aid, prepared, items, link_updates, tell, cancel)
            if not dry_run:
                self._apply_links(aid, items, link_updates)
            report = self._report(aid, "preview" if dry_run else "update", items, errors, started, **made)
            if not dry_run:
                self._remember(aid, report, "update")
            return report

    def _one(self, aid: str, item: Dict[str, Any], remote: Remote, data_src: Dict[str, Any], by_doc: Dict[str, Dict[str, Any]], old_chunks: Dict[str, List[Dict[str, Any]]], dry_run: bool, prepared: List[Dict[str, Any]],
             link_updates: Dict[str, Dict[str, Any]], tell: Progress, cancel: Optional[threading.Event]) -> None:
        """Fetches one item and either prepares the new reading of its document or adds a new document."""
        prov = self.providers[data_src["kind"]]
        fetched = prov.fetch(data_src["config"], remote, self.ctx.stage)
        now = self.clock()
        if item["doc"]:
            doc = by_doc[item["doc"]]
            if fetched.sha256 == doc["sha256"]:
                link_updates[doc["id"]] = {"stamp": fetched.stamp, "fetched": now, "version": fetched.version, "origin": fetched.origin}
                item.update(state="unchanged", detail="the source says it changed but the text is the same")
                return
            tell({"event": "stage", "stage": "reading"})
            reading = read_document(fetched.path, fetched.name, self.store.settings.read(), progress=tell, cancel=cancel)
            tell({"event": "stage", "stage": "cutting"})
            chunks, sections, stats = self.store.passages_of(doc["id"], fetched.name, reading["blocks"], aid)
            fset = {"records": [], "staging": None} if dry_run else self.store.figure_set(aid, doc["id"], fetched.path, fetched.name, reading, chunks, tell, cancel)
            diff = self._diff(old_chunks.get(doc["id"], []), chunks)
            new = {k: v for k, v in doc.items() if k not in ("skipped_pages", "empty_pages", "coverage", "figures")}
            new.update(name=fetched.name, ext=Path(fetched.name).suffix.lower(), bytes=fetched.bytes, chars=stats["chars"], chunks=len(chunks), sha256=fetched.sha256,
                       extraction=reading["extraction"], pages=int(reading.get("pages") or 0), flagged=stats["flagged"], updated=now,
                       origin={"kind": data_src["kind"], "ref": fetched.origin, "version": fetched.version or stamp_text(fetched.stamp)}, revision=int(doc.get("revision") or 1) + 1,
                       figures=len(fset["records"]))
            if "info" in stats:
                new["info"] = stats["info"]
            self.store.describe_documents(aid, [new])
            if reading.get("skipped_pages"):
                new["skipped_pages"] = reading["skipped_pages"]
            if reading.get("empty_pages"):
                new["empty_pages"] = reading["empty_pages"]
            new["coverage"] = coverage.of_document(new, chunks)
            item.update(state="would update" if dry_run else "updated", detail=f"{item.get('old', '')} -> {item.get('new', '')}".strip(" ->"), passages=diff["counts"],
                        examples=diff["examples"], coverage=new["coverage"]["status"])
            prepared.append({"item": item, "doc": new, "chunks": chunks, "sections": sections, "blocks": reading["blocks"], "fetched": fetched, "old_doc": doc, "figures": fset})
            return
        # a new item: adopt a document that already holds the same file, else add it
        same = next((d for d in by_doc.values() if d["sha256"] == fetched.sha256), None)
        if same is not None:
            link_updates[same["id"]] = {"stamp": fetched.stamp, "fetched": now, "version": fetched.version, "origin": fetched.origin, "source": item["source"], "key": item["key"]}
            item.update(state="linked", doc=same["id"], name=same["name"], detail="the archive already held this text: it is followed from now on")
            return
        if dry_run:
            reading = read_document(fetched.path, fetched.name, self.store.settings.read(), progress=tell, cancel=cancel)
            chunks, _, _ = self.store.passages_of("d00000000", fetched.name, reading["blocks"])
            item.update(state="would add", passages={"added": len(chunks), "removed": 0, "kept": 0})
            return
        got = self.store.add_file(aid, fetched.name, fetched.path, tell, cancel)
        doc = got["document"]
        by_doc[doc["id"]] = doc
        link_updates[doc["id"]] = {"stamp": fetched.stamp, "fetched": now, "version": fetched.version, "origin": fetched.origin, "source": item["source"], "key": item["key"]}
        self.store.annotate_document(aid, doc["id"], {"updated": now, "origin": {"kind": data_src["kind"], "ref": fetched.origin, "version": fetched.version or stamp_text(fetched.stamp)}})
        item.update(state="added", doc=doc["id"], passages={"added": doc["chunks"], "removed": 0, "kept": 0}, coverage=doc.get("coverage", {}).get("status", ""), detail=got.get("note", ""))

    @staticmethod
    def _diff(old: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> Dict[str, Any]:
        before, after = Counter(_core(c) for c in old), Counter(_core(c) for c in new)
        added, removed = after - before, before - after
        texts = {_core(c): c["text"] for c in old + new}
        cut = lambda keys: [texts[k][:SAMPLE_CHARS] for k in list(keys)[:SAMPLES]]
        return {"counts": {"added": sum(added.values()), "removed": sum(removed.values()), "kept": sum((before & after).values())},
                "examples": {"added": cut(added), "removed": cut(removed)}}

    def _commit(self, aid: str, prepared: List[Dict[str, Any]], items: List[Dict[str, Any]], link_updates: Dict[str, Dict[str, Any]], tell: Progress,
                cancel: Optional[threading.Event]) -> Dict[str, Any]:
        """The vectors of the passages that did not change are kept, the rest embedded; then one commit for all the replaced documents, the old readings kept as versions."""
        tell({"event": "stage", "stage": "indexing"})
        archive = self.store.load(aid)
        known: Dict[str, np.ndarray] = {}
        if archive.vectors is not None:
            gone = {p["doc"]["id"] for p in prepared}
            for i, c in enumerate(archive.chunks):
                if c["doc"] in gone:
                    known[_hash(searchable(c))] = archive.vectors[i]
        texts = [searchable(c) for p in prepared for c in p["chunks"]]
        rows: List[Optional[np.ndarray]] = [known.get(_hash(t)) for t in texts]
        missing = [i for i, r in enumerate(rows) if r is None]
        model, version, note = archive.meta.get("embed_model", ""), archive.meta.get("embed_version", ""), ""
        text_only = archive.vectors is None and bool(archive.chunks)
        if missing and not text_only:
            fresh, f_model, f_version, note = self.store.embed_pieces(aid, [texts[i] for i in missing], tell, cancel)
            if fresh is None:
                text_only = True
            else:
                for j, i in enumerate(missing):
                    rows[i] = fresh[j]
                model, version = f_model, f_version
        elif missing:
            note = "the archive has no vectors: index it again to search it by meaning"
        have = (not text_only) and all(r is not None for r in rows)
        embedded = len(missing) if have else 0
        changes, at = [], 0
        for p in prepared:
            n = len(p["chunks"])
            part = np.vstack(rows[at:at + n]) if have and n else None
            at += n
            changes.append({"doc": p["doc"], "chunks": p["chunks"], "sections": p["sections"], "blocks": p["blocks"], "rows": part, "model": model, "version": version,
                            "figures": p.get("figures")})
        settings = self.settings.read()
        state, now = self.state(aid), self.clock()
        kept: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

        def keep(d: Dict[str, Any]) -> None:
            for p in prepared:
                link = d["links"].get(p["old_doc"]["id"], {})
                entry = state.keep_version(d, p["old_doc"], self.store._backend(aid).get_blob(aid, "blocks", p["old_doc"]["id"]), link.get("version", ""), now, int(settings["keep_versions"]))
                if entry:
                    kept.append((p["old_doc"], entry))
        state.change(keep)
        tell({"event": "stage", "stage": "saving"})
        try:
            result = self.store.swap_documents(aid, changes)
        except BaseException:
            def undo(d: Dict[str, Any]) -> None:
                for doc, entry in kept:
                    d["versions"].get(doc["id"], []).remove(entry) if entry in d["versions"].get(doc["id"], []) else None
                    state.drop_version(doc["id"], entry["n"])
            state.change(undo)
            raise
        finally:
            for p in prepared:                                          # staged figure images that were not installed (a failed commit) are not kept
                staging = (p.get("figures") or {}).get("staging")
                if staging:
                    shutil.rmtree(staging, ignore_errors=True)
        for p in prepared:
            f: Fetched = p["fetched"]
            link_updates[p["doc"]["id"]] = {"stamp": f.stamp, "fetched": now, "version": f.version, "origin": f.origin}
        state.change(lambda d: state.prune(d, int(settings["keep_versions"]), int(settings["keep_days"]), now))
        return {"embedded": embedded, "reused": len(texts) - len(missing) if have else 0, "note": note, "committed": result["documents"]}

    def _apply_links(self, aid: str, items: List[Dict[str, Any]], updates: Dict[str, Dict[str, Any]]) -> None:
        """Records, for each document read, which item of which source it is and its stamp; a pinned document that was brought up to date is no longer pinned."""
        if not updates:
            return
        by_doc = {i["doc"]: i for i in items if i["doc"]}

        def write(d: Dict[str, Any]) -> None:
            for did, change in updates.items():
                item = by_doc.get(did, {})
                link = d["links"].get(did) or {"source": change.get("source") or item.get("source", ""), "key": change.get("key") or item.get("key", "")}
                link.update({k: v for k, v in change.items() if k not in ("source", "key")})
                link.pop("pinned", None)
                d["links"][did] = link
                d["acknowledged"].pop(did, None)
        self.state(aid).change(write)

    PAST_NOTE = "past version, no longer available at its source"

    def _mark_past(self, aid: Any, items: List[Dict[str, Any]]) -> None:
        """A document its source no longer has stays in the archive, marked `past` with the time it was first missed; one that is there again loses the mark. The check
        alone does not write this; an update (the person's or a schedule's) does. Nothing is ever removed here."""
        documents = {d["id"]: d for d in self.store.load(aid).meta["documents"]}
        gone = {i["doc"] for i in items if i["state"] == "gone" and i["doc"] in documents}
        back = {i["doc"] for i in items if i["state"] != "gone" and i["state"] != "error" and i["doc"] in documents} - gone
        now = self.clock()
        for doc in sorted(gone):
            if not documents[doc].get("past"):
                self.store.annotate_document(aid, doc, {"past": {"since": now, "note": self.PAST_NOTE}})
        for doc in sorted(back):
            if documents[doc].get("past"):
                self.store.annotate_document(aid, doc, {"past": None})
        for i in items:
            if i["state"] == "gone" and i["doc"] in gone:
                i["marked"] = True

    # -- the person's decisions ------------------------------------------------------------------

    def resolve_gone(self, aid: Any, doc_id: str, action: str) -> Dict[str, Any]:
        """A document its source no longer has: `remove` it from the archive, or `keep` it (the check stops asking; it asks again if the item comes back and goes)."""
        if action not in ("remove", "keep"):
            raise RagError("action: remove or keep")
        data = self.sources(aid)
        if doc_id not in data["links"]:
            raise RagError("this document is not followed by a source", "not_found")
        if action == "remove":
            self.store.remove_document(aid, doc_id)
            self.state(aid).change(lambda d: self.state(aid).forget_document(d, doc_id))
        else:
            self.state(aid).change(lambda d: d["acknowledged"].__setitem__(doc_id, self.clock()))
        return {"document": doc_id, "action": action}

    def versions(self, aid: Any, doc_id: str) -> List[Dict[str, Any]]:
        self.store.load(aid)
        return sorted(self.state(aid).read()["versions"].get(doc_id, []), key=lambda r: -r["n"])

    def rollback(self, aid: Any, doc_id: str, n: Any, progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        """Puts a kept version of a document back: its saved reading is cut again and replaces the current one (which is kept as a version in its turn). The document is pinned:
        the next update does not undo this until the person asks."""
        tell = progress or (lambda event: None)
        with self._only(aid):
            state = self.state(aid)
            entry = next((r for r in state.read()["versions"].get(doc_id, []) if r["n"] == n), None)
            doc = next((d for d in self.store.load(aid).meta["documents"] if d["id"] == doc_id), None)
            if entry is None or doc is None:
                raise RagError("there is no such version", "not_found")
            try:
                blocks = state.version_blocks(doc_id, entry["n"])
            except ValueError as e:
                raise RagError(str(e)) from None
            chunks, sections, stats = self.store.passages_of(doc_id, entry["name"], blocks, aid)
            now = self.clock()
            new = {k: v for k, v in doc.items() if k not in ("skipped_pages", "empty_pages", "coverage")}
            new.update(name=entry["name"], bytes=entry["bytes"], chars=stats["chars"], chunks=len(chunks), sha256=entry["sha256"], extraction=entry["extraction"] or doc.get("extraction", ""),
                       pages=entry["pages"], flagged=stats["flagged"], updated=now, origin={**(doc.get("origin") or {}), "version": f"{entry['version'] or 'version ' + str(entry['n'])} (restored)"},
                       revision=int(doc.get("revision") or 1) + 1)
            if "info" in stats:
                new["info"] = stats["info"]
            self.store.describe_documents(aid, [new])
            for key in ("skipped_pages", "empty_pages"):
                if (entry.get("coverage") or {}).get(key[:-6]):
                    new[key] = entry["coverage"][key[:-6]]
            new["coverage"] = coverage.of_document(new, chunks)
            prepared = [{"item": {}, "doc": new, "chunks": chunks, "sections": sections, "blocks": blocks, "fetched": Fetched(Path(), new["name"], new["sha256"], new["bytes"], {}, "", entry["version"]),
                         "old_doc": doc}]
            link = state.read()["links"].get(doc_id)
            self._commit(aid, prepared, [], {}, tell, cancel)
            if link:
                def pin(d: Dict[str, Any]) -> None:
                    d["links"][doc_id] = {**link, "pinned": True}
                state.change(pin)
            return {"document": doc_id, "restored": entry["n"], "chunks": len(chunks), "pinned": bool(link)}
