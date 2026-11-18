"""The first-run wizard: a step offered once, the first time its own part of the app is opened, each a deliberate yes/no (nothing downloaded or installed
without the person's own answer). Today's only step is the embeddings/Docling Python environment, offered the first time the Knowledge tab opens and the
environment is not already found on this PC."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

STEPS = ("embed_env",)
CHOICES = ("installed", "skipped")


class Wizard:
    def __init__(self, file: Path) -> None:
        self.file = Path(file)

    def _read(self) -> Dict[str, str]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def seen(self, step: str) -> bool:
        if step not in STEPS:
            raise ValueError(f"no such step: {step}")
        return step in self._read()

    def answer(self, step: str, choice: str) -> None:
        if step not in STEPS:
            raise ValueError(f"no such step: {step}")
        if choice not in CHOICES:
            raise ValueError(f"choice: one of {', '.join(CHOICES)}")
        data = self._read()
        data[step] = choice
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name(self.file.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.file)
