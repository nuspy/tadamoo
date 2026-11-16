"""A record of every model call of the agent loop, for measuring how the context and the server's cache behave (experiment: ramo exp/smart-context). Off unless `TADAMOO_STEP_LOG` names a folder.

`usage.jsonl`  one line per model call: the tokens the server counted for the prompt, the ones it took from its cache (`cached_tokens`) and read again (`reread`), the tokens written (`completion_tokens`),
               the size of the reasoning and of the answer, how many messages the prompt had, how many of them were the same as in the call before (`same_prefix`) and where the first difference was
               (`changed_at`: the place where the server's cache stops being valid), and what the context did to the history (`masked`, `compacted`).
`trace.jsonl`  the content, so that the same session can be written again under another policy: the first time a message is seen (by its hash) with its place, and for every call the reasoning the model wrote.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ENV = "TADAMOO_STEP_LOG"


def _hash(message: Dict[str, Any]) -> str:
    keep = {k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]


class StepLog:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._previous: List[str] = []
        self._seen: set = set()
        self._calls = 0

    @classmethod
    def from_environment(cls) -> Optional["StepLog"]:
        folder = os.environ.get(ENV, "").strip()
        return cls(Path(folder)) if folder else None

    def _write(self, name: str, row: Dict[str, Any]) -> None:
        with (self.folder / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def record(self, messages: List[Dict[str, Any]], turn: Any, extra: Optional[Dict[str, Any]] = None) -> None:
        """One model call that has answered: `messages` is the history the call was made with, `turn` the answer (an AgentTurn)."""
        with self._lock:
            hashes = [_hash(m) for m in messages]
            same = 0
            for a, b in zip(hashes, self._previous):
                if a != b:
                    break
                same += 1
            changed_at = same if same < min(len(hashes), len(self._previous)) else None
            self._calls += 1
            prompt, cached = int(turn.prompt_tokens or 0), int(turn.cached_tokens or 0)
            tool_chars = sum(len(str(m.get("content") or "")) for m in messages if m.get("role") == "tool")
            self._write("usage.jsonl", {
                "n": self._calls, "ts": round(time.time(), 2), "turn": turn.turn_index, "prompt_tokens": prompt, "cached_tokens": cached, "reread": prompt - cached,
                "completion_tokens": int(turn.completion_tokens or 0), "reasoning_chars": len(turn.reasoning or ""), "content_chars": len(turn.assistant_content or ""),
                "messages": len(hashes), "same_prefix": same, "changed_at": changed_at, "tool_chars": tool_chars,
                "masked": sum(1 for m in messages if m.get("masked")), "compacted": sum(1 for m in messages if m.get("compacted") or m.get("compaction") or m.get("summary")),
                "finish": turn.finish_reason, **(extra or {})})
            for i, (h, m) in enumerate(zip(hashes, messages)):
                if h not in self._seen:
                    self._seen.add(h)
                    self._write("trace.jsonl", {"kind": "message", "hash": h, "index": i, "call": self._calls, "message": {k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")}})
            self._write("trace.jsonl", {"kind": "answer", "call": self._calls, "turn": turn.turn_index, "reasoning": turn.reasoning or "", "content": turn.assistant_content or ""})
            self._previous = hashes
