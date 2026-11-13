"""The tools with which a conversation builds a knowledge archive: `knowledge_create` (make or choose the archive of the project), `knowledge_add` (files, folders, web
addresses and documents the search found, in a batch), `knowledge_index` (index again, and have a model describe the passages) and `knowledge_status` (how the archive stands).

The work is the server's, as for `knowledge_check` and `knowledge_update` (tadamoo/rag/sync_tools.py): it holds the readers, the embedding model, the one-run-per-archive guard
and the politeness towards websites. These tools ask its `/api/rag` and follow the job; an adding that outlives the wait goes on in the server and `knowledge_status` shows it."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from tadamoo.rag.match import find
from tadamoo.rag.recall import ProjectRecall
from tadamoo.rag.store import RagStore
from tadamoo.rag.sync_tools import WAIT_S

SHOWN = 40                           # results listed in an answer; the counts are exact
STATUS_ARCHIVES = 12


def register_manage_tools(registry: Any, store: RagStore, recall: ProjectRecall, call: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]]) -> None:
    """Registers the four tools. `call` is `server_call(...)` (it also carries `.ask`, the plain request); without a server on this PC the tools say so."""
    from tadamoo.tools.registry import EffectClass

    def server_missing() -> Dict[str, Any]:
        return {"status": "error", "note": "adding to an archive is run by the TaDaMoo server, which is not on this PC here"}

    def pick(archive: str) -> Dict[str, Any]:
        rows = store.list()
        if not archive:                                              # no name: the one archive of this project, when there is only one
            mine = [a for a in rows if a.get("project") == recall.project]
            if len(mine) == 1:
                archive = mine[0]["name"]
            else:
                return {"status": "ask", "candidates": [{"id": a["id"], "name": a["name"]} for a in mine], "note": "name the archive (knowledge_list shows them), or make one with knowledge_create"}
        found = find(rows, archive)
        if found["status"] == "many":
            return {"status": "ambiguous", "candidates": [{"id": a["id"], "name": a["name"]} for a in found["candidates"]], "note": "several archives match: nothing was done; ask which one"}
        if found["status"] == "none":
            return {"status": "none", "note": "no archive has that name: call knowledge_list, or make it with knowledge_create"}
        row = found["archive"]
        if not recall.local() and not recall._cloud_ok(row["id"]):
            return {"status": "not_allowed", "note": "this archive is not cleared to be shown to a model outside this PC"}
        if call is None:
            return server_missing()
        return {"status": "ok", "id": row["id"], "name": row["name"]}

    def ask(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return call.ask(method, path, body)                          # type: ignore[union-attr]

    def knowledge_create(name: str, description: str = "") -> Dict[str, Any]:
        if call is None:
            return server_missing()
        if not recall.project:
            return {"status": "error", "note": "this conversation has no project to keep an archive in"}
        existing = find([a for a in store.list() if a.get("project") == recall.project], name)
        if existing["status"] == "one":
            row = existing["archive"]
            return {"status": "exists", "archive": {"id": row["id"], "name": row["name"], "documents": row["documents"], "passages": row["chunks"]},
                    "note": "this project already has an archive with that name: add to it with knowledge_add"}
        if existing["status"] == "many":
            return {"status": "ambiguous", "candidates": [{"id": a["id"], "name": a["name"]} for a in existing["candidates"]], "note": "several archives of this project match that name: use one of them or give the new one a clearer name"}
        try:
            made = ask("POST", "archives", {"name": name, "description": description, "project": recall.project})["archive"]
        except RuntimeError as e:
            return {"status": "error", "note": str(e)}
        recall.on_change(list(recall.attached), "created", made["name"])
        return {"status": "created", "archive": {"id": made["id"], "name": made["name"]}, "note": "it is empty: add documents with knowledge_add"}

    def knowledge_add(items: Any = None, archive: str = "", urls: Any = None, files: Any = None, folders: Any = None) -> Dict[str, Any]:
        got = pick(archive)
        if got["status"] != "ok":
            return got
        wanted: List[str] = []
        for value in (items, urls, files, folders):
            wanted += [str(v) for v in (value if isinstance(value, list) else [value] if isinstance(value, str) else []) if str(v).strip()]
        if not wanted:
            return {"status": "error", "note": "give the files, folders or web addresses to add in `items`"}
        try:
            report = call(f"archives/{got['id']}/add", {"items": wanted})              # type: ignore[misc]
        except RuntimeError as e:
            return {"status": "error", "archive": got["name"], "note": str(e)}
        if "running" in report:
            return {"status": "running", "archive": got["name"], "note": "it takes longer than this call waits: it goes on in the server; call knowledge_status to see how far it is"}
        results = report["results"]
        return {"status": "ok", "archive": got["name"], "counts": report["counts"], "documents_in_archive": report["documents"], "passages": report["chunks"], "search": report["mode"],
                "results": [{k: v for k, v in r.items() if k != "embedded"} for r in results[:SHOWN]], **({"more_results": len(results) - SHOWN} if len(results) > SHOWN else {}),
                "note": "added documents are read and indexed; a duplicate was already in the archive; a failed item says why (a format that cannot be read, a site that refuses, a wrong path)"}

    def knowledge_index(archive: str = "", describe: bool = False) -> Dict[str, Any]:
        got = pick(archive)
        if got["status"] != "ok":
            return got
        out: Dict[str, Any] = {"status": "ok", "archive": got["name"]}
        try:
            done = call(f"archives/{got['id']}/reindex", {})                          # type: ignore[misc]
            if "running" in done:
                return {"status": "running", "archive": got["name"], "note": "indexing takes longer than this call waits: it goes on in the server; call knowledge_status"}
            out["indexed"] = {"passages": done["chunks"], "embedded": done["embedded"], **({"note": done["note"]} if done.get("note") else {})}
            if describe is True:
                said = call(f"archives/{got['id']}/enrich", {})                       # type: ignore[misc]
                out["described"] = said if "running" not in said else {"running": "it goes on in the server; call knowledge_status"}
        except RuntimeError as e:
            return {"status": "error", "archive": got["name"], "note": str(e), **({"indexed": out["indexed"]} if "indexed" in out else {})}
        return out

    def knowledge_status(archive: str = "") -> Dict[str, Any]:
        if call is None:
            return server_missing()
        if archive:
            got = pick(archive)
            if got["status"] != "ok":
                return got
            ids = [got["id"]]
        else:
            ids = [a["id"] for a in store.list() if a.get("project") == recall.project and (recall.local() or recall._cloud_ok(a["id"]))][:STATUS_ARCHIVES]
            if not ids:
                return {"status": "none", "note": "this project has no archive yet: make one with knowledge_create"}
        rows = []
        for aid in ids:
            try:
                s = ask("GET", f"archives/{aid}/status")
            except RuntimeError as e:
                rows.append({"archive": aid, "error": str(e)})
                continue
            a = s["archive"]
            d = s["describing"]
            rows.append({"archive": a["name"], "documents": a["documents"], "passages": a["chunks"], "search": "by meaning and words" if a["mode"] == "hybrid" else "by words only",
                         "embedding_model": a["embed_model"] or "none", "index_again": bool(a["reindex"] or a["rechunk"]), "database": s["database"],
                         "described": (f"{d.get('applied', {}).get('passages', 0)} passages described in {d.get('language', '')}" if d.get("applied") else "not described"),
                         "sources": [x["label"] for x in s["sources"]], "last_update": s["last_update"], "running": s["running"], "recent_jobs": s["recent"],
                         **({"documents_list": [{"name": x["name"], "pages": x["pages"], "passages": x["passages"]} for x in s["documents"][:SHOWN]]} if archive else {})})
        return {"status": "ok", "archives": rows}

    registry.register(
        name="knowledge_create",
        description=("Makes a knowledge archive (RAG) in this project, or finds the one that already has that name. Use it before `knowledge_add` when the person asks to start a new "
                     "collection ('crea un RAG sui brevetti CRISPR', 'make a knowledge base for these papers') and no archive of the project fits (`knowledge_list` shows them). "
                     "`name` is a short name of the subject; `description` says what it will hold."),
        parameters={"type": "object", "properties": {"name": {"type": "string", "description": "Name of the archive"}, "description": {"type": "string", "description": "What it holds"}},
                    "required": ["name"]},
        effect=EffectClass.REVERSIBLE, handler=knowledge_create)
    registry.register(
        name="knowledge_add",
        description=("Adds documents to a knowledge archive (RAG), in one call or in a batch: `items` is a list of files on this PC, folders on this PC and web addresses (http or https, "
                     "a PDF, a page, a Word file...). Each is read (OCR for scans), cut and indexed. A folder is read with its subfolders and stays a source of the archive, so "
                     "`knowledge_update` reads again what changes in it; an address is downloaded into the archive's download folder first (robots.txt and a pause between requests "
                     "to the same site are respected, 50 MB at most each). Use it whenever the person asks to put documents in a RAG, in any wording ('aggiungi questi PDF al RAG', "
                     "'cerca tutti i brevetti su CRISPR e aggiungili al RAG'): for a request to find documents, search the web first (`search_web`, `browse_url`), collect the addresses "
                     "of the documents themselves (the PDF links, not the result pages), then call this with all of them, up to 200 at a time, and report what was added, what was "
                     "already there and what failed. `archive` is the archive's name; empty: the only archive of this project. It can take many minutes for a big batch: if the call "
                     "returns `running`, it goes on in the server and `knowledge_status` shows how far it is."),
        parameters={"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}, "description": "Files, folders and web addresses to add (at most 200)"},
                                                     "archive": {"type": "string", "description": "Name of the archive; empty: the project's only archive"}}, "required": ["items"]},
        effect=EffectClass.REVERSIBLE, handler=knowledge_add, untrusted=True, timeout=WAIT_S + 60)
    registry.register(
        name="knowledge_index",
        description=("Cuts the saved texts of a knowledge archive again and indexes them with the embedding model in use (what the person calls 'index again' or 'reindex'), "
                     "and with `describe: true` has a model write a summary and the questions each passage answers (the descriptions make the search find more; it can take a long "
                     "time and needs the model). Use it when `knowledge_status` says `index_again`, after the embedding model changed, or when the person asks to index or describe an archive."),
        parameters={"type": "object", "properties": {"archive": {"type": "string", "description": "Name of the archive; empty: the project's only archive"},
                                                     "describe": {"type": "boolean", "description": "Also have a model describe the passages", "default": False}}},
        effect=EffectClass.REVERSIBLE, handler=knowledge_index, timeout=WAIT_S + 60)
    registry.register(
        name="knowledge_status",
        description=("Says how a knowledge archive stands: documents and passages, whether it is searched by meaning and words or by words only, the embedding model, whether it should be "
                     "indexed again, whether a model described its passages, the database it lives in, its sources, and what is running or just ended in the server (an adding, an "
                     "indexing). Without `archive` it tells every archive of this project. Use it after `knowledge_add` or `knowledge_index`, and when the person asks what is in the "
                     "RAG or how it is going."),
        parameters={"type": "object", "properties": {"archive": {"type": "string", "description": "Name of the archive; empty: all of this project's"}}},
        effect=EffectClass.PURE, handler=knowledge_status)
