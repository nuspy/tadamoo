"""The tree of an archive's documents by where they come from, with what to follow and how often.

    kind of source (folder, web address, wiki...)
      source (one folder, one address, one wiki)
        directory or section (nested as deep as the keys go)
          document

Every node has a switch to follow it (on by default). A node that is off turns off everything below it, without losing what each one below had set: turn it on again and they are as
they were. A node shows `on` when everything below follows, `off` when nothing does and `mixed` between; a document is on or off. Each node may also have how often it is checked:
by hand only, every so many minutes, hours, days, weeks, months or years, on certain days of the week or of the month, or "as its parts say" (the node has no schedule of its own: the
nodes and documents below have theirs). A node's own setting is kept in the sources record of the archive (`nodes`); nothing here reads or writes the archive's documents."""
from __future__ import annotations

import calendar
import datetime as dt
from typing import Any, Dict, Iterator, List, Optional, Tuple

MODES = ("manual", "interval", "parts", "occurrence")
UNITS = ("minutes", "hours", "days", "weeks", "months", "years")
OCCURRENCES = ("weekday", "monthday", "nth_weekday", "first_workday", "last_workday")
NTH = (1, 2, 3, 4, -1)                                  # first, second, third, fourth, last
MIN_INTERVAL_MIN = 5
KIND_ORDER = ("folder", "file", "url", "mediawiki", "wikibridge")
HAND = "k:hand"


class TreeError(ValueError):
    pass


# -- the frequency --------------------------------------------------------------------------------

def clean_freq(raw: Any) -> Dict[str, Any]:
    """A frequency as the person set it, checked and reduced to its fields; an error says what is wrong."""
    if not isinstance(raw, dict) or raw.get("mode") not in MODES:
        raise TreeError("mode: one of " + ", ".join(MODES))
    mode = raw["mode"]
    if mode in ("manual", "parts"):
        return {"mode": mode}
    if mode == "interval":
        n, unit = raw.get("every"), raw.get("unit")
        if isinstance(n, bool) or not isinstance(n, int) or n < 1 or n > 100000:
            raise TreeError("every: a whole number of at least 1")
        if unit not in UNITS:
            raise TreeError("unit: one of " + ", ".join(UNITS))
        if (unit == "minutes" and n < MIN_INTERVAL_MIN):
            raise TreeError(f"every: at least {MIN_INTERVAL_MIN} minutes")
        return {"mode": "interval", "every": n, "unit": unit}
    kind, at = raw.get("type"), str(raw.get("at") or "03:00")
    if kind not in OCCURRENCES:
        raise TreeError("type: one of " + ", ".join(OCCURRENCES))
    try:
        hh, mm = (int(x) for x in at.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError
    except ValueError:
        raise TreeError("at: a time such as 03:00") from None
    out: Dict[str, Any] = {"mode": "occurrence", "type": kind, "at": f"{hh:02d}:{mm:02d}"}
    if kind == "weekday":
        days = raw.get("days")
        if not isinstance(days, list) or not days or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days):
            raise TreeError("days: weekdays from 0 (Monday) to 6 (Sunday)")
        out["days"] = sorted(set(days))
    elif kind == "monthday":
        days = raw.get("days")
        if not isinstance(days, list) or not days or any(d != "last" and (isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 31) for d in days):
            raise TreeError("days: days of the month from 1 to 31, or last")
        out["days"] = sorted({d for d in days if d != "last"}) + (["last"] if "last" in days else [])
    elif kind == "nth_weekday":
        nth, wd = raw.get("nth"), raw.get("weekday")
        if nth not in NTH or isinstance(nth, bool):
            raise TreeError("nth: 1, 2, 3, 4 or -1 (the last)")
        if isinstance(wd, bool) or not isinstance(wd, int) or not 0 <= wd <= 6:
            raise TreeError("weekday: from 0 (Monday) to 6 (Sunday)")
        out.update(nth=nth, weekday=wd)
    return out


def _add_months(d: dt.datetime, months: int) -> dt.datetime:
    month = d.month - 1 + months
    year, month = d.year + month // 12, month % 12 + 1
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


def _after_interval(last: dt.datetime, f: Dict[str, Any]) -> dt.datetime:
    n, unit = f["every"], f["unit"]
    if unit == "months":
        return _add_months(last, n)
    if unit == "years":
        return _add_months(last, 12 * n)
    return last + dt.timedelta(**{unit: n})


