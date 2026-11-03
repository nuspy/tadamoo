"""Where the add-ons shipped with the app live (Node.js, Billion Context, REA, Openweight Canary)."""
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEV = _ROOT / "dist" / "stage" / "BasaltWinGUI" / "addons"
_MAIN_DEV = Path(r"E:\Projects\basalt\dist\stage\BasaltWinGUI\addons")


def addons_dir() -> Path | None:
    """The folder with node/, billion-context/, rea-agents/, openweight-canary/, or None when not installed."""
    for cand in (os.environ.get("TADAMOO_ADDONS"), _ROOT / "addons", _DEV, _MAIN_DEV):
        if cand and (Path(cand) / "addons.json").is_file():
            return Path(cand)
    return None


def info() -> dict:
    d = addons_dir()
    if d is None:
        return {}
    try:
        return json.loads((d / "addons.json").read_text(encoding="utf-8-sig")).get("addons", {})
    except (OSError, ValueError):
        return {}


def node_exe() -> Path | None:
    d = addons_dir()
    exe = d / "node" / ("node.exe" if sys.platform == "win32" else "node") if d else None
    return exe if exe and exe.is_file() else None


def npm_entry(package: str, relative: str) -> Path | None:
    """File inside an npm add-on, e.g. npm_entry("rea-agents", "scripts/rea.mjs")."""
    d = addons_dir()
    p = d / package / "node_modules" / package / relative if d else None
    return p if p and p.is_file() else None


def canary_path() -> Path | None:
    """Folder to put on PYTHONPATH to run `python -m canary`."""
    d = addons_dir()
    p = d / "openweight-canary" if d else None
    if p and (p / "canary" / "__init__.py").is_file():
        return p
    sib = _ROOT.parent / "openweight-canary"
    return sib if (sib / "canary" / "__init__.py").is_file() else None
