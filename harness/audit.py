"""Append-only audit ledger with SHA-256 hash chaining.

The chain makes edits, deletions and reordering detectable; it cannot stop someone who rewrites the
whole file. A small head file next to the ledger (count and last hash) also exposes a truncated tail.

A tool call is recorded without its arguments in full: their SHA-256, a masked preview with every long value
cut (under `arguments`, where older rows hold the whole), the permission decision, the outcome and the time
the tool took (`AuditLedger.call_details`). Rows written before that still verify: the chain covers
whatever the details hold.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

GENESIS = "0" * 64
FIELDS = ("index", "timestamp", "actor", "action", "details", "prev_hash")
PREVIEW_VALUE_CHARS = 60          # each value of the arguments in the preview
PREVIEW_CHARS = 600               # the whole preview; past it the preview is plain text, cut


def _redact(value: Any, redactor: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, dict):
        return {k: _redact(v, redactor) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, redactor) for v in value]
    return value


class AuditLedger:
    def __init__(self, ledger_file: str | Path, redactor: Optional[Callable[[str], str]] = None) -> None:
        self.path = Path(ledger_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.head_path = self.path.with_suffix(self.path.suffix + ".head")
        self.redactor = redactor
        self._lock = threading.RLock()
        self.records: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if not self.path.is_file():
            return []
        records: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    rec = None
                # an unreadable line stays in the chain as a marker, so verification fails on it
                records.append(rec if isinstance(rec, dict) else {"_corrupt": line[:200]})
        return records

    def call_details(self, tool: str, arguments: Any, *, permission: str, outcome: str, duration_s: float,
                     effect: str = "", reason: str = "") -> Dict[str, Any]:
        """What a tool call leaves in the ledger: the tool, the SHA-256 of its arguments (as masked), a masked and shortened
        preview of them, the permission decision, how it ended (ok, error, refused or cancelled; "started" for the note made
        when it is let through) and how long it took."""
        masked = _redact(arguments, self.redactor) if self.redactor else arguments       # masked first: a cut-off secret would not be recognised
        canonical = json.dumps(masked, sort_keys=True, ensure_ascii=False, default=str)
        details: Dict[str, Any] = {
            "tool": tool,
            "args_sha256": hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest(),
            "arguments": self._preview(masked),
            "permission": permission, "outcome": outcome, "duration_ms": int(round(duration_s * 1000)),
        }
        if effect:
            details["effect"] = effect
        if reason:
            details["reason"] = reason[:300]
        return details

    @staticmethod
    def _preview(arguments: Any) -> Any:
        def short(value: Any) -> Any:
            if isinstance(value, str):
                return value if len(value) <= PREVIEW_VALUE_CHARS else value[:PREVIEW_VALUE_CHARS] + f"... [{len(value)} chars]"
            if isinstance(value, dict):
                return {k: short(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [short(v) for v in value[:8]] + (["..."] if len(value) > 8 else [])
            return value
        shown = short(arguments)
        text = json.dumps(shown, ensure_ascii=False, default=str)
        return shown if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "..."

    @staticmethod
    def _calculate_hash(record_data: Dict[str, Any]) -> str:
        canonical = json.dumps({k: record_data[k] for k in FIELDS}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def record(self, actor: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """Appends a chained record; secret values are masked by the redactor before anything is written."""
        if self.redactor:
            details = _redact(details, self.redactor)
        with self._lock:
            prev = self.records[-1].get("hash", GENESIS) if self.records else GENESIS
            entry = {
                "index": len(self.records),
                "timestamp": time.time(),
                "actor": actor,
                "action": action,
                "details": details,
                "prev_hash": prev,
            }
            entry["hash"] = self._calculate_hash(entry)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.records.append(entry)
            self._write_head(entry)
            return entry

    def _write_head(self, entry: Dict[str, Any]) -> None:
        tmp = self.head_path.with_suffix(self.head_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"count": entry["index"] + 1, "hash": entry["hash"]}), encoding="utf-8")
        os.replace(tmp, self.head_path)

    def problems(self) -> List[str]:
        """Everything wrong with the ledger, in order; an unreadable or malformed line is a problem of its own, never skipped."""
        found: List[str] = []
        expected_prev: Optional[str] = GENESIS            # None after a line that cannot be read: the next link cannot be checked
        for i, rec in enumerate(self.records):
            if "_corrupt" in rec:
                found.append(f"Unreadable record at position {i}")
                expected_prev = None
                continue
            if any(k not in rec for k in (*FIELDS, "hash")):
                found.append(f"Incomplete record at position {i}")
                expected_prev = None
                continue
            try:
                if rec["index"] != i:
                    found.append(f"Broken sequence index at position {i}: found {rec['index']}")
                if expected_prev is not None and rec["prev_hash"] != expected_prev:
                    found.append(f"Hash chain broken at index {i}: expected prev_hash '{expected_prev}', got '{rec['prev_hash']}'")
                if self._calculate_hash(rec) != rec["hash"]:
                    found.append(f"Tampered record at index {i}: hash mismatch")
            except (TypeError, ValueError, KeyError, AttributeError):
                found.append(f"Malformed record at position {i}")
            expected_prev = rec["hash"] if isinstance(rec.get("hash"), str) else None

        if self.head_path.is_file():
            try:
                head = json.loads(self.head_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                found.append("The ledger head file is unreadable")
            else:
                if not isinstance(head, dict):
                    found.append("The ledger head file is unreadable")
                elif head.get("count") != len(self.records) or (expected_prev is not None and head.get("hash") != expected_prev):
                    found.append("The ledger was truncated or replaced: it does not match its head file")
        return found

    def verify_integrity(self) -> Tuple[bool, Optional[str]]:
        """Checks the whole chain. Returns (is_valid, error_reason); the reason names the first problem and how many more there are."""
        found = self.problems()
        if not found:
            return True, None
        return False, found[0] + (f" (and {len(found) - 1} more problem(s))" if len(found) > 1 else "")
