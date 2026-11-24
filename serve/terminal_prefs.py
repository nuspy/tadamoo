"""What the person chose for the command line of API-CLI (the prefix that sends a line to the harness, the colour of the agent's answer, the server it talks to, the level
it may ask for). Kept in `terminal.json` in the user's data folder, which the PowerShell module reads itself; the key is written but never sent back."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

from tadamoo.stack import PERMISSION_MODES

DEFAULTS = {"prefix": "§", "color": "#2E7BFF", "server": "http://127.0.0.1:7232", "mode": ""}
PREFIX_CHARS = 3
KEY_CHARS = 200


class TerminalError(ValueError):
    """What is wrong with a value, in words for the person; `field` names it."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class TerminalPrefs:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _stored(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def read(self) -> Dict[str, Any]:
        data = self._stored()
        out = dict(DEFAULTS)
        for key in ("prefix", "color", "server"):
            if isinstance(data.get(key), str) and data[key]:
                out[key] = data[key]
        out["mode"] = data["mode"] if isinstance(data.get("mode"), str) and data["mode"] in PERMISSION_MODES else ""
        return out

    def write(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Saves the values given and keeps the others; a wrong value is refused whole, with the reason."""
        data = self._stored()
        if "prefix" in changes:
            value = changes["prefix"]
            if not isinstance(value, str) or not 1 <= len(value) <= PREFIX_CHARS or any(c.isspace() or c.isalnum() for c in value):
                raise TerminalError(f"One to {PREFIX_CHARS} signs that are not letters, digits or spaces.", "prefix")
            data["prefix"] = value
        if "color" in changes:
            if not isinstance(changes["color"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", changes["color"]):
                raise TerminalError("A colour as #RRGGBB.", "color")
            data["color"] = changes["color"].upper()
        if "server" in changes:
            value = changes["server"]
            parts = urlsplit(value) if isinstance(value, str) else None
            if parts is None or parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.path.strip("/") or parts.query:
                raise TerminalError("An address like http://127.0.0.1:7232 (http or https, no user name, no path).", "server")
            data["server"] = value.rstrip("/")
        if "mode" in changes:
            if changes["mode"] not in ("", *PERMISSION_MODES):
                raise TerminalError("Empty, or one of: " + ", ".join(PERMISSION_MODES) + ".", "mode")
            data["mode"] = changes["mode"]
        if "key" in changes:
            value = changes["key"]
            if not isinstance(value, str) or len(value) > KEY_CHARS or any(c.isspace() for c in value):
                raise TerminalError(f"A key of at most {KEY_CHARS} characters with no spaces; empty takes it away.", "key")
            data["key"] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        return self.read()

    def view(self) -> Dict[str, Any]:
        """The values and the facts the page shows beside them; the key is never in it."""
        return {**self.read(), "has_key": bool(self._stored().get("key")), "modes": list(PERMISSION_MODES), "defaults": dict(DEFAULTS),
                "path": str(self.path)}
