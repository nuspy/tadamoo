"""Which archives a voice function may use: the resolver of `tadamoo/rag/resolve.py` when it is there, else a stand-in that applies the same rules to the ids
the function was given.

The contract: `resolve(store, context)` answers `archives`, a list of `{id, name, project_name, description, documents, cloud_ok, others_ok}` that may be used,
and `blocked`, a list of `{id, name, reason}`. `context` is `{function: copilot|meeting|call|avatar, ids, project, attached, routed_to_cloud, audience: me|others}`.
`others_ok` (allow, deny, ask_dialog, ask_voice) says whether the content may be said, written or shown to other people; the resolver blocks `deny` when the
audience is others and passes the two asking kinds on, the caller asks."""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from tadamoo.rag.errors import RagError

OTHERS = ("allow", "deny", "ask_dialog", "ask_voice")
OTHERS_DEFAULT = "ask_dialog"


def others_of(meta: Dict[str, Any]) -> str:
    value = meta.get("others_ok")
    return value if value in OTHERS else OTHERS_DEFAULT


def _field(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def stand_in(store: Any, context: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    archives, blocked = [], []
    for aid in dict.fromkeys(list(context.get("ids") or []) + list(context.get("attached") or [])):
        try:
            meta = store.load(aid).meta
        except RagError:
            blocked.append({"id": aid, "name": "", "reason": "missing"})
            continue
        row = {"id": meta["id"], "name": meta["name"], "project_name": meta.get("project_name", ""), "description": meta.get("description", ""),
               "documents": len(meta.get("documents", ())), "cloud_ok": bool(meta.get("cloud_ok")), "others_ok": others_of(meta)}
        if context.get("routed_to_cloud") and not row["cloud_ok"]:
            blocked.append({"id": aid, "name": row["name"], "reason": "not_cloud_ok"})
        elif context.get("audience") == "others" and row["others_ok"] == "deny":
            blocked.append({"id": aid, "name": row["name"], "reason": "others_deny"})
        else:
            archives.append(row)
    return {"archives": archives, "blocked": blocked}


def resolve(store: Any, context: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """`{archives, blocked}` as plain dicts, from the real resolver or the stand-in."""
    try:
        from tadamoo.rag.resolve import resolve as real
    except ImportError:
        return stand_in(store, context)
    got = real(store, context)
    archives = [{**dict(a), "others_ok": others_of(dict(a))} if not isinstance(a, dict) else {**a, "others_ok": others_of(a)} for a in (_field(got, "archives", []) or [])]
    return {"archives": archives, "blocked": [dict(b) if isinstance(b, dict) else {"id": _field(b, "id", ""), "name": _field(b, "name", ""), "reason": _field(b, "reason", "")}
                                              for b in (_field(got, "blocked", []) or [])]}


def ids_of(resolution: Dict[str, Sequence[Dict[str, Any]]]) -> List[str]:
    return [a["id"] for a in resolution["archives"]]
