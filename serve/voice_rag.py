"""What the voice service may ask of the knowledge archives: the passages for a function that is running now (the copilot, the meeting bot, a call, the avatar,
the minutes of a meeting), composed here, and the questions about letting other people perceive an archive's content.

The voice service cannot call `/api/rag/*`; it calls the routes of this module with its token (`serve/voice_service.py` SERVICE_ROUTES):

    POST /api/voice/rag/passages   {function, archives: [ids], query, session?, granted?, denied?, how?, audience?}   the data block and what goes with it
    POST /api/voice/rag/warm       {}                                                                                  loads the small embedder now
    POST /api/voice/rag/ask        {session, function, archive_id, preview?, how?}                                     a question for the person (dialog, and aloud when the archive says ask_voice)
    GET  /api/voice/rag/ask/<id>   the question and its answer
    POST /api/voice/rag/ask/<id>/answer {answer}                                                                       an answer given by voice

and the page's: `GET /api/voice/consent` (the open questions) and `POST /api/voice/consent/answer {id, answer}`.

The query is other people's words: it is cleaned and cut (`tadamoo/rag/rt_query.py`), searched by words and meaning inside the function's time budget
(`tadamoo/rag/realtime.py`; the minutes use the full search) and never reaches a tool. The block is for the TAIL of the prompt, fenced as data; the caller keeps
it out of the system message. Archives not cleared for cloud models are left out while a heavy job has the local model out. An archive whose `others_ok` is
deny, or is an asking kind not yet answered yes, adds nothing to what other people can perceive: with `audience` others (the meeting bot, a call, an avatar
on a call) its passages stay out of the block and a question is reported in `pending`; the private copilot window never needs the answer, and the passages it
used come back in `sources` so that the click that says a suggestion aloud can ask first."""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.rag import realtime, rt_block, rt_query
from tadamoo.rag.errors import RagError
from tadamoo.rag.recall import citation, shown
from serve import voice_resolve
from serve.voice_consent import Consents, HOWS

FUNCTIONS = ("copilot", "meeting", "call", "avatar", "minutes")
AUDIENCE = {"copilot": "me", "minutes": "me", "meeting": "others", "call": "others", "avatar": "others"}
RESOLVER_FUNCTION = {"minutes": "meeting"}
TOP_K = 3
BLOCK_CHARS = {"minutes": 6000}
BLOCK_DEFAULT = 2500
QUERY_CAP = {"minutes": 800}
MAX_ARCHIVES = 20
PREVIEW_CHARS = 240


def _ids(value: Any, what: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(i, str) or not i or len(i) > 80 for i in value) or len(value) > MAX_ARCHIVES:
        raise RagError(f"{what}: a list of at most {MAX_ARCHIVES} archive ids")
    return list(dict.fromkeys(value))


