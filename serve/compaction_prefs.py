"""What the person chose about summarising the older steps of a long piece of work: on or off, and the share of the prompt budget at which it
starts (harness/compaction.py). Kept in one small file; the conversations are started with the matching options."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from harness.compaction import DEFAULT_THRESHOLD, MAX_THRESHOLD, MIN_THRESHOLD


class CompactionError(ValueError):
    """What is wrong with a value, in words for the person; `field` names it."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


def _share(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and MIN_THRESHOLD <= value <= MAX_THRESHOLD


class CompactionPrefs:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        return {"compaction": data.get("compaction") is not False,
                "compaction_threshold": float(data["compaction_threshold"]) if _share(data.get("compaction_threshold")) else DEFAULT_THRESHOLD}

    def write(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Saves the values given and keeps the others; a wrong value is refused whole, with the reason."""
        now = self.read()
        if "compaction" in changes:
            if not isinstance(changes["compaction"], bool):
                raise CompactionError("This is on or off.", "compaction")
            now["compaction"] = changes["compaction"]
        if "compaction_threshold" in changes:
            if not _share(changes["compaction_threshold"]):
                raise CompactionError(f"This is a share of the budget from {MIN_THRESHOLD} to {MAX_THRESHOLD}.", "compaction_threshold")
            now["compaction_threshold"] = float(changes["compaction_threshold"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(now, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        return self.read()

    def view(self) -> Dict[str, Any]:
        return {**self.read(), "defaults": {"compaction": True, "compaction_threshold": DEFAULT_THRESHOLD},
                "limits": {"compaction_threshold": [MIN_THRESHOLD, MAX_THRESHOLD]}}

    def flags(self) -> List[str]:
        """The options a conversation's process is started with."""
        v = self.read()
        return ["--compaction-threshold", str(v["compaction_threshold"])] + ([] if v["compaction"] else ["--no-compaction"])
