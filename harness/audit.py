"""Append-only audit ledger with SHA-256 hash chaining.

The chain makes edits, deletions and reordering detectable; it cannot stop someone who rewrites the
whole file. A small head file next to the ledger (count and last hash) also exposes a truncated tail.
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
        with open(self.path, "r", encoding="utf-8") as f:
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

    def verify_integrity(self) -> Tuple[bool, Optional[str]]:
        """Checks the whole chain. Returns (is_valid, error_reason)."""
        expected_prev = GENESIS
        for i, rec in enumerate(self.records):
            if "_corrupt" in rec:
                return False, f"Unreadable record at position {i}"
            if any(k not in rec for k in (*FIELDS, "hash")):
                return False, f"Incomplete record at position {i}"
            if rec["index"] != i:
                return False, f"Broken sequence index at position {i}: found {rec['index']}"
            if rec["prev_hash"] != expected_prev:
                return False, f"Hash chain broken at index {i}: expected prev_hash '{expected_prev}', got '{rec['prev_hash']}'"
            if self._calculate_hash(rec) != rec["hash"]:
                return False, f"Tampered record at index {i}: hash mismatch"
            expected_prev = rec["hash"]

        if self.head_path.is_file():
            try:
                head = json.loads(self.head_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False, "The ledger head file is unreadable"
            if head.get("count") != len(self.records) or head.get("hash") != expected_prev:
                return False, "The ledger was truncated or replaced: it does not match its head file"
        return True, None
