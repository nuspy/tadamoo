"""Work that keeps coming back: the same kind of request, done at length, again and again, could be a personal skill.

A request has a signature: its meaningful words (common words dropped, accents and case folded), the first few of them, sorted, so the same
request in a different word order or with the filler words changed has the same one. From the project's `outcomes.jsonl`, a signature seen at
least `MIN_COUNT` times, each time done in at least `MIN_STEPS` steps, is a candidate. The person is told once per signature (kept in
`<project>/.tadamoo/repeats.json`). Nothing here calls a model."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from tadamoo.lessons import PROJECT_FOLDER, OutcomeLog, write_atomic
from tadamoo.memory import query_terms

FILE = "repeats.json"
MIN_COUNT, MIN_STEPS, SIGNATURE_WORDS, MIN_WORDS = 3, 8, 10, 2
_NOT_A_REQUEST = ("The user answered", "[harness]", "ask:")


def request_signature(text: Any) -> str:
    """The words that make a request what it is, in a fixed order; "" for a text too short to tell requests apart or one that is not a request."""
    text = str(text or "").strip()
    if not text or text.startswith(_NOT_A_REQUEST):
        return ""
    terms = query_terms(text)[:SIGNATURE_WORDS]
    return " ".join(sorted(set(terms))) if len(terms) >= MIN_WORDS else ""


def _long_and_done(record: Dict[str, Any]) -> bool:
    steps = record.get("steps")
    return record.get("result") == "done" and isinstance(steps, (int, float)) and not isinstance(steps, bool) and steps >= MIN_STEPS


def groups(records: List[Dict[str, Any]], min_count: int = MIN_COUNT) -> List[Dict[str, Any]]:
    """The requests done at length at least `min_count` times: `{signature, count, steps_avg, request, last_id}`, the most repeated first."""
    by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        if isinstance(r, dict) and _long_and_done(r):
            sig = request_signature(r.get("request"))
            if sig:
                by[sig].append(r)
    out = [{"signature": sig, "count": len(rs), "steps_avg": round(sum(r["steps"] for r in rs) / len(rs), 1), "request": str(rs[-1].get("request") or "")[:200],
            "last_id": str(rs[-1].get("id") or "")} for sig, rs in by.items() if len(rs) >= min_count]
    return sorted(out, key=lambda g: (-g["count"], g["signature"]))


class Told:
    """The signatures the person was already told about, in the project's `.tadamoo` folder."""

    def __init__(self, project_dir: "str | Path") -> None:
        self.path = Path(project_dir) / PROJECT_FOLDER / FILE

    def read(self) -> List[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        told = data.get("told") if isinstance(data, dict) else None
        return [s for s in told if isinstance(s, str)] if isinstance(told, list) else []

    def add(self, signature: str) -> None:
        told = self.read()
        if signature not in told:
            write_atomic(self.path, json.dumps({"told": (told + [signature])[-200:]}, indent=1))


def hint_after(log: OutcomeLog, record: Dict[str, Any], told: Optional[Told] = None) -> Optional[Dict[str, Any]]:
    """Called when `record` has just been appended to `log`: the hint to show the person if this task made its request a repeated one that they
    were not told about yet (the signature is marked as told), else None."""
    if not _long_and_done(record):
        return None
    sig = request_signature(record.get("request"))
    found = next((g for g in groups(log.read()) if g["signature"] == sig), None) if sig else None
    told = told or Told(log.path.parent.parent)
    if found is None or sig in told.read():
        return None
    try:
        told.add(sig)
    except OSError:
        return None
    return {"signature": sig, "count": found["count"], "steps_avg": found["steps_avg"], "request": str(record.get("request") or "")[:200]}
