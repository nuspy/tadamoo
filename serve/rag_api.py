"""The Knowledge tab: archives, their documents, a question to try, and which personalities use which archive.

Only the server's own page reaches it (this PC): the archives hold the person's documents. The storage and the search are tadamoo/rag; the link
between an archive and a personality is the personality's own `knowledge` list (tadamoo/personas.py), changed here so the two never disagree."""
from __future__ import annotations

import importlib.util
import json
from typing import Any, Dict, List, Sequence

from tadamoo.personas import PersonaError, PersonaStore
from tadamoo.rag import LIMITS, RagError, RagStore
from tadamoo.rag.extract import FORMATS

UPLOAD_MAX = LIMITS["document_bytes"]
JSON_MAX = 1 << 17
QUERY_CHARS = 2000


def upload_limit(parts: Sequence[str]) -> int:
    """How many bytes the body of a POST to these route parts may hold."""
    return UPLOAD_MAX if parts[-1:] == ["upload"] else JSON_MAX


class RagApi:
    def __init__(self, store: RagStore, personas: PersonaStore) -> None:
        self.store, self.personas = store, personas

    # -- reading ---------------------------------------------------------------------------------

    def embedding(self) -> Dict[str, Any]:
        """Whether hybrid search is on: it is when an embedder is installed; else every archive is searched by words alone."""
        emb = self.store.embedder()
        return {"available": emb is not None, "model": emb.model if emb is not None else ""}

    def overview(self) -> Dict[str, Any]:
        pdf = any(importlib.util.find_spec(m) is not None for m in ("pypdf", "fitz"))
        return {"archives": self.store.list(), "limits": LIMITS, "formats": list(FORMATS), "pdf": pdf, "embedding": self.embedding()}

    def _users(self, aid: str) -> List[Dict[str, Any]]:
        return [{"slug": p["slug"], "name": p["name"], "linked": aid in p["knowledge"]} for p in self.personas.list()]

    def get(self, aid: str) -> Dict[str, Any]:
        return {"archive": self.store.get(aid), "personas": self._users(aid), "embedding": self.embedding()}

    # -- changing --------------------------------------------------------------------------------

    def create(self, req: Dict[str, Any]) -> Dict[str, Any]:
        extra = [k for k in req if k not in ("name", "description")]
        if extra:
            raise RagError("unknown field: " + ", ".join(sorted(map(str, extra))[:5]))
        return {"archive": self.store.create(req.get("name"), req.get("description", ""))}

    def update(self, aid: str, req: Dict[str, Any]) -> Dict[str, Any]:
        return {"archive": self.store.update(aid, req)}

    def delete(self, aid: str, req: Dict[str, Any]) -> Dict[str, Any]:
        self.store.load(aid)
        out = self.store.delete(aid, req.get("confirm"))
        unlinked = 0
        for p in self.personas.list():
            if aid in p["knowledge"]:
                try:
                    self.personas.update(p["slug"], {"knowledge": [i for i in p["knowledge"] if i != aid]})
                    unlinked += 1
                except PersonaError:
                    continue
        return {**out, "unlinked": unlinked}

    def add_path(self, aid: str, req: Dict[str, Any]) -> Dict[str, Any]:
        return self.store.add_path(aid, req.get("path"))

    def upload(self, aid: str, name: str, data: bytes) -> Dict[str, Any]:
        return self.store.add_bytes(aid, name, data)

    def remove_document(self, aid: str, doc_id: str) -> Dict[str, Any]:
        return self.store.remove_document(aid, doc_id)

    def reindex(self, aid: str) -> Dict[str, Any]:
        return self.store.reindex(aid)

    def link(self, aid: str, req: Dict[str, Any]) -> Dict[str, Any]:
        """Links the archive to a personality or unlinks it: `{persona: slug, linked: true|false}`."""
        self.store.load(aid)
        slug, linked = req.get("persona"), req.get("linked")
        if not isinstance(linked, bool):
            raise RagError("linked: true or false")
        try:
            ids = [i for i in self.personas.get(slug)["knowledge"] if i != aid] + ([aid] if linked else [])
            self.personas.update(slug, {"knowledge": ids})
        except PersonaError as e:
            raise RagError(str(e), e.code) from None
        return {"personas": self._users(aid)}

    def search(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """`{query, archives: [ids] | persona: slug, top_k}`: the best passages; with neither archives nor persona, all archives."""
        query = req.get("query")
        if not isinstance(query, str) or not query.strip():
            raise RagError("query: the text of the question")
        top_k = req.get("top_k", 4)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= LIMITS["top_k"]:
            raise RagError(f"top_k: a number from 1 to {LIMITS['top_k']}")
        if req.get("persona"):
            try:
                ids = list(self.personas.get(req["persona"])["knowledge"])
            except PersonaError as e:
                raise RagError(str(e), e.code) from None
            strict = False
        elif req.get("archives") is not None:
            ids, strict = req["archives"], True
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise RagError("archives: a list of archive ids")
        else:
            ids, strict = [a["id"] for a in self.store.list()], True
        out = self.store.search(ids, query[:QUERY_CHARS], top_k=top_k, strict=strict)
        return {**out, "searched": len(ids)}

    # -- the routes ------------------------------------------------------------------------------

    def route_get(self, parts: Sequence[str]) -> Dict[str, Any]:
        """GET /api/rag, /api/rag/archives/<id>: `parts` are the path segments after /api/rag."""
        if not parts:
            return self.overview()
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
        if parts == ["archives"]:
            return self.create(req)
        if len(parts) == 3 and parts[0] == "archives":
            aid, action = parts[1], parts[2]
            handlers = {"update": self.update, "delete": self.delete, "documents": self.add_path, "link": self.link, "reindex": lambda a, _req: self.reindex(a)}
            if action in handlers:
                return handlers[action](aid, req)
        if len(parts) == 5 and parts[0] == "archives" and parts[2] == "documents" and parts[4] == "remove":
            return self.remove_document(parts[1], parts[3])
        raise RagError("not found", "not_found")