class VoiceRag:
    def __init__(self, store: Any, searcher: Any, consents: Consents, indexer: Any = None, cloud: Callable[[], bool] = lambda: False,
                 resolver: Callable[[Any, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]] = voice_resolve.resolve,
                 batch: Optional[Callable[[Sequence[str], str, int], Dict[str, Any]]] = None) -> None:
        self.store, self.searcher, self.consents, self.indexer, self.cloud, self.resolver = store, searcher, consents, indexer, cloud, resolver
        self.batch = batch or (lambda ids, query, top_k: store.search(ids, query, top_k=top_k, min_share=0.0, strict=False))

    # -- the passages ------------------------------------------------------------------------------------

    def passages(self, req: Dict[str, Any]) -> Dict[str, Any]:
        function = req.get("function")
        if function not in FUNCTIONS:
            raise RagError("function: one of " + ", ".join(FUNCTIONS))
        ids = _ids(req.get("archives"), "archives")
        granted, denied = set(_ids(req.get("granted"), "granted")), set(_ids(req.get("denied"), "denied"))
        how = req.get("how", "speech" if AUDIENCE[function] == "others" else "screen")
        if how not in ("speech", "screen"):
            raise RagError("how: speech or screen")
        audience = AUDIENCE[function]
        if function == "avatar" and req.get("audience") == "me":
            audience = "me"                                              # the avatar talking to the person alone: nobody else perceives it
        routed = bool(self.cloud())
        out: Dict[str, Any] = {"block": "", "sources": [], "pending": [], "blocked": [], "marker": "", "profile": "", "mode": "", "timed_out": False, "elapsed_ms": 0,
                               "dropped": 0, "routed_to_cloud": routed, "audience": audience}
        query, dropped = rt_query.clean(req.get("query") if isinstance(req.get("query"), str) else "", QUERY_CAP.get(function, rt_query.QUERY_CHARS))
        out["dropped"] = dropped
        if not ids or not query:
            out["marker"] = "" if ids else "no archive is chosen"
            return out
        resolution = self.resolver(self.store, {"function": RESOLVER_FUNCTION.get(function, function), "ids": ids, "project": None, "attached": ids,
                                                "routed_to_cloud": routed, "audience": audience})
        out["blocked"] = resolution["blocked"]
        usable = {a["id"]: a for a in resolution["archives"]}
        if not usable:
            return out
        if function == "minutes":
            found = self.batch(list(usable), query, TOP_K * 2)
            found = {**found, "profile": "full", "timed_out": False, "marker": found.get("note", "") if found.get("mode") != "hybrid" else "", "elapsed_ms": 0}
        else:
            if self.indexer is not None:
                self.indexer.ensure(list(usable))                         # the small model's vectors are made in the background for the archives used here
            budget = realtime.BUDGET_S.get(function, realtime.DEFAULT_BUDGET_S)
            if isinstance(req.get("budget_ms"), (int, float)) and not isinstance(req.get("budget_ms"), bool):
                budget = min(30.0, max(0.1, float(req["budget_ms"]) / 1000.0))
            found = self.searcher.search(list(usable), query, TOP_K, budget)
        out.update(profile=found.get("profile", ""), mode=found.get("mode", ""), timed_out=bool(found.get("timed_out")), marker=found.get("marker", ""),
                   elapsed_ms=int(found.get("elapsed_ms", 0)))
        drop_flagged = self.store.settings.read().get("flagged") == "drop"
        lines: List[str] = []
        used = 0
        limit = BLOCK_CHARS.get(function, BLOCK_DEFAULT)
        first: Dict[str, str] = {}
        sources: Dict[str, Dict[str, Any]] = {}
        for hit in found.get("hits", []):
            aid = hit["archive_id"]
            row = usable.get(aid)
            if row is None or (drop_flagged and hit.get("flag")):
                continue
            first.setdefault(aid, hit["text"])
            if audience == "others" and row["others_ok"] in ("ask_dialog", "ask_voice") and aid not in granted:
                continue                                                  # asked about below, and not said until the person agrees
            if audience == "others" and aid in denied:
                continue
            block = f"{citation(hit)}\n{shown(hit)}"
            if used + len(block) > limit and lines:
                break
            lines.append(block)
            used += len(block)
            sources.setdefault(aid, {"archive_id": aid, "archive": row["name"], "others_ok": row["others_ok"], "preview": " ".join(hit["text"].split())[:PREVIEW_CHARS]})
        for aid, row in usable.items():
            if audience == "others" and aid in first and row["others_ok"] in ("ask_dialog", "ask_voice") and aid not in granted and aid not in denied:
                out["pending"].append({"archive_id": aid, "archive": row["name"], "ask": "voice" if row["others_ok"] == "ask_voice" else "dialog",
                                       "preview": " ".join(first[aid].split())[:PREVIEW_CHARS]})
        out["sources"] = list(sources.values())
        out["block"] = rt_block.build(lines, how == "speech", routed)
        return out

    def warm(self) -> Dict[str, Any]:
        """Starts the small embedder in the background: a meeting or a call is about to begin."""
        threading.Thread(target=self.searcher.warm, name="rag-rt-warm", daemon=True).start()
        return {"warming": True}

    # -- the questions -----------------------------------------------------------------------------------

    def ask(self, req: Dict[str, Any]) -> Dict[str, Any]:
        aid = req.get("archive_id")
        if not isinstance(aid, str) or not aid:
            raise RagError("archive_id: the id of an archive")
        meta = self.store.load(aid).meta
        how = req.get("how", "spoken")
        if how not in HOWS:
            raise RagError("how: spoken, written or shown")
        asks_aloud = voice_resolve.others_of(meta) == "ask_voice"
        voice = asks_aloud and req.get("voice_ok") is True              # the service has a channel the others cannot hear; without one the question stays in the dialog
        try:
            return {"question": self.consents.ask(str(req.get("session") or ""), str(req.get("function") or ""), meta["id"], meta["name"],
                                                  str(req.get("preview") or ""), how, "voice" if voice else "dialog", asks_aloud and not voice)}
        except ValueError as e:
            raise RagError(str(e)) from None

    def question(self, rid: str) -> Dict[str, Any]:
        got = self.consents.get(rid)
        if got is None:
            raise RagError("no such question", "not_found")
        return {"question": got}

    def answer(self, rid: str, answer: Any, by: str) -> Dict[str, Any]:
        try:
            got = self.consents.answer(rid, answer, by)
        except ValueError as e:
            raise RagError(str(e)) from None
        if got is None:
            raise RagError("no such question", "not_found")
        return {"question": got}

    def save_always(self, aid: str) -> bool:
        """"Always": the archive's `others_ok` becomes allow. False when the store cannot keep it (the answer then holds for the meeting only)."""
        try:
            self.store.update(aid, {"others_ok": "allow"})
            return True
        except RagError:
            return False
