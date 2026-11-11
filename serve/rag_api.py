"""The Knowledge tab: the archives of a project, their documents, and a question to try.

Only the server's own page reaches it (this PC): the archives hold the person's documents. The storage and the search are tadamoo/rag; an archive
belongs to a project (a workspace folder, or the Chat's own folder), named by its key (tadamoo/rag/project.py)."""
from __future__ import annotations

import importlib.util
import json
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.rag import LIMITS, RagError, RagStore
from tadamoo.rag.choice import decide, known
from tadamoo.rag.ingest import docling_client, extraction_of
from tadamoo.rag.project import is_key, project_key
from tadamoo.rag.recall import compose_message
from serve.rag_jobs import Jobs

UPLOAD_MAX = LIMITS["document_bytes"]
JSON_MAX = 1 << 17
QUERY_CHARS = 2000


def upload_limit(parts: Sequence[str]) -> int:
    """How many bytes the body of a POST to these route parts may hold."""
    return UPLOAD_MAX if parts[-1:] == ["upload"] else JSON_MAX


def _project(value: Any) -> str:
    if not is_key(value):
        raise RagError("project: the key of a project")
    return value


class RagApi:
    def __init__(self, store: RagStore, folders: Callable[[], List[Dict[str, Any]]] = lambda: [], jobs: Optional[Jobs] = None, service: Any = None) -> None:
        """`folders` lists the projects as `{name, path, chat}`: the folders of the Agent tab and the Chat's own folder (`chat` true). `jobs` is the
        board of the long jobs, kept by the server between requests. `service` is the embedding service (tadamoo/rag/embed_client.py) the store's embedder
        comes from, when there is one: it tells where the embeddings run and can be tried."""
        self.store, self.folders, self.service = store, folders, service
        self.jobs = jobs if jobs is not None else Jobs()

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

    def choose(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """`{action: attach|detach|clear, name, attached: [ids], project}` for a conversation the server does not keep (the model-only chat): what the
        choice does to its list of attached archives, `{status, ids, archive | candidates}`; the same rule as a conversation of the agent."""
        action, attached = req.get("action"), req.get("attached", [])
        if action not in ("attach", "detach", "clear") or not isinstance(req.get("name", ""), str) or not isinstance(attached, list):
            raise RagError("send {action: attach | detach | clear, name, attached: [ids], project}")
        rows = self.catalog()["archives"]
        return decide(rows, _project(req.get("project")), known(attached, rows), action, req.get("name", ""))

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
        out = self.store.search(ids, query[:QUERY_CHARS], top_k=top_k, min_share=float(share), strict=strict, filters=req.get("filters"))
        return {**out, "searched": len(ids)}

    def recall(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """`{project, goal, attached}`: the data block of the passages of the project's archives and of the attached ones that match the request, for a
        conversation without a harness."""
        goal, attached = req.get("goal", ""), req.get("attached", [])
        if not isinstance(goal, str):
            raise RagError("goal: the text of the request")
        if not isinstance(attached, list) or any(not isinstance(i, str) for i in attached) or len(attached) > LIMITS["archives"]:
            raise RagError("attached: a list of archive ids")
        return {"block": compose_message(self.store, _project(req.get("project")), goal[:QUERY_CHARS], attached)}

    # -- the routes ------------------------------------------------------------------------------

    def route_get(self, parts: Sequence[str], query: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
        """GET /api/rag?project=<key>, /api/rag/projects, /api/rag/archives/<id>: `parts` are the path segments after /api/rag."""
        if not parts:
            return self.overview(((query or {}).get("project") or [""])[0])
        if parts == ["projects"]:
            return self.projects()
        if parts == ["catalog"]:
            return self.catalog()
        if parts == ["settings"]:
            return self.settings()
        if len(parts) == 2 and parts[0] == "jobs":
            return self.job(parts[1])
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
        if parts == ["search"]:
            return self.search(req)
        if parts == ["recall"]:
            return self.recall(req)
        if parts == ["choose"]:
            return self.choose(req)
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
            handlers = {"update": self.update, "delete": self.delete, "documents": self.add_path, "reindex": lambda a, _req: self.reindex(a)}
            if action in handlers:
                return handlers[action](aid, req)
        if len(parts) == 5 and parts[0] == "archives" and parts[2] == "documents" and parts[4] == "remove":
            return self.remove_document(parts[1], parts[3])
        raise RagError("not found", "not_found")
