"""The queue of install requests: what the harness found online by itself and wants the person to authorise.

Nothing found by the harness is installed unasked. When no one can be asked in the conversation, the candidates are put here, with
their source, what each does and the comparison the model wrote (summary, advantages, disadvantages). The person approves one
candidate, or rejects the request, from Settings or from the Chat; only then does the installer run. The file is shared by the
agent and the server, so a request made in one is seen in the other.

A request stores the fields of each candidate, not a command: on approval they are rebuilt into a candidate and checked against
the same strict patterns as an install straight after a search.
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness.discovery.candidates import Candidate, CandidateStore
from harness.discovery.installer import InstallError

FILE = "install_requests.json"
LIMIT = 40
TEXT_MAX = 600
SPEC_FIELDS = ("kind", "name", "source", "route", "title", "description", "version", "package", "url", "transport", "repo", "path", "commit",
               "integrity", "digests", "bins", "args", "env")
STATUSES = ("pending", "installed", "rejected", "failed")
_lock = threading.Lock()


class UnknownRequest(KeyError):
    pass


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())[:TEXT_MAX]


def candidate_spec(c: Candidate) -> Dict[str, Any]:
    return {k: getattr(c, k) for k in SPEC_FIELDS}


class InstallRequests:
    def __init__(self, root: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.root = Path(root)
        self.path = self.root / FILE
        self.now = now

    def _load(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = data.get("items") if isinstance(data, dict) else None
        return [r for r in items if isinstance(r, dict) and isinstance(r.get("id"), str)] if isinstance(items, list) else []

    def _write(self, items: List[Dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": items[-LIMIT:]}, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, need: str, candidates: List[Candidate], min_score: int = 0, notes: Optional[List[str]] = None) -> Dict[str, Any]:
        entries = [{"card": c.card(min_score), "spec": candidate_spec(c), "summary": "", "pros": "", "cons": ""} for c in candidates]
        record = {"id": "r" + secrets.token_hex(3), "need": _text(need), "created": self.now().isoformat(timespec="seconds"), "status": "pending",
                  "candidates": entries, "notes": [_text(n) for n in (notes or [])][:10], "detail": ""}
        with _lock:
            items = self._load()
            items.append(record)
            self._write(items)
        return record

    def get(self, request_id: str) -> Dict[str, Any]:
        for r in self._load():
            if r["id"] == str(request_id):
                return r
        raise UnknownRequest(str(request_id))

    def listing(self) -> List[Dict[str, Any]]:
        """Newest first; the pending ones are what the person has to look at."""
        return list(reversed(self._load()))

    def pending(self) -> List[Dict[str, Any]]:
        return [r for r in self.listing() if r["status"] == "pending"]

    def _update(self, request_id: str, change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        with _lock:
            items = self._load()
            for r in items:
                if r["id"] == str(request_id):
                    change(r)
                    self._write(items)
                    return r
        raise UnknownRequest(str(request_id))

    def explain(self, request_id: str, comparison: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Stores the model's comparison: a list of {n (1-based), summary, pros, cons}. Plain text only; the person reads it, nothing runs it."""
        def apply(r: Dict[str, Any]) -> None:
            for row in comparison if isinstance(comparison, list) else []:
                n = row.get("n") if isinstance(row, dict) else None
                if isinstance(n, int) and 1 <= n <= len(r["candidates"]):
                    cand = r["candidates"][n - 1]
                    for key in ("summary", "pros", "cons"):
                        cand[key] = _text(row.get(key))
        return self._update(request_id, apply)

    def reject(self, request_id: str) -> Dict[str, Any]:
        def apply(r: Dict[str, Any]) -> None:
            if r["status"] == "pending":
                r["status"], r["detail"] = "rejected", "The person rejected it."
        return self._update(request_id, apply)

    def approve(self, request_id: str, number: int, installer_for: Callable[[CandidateStore], Any]) -> Dict[str, Any]:
        """Installs candidate `number` (1-based) of a pending request. `installer_for(store)` builds the installer that reads the candidate from `store`.
        Returns the request; a failed install leaves it marked failed with the reason."""
        request = self.get(request_id)
        if request["status"] != "pending":
            raise InstallError(f"This request is already {request['status']}.")
        entries = request["candidates"]
        if not isinstance(number, int) or not 1 <= number <= len(entries):
            raise InstallError("Choose one of the candidates of the request.")
        try:
            candidate = Candidate(**entries[number - 1]["spec"])
        except TypeError:
            raise InstallError("The request is damaged: search again.") from None
        problem = candidate.check()
        if problem:
            raise InstallError(f"It cannot be installed: {problem}.")
        store = CandidateStore()
        store.add(candidate)
        installer = installer_for(store)
        missing = installer.missing(candidate)
        if missing:
            raise InstallError(missing[0].upper() + missing[1:])
        try:
            record = installer.install(candidate.id)
        except InstallError as exc:
            self._update(request_id, lambda r: r.update(status="failed", detail=str(exc)[:TEXT_MAX]))
            raise
        return self._update(request_id, lambda r: r.update(status="installed", detail=f"Installed {record['id']}.", installed=record["id"]))
