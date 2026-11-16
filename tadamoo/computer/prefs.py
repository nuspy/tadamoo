"""What the person chose about computer control (computer.json in the user's data folder): whether the agent has it, and which screen it works on."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from tadamoo.computer.backend import BACKEND_CHOICES


class ComputerPrefs:
    """`enabled` (on unless the person turns it off: the permission levels decide what runs), `backend` (auto: agent-desktop when it is installed, else this system's own; or one by name) and `monitor` (0: all screens, 1, 2... one screen, so the agent can work on a second screen while the person
    works on the first). The conversations read the file at each call, so a change of screen applies at once."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        monitor = data.get("monitor")
        backend = data.get("backend")
        return {"enabled": data.get("enabled") is not False, "monitor": monitor if isinstance(monitor, int) and not isinstance(monitor, bool) and 0 <= monitor <= 16 else 0,
                "backend": backend if backend in BACKEND_CHOICES else "auto"}

    def write(self, enabled: bool, monitor: int = 0, backend: str = "auto") -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"enabled": bool(enabled), "monitor": monitor if isinstance(monitor, int) and not isinstance(monitor, bool) and 0 <= monitor <= 16 else 0,
                                         "backend": backend if backend in BACKEND_CHOICES else "auto"}), encoding="utf-8")
        return self.read()

    def monitor(self) -> int:
        return self.read()["monitor"]
