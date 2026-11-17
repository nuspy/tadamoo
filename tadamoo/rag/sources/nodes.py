"""What the person does on the tree of an archive's documents (tadamoo/rag/sources/tree.py): switch a node on or off, give it a schedule, remove it, read it again, and read what is due.

Each function takes the sources engine (`Sync`) and the archive. The settings of a node are kept in the archive's sources record (`nodes`); what was removed on purpose is kept as
`excluded` keys, so that the next check does not bring it back."""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from tadamoo.rag.errors import RagError
from tadamoo.rag.sources import tree

Progress = Callable[[Dict[str, Any]], None]


def _roots(sync: Any, aid: str) -> List[Dict[str, Any]]:
    return tree.build(sync.sources(aid), sync.store.load(aid).meta["documents"])


def _node(sync: Any, aid: str, node_id: Any) -> Dict[str, Any]:
    n = tree.find(_roots(sync, aid), str(node_id))
    if n is None:
        raise RagError("there is no such node", "not_found")
    return n


def view(sync: Any, aid: str) -> Dict[str, Any]:
    """The tree and what a schedule can be made of."""
    return {"tree": _roots(sync, aid), "options": {"modes": list(tree.MODES), "units": list(tree.UNITS), "occurrences": list(tree.OCCURRENCES), "nth": list(tree.NTH),
                                                    "min_minutes": tree.MIN_INTERVAL_MIN}}


def set_follow(sync: Any, aid: str, node_id: Any, on: Any) -> Dict[str, Any]:
    """Switches a node on or off. A node that is off turns off everything below it; what each one below had set is kept."""
    if not isinstance(on, bool):
        raise RagError("on: true or false")
    n = _node(sync, aid, node_id)
    if n.get("hand"):
        raise RagError("a document added by hand is not followed by any source: there is nothing to switch")
    sync.state(aid).change(lambda d: d["nodes"].setdefault(n["id"], {}).__setitem__("follow", on))
    return view(sync, aid)


def set_frequency(sync: Any, aid: str, node_id: Any, freq: Any) -> Dict[str, Any]:
    """Gives a node its schedule (`tree.clean_freq`); the moment it is set counts, so a schedule never fires for a time before it was set."""
    n = _node(sync, aid, node_id)
    if n.get("hand"):
        raise RagError("a document added by hand has no source to check")
    try:
        f = tree.clean_freq(freq)
    except tree.TreeError as e:
        raise RagError(str(e)) from None
    now = sync.clock()

    def put(d: Dict[str, Any]) -> None:
        row = d["nodes"].setdefault(n["id"], {})
        row["freq"], row["since"] = f, now
    sync.state(aid).change(put)
    return view(sync, aid)


def _key_of(data: Dict[str, Any], doc_id: str) -> Optional[tuple]:
    link = data["links"].get(doc_id)
    return (link["source"], link["key"]) if link else None


def remove_node(sync: Any, aid: str, node_id: Any, confirm: Any) -> Dict[str, Any]:
    """Removes a node with everything below it: its documents leave the archive, and what was removed is not read again by the next check. A source removed with its documents is
    simply gone. Nothing is touched at the source (the folder, the address, the wiki)."""
    if confirm is not True:
        raise RagError("removing a node removes its documents from the archive: it needs the confirmation")
    n = _node(sync, aid, node_id)
    data = sync.sources(aid)
    docs = tree.docs_under(n)
    if n["type"] == "source":
        sync.remove_source(aid, n["source"], True)
        return {**view(sync, aid), "removed_documents": len(docs)}
    if n["type"] == "kind" and n["id"] != tree.HAND:
        for child in n["children"]:
            sync.remove_source(aid, child["source"], True)
        return {**view(sync, aid), "removed_documents": len(docs)}
    skip: Dict[str, List[str]] = {}
    for doc in docs:
        found = _key_of(data, doc)
        if found:
            skip.setdefault(found[0], []).append(n["path"] if n["type"] == "dir" else found[1])
        sync.store.remove_document(aid, doc)

    def drop(d: Dict[str, Any]) -> None:
        for doc in docs:
            sync.state(aid).forget_document(d, doc)
        for sid, keys in skip.items():
            have = d["excluded"].setdefault(sid, [])
            have += [k for k in dict.fromkeys(keys) if k not in have]
    sync.state(aid).change(drop)
    return {**view(sync, aid), "removed_documents": len(docs)}


def _scope(node: Dict[str, Any], data: Dict[str, Any]) -> Callable[[Dict[str, Any]], bool]:
    """Which items of a check belong to a node."""
    if node["type"] == "doc":
        return lambda i: i.get("doc") == node["doc"]
    if node["type"] == "dir":
        prefix = node["path"].rstrip("/") + "/"
        return lambda i: i.get("source") == node["source"] and str(i.get("key", "")).replace("\\", "/").startswith(prefix)
    if node["type"] == "source":
        return lambda i: i.get("source") == node["source"]
    ids = {c["source"] for c in node["children"]}
    return lambda i: i.get("source") in ids


def refresh_node(sync: Any, aid: str, node_id: Any, progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Looks again at a node and everything below it: what changed or is new there is read and put in the archive. Returns the update's report."""
    n = _node(sync, aid, node_id)
    if n.get("hand"):
        raise RagError("a document added by hand has no source to look at")
    report = sync.update(aid, None, True, False, None, progress, cancel, scope=_scope(n, sync.sources(aid)))
    stamp = sync.clock() if now is None else now
    sync.state(aid).change(lambda d: d["nodes"].setdefault(n["id"], {}).__setitem__("last", stamp))
    return report


def run_due(sync: Any, aid: str, progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Reads again every node whose schedule says it is due, one after the other. `{name, counts, items, summary, nodes}`; nothing due is an empty report."""
    at = sync.clock() if now is None else now
    due = tree.due_nodes(_roots(sync, aid), at)
    counts: Dict[str, int] = {}
    items: List[Dict[str, Any]] = []
    done: List[str] = []
    for n in due:
        if cancel is not None and cancel.is_set():
            break
        try:
            report = refresh_node(sync, aid, n["id"], progress, cancel, at)
        except RagError as e:
            if e.code == "busy":
                break
            raise
        done.append(n["id"])
        for k, v in report["counts"].items():
            counts[k] = counts.get(k, 0) + v
        items += report["items"]
    name = sync.store.load(aid).meta["name"]
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()) if v) or "nothing due"
    return {"name": name, "counts": counts, "items": items, "summary": f"{len(done)} node(s) read again: {summary}" if done else "nothing due", "nodes": done}