def _is_workday(d: dt.date) -> bool:
    return d.weekday() < 5


def _matches(day: dt.date, f: Dict[str, Any]) -> bool:
    kind = f["type"]
    if kind == "weekday":
        return day.weekday() in f["days"]
    if kind == "monthday":
        return day.day in f["days"] or ("last" in f["days"] and day.day == calendar.monthrange(day.year, day.month)[1])
    if kind == "nth_weekday":
        if day.weekday() != f["weekday"]:
            return False
        if f["nth"] == -1:
            return day.day + 7 > calendar.monthrange(day.year, day.month)[1]
        return (day.day - 1) // 7 + 1 == f["nth"]
    days = [dt.date(day.year, day.month, n) for n in range(1, calendar.monthrange(day.year, day.month)[1] + 1)]
    workdays = [d for d in days if _is_workday(d)]
    return bool(workdays) and day == (workdays[0] if kind == "first_workday" else workdays[-1])


def last_occurrence(f: Dict[str, Any], now: dt.datetime) -> Optional[dt.datetime]:
    """The latest moment of an occurrence schedule that is not after `now`, looking back two months; None when there is none."""
    hh, mm = (int(x) for x in f["at"].split(":"))
    for back in range(0, 63):
        day = (now - dt.timedelta(days=back)).date()
        if _matches(day, f):
            at = dt.datetime(day.year, day.month, day.day, hh, mm)
            if at <= now:
                return at
    return None


def is_due(f: Optional[Dict[str, Any]], last: float, since: float, now: float) -> bool:
    """Whether a node with frequency `f` is to be checked at `now` (all in seconds since the epoch). `last` is when it was last checked (0: never) and `since` when the schedule was set: a
    schedule never fires for a time before it was set. By hand and "as its parts say" are never due by themselves."""
    if not f or f["mode"] in ("manual", "parts"):
        return False
    if f["mode"] == "interval":
        base = last or since
        return now >= _after_interval(dt.datetime.fromtimestamp(base), f).timestamp()
    occurred = last_occurrence(f, dt.datetime.fromtimestamp(now))
    return occurred is not None and occurred.timestamp() > max(last, since)


def describe(f: Optional[Dict[str, Any]]) -> str:
    """The schedule in a few plain words (English; the page translates the pieces it shows)."""
    if not f or f["mode"] == "manual":
        return "manual"
    if f["mode"] == "parts":
        return "parts"
    if f["mode"] == "interval":
        return f"every {f['every']} {f['unit']}"
    return f"{f['type']} {f.get('days') or ''}{f.get('nth', '')}{f.get('weekday', '')} at {f['at']}".replace("  ", " ")


# -- the tree -------------------------------------------------------------------------------------

def _parts(key: str) -> List[str]:
    return [p for p in str(key).replace("\\", "/").split("/") if p]


