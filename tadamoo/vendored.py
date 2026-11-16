"""Where the programs built from third_party/ are (agent-desktop for computer control, agent-browser for the browser tools).

The installers put each one in `<program folder>/tools/vendored/<name>/`. A copy in `<data>/tools/<name>/` (the person's own update, or what `python -m tadamoo.computer install`
puts there) is taken before it, and a program of that name on PATH comes last.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]


def bundled_dir(name: str, root: Optional[Path] = None) -> Path:
    return (root or ROOT) / "tools" / "vendored" / name


def exe_names(name: str) -> Tuple[str, ...]:
    return (f"{name}.exe", name) if sys.platform == "win32" else (name, f"{name}.exe")


def find(name: str, tools_dir: Optional[Path] = None, root: Optional[Path] = None) -> Tuple[str, str]:
    """(path, where) of the program: `installed` (in the data folder's tools/<name>), `bundled` (shipped with the program) or `path`; ('', '') when there is none."""
    for where, folder in (("installed", tools_dir), ("bundled", bundled_dir(name, root))):
        if folder is None:
            continue
        for exe in exe_names(name):
            candidate = Path(folder) / exe
            if candidate.is_file():
                return str(candidate), where
    found = shutil.which(name)
    return (found, "path") if found else ("", "")
