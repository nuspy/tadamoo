"""What the person chose about the questions and suggestions of the Chat and the Agent: how a request is looked at before the model works
(off, rules or full), whether next-prompt chips are offered, whether each message is first rewritten by the technical session (Optimize prompt),
and the size of conversation up to which the extra model calls are made.
Kept in one small file; the conversations are started with the matching options."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from tadamoo.intake import DEFAULT_MODE, DEFAULT_SIDE_CALL_TOKENS, MODES

LIMITS = {"side_call_tokens": (500, 200_000)}


class IntakeError(ValueError):
    """What is wrong with a value, in words for the person; `field` names it."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class IntakePrefs:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        lo, hi = LIMITS["side_call_tokens"]
        tokens = data.get("side_call_tokens")
        ok = isinstance(tokens, int) and not isinstance(tokens, bool) and lo <= tokens <= hi
        return {"intake": data.get("intake") if data.get("intake") in MODES else DEFAULT_MODE,
                "suggestions": data.get("suggestions") is not False,
                "optimize_prompt": data.get("optimize_prompt") is True,
                "empty_retry": data.get("empty_retry") is not False,
                "side_call_tokens": tokens if ok else DEFAULT_SIDE_CALL_TOKENS}

    def write(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Saves the values given and keeps the others; a wrong value is refused whole, with the reason."""
        now = self.read()
        if "intake" in changes:
            if changes["intake"] not in MODES:
                raise IntakeError("Choose off, rules or full.", "intake")
            now["intake"] = changes["intake"]
        if "suggestions" in changes:
            if not isinstance(changes["suggestions"], bool):
                raise IntakeError("This is on or off.", "suggestions")
            now["suggestions"] = changes["suggestions"]
        if "optimize_prompt" in changes:
            if not isinstance(changes["optimize_prompt"], bool):
                raise IntakeError("This is on or off.", "optimize_prompt")
            now["optimize_prompt"] = changes["optimize_prompt"]
        if "empty_retry" in changes:
            if not isinstance(changes["empty_retry"], bool):
                raise IntakeError("This is on or off.", "empty_retry")
            now["empty_retry"] = changes["empty_retry"]
        if "side_call_tokens" in changes:
            value, (lo, hi) = changes["side_call_tokens"], LIMITS["side_call_tokens"]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise IntakeError(f"This is a whole number from {lo} to {hi}.", "side_call_tokens")
            now["side_call_tokens"] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(now, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        return self.read()

    def view(self) -> Dict[str, Any]:
        return {**self.read(), "modes": list(MODES), "defaults": {"intake": DEFAULT_MODE, "suggestions": True, "optimize_prompt": False, "empty_retry": True, "side_call_tokens": DEFAULT_SIDE_CALL_TOKENS},
                "limits": {k: list(v) for k, v in LIMITS.items()}}

    def flags(self) -> List[str]:
        """The options a conversation's process is started with."""
        v = self.read()
        return ["--intake", v["intake"], "--side-call-tokens", str(v["side_call_tokens"]), "--suggestions", "on" if v["suggestions"] else "off",
                "--optimize-prompt", "on" if v["optimize_prompt"] else "off", "--empty-retry", "on" if v["empty_retry"] else "off"]
