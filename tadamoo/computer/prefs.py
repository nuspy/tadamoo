"""What the person chose about computer control (computer.json in the user's data folder): whether the agent has it, and which screen it works on."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class ComputerPrefs:
    """`enabled` (off until the person turns it on) and `monitor` (0: all screens, 1, 2... one screen, so the agent can work on a second screen while the person
    works on the first). The conversations read the file at each call, so a change of screen applies at once."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        monitor = data.get("monitor")
        return {"enabled": data.get("enabled") is True, "monitor": monitor if isinstance(monitor, int) and not isinstance(monitor, bool) and 0 <= monitor <= 16 else 0}

    def write(self, enabled: bool, monitor: int = 0) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"enabled": bool(enabled), "monitor": monitor if isinstance(monitor, int) and not isinstance(monitor, bool) and 0 <= monitor <= 16 else 0}),
                             encoding="utf-8")
        return self.read()

    def monitor(self) -> int:
        return self.read()["monitor"]
