"""What the person chose, kept in the user data folder, and the two places the choice shows.

`skills/choices.json` has one entry for each skill that is installed (a skill that is not installed has no entry): its kind, where it came from (`source`:
`{type: builtin|folder|github|web, ref, ...}`), whether it is turned on, the version and the content hash it was installed with, when, and what is known about
its models. The copies themselves are the importer's; the choice only says which ones the person wants and how.

A skill is turned off in two places the harness already honours: its id in the `off` list of the capability settings (the index leaves it out of every search
and refuses to load it), and, for a skill that is also a personal skill, its folder renamed with a `.off` ending (the list of personal skills reads only
folders with a plain name). Nothing is deleted."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable

from tadamoo.importer.common import Lock, write_json

OFF_SUFFIX = ".off"


class Choices:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = data.get("skills") if isinstance(data, dict) else None
        return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(items, dict) else {}

    def save(self, items: Dict[str, Dict[str, Any]]) -> None:
        write_json(self.path, {"version": 1, "skills": items})

    def put(self, name: str, **fields: Any) -> Dict[str, Any]:
        with Lock():
            items = self.load()
            items[name] = {**items.get(name, {}), **fields}
            self.save(items)
            return items[name]

    def drop(self, name: str) -> None:
        with Lock():
            items = self.load()
            if items.pop(name, None) is not None:
                self.save(items)


def _settings_file(cap_dir: Path) -> Path:
    return Path(cap_dir) / "settings.json"


def off_ids(cap_dir: Path) -> set:
    """The ids the person turned off in the capability settings (`skill:name`, `mcp:name`)."""
    try:
        data = json.loads(_settings_file(cap_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {x for x in data.get("off", []) if isinstance(x, str)} if isinstance(data, dict) else set()


def set_off(cap_dir: Path, ids: Iterable[str], off: bool) -> None:
    """Adds the ids to the `off` list of the capability settings, or takes them out; every other setting is kept as it is."""
    ids = set(ids)
    if not ids:
        return
    path = _settings_file(cap_dir)
    with Lock():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        now = [x for x in data.get("off", []) if isinstance(x, str)]
        new = sorted(set(now) | ids) if off else [x for x in now if x not in ids]
        if new != now:
            data["off"] = new
            write_json(path, data)


def hide_personal(personal_dir: Path, slug: str, hide: bool) -> bool:
    """Renames the personal skill's folder out of (or back into) the list; False when there was nothing to rename or the target is taken."""
    plain, aside = Path(personal_dir) / slug, Path(personal_dir) / (slug + OFF_SUFFIX)
    src, dst = (plain, aside) if hide else (aside, plain)
    if not src.is_dir() or dst.exists():
        return False
    os.replace(src, dst)
    return True


def personal_hidden(personal_dir: Path, slug: str) -> bool:
    return (Path(personal_dir) / (slug + OFF_SUFFIX)).is_dir()

