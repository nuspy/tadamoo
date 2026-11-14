"""Small file helpers of the server."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict


def write_json(path: Path, data: Dict[str, Any]) -> None:
    """Writes a JSON file so that a reader sees the old one or the new one, never part of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    finally:
        if Path(tmp).exists():
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass
