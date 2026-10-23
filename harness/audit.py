"""Append-only audit ledger with cryptographic SHA-256 hash chaining."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class AuditLedger:
    def __init__(self, ledger_file: str | Path) -> None:
        self.path = Path(ledger_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if not self.path.is_file():
            return []
        records = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return records

    def _calculate_hash(self, record_data: Dict[str, Any]) -> str:
        # Create a deterministic copy without the hash field
        data_to_hash = {
            "index": record_data["index"],
            "timestamp": record_data["timestamp"],
            "actor": record_data["actor"],
            "action": record_data["action"],
            "details": record_data["details"],
            "prev_hash": record_data["prev_hash"],
        }
        canonical = json.dumps(data_to_hash, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def record(self, actor: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """Appends a cryptographically chained record."""
        prev_hash = self.records[-1]["hash"] if self.records else "0" * 64
        index = len(self.records)
        now = time.time()

        entry = {
            "index": index,
            "timestamp": now,
            "actor": actor,
            "action": action,
            "details": details,
            "prev_hash": prev_hash,
        }
        entry_hash = self._calculate_hash(entry)
        entry["hash"] = entry_hash

        self.records.append(entry)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return entry

    def verify_integrity(self) -> Tuple[bool, Optional[str]]:
        """Verifies the complete SHA-256 hash chain of the ledger.

        Returns (is_valid: bool, error_reason: Optional[str]).
        """
        expected_prev = "0" * 64
        for i, rec in enumerate(self.records):
            if rec["index"] != i:
                return False, f"Broken sequence index at position {i}: found {rec['index']}"
            if rec["prev_hash"] != expected_prev:
                return False, f"Hash chain broken at index {i}: expected prev_hash '{expected_prev}', got '{rec['prev_hash']}'"
            computed_hash = self._calculate_hash(rec)
            if computed_hash != rec["hash"]:
                return False, f"Tampered record at index {i}: hash mismatch"
            expected_prev = rec["hash"]

        return True, None
