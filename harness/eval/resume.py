"""Resuming an interrupted run: every finished trial is written as one JSON line in the run's folder, and a later run with the same fingerprint reuses
the valid ones. A different fingerprint refuses to reuse anything and says which part differs, so results of two setups are never mixed. A trial that
was invalid (the server or the connection failed) is redone."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

FILE = "trials.jsonl"
TREE_PATHS = ("harness", "serve", "src")            # the code a result depends on
SHOWN_CHARS = 80

Key = Tuple[Any, ...]


class ResumeMismatch(Exception):
    """The folder holds a run made with another fingerprint; `parts` says which parts differ."""

    def __init__(self, folder: Path, parts: List[str]) -> None:
        self.parts = parts
        super().__init__(f"{folder} holds a run with another fingerprint, so none of its trials can be reused; differs in: " + "; ".join(parts))


def git_state(root: Path) -> Dict[str, Any]:
    """The commit, whether harness/ serve/ src/ have uncommitted changes, and a digest of those changes (so an edit made between two runs shows)."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace").stdout

    try:
        commit = git("rev-parse", "HEAD").strip() or "unknown"
        status = git("status", "--porcelain", "--", *TREE_PATHS)
        diff = git("diff", "HEAD", "--", *TREE_PATHS) if status.strip() else ""
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "dirty": None, "changes": ""}
    return {"commit": commit, "dirty": bool(status.strip()), "changes": hashlib.sha1((status + diff).encode("utf-8", "replace")).hexdigest()[:12] if status.strip() else ""}


def model_file_state(path: Optional[str]) -> Dict[str, Any]:
    """The model file's name and size; none when no file was named."""
    if not path:
        return {}
    p = Path(path)
    try:
        return {"name": p.name, "size": p.stat().st_size}
    except OSError:
        return {"name": p.name, "size": None}


def make_fingerprint(*, experiment: str, k: int, arms: Sequence[Mapping[str, Any]], tasks: Sequence[str], sampling: Mapping[str, Any],
                     environment: Mapping[str, Any], model_file: Optional[str] = None, root: Optional[Path] = None) -> Dict[str, Any]:
    """Everything a result depends on. `sampling` is the sampling and server settings the run records, `environment` what it says about the model and hardware."""
    repo = root or Path(__file__).resolve().parents[2]
    raw = {"experiment": experiment, "k": k, "git": git_state(repo), "model_file": model_file_state(model_file), "sampling": dict(sampling),
           "environment": dict(environment), "arms": [dict(a) for a in arms], "tasks": sorted(tasks)}
    return json.loads(json.dumps(raw, sort_keys=True, default=str))            # plain JSON values, so a stored fingerprint compares equal to a fresh one


def _flat(value: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k in sorted(value):
            out.update(_flat(value[k], f"{prefix}.{k}" if prefix else str(k)))
        return out or {prefix: value}
    return {prefix: value}


def _shown(v: Any) -> str:
    text = "(absent)" if v is None else json.dumps(v, ensure_ascii=False, default=str)
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS] + "..."


def differences(stored: Mapping[str, Any], now: Mapping[str, Any]) -> List[str]:
    """The parts of two fingerprints that differ, as `part: stored -> now` (nested settings by dotted name); empty when they are the same."""
    a, b = _flat(stored), _flat(now)
    return [f"{name}: {_shown(a.get(name))} -> {_shown(b.get(name))}" for name in sorted(set(a) | set(b)) if a.get(name) != b.get(name)]


class Journal:
    """The trials.jsonl of a run folder: a first line with the fingerprint, then one line per finished trial, written and flushed as it finishes."""

    def __init__(self, folder: Path, fingerprint: Mapping[str, Any]) -> None:
        self.folder = Path(folder)
        self.path = self.folder / FILE
        self.fingerprint = dict(fingerprint)
        self.records: Dict[Key, Dict[str, Any]] = {}              # the latest line of each trial: {"key", "valid", "trial"}
        self.reused = 0
        self.folder.mkdir(parents=True, exist_ok=True)
        stored = self._read()
        if stored is None:
            self.path.write_text(json.dumps({"fingerprint": self.fingerprint}, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            parts = differences(stored, self.fingerprint)
            if parts:
                self.records = {}
                raise ResumeMismatch(self.folder, parts)

    def _read(self) -> Optional[Dict[str, Any]]:
        """The stored fingerprint, with the finished trials loaded. A torn last line (a crash in the middle of a write) is dropped from the file; a
        damaged line anywhere else is an error. None when there is no usable header, so the run starts afresh."""
        if not self.path.is_file():
            return None
        lines = self.path.read_bytes().split(b"\n")
        torn = lines.pop()                                              # after the last newline: empty, or a line the crash cut short
        stored: Optional[Dict[str, Any]] = None
        for i, line in enumerate(lines):
            try:
                row = json.loads(line.decode("utf-8"))
                if not isinstance(row, dict):
                    raise ValueError("not an object")
            except ValueError:
                raise ValueError(f"{self.path}: line {i + 1} is damaged; the journal cannot be trusted") from None
            if i == 0:
                if "fingerprint" not in row:
                    raise ValueError(f"{self.path}: no fingerprint on the first line")
                stored = row["fingerprint"]
            else:
                self.records[tuple(row["key"])] = row
        if stored is None:
            return None
        if torn:
            with open(self.path, "r+b") as f:
                f.truncate(sum(len(line) + 1 for line in lines))
        return stored

    def get(self, key: Key) -> Optional[Dict[str, Any]]:
        """The stored trial for a key when it finished validly; None when there is none or it must be redone."""
        row = self.records.get(tuple(key))
        if row is not None and row.get("valid"):
            self.reused += 1
            return row["trial"]
        return None

    def add(self, key: Key, trial: Mapping[str, Any], valid: bool) -> None:
        row = {"key": list(key), "valid": bool(valid), "trial": dict(trial)}
        self.records[tuple(key)] = row
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
