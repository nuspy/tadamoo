"""The refresh of knowledge archives over the API: the sources of an archive, a check for what changed, an update, the versions kept, the verify, and the settings. The
work is tadamoo/rag/sources; a check, an update and a verify are jobs of the same board as adding a document (serve/rag_jobs.py), so the page follows them the same way.

Routes, under /api/rag (this PC only, as the rest):

  GET  sync/settings                          the settings of the refresh;   POST sync/settings   any of them
  GET  archives/<id>/sync                     the sources, what the last check and update found, what needs attention, the coverage
  POST archives/<id>/sources                  {kind, label?, ...its settings} follows a source (nothing is read now)
  POST archives/<id>/sources/<sid>/remove     {remove_documents?}
  POST archives/<id>/check                    {sources?}: {job}, its result is the report
  POST archives/<id>/refresh                  {only?, include_new?, dry_run?, force?}: {job}; dry_run says what would change and changes nothing
  POST archives/<id>/verify                   {deep?}: {job}
  POST archives/<id>/documents/<doc>/gone     {action: remove | keep}
  GET  archives/<id>/documents/<doc>/versions the kept versions;   POST .../rollback  {version}: {job}"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.rag import RagError
from tadamoo.rag.sources import coverage
from tadamoo.rag.sources.engine import Sync
from tadamoo.rag.sources.verify import verify


def _flag(req: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = req.get(key, default)
    if not isinstance(value, bool):
        raise RagError(f"{key}: true or false")
    return value


def _ids(value: Any, key: str) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 2000 or any(not isinstance(v, str) for v in value):
        raise RagError(f"{key}: a list of ids")
    return value


class RagSyncApi:
    def __init__(self, sync: Sync, jobs: Any) -> None:
        self.sync, self.jobs = sync, jobs

    @property
    def store(self) -> Any:
        return self.sync.store

    def view(self, aid: str) -> Dict[str, Any]:
        """What the page shows about an archive's sources: the sources and their documents (name, stamp, version), the last check and update, the attention, the history,
        and the coverage of the documents."""
        archive = self.store.load(aid)
        data = self.sync.sources(aid)
        names = {d["id"]: d["name"] for d in archive.meta["documents"]}
        links = [{"doc": did, "name": names[did], "source": link["source"], "key": link["key"], "version": link.get("version", ""), "fetched": link.get("fetched", 0),
                  "pinned": bool(link.get("pinned")), "acknowledged": did in data["acknowledged"], "versions": len(data["versions"].get(did, []))} for did, link in data["links"].items()]
        return {"archive": aid, "running": self.sync.running(aid), "sources": data["sources"], "links": links, "unlinked": data["unlinked"], "check": data["check"], "last": data["last"],
                "attention": data["attention"], "history": data["history"], "settings": self.sync.settings.read(),
                "coverage": coverage.of_archive(archive.meta["documents"], [{"name": i["name"], "reason": i["detail"]} for i in data["last"].get("items", []) if i["state"] == "failed"])}

    def _job(self, label: str, aid: str, work: Callable[[Any, Any], Dict[str, Any]]) -> Dict[str, Any]:
        if self.sync.running(aid):
            raise RagError("a check or an update of this archive is already running", "busy")
        return {"job": self.jobs.start(label + " " + self.store.load(aid).meta["name"], work, aid)}

    def route_get(self, parts: Sequence[str]) -> Optional[Dict[str, Any]]:
        if parts == ["sync", "settings"]:
            return {"settings": self.sync.settings.read()}
        if len(parts) == 3 and parts[0] == "archives" and parts[2] == "sync":
            return self.view(parts[1])
        if len(parts) == 5 and parts[0] == "archives" and parts[2] == "documents" and parts[4] == "versions":
            return {"versions": self.sync.versions(parts[1], parts[3])}
        return None

    def route_post(self, parts: Sequence[str], req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if parts == ["sync", "settings"]:
            return {"settings": self.sync.settings.write(req)}
        if len(parts) < 3 or parts[0] != "archives":
            return None
        aid, action = parts[1], parts[2]
        if len(parts) == 3 and action == "sources":
            kind = req.get("kind")
            return {"source": self.sync.add_source(aid, kind, {k: v for k, v in req.items() if k not in ("kind", "label")}, req.get("label", ""))}
        if len(parts) == 5 and action == "sources" and parts[4] == "remove":
            return self.sync.remove_source(aid, parts[3], _flag(req, "remove_documents"))
        if len(parts) == 3 and action == "check":
            only = _ids(req.get("sources"), "sources")
            return self._job("check", aid, lambda progress, cancel: self.sync.check(aid, only, progress, cancel))
        if len(parts) == 3 and action == "refresh":
            only, force = _ids(req.get("only"), "only"), _ids(req.get("force"), "force")
            new, dry = _flag(req, "include_new", True), _flag(req, "dry_run")
            return self._job("preview" if dry else "update", aid, lambda progress, cancel: self.sync.update(aid, only, new, dry, force, progress, cancel))
        if len(parts) == 3 and action == "verify":
            deep = _flag(req, "deep")
            return self._job("verify", aid, lambda progress, cancel: verify(self.sync, aid, deep, True, progress, cancel))
        if len(parts) == 5 and action == "documents" and parts[4] == "gone":
            return self.sync.resolve_gone(aid, parts[3], str(req.get("action") or ""))
        if len(parts) == 5 and action == "documents" and parts[4] == "rollback":
            version = req.get("version")
            if isinstance(version, bool) or not isinstance(version, int):
                raise RagError("version: the number of a kept version")
            return self._job("restore", aid, lambda progress, cancel: self.sync.rollback(aid, parts[3], version, progress, cancel))
        return None

    # -- the routine and the attention mark ------------------------------------------------------

    def archive_of(self, ref: str) -> str:
        """The id of the archive a routine names: its id, or its exact name (case and accents do not matter). A fuzzy match is never taken for a scheduled job."""
        from tadamoo.rag.match import fold
        rows = self.store.list()
        hits = [a for a in rows if a["id"] == ref] or [a for a in rows if fold(a["name"]) == fold(ref)]
        if len(hits) == 1:
            return hits[0]["id"]
        raise RagError("no archive has that name or id" if not hits else "several archives have that name: use its id")

    def scheduled(self, ref: str, action: str, cancel: Any) -> Dict[str, Any]:
        """A routine's fire: checks or updates the archive and returns the run's outcome. Nothing to do is `nothing_new`; changes found or made are `progress`; an update
        that has to wait for a heavy job is skipped and tried at the next time."""
        try:
            aid = self.archive_of(ref)
            report = self.sync.check(aid, None, None, cancel) if action == "check" else self.sync.update(aid, None, True, False, None, None, cancel)
        except RagError as e:
            if e.code == "busy":
                return {"status": "skipped", "outcome": "nothing_new", "summary": str(e)}
            if e.code == "cancelled":
                return {"status": "cancelled", "summary": str(e)}
            return {"status": "failed", "summary": str(e)}
        counts = report["counts"]
        text = f"{report['name']}: {report['summary']}"
        bad = [i for i in report["items"] if i["state"] in ("failed", "error")]
        if bad:
            text += "; " + "; ".join(f"{i['name']}: {i['detail']}" for i in bad)[:600]
        quiet = not any(k for k in counts if k not in ("unchanged", "kept"))
        worked = counts.get("updated", 0) + counts.get("added", 0) + counts.get("linked", 0)
        status = "failed" if counts.get("error") or (counts.get("failed") and not worked) else "done"
        return {"status": status, "outcome": "nothing_new" if quiet else "progress", "summary": text}


def attention(root: Any) -> Dict[str, Any]:
    """The time of the latest news about any archive in the folder `root` (a change found, a document that failed, a source that could not be read), for the tab that flashes.
    One small file is read for each archive that has sources."""
    from pathlib import Path
    from tadamoo.rag.sources.state import State
    latest, name, counts = 0.0, "", {}
    folder_root = Path(root)
    if folder_root.is_dir():
        for folder in folder_root.iterdir():
            if folder.is_dir() and (folder / "sources.json").is_file():
                mark = State(folder).read()["attention"]
                if float(mark.get("t") or 0) > latest:
                    latest, name, counts = float(mark["t"]), folder.name, mark.get("counts") or {}
    return {"t": latest, "archive": name, "counts": counts}
