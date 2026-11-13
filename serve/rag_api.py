"""The Knowledge tab: the archives of a project, their documents, and a question to try.

Only the server's own page reaches it (this PC): the archives hold the person's documents. The storage and the search are tadamoo/rag; an archive
belongs to a project (a workspace folder, or the Chat's own folder), named by its key (tadamoo/rag/project.py)."""
from __future__ import annotations

import importlib.util
import json
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.rag import LIMITS, RagError, RagStore
from tadamoo.rag.ingest import docling_client, extraction_of
from tadamoo.rag.project import is_key, project_key
from tadamoo.rag.resolve import FUNCTIONS, Resolution, resolve, routes_outside
from serve.rag_db_api import RagDbApi
from serve.rag_jobs import Jobs
from serve.rag_sync_api import RagSyncApi
from tadamoo.rag.sources.engine import Sync

UPLOAD_MAX = LIMITS["document_bytes"]
JSON_MAX = 1 << 17
QUERY_CHARS = 2000
CONTEXT_IDS = ("function", "session", "plan", "node", "routine", "meeting", "project", "audience")


def upload_limit(parts: Sequence[str]) -> int:
    """How many bytes the body of a POST to these route parts may hold."""
    return UPLOAD_MAX if parts[-1:] == ["upload"] else JSON_MAX


def _project(value: Any) -> str:
    if not is_key(value):
        raise RagError("project: the key of a project")
    return value


