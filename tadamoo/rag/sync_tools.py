"""The tools with which a conversation looks after the freshness of a knowledge archive: `knowledge_check` (what changed in the sources of an archive, nothing is read)
and `knowledge_update` (read again what changed). The work is the server's (it holds the embedding model, the one-run-per-archive guard and the wait for heavy jobs):
these ask its `/api/rag` and follow the job. A document that its source no longer has is reported, never removed; that is the person's decision in the Knowledge tab."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from tadamoo.rag.match import find
from tadamoo.rag.recall import ProjectRecall
from tadamoo.rag.store import RagStore

LOCAL = ("127.0.0.1", "localhost", "::1")
WAIT_S = 900.0                        # how long a tool follows a job before it says the job still runs
POLL_S = 1.5
SHOWN = 25                            # items listed in an answer; the counts are exact


def server_call(endpoint: str, api_key: str = "", wait_s: float = WAIT_S, sleep: Callable[[float], None] = time.sleep) -> Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]]:
    """A function `(path, body) -> the result of the job` that POSTs to the server's `/api/rag/<path>` and follows the job it answers, or None when `endpoint` is not this PC.
    It raises RuntimeError with the reason when the request or the job fails, and returns `{"running": job id}` when the job outlives `wait_s`."""
    parts = urlsplit(endpoint or "")
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in LOCAL:
        return None
    base = endpoint.rstrip("/") + "/api/rag/"
    headers = {"Content-Type": "application/json", **({"Authorization": "Bearer " + api_key} if api_key else {})}

    def ask(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read()).get("error", {}).get("message", "")
            except ValueError:
                why = ""
            raise RuntimeError(why or f"the server answered HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"the server did not answer: {e}") from None

    def call(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        job = ask("POST", path, body)["job"]
        end = time.monotonic() + wait_s
        while job["state"] == "running":
            if time.monotonic() > end:
                return {"running": job["id"]}
            sleep(POLL_S)
            job = ask("GET", "jobs/" + job["id"])["job"]
        if job["state"] != "done":
            raise RuntimeError(job.get("error") or f"the job {job['state']}")
        return job["result"]
    return call


def _items(report: Dict[str, Any], states: tuple) -> List[Dict[str, Any]]:
    return [{"document": i["name"], "state": i["state"], "detail": str(i.get("detail", ""))[:200]} for i in report["items"]
            if i["state"] in states and not i.get("acknowledged")][:SHOWN]


def register_sync_tools(registry: Any, store: RagStore, recall: ProjectRecall, call: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]]) -> None:
    """Registers `knowledge_check` and `knowledge_update`. `call` is `server_call(...)`; without a server on this PC the tools say so."""
    from tadamoo.tools.registry import EffectClass

    def pick(archive: str) -> Dict[str, Any]:
        found = find(store.list(), archive)
        if found["status"] == "many":
            return {"status": "ambiguous", "candidates": [{"id": a["id"], "name": a["name"]} for a in found["candidates"]], "note": "several archives match: nothing was done; ask which one"}
        if found["status"] == "none":
            return {"status": "none", "note": "no archive has that name: call knowledge_list"}
        row = found["archive"]
        if not recall.local() and not recall._cloud_ok(row["id"]):
            return {"status": "not_allowed", "note": "this archive is not cleared to be shown to a model outside this PC"}
        if call is None:
            return {"status": "error", "note": "the refresh is run by the TaDaMoo server, which is not on this PC here"}
        return {"status": "ok", "id": row["id"], "name": row["name"]}

    def run(archive: str, path: str, body: Dict[str, Any], states: tuple) -> Dict[str, Any]:
        row = pick(archive)
        if row["status"] != "ok":
            return row
        try:
            report = call(f"archives/{row['id']}/{path}", body)                      # type: ignore[misc]
        except RuntimeError as e:
            return {"status": "error", "archive": row["name"], "note": str(e)}
        if "running" in report:
            return {"status": "running", "archive": row["name"], "note": "it takes longer than this call waits: it goes on in the server; ask again later"}
        return {"status": "ok", "archive": row["name"], "summary": report["summary"], "counts": report["counts"], "documents": _items(report, states),
                **({"embedded": report["embedded"], "reused": report["reused"]} if "embedded" in report else {}),
                **({"sources_failed": [e["error"][:200] for e in report["errors"]]} if report.get("errors") else {})}

    def knowledge_check(archive: str) -> Dict[str, Any]:
        out = run(archive, "check", {}, ("changed", "new", "gone", "error", "pinned"))
        if out["status"] == "ok" and out["counts"].get("gone"):
            out["note"] = "documents their source no longer has are only marked; the person decides in the Knowledge tab whether to remove them"
        return out

    def knowledge_update(archive: str, include_new: bool = True) -> Dict[str, Any]:
        out = run(archive, "refresh", {"include_new": include_new is not False}, ("updated", "added", "linked", "failed", "gone", "error", "pinned"))
        if out["status"] == "ok":
            out["note"] = "a failed document keeps its old version; a document its source lacks is marked, never removed"
        return out

    registry.register(
        name="knowledge_check",
        description=("Looks, without reading any document, for what changed in the sources of a knowledge archive (RAG): files and folders on this PC, web pages, Wikipedia or "
                     "other wiki pages, the company wiki. Say what is changed, new or gone. Use it when the person asks whether an archive is up to date ('is the company wiki "
                     "archive current?', 'controlla se il RAG della wiki è aggiornato'). `archive` is the name the person used."),
        parameters={"type": "object", "properties": {"archive": {"type": "string", "description": "Name of the archive"}}, "required": ["archive"]},
        effect=EffectClass.PURE, handler=knowledge_check, untrusted=True, timeout=WAIT_S + 60)
    registry.register(
        name="knowledge_update",
        description=("Updates a knowledge archive (RAG) from its sources: reads again only the documents that changed, adds the new ones, keeps the old version of a document that "
                     "cannot be read, and keeps the previous versions to roll back. A document its source no longer has is never removed. Use it when the person asks to update "
                     "or refresh an archive ('aggiorna il RAG della wiki aziendale', 'update the company wiki archive'); it can take minutes. `include_new: false` leaves new items out."),
        parameters={"type": "object", "properties": {"archive": {"type": "string", "description": "Name of the archive"},
                                                     "include_new": {"type": "boolean", "description": "Also add the items that are new in the source", "default": True}},
                    "required": ["archive"]},
        effect=EffectClass.REVERSIBLE, handler=knowledge_update, untrusted=True, timeout=WAIT_S + 60)
