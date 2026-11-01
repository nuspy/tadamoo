"""The routes under /api/llamacpp: llama-server in the Loader. One function per route; the handler checks that the caller is this PC."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from serve import llamacpp
from serve.llamacpp import LlamaError


def gguf_models() -> List[Dict[str, Any]]:
    """The GGUF files in the models folders that a llama-server can be given (not the vision projectors)."""
    try:
        from serve import models
        folders = models.dirs()
        found = models.scan(folders)["models"] if folders else []
    except Exception:                                     # the list is a convenience: the page also takes a path
        return []
    return [{"path": m["path"], "name": m.get("name") or m["path"], "size_bytes": m.get("size_bytes")}
            for m in found if m.get("kind") == "gguf" and m.get("role") == "model" and m.get("path")]


def view() -> Dict[str, Any]:
    return {**llamacpp.get().status(), "models": gguf_models()}


def route_get(path: str) -> Tuple[int, Dict[str, Any]]:
    if path == "/api/llamacpp":
        return 200, view()
    return 404, {"error": {"message": "not found"}}


def route_post(path: str, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    mgr = llamacpp.get()
    action = path[len("/api/llamacpp/"):]
    try:
        if action == "path":
            return 200, mgr.set_path(req.get("path"))
        if action == "start":
            return 202, mgr.start({k: v for k, v in req.items() if k in ("model", "port", "ctx", "ngl", "parallel", "alias", "autostart")})
        if action == "stop":
            return 200, mgr.stop()
        if action == "update/check":
            return 200, {"plan": mgr.update_check()}
        if action == "update/apply":
            return 202, {"job": mgr.update_apply(req.get("token"), req.get("confirm"))}
    except LlamaError as exc:
        return exc.status, exc.body
    return 404, {"error": {"message": "not found"}}


def start_if_enabled() -> None:
    """At server start: the program runs again when the person left it set to (off unless they asked)."""
    try:
        llamacpp.get().start_if_saved()
    except Exception:                                     # it never keeps the server from starting
        pass
