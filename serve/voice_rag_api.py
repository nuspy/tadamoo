"""The HTTP side of `serve/voice_rag.py`: the routes the voice service calls with its token and the two routes of the page's dialog.

    voice service (token):  POST /api/voice/rag/passages | warm | ask | index    GET /api/voice/rag/ask/<id> | index?archives=a,b    POST /api/voice/rag/ask/<id>/answer
    page (this PC):         GET /api/voice/consent    POST /api/voice/consent/answer {id, answer}"""
from __future__ import annotations

import json
import re
from typing import Any, Dict
from urllib.parse import parse_qs, urlsplit

from serve.voice_rag import VoiceRag
from tadamoo.rag.errors import RagError

BASE = "/api/voice/rag"
CONSENT = "/api/voice/consent"
ASK = re.compile(r"^/api/voice/rag/ask/([0-9a-f]{16})(/answer)?$")
BODY_MAX = 1 << 14


def instance(svc: Any, make: Any) -> VoiceRag:
    """The server's one VoiceRag, made on first use by `make()` and kept on the service."""
    got = getattr(svc, "voice_rag", None)
    if got is None:
        got = svc.voice_rag = make()
    return got


def handles(path: str) -> bool:
    return path.startswith(BASE + "/") or path == CONSENT or path.startswith(CONSENT + "/")


def _body(handler) -> Dict[str, Any]:
    raw = handler._body(BODY_MAX) or b"{}"
    req = json.loads(raw)
    if not isinstance(req, dict):
        raise ValueError("send a JSON object")
    return req


def route(handler, vr: VoiceRag, method: str) -> None:
    """Answers one request; the caller made sure this PC is the one asking (loopback, not the phones' listener)."""
    path = urlsplit(handler.path).path.rstrip("/")
    query = parse_qs(urlsplit(handler.path).query)
    try:
        if method == "GET":
            if path == CONSENT:
                return handler._json(200, {"questions": vr.consents.pending()})
            if path == BASE + "/index":
                ids = [i for i in ((query.get("archives") or [""])[0]).split(",") if i][:20]
                return handler._json(200, {"archives": vr.indexer.status(ids) if vr.indexer is not None else {}})
            found = ASK.match(path)
            if found and not found.group(2):
                return handler._json(200, vr.question(found.group(1)))
            return handler._json(404, {"error": {"type": "voice_rag", "message": "not found"}})
        req = _body(handler)
        if path == BASE + "/passages":
            return handler._json(200, vr.passages(req))
        if path == BASE + "/warm":
            return handler._json(200, vr.warm(req))
        if path == BASE + "/ask":
            return handler._json(200, vr.ask(req))
        if path == BASE + "/index":
            ids = req.get("archives")
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) > 20:
                raise RagError("archives: a list of at most 20 archive ids")
            return handler._json(200, {"archives": (vr.indexer.ensure(ids), vr.indexer.status(ids))[1] if vr.indexer is not None else {}})
        found = ASK.match(path)
        if found and found.group(2):
            return handler._json(200, vr.answer(found.group(1), req.get("answer"), "voice"))
        if path == CONSENT + "/answer":
            return handler._json(200, vr.answer(str(req.get("id") or ""), req.get("answer"), "dialog"))
        return handler._json(404, {"error": {"type": "voice_rag", "message": "not found"}})
    except RagError as e:
        return handler._json(404 if e.code == "not_found" else 400, {"error": {"type": "voice_rag", "code": e.code, "message": str(e)}})
    except (ValueError, TypeError) as e:
        return handler._json(400, {"error": {"type": "voice_rag", "message": str(e) or "bad request"}})