def build(data: Dict[str, Any], documents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The tree for a sources record (`sources`, `links`, `nodes`) and the archive's documents. Every document is in it: those that no source gave are under "added by hand". A node is
    `{id, type, label, own, state, freq, children}` and a document `{id, type: "doc", doc, label, own, state}`; `own` is the node's own switch and `state` what it shows after the nodes above."""
    nodes = data.get("nodes") if isinstance(data.get("nodes"), dict) else {}
    by_doc = {d["id"]: d for d in documents}
    kinds: Dict[str, Dict[str, Any]] = {}

    def node(nid: str, typ: str, label: str, **more: Any) -> Dict[str, Any]:
        own = nodes.get(nid, {})
        return {"id": nid, "type": typ, "label": label, "own": own.get("follow", True) is not False, "freq": own.get("freq"), "last": own.get("last", 0), "since": own.get("since", 0), "children": [], **more}

    for src in data.get("sources", []):
        kind = src["kind"]
        k = kinds.setdefault(kind, node("k:" + kind, "kind", kind, kind=kind))
        s = node("s:" + src["id"], "source", src.get("label") or src["id"], kind=kind, source=src["id"])
        k["children"].append(s)
        dirs: Dict[str, Dict[str, Any]] = {"": s}
        for doc_id, link in sorted(data.get("links", {}).items(), key=lambda kv: str(kv[1].get("key"))):
            if link.get("source") != src["id"] or doc_id not in by_doc:
                continue
            parts = _parts(link.get("key", ""))
            prefix, parent = "", s
            for p in parts[:-1]:
                prefix = f"{prefix}/{p}" if prefix else p
                if prefix not in dirs:
                    dirs[prefix] = node(f"d:{src['id']}:{prefix}", "dir", p, source=src["id"], path=prefix)
                    parent["children"].append(dirs[prefix])
                parent = dirs[prefix]
            parent["children"].append(node("x:" + doc_id, "doc", by_doc[doc_id]["name"], doc=doc_id, source=src["id"]))
    linked = {i for i, l in data.get("links", {}).items()}
    hand = [d for d in documents if d["id"] not in linked]
    roots: List[Dict[str, Any]] = [kinds[k] for k in KIND_ORDER if k in kinds] + [kinds[k] for k in sorted(kinds) if k not in KIND_ORDER]
    if hand:
        h = node(HAND, "kind", "hand", kind="hand")
        h["children"] = [{"id": "x:" + d["id"], "type": "doc", "label": d["name"], "doc": d["id"], "own": True, "freq": None, "last": 0, "since": 0, "children": [], "hand": True} for d in hand]
        roots.append(h)

    def settle(n: Dict[str, Any], above: bool) -> str:
        eff = above and n["own"]
        if n["type"] == "doc":
            n["state"] = "on" if eff else "off"
            return n["state"]
        kids = [settle(c, eff) for c in n["children"]]
        if not eff:
            n["state"] = "off"
        elif not kids or all(k == "on" for k in kids):
            n["state"] = "on"
        elif all(k == "off" for k in kids):
            n["state"] = "off"
        else:
            n["state"] = "mixed"
        return n["state"]
    for r in roots:
        settle(r, True)
    return roots


def walk(roots: List[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """Every node with the nodes above it, from the top down."""
    def go(n: Dict[str, Any], above: List[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
        yield n, above
        for c in n["children"]:
            yield from go(c, above + [n])
    for r in roots:
        yield from go(r, [])


def find(roots: List[Dict[str, Any]], node_id: str) -> Optional[Dict[str, Any]]:
    return next((n for n, _ in walk(roots) if n["id"] == node_id), None)


def docs_under(n: Dict[str, Any]) -> List[str]:
    """The ids of the documents at or below a node."""
    if n["type"] == "doc":
        return [n["doc"]]
    return [d for c in n["children"] for d in docs_under(c)]


def followed(data: Dict[str, Any], source_id: str, key: str) -> bool:
    """Whether an item of a source (its key) is followed: its own switch and those of the directories, the source and the kind above it. An item with no document yet is followed unless a
    directory or the source above it is off, or it was removed on purpose (`excluded`)."""
    nodes = data.get("nodes") if isinstance(data.get("nodes"), dict) else {}
    src = next((s for s in data.get("sources", []) if s["id"] == source_id), None)
    if src is None:
        return True
    chain = ["k:" + src["kind"], "s:" + source_id]
    prefix = ""
    for p in _parts(key)[:-1]:
        prefix = f"{prefix}/{p}" if prefix else p
        chain.append(f"d:{source_id}:{prefix}")
    link = next((i for i, l in data.get("links", {}).items() if l.get("source") == source_id and l.get("key") == key), None)
    if link:
        chain.append("x:" + link)
    return all(nodes.get(c, {}).get("follow", True) is not False for c in chain)


def excluded(data: Dict[str, Any], source_id: str, key: str) -> bool:
    """A key the person removed from the source's tree: it is not read again. A directory removed excludes every key below it."""
    return any(key == e or key.startswith(e.rstrip("/") + "/") for e in (data.get("excluded") or {}).get(source_id, []))


def due_nodes(roots: List[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    """The nodes whose own schedule says they are to be checked at `now`, leaving out those inside a node that is off, and those below another due node (checking the node above covers them)."""
    out: List[Dict[str, Any]] = []
    covered: set = set()
    for n, above in walk(roots):
        if n["type"] == "doc" and n.get("hand"):
            continue
        if any(a["id"] in covered for a in above) or n["state"] == "off" and n["type"] != "doc":
            continue
        if n["type"] == "doc" and n["state"] == "off":
            continue
        if is_due(n.get("freq"), float(n.get("last") or 0), float(n.get("since") or 0), now):
            out.append(n)
            covered.add(n["id"])
    return out
