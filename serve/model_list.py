"""GET /v1/models and GET /v1/models/{id}: the model this server serves, and every other model it could serve from the
models folders (the Loader's scan, serve/models.py: the .basalt files that are "ready"; a GGUF to convert or a file
Basalt cannot run is not listed), in the OpenAI shape or, for an Anthropic client, in Anthropic's.

Each model has a stable id: the served name (--alias, else the file's own name) for the one the server runs, the
file's name without .basalt for the others (with a short hash after it when two folders hold the same file name). The
others are listed with "status": {"value": "unloaded"}; a request that names one of them is refused with a 404 that
says how to load it (the Loader, or Avvia-Basalt.ps1 --model) instead of being answered by the model that is loaded.
Any other name (the served one, "default", "x", none at all) is served as before. Nothing switches the model on
request.

The scan reads headers only (serve/models.py keeps them by path, size and modification time) and runs on a thread at
most every REFRESH_S seconds: a chat request never waits for it (before the first scan nothing is refused), and only
the first GET /v1/models waits for it, at most FIRST_WAIT_S. File paths are shown only on a server bound to a
loopback address."""
from __future__ import annotations

import datetime
import ipaddress
import os
import threading
import time
from pathlib import Path

REFRESH_S = 30.0                  # a listing older than this is scanned again, on a thread
FIRST_WAIT_S = 5.0                # how long the first GET /v1/models waits for the first scan
OWNER = "basalt"
STARTED = int(time.time())        # the "created" of a model whose file cannot be read


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _mtime(path) -> int:
    try:
        return int(os.stat(path).st_mtime)
    except (OSError, TypeError, ValueError):
        return STARTED


def rfc3339(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_loopback(host) -> bool:
    """A server bound to this address answers only this PC."""
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


# ------------------------------------------------------------------------------------------------ the folders' models
def folder_models() -> list[dict]:
    """The .basalt files under the models folders that the engine can open ("ready"), one entry each:
    {"id", "name", "path", "names" (what a request may call it, folded), "created", "context_length", "vision",
    "quant", "size_bytes"}."""
    from serve import models
    folders = models.dirs()
    if not folders:
        return []
    found = [m for m in models.scan(folders)["models"]
             if m.get("kind") == "basalt" and m.get("status") == "ready" and m.get("role") == "model"]
    stems: dict = {}
    for m in found:
        stems.setdefault(Path(m["path"]).stem.casefold(), []).append(m)
    out = []
    for m in found:
        stem = Path(m["path"]).stem
        mid = stem if len(stems[stem.casefold()]) == 1 else f"{stem}-{m['id'][:6]}"
        out.append({"id": mid, "name": m.get("name") or stem, "path": m["path"],
                    "names": {mid.casefold(), stem.casefold(), Path(m["path"]).name.casefold(), _norm(m["path"])},
                    "created": _mtime(m["path"]), "context_length": m.get("context_length"),
                    "vision": bool(m.get("vision")), "quant": m.get("quant"), "size_bytes": m.get("size_bytes")})
    return out


class Catalog:
    """The folders' models from `scan`, kept and scanned again on a thread when older than `refresh_s`."""

    def __init__(self, scan=folder_models, refresh_s: float = REFRESH_S):
        self.scan, self.refresh_s = scan, refresh_s
        self._lock = threading.Lock()
        self._items: list[dict] | None = None
        self._at = 0.0
        self._thread: threading.Thread | None = None

    def items(self, wait: float = 0.0) -> list[dict] | None:
        """The models (None before the first scan has finished); a scan starts on a thread when they are old.
        `wait`: seconds to wait for a first scan."""
        with self._lock:
            old = self._items is None or time.monotonic() - self._at >= self.refresh_s
            if old and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._refresh, name="models-list-scan", daemon=True)
                self._thread.start()
            items, thread = self._items, self._thread
        if items is None and wait > 0 and thread is not None:
            thread.join(wait)
            with self._lock:
                items = self._items
        return items

    def _refresh(self) -> None:
        try:
            items = list(self.scan())
        except Exception as e:  # noqa: BLE001 - a folder that cannot be scanned lists nothing more, never fails
            print(f"[server] warning: the models folders could not be listed for /v1/models: {e}", flush=True)
            items = self._items if self._items is not None else []
        with self._lock:
            self._items, self._at = items, time.monotonic()

    def invalidate(self) -> None:
        """Scan again at the next look (the models folders changed)."""
        with self._lock:
            self._at = float("-inf")

    def refresh(self) -> list[dict]:
        """Scan now, on this thread (tests, a Loader change)."""
        self._refresh()
        return self._items or []


# ------------------------------------------------------------------------------------------------ the served model
def served_path(svc) -> str | None:
    return (getattr(svc, "settings_info", None) or {}).get("model") or None


def served_names(svc) -> list[str]:
    names = getattr(svc, "model_names", None)
    return list(names()) if callable(names) else [svc.model]


def _context(svc) -> int:
    reported = getattr(svc, "reported_ctx", None)
    return int(reported() if callable(reported) else getattr(svc.engine, "max_context", 0) or 0)