class RagApi:
    def __init__(self, store: RagStore, folders: Callable[[], List[Dict[str, Any]]] = lambda: [], jobs: Optional[Jobs] = None, service: Any = None,
                 sources: Callable[[], Dict[str, Callable[[Dict[str, Any]], Any]]] = lambda: {}, leaves_pc: Callable[[str], bool] = routes_outside, sync: Any = None,
                 describer: Optional[Callable[[], Any]] = None, vault_dir: Any = None) -> None:
        """`folders` lists the projects as `{name, path, chat}`: the folders of the Agent tab and the Chat's own folder (`chat` true). `jobs` is the
        board of the long jobs, kept by the server between requests. `service` is the embedding service (tadamoo/rag/embed_client.py) the store's embedder
        comes from, when there is one: it tells where the embeddings run and can be tried. `sources` gives what the resolver cannot know alone (the session's
        record, the archives a plan step, a routine or a meeting declares); `leaves_pc(function)` says whether a function's model calls leave this PC. `sync` is the
        refresh of the archives (tadamoo/rag/sources), kept by the server between requests: it holds the run guard and the politeness state."""
        self.store, self.folders, self.service, self.sources, self.leaves_pc = store, folders, service, sources, leaves_pc
        self.describer = describer                          # a function that gives the model client for the function `rag_enrich` (None: no model to describe archives)
        self.jobs = jobs if jobs is not None else Jobs()
        self.syncing = RagSyncApi(sync if sync is not None else Sync(store), self.jobs)
        self.database = RagDbApi(store, self.jobs, vault_dir)               # the Database section of a project's settings (serve/rag_db_api.py)

    # -- reading ---------------------------------------------------------------------------------

    def embedding(self) -> Dict[str, Any]:
        """Whether hybrid search is on: it is when an embedder is installed; else every archive is searched by words alone. With the embedding service
        also the reason it is not, the reranker, and where the work runs now."""
        emb = self.store.embedder()
        out: Dict[str, Any] = {"available": emb is not None, "model": emb.model if emb is not None else ""}
        if self.service is not None:
            out.update({k: v for k, v in self.service.info().items() if k not in ("available", "model")})
        return out

    def embedding_test(self) -> Dict[str, Any]:
        """Embeds one sentence with the model in use: `{ok, model, device, seconds, dim}`, or `{ok: false, error}`."""
        if self.service is None:
            return {"ok": False, "error": "there is no embedding service"}
        return self.service.test()

    def overview(self, project: Any) -> Dict[str, Any]:
        pdf = any(importlib.util.find_spec(m) is not None for m in ("pypdf", "fitz"))
        settings = self.store.settings.read()
        return {"project": _project(project), "archives": self.store.list(project), "limits": LIMITS, "formats": self.store.formats(), "pdf": pdf, "embedding": self.embedding(),
                "extraction": extraction_of(settings), "settings": settings, "docling_python": docling_client.find_python(settings["docling_python"]),
                "jobs": [j for j in self.jobs.list() if j["state"] == "running"]}

    def settings(self) -> Dict[str, Any]:
        settings = self.store.settings.read()
        return {"settings": settings, "extraction": extraction_of(settings), "docling_python": docling_client.find_python(settings["docling_python"]), "formats": self.store.formats(),
                "embedding": self.embedding()}

    def set_settings(self, req: Dict[str, Any]) -> Dict[str, Any]:
        self.store.settings.write(req)
        return self.settings()

    def projects(self) -> Dict[str, Any]:
        """The projects the page can pick, each with its key and how many archives it holds."""
        counts = Counter(a["project"] for a in self.store.list())
        out, seen = [], set()
        for row in self.folders():
            key = project_key(row["path"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"key": key, "name": row["name"], "path": row["path"], "chat": bool(row.get("chat")), "archives": counts.get(key, 0)})
        return {"projects": out}

    def names(self) -> Dict[str, str]:
        """Project key -> name, for the folders the page can pick."""
        return {project_key(row["path"]): ("Chat" if row.get("chat") else row["name"]) for row in self.folders()}

    def catalog(self) -> Dict[str, Any]:
        """Every archive of every project, with the name of its project: what a conversation may attach."""
        names = self.names()
        return {"archives": [{**a, "project_name": names.get(a["project"]) or a["project_name"]} for a in self.store.list()]}

    def resolve(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """`{function, session?, plan?, node?, routine?, meeting?, project?, attached?, routed_to_cloud?, audience?, granted?}`: the archives that apply, by name,
        and those left out with the reason (tadamoo/rag/resolve.py). Where the model runs and who listens are worked out here; the caller can only say more."""
        for key in CONTEXT_IDS:
            if key in req and not isinstance(req[key], str):
                raise RagError(f"{key}: a text")
        for key in ("attached", "granted"):
            if key in req and (not isinstance(req[key], list) or any(not isinstance(i, str) for i in req[key]) or len(req[key]) > LIMITS["archives"] * 4):
                raise RagError(f"{key}: a list of archive ids")
        if "routed_to_cloud" in req and not isinstance(req["routed_to_cloud"], bool):
            raise RagError("routed_to_cloud: true or false")
        if req.get("function") not in FUNCTIONS:
            raise RagError("function: one of " + ", ".join(FUNCTIONS))
        got: Resolution = resolve(self.store, {k: req[k] for k in (*CONTEXT_IDS, "attached", "granted", "routed_to_cloud") if k in req}, self.sources(), self.leaves_pc)
        names = self.names()
        out = got.to_dict()
        for row in out["archives"] + out["blocked"]:
            row["project_name"] = names.get(row.get("project", "")) or row.get("project_name", "")
        return out

    def get(self, aid: str) -> Dict[str, Any]:
        return {"archive": self.store.get(aid), "embedding": self.embedding()}

    # -- changing --------------------------------------------------------------------------------

    def create(self, req: Dict[str, Any]) -> Dict[str, Any]:
        extra = [k for k in req if k not in ("name", "description", "project")]
        if extra:
            raise RagError("unknown field: " + ", ".join(sorted(map(str, extra))[:5]))
        return {"archive": self.store.create(req.get("name"), req.get("description", ""), req.get("project"), self.names().get(req.get("project"), ""))}

    def update(self, aid: str, req: Dict[str, Any]) -> Dict[str, Any]:
        return {"archive": self.store.update(aid, req)}

    def delete(self, aid: str, req: Dict[str, Any]) -> Dict[str, Any]:
        return self.store.delete(aid, req.get("confirm"))

    def add_path(self, aid: str, req: Dict[str, Any]) -> Dict[str, Any]:
        """Adds the file at `path` in the background: `{job}`; the file is checked at once (a bad path or an archive that is not there is refused now)."""
        self.store.load(aid)
        path = self.store.check_path(req.get("path"))
        return {"job": self.jobs.start("add " + path.name, lambda progress, cancel: self.store.add_file(aid, path.name, path, progress, cancel), aid)}

    def upload(self, aid: str, name: str, data: bytes) -> Dict[str, Any]:
        """Adds an uploaded file in the background: `{job}`. The name, the format and the size are checked now; the reading is the job."""
        name, tmp = self.store.stage_bytes(aid, name, data)

        def work(progress: Callable[[Dict[str, Any]], None], cancel: Any) -> Dict[str, Any]:
            try:
                return self.store.add_file(aid, name, tmp, progress, cancel)
            finally:
                tmp.unlink(missing_ok=True)
        try:
            return {"job": self.jobs.start("add " + name, work, aid)}
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def job(self, jid: str) -> Dict[str, Any]:
        return {"job": self.jobs.get(jid)}

    def cancel_job(self, jid: str) -> Dict[str, Any]:
        return {"job": self.jobs.cancel(jid)}

    def section(self, aid: str, sid: str) -> Dict[str, Any]:
        return {"section": self.store.section(aid, sid)}

    def remove_document(self, aid: str, doc_id: str) -> Dict[str, Any]:
        return self.store.remove_document(aid, doc_id)

    def reindex(self, aid: str) -> Dict[str, Any]:
        """Cuts the saved texts again and embeds them, in the background: `{job}`; its result is `{chunks, embedded, note}`."""
        name = self.store.load(aid).meta["name"]
        return {"job": self.jobs.start("index " + name, lambda progress, cancel: self.store.reindex(aid, progress, cancel), aid)}

    def enrich(self, aid: str, req: Dict[str, Any]) -> Dict[str, Any]:
        """Has a model describe every passage, section and document of the archive that is not described yet (`tadamoo/rag/enrich.py`), in the background: `{job}`.
        `language` sets the language of the descriptions first. The job reports its calls and tokens, can be cancelled, and is paused (not failed) when the model stays away."""
        from tadamoo.rag.enrich import Enricher
        meta = self.store.load(aid).meta
        if self.describer is None:
            raise RagError("there is no model to describe the archive with")
        if req.get("language") is not None:
            self.store.settings.write({"enrich_language": req["language"]})
        client = self.describer()
        worker = Enricher(self.store, client)
        worker.allowed(meta)
        return {"job": self.jobs.start("describe " + meta["name"], lambda progress, cancel: worker.run(aid, progress, cancel), aid)}

    def enrichment(self, aid: str) -> Dict[str, Any]:
        return {"enrichment": self.store.enrichment(aid)}

    def search(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """`{query, archives: [ids] | project: key, top_k, min_share?, strict?}`: the best passages of those archives, or of all the archives of the project.
        `min_share` (0 to 1) is how much of the question's words a words-only hit must answer; `strict` false leaves out an archive that is gone.
        `filters` narrows the passages searched (`language`, `number`, `gene`, `seq`, `year`, `kind`, `unit`, `document`)."""
        query = req.get("query")
        if not isinstance(query, str) or not query.strip():
            raise RagError("query: the text of the question")
        top_k = req.get("top_k", 4)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= LIMITS["top_k"]:
            raise RagError(f"top_k: a number from 1 to {LIMITS['top_k']}")
        share = req.get("min_share", 0)
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 <= share <= 1:
            raise RagError("min_share: a number from 0 to 1")
        if req.get("archives") is not None:
            ids, strict = req["archives"], req.get("strict", True) is not False
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise RagError("archives: a list of archive ids")
        else:
            ids, strict = self.store.ids(_project(req.get("project"))), False
        blocked: List[Dict[str, Any]] = []
        if req.get("context") is not None:                  # a caller that says what it is for gets only what the resolver allows it, whatever it asks for
            context = req["context"]
            if not isinstance(context, dict):
                raise RagError("context: an object, as for /api/rag/resolve")
            mine = self.resolve({**context, "attached": ids, "project": "", **({"session": context["session"]} if "session" in context else {})})
            allowed = set(mine["ids"])
            blocked = mine["blocked"]
            ids = [i for i in ids if i in allowed]
        out = self.store.search(ids, query[:QUERY_CHARS], top_k=top_k, min_share=float(share), strict=strict, filters=req.get("filters"))
        return {**out, "searched": len(ids), **({"blocked": blocked} if blocked else {})}

    # -- the routes ------------------------------------------------------------------------------

    def route_get(self, parts: Sequence[str], query: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
        """GET /api/rag?project=<key>, /api/rag/projects, /api/rag/archives/<id>: `parts` are the path segments after /api/rag."""
        if not parts:
            return self.overview(((query or {}).get("project") or [""])[0])
        if parts == ["projects"]:
            return self.projects()
        if parts == ["catalog"]:
            return self.catalog()
        if parts == ["resolve"]:
            q = query or {}
            req: Dict[str, Any] = {k: (q.get(k) or [""])[0] for k in (*CONTEXT_IDS,) if q.get(k)}
            for key in ("attached", "granted"):
                if q.get(key):
                    req[key] = [i for i in q[key][0].split(",") if i]
            if q.get("routed_to_cloud"):
                req["routed_to_cloud"] = q["routed_to_cloud"][0] == "true"
            return self.resolve(req)
        got = self.syncing.route_get(parts)
        if got is None:
            got = self.database.route_get(parts, query)
        if got is not None:
            return got
        if parts == ["settings"]:
            return self.settings()
        if len(parts) == 2 and parts[0] == "jobs":
            return self.job(parts[1])
        if len(parts) == 3 and parts[0] == "archives" and parts[2] == "enrichment":
            return self.enrichment(parts[1])
        if len(parts) == 4 and parts[0] == "archives" and parts[2] == "sections":
            return self.section(parts[1], parts[3])
        if len(parts) == 2 and parts[0] == "archives":
            return self.get(parts[1])
        raise RagError("not found", "not_found")

    def route_post(self, parts: Sequence[str], query: Dict[str, List[str]], raw: bytes) -> Dict[str, Any]:
        """POST under /api/rag. `raw` is the body: JSON, except for `archives/<id>/upload`, where it is the file and `?name=` its name."""
        if parts[-1:] == ["upload"]:
            if len(parts) == 3 and parts[0] == "archives":
                return self.upload(parts[1], (query.get("name") or [""])[0], raw)
            raise RagError("not found", "not_found")
        req = json.loads(raw or b"{}")
        if not isinstance(req, dict):
            raise RagError("send a JSON object")
        got = self.syncing.route_post(parts, req)
        if got is None:
            got = self.database.route_post(parts, req)
        if got is not None:
            return got
        if parts == ["search"]:
            return self.search(req)
        if parts == ["resolve"]:
            return self.resolve(req)
        if parts == ["settings"]:
            return self.set_settings(req)
        if parts == ["embedding", "test"]:
            return self.embedding_test()
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
            return self.cancel_job(parts[1])
        if parts == ["archives"]:
            return self.create(req)
        if len(parts) == 3 and parts[0] == "archives":
            aid, action = parts[1], parts[2]
            handlers = {"update": self.update, "delete": self.delete, "documents": self.add_path, "reindex": lambda a, _req: self.reindex(a), "enrich": self.enrich}
            if action in handlers:
                return handlers[action](aid, req)
        if len(parts) == 5 and parts[0] == "archives" and parts[2] == "documents" and parts[4] == "remove":
            return self.remove_document(parts[1], parts[3])
        raise RagError("not found", "not_found")
