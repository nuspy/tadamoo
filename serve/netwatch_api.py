"""The page's view of the security monitor: the routes under /api/netwatch, one function per route."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from serve.netwatch import NetwatchError, NetWatch

_WATCH: Optional[NetWatch] = None


def get() -> NetWatch:
    global _WATCH
    if _WATCH is None:
        _WATCH = NetWatch()
    return _WATCH


def start_if_enabled() -> None:
    try:
        get().apply()
    except Exception:                                   # the monitor never keeps the server from starting
        pass


def stop() -> None:
    if _WATCH is not None:
        _WATCH.stop()


def _number(query: Dict[str, str], key: str, default: float) -> float:
    try:
        return float(query.get(key, default))
    except (TypeError, ValueError):
        raise NetwatchError(f"{key} is a number.") from None


def route_get(path: str, query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    nw = get()
    if path == "/api/netwatch":
        return 200, nw.status()
    if path == "/api/netwatch/events":
        only = query.get("only", "all")
        if only not in ("all", "suspicious"):
            raise NetwatchError('only is "all" or "suspicious".')
        rows = nw.events_list(_number(query, "since", 0), int(_number(query, "limit", 200)), only)
        return 200, {"events": rows, "total": len(nw.events)}
    if path == "/api/netwatch/alerts":
        return 200, {"alerts": nw.alerts(), "enabled": bool(nw.store.settings["enabled"])}
    if path == "/api/netwatch/exceptions":
        return 200, {"exceptions": nw.exceptions_list()}
    if path == "/api/netwatch/verify":
        return 200, nw.verify()
    if path == "/api/netwatch/export":
        return 200, nw.export()
    return 404, {"error": {"type": "not_found", "message": "No such route."}}


def route_post(path: str, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    nw = get()
    if path == "/api/netwatch/settings":
        nw.update_settings(req)
        return 200, nw.status()
    if path == "/api/netwatch/exceptions":
        return 200, nw.save_exception(req)
    if path == "/api/netwatch/events":
        return 200, nw.add_manual(req)
    if path == "/api/netwatch/events/delete":
        return 200, nw.delete_events(req)
    parts = path.split("/")                              # ['', 'api', 'netwatch', 'alerts'|'exceptions', id, action]
    if len(parts) == 6 and parts[3] == "alerts" and parts[5] == "resolve":
        return 200, nw.resolve(parts[4], req)
    if len(parts) == 6 and parts[3] == "exceptions" and parts[5] == "delete":
        nw.delete_exception(parts[4])
        return 200, {"deleted": parts[4]}
    return 404, {"error": {"type": "not_found", "message": "No such route."}}


def error_view(exc: NetwatchError) -> Tuple[int, Dict[str, Any]]:
    status = 404 if exc.code == "gone" else 400
    return status, {"error": {"type": "netwatch", "code": exc.code, "message": str(exc)}}