def served(svc, local: bool) -> list[dict]:
    """The served model in the OpenAI shape, then one entry per alias; empty when it is not listed (the engine is
    down and does not load on use, or a standby server has no model)."""
    loaded = svc.loaded()
    if not loaded and not (getattr(svc, "idle_unload_s", 0) or getattr(svc.engine, "unloaded", False)):
        return []
    path = served_path(svc)
    ctx = _context(svc)
    model = {"id": svc.model, "object": "model", "created": _mtime(path) if path else STARTED, "owned_by": OWNER,
             "display_name": svc.model,
             # like llama-server's router: listed, and "unloaded" when it loads on use
             "status": {"value": "loaded" if loaded else "unloaded"},
             "meta": {"n_ctx": ctx}, "context_length": ctx,
             "architecture": {"input_modalities": ["text", "image"] if svc.vision is not None else ["text"],
                              "output_modalities": ["text"]}}
    if local and path:
        model["path"] = path
    aliases = [x for x in served_names(svc) if x != svc.model]
    if aliases:
        model["aliases"] = aliases
    return [model, *({**model, "id": x, "alias_of": svc.model} for x in aliases)]


def others(svc, wait: float = 0.0) -> list[dict]:
    """The folders' models other than the served one (by file, and by name: a served name always wins)."""
    catalog = getattr(svc, "model_catalog", None)
    items = catalog.items(wait) if catalog is not None else None
    if not items:
        return []
    path = served_path(svc)
    taken = {x.casefold() for x in served_names(svc)}
    keep = [e for e in items if not (path and _norm(e["path"]) == _norm(path)) and e["id"].casefold() not in taken]
    return sorted(keep, key=lambda e: (e["name"].casefold(), e["id"].casefold()))


def _other_openai(e: dict, local: bool) -> dict:
    out = {"id": e["id"], "object": "model", "created": e["created"], "owned_by": OWNER, "display_name": e["name"],
           "status": {"value": "unloaded"},
           "architecture": {"input_modalities": ["text", "image"] if e.get("vision") else ["text"],
                            "output_modalities": ["text"]}}
    if e.get("context_length"):
        out["context_length"] = e["context_length"]
        out["meta"] = {"n_ctx_train": e["context_length"]}
    if e.get("quant"):
        out["quant"] = e["quant"]
    if local:
        out["path"] = e["path"]
    return out


def listing(svc, local: bool, wait: float = FIRST_WAIT_S) -> list[dict]:
    """Every model, the served one (and its aliases) first, in the OpenAI shape."""
    return [*served(svc, local), *(_other_openai(e, local) for e in others(svc, wait))]


def openai_list(svc, local: bool) -> dict:
    return {"object": "list", "data": listing(svc, local)}


def _anthropic(m: dict) -> dict:
    return {"type": "model", "id": m["id"], "display_name": m.get("display_name") or m["id"],
            "created_at": rfc3339(m.get("created") or STARTED)}


def anthropic_list(svc, local: bool) -> dict:
    data = [_anthropic(m) for m in listing(svc, local)]
    return {"data": data, "has_more": False, "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None}


def find(svc, model_id: str, local: bool) -> dict | None:
    """GET /v1/models/{id}: the model with that id (case ignored when no id matches exactly), OpenAI shape."""
    every = listing(svc, local)
    return next((m for m in every if m["id"] == model_id), None) or \
        next((m for m in every if m["id"].casefold() == str(model_id).casefold()), None)


def anthropic_one(m: dict) -> dict:
    return _anthropic(m)


def not_found(model_id: str, anthropic: bool) -> dict:
    text = f"model {model_id!r} not found: GET /v1/models lists the models of this server"
    if anthropic:
        return {"type": "error", "error": {"type": "not_found_error", "message": text}}
    return {"error": {"message": text, "type": "invalid_request_error", "param": "model", "code": "model_not_found"}}


# ------------------------------------------------------------------------------------------------ requests
def refusal(svc, asked, local: bool) -> str | None:
    """The sentence of the 404 for a request whose "model" names a listed model that is not loaded, else None (the
    served model's names, unknown and generic names, and no name are served as before). Never waits for a scan."""
    if not isinstance(asked, str) or not asked.strip():
        return None
    want = asked.strip()
    if want in served_names(svc) or want.casefold() in {x.casefold() for x in served_names(svc)}:
        return None
    path = served_path(svc)
    if path and _norm(want) == _norm(path):
        return None
    key = want.casefold()
    hit = next((e for e in others(svc) if key in e["names"] or _norm(want) in e["names"]), None)
    if hit is None:
        return None
    where = f'"{hit["path"]}"' if local else f'"<the {hit["id"]}.basalt file>"'
    now = (f"this server is serving {svc.model!r}" if svc.loaded() or getattr(svc.engine, "unloaded", False) or
           getattr(svc, "idle_unload_s", 0) else "no model is loaded on this server")
    return (f"The model {hit['id']!r} is not loaded ({now}). Load it in the web page's Loader (choose it in the model "
            f"list, then Load), or start the server with it: .\\Avvia-Basalt.ps1 --model {where}")


def refused_body(message: str, kind: str) -> dict:
    """The 404's body for /v1/chat/completions ("openai"), /v1/messages ("anthropic") or /v1/responses."""
    if kind == "anthropic":
        return {"type": "error", "error": {"type": "not_found_error", "message": message}}
    return {"error": {"message": message, "type": "invalid_request_error", "param": "model", "code": "model_not_found"}}


def wants_anthropic(headers) -> bool:
    """An Anthropic client: it sends anthropic-version, or its key as x-api-key without an OpenAI Authorization."""
    if headers.get("anthropic-version"):
        return True
    return bool(headers.get("x-api-key")) and not headers.get("Authorization")
