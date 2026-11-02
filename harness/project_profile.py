"""The checks a project seems to have (tests, a linter, a type check, a build), read from its files without running anything.

Each is a command written out in full, the file it came from and, when the command only starts a script of the project (an npm script, a
Makefile target), the script's text. Nothing here is run or approved: the person looks at the list and accepts commands one by one
(serve/coding_prefs.py), and an acceptance holds only while the command and the script text it showed are the same, so a project file that
changes what a command does asks again.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

KINDS = ("test", "lint", "typecheck", "build")
RUNS_CHARS = 300
COMMAND_CHARS = 500
MAX_SAFE = 24
READ_BYTES = 1 << 20
_NPM_PLACEHOLDER = re.compile(r"no test specified", re.IGNORECASE)
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".tox"}


@dataclass
class Check:
    kind: str
    command: str                  # '' when the project needs a command the person must write (CMake)
    source: str                   # the file it was read from, relative to the folder
    runs: str = ""                # the script behind the command, when there is one
    hint: str = ""                # for an entry with no command: what to do

    @property
    def digest(self) -> str:
        return hashlib.sha256(f"{self.command}\x00{self.runs}".encode("utf-8")).hexdigest()[:16]

    def view(self) -> Dict[str, Any]:
        return {"kind": self.kind, "command": self.command, "source": self.source, "runs": self.runs, "hint": self.hint, "digest": self.digest}


def clean_list(raw: Any, limit: int = MAX_SAFE) -> List[str]:
    """Commands as single lines of text: trimmed, none blank, no repeats, each of a sensible length, at most `limit`."""
    items = raw.splitlines() if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
    out: List[str] = []
    for item in items:
        text = " ".join(str(item).split())
        if text and len(text) <= COMMAND_CHARS and text not in out:
            out.append(text)
    return out[:limit]


def _read(path: Path) -> str:
    try:
        return path.read_bytes()[:READ_BYTES].decode("utf-8", "replace")
    except OSError:
        return ""


def _short(text: str) -> str:
    return " ".join(text.split())[:RUNS_CHARS]


def _has_tests(root: Path) -> bool:
    for name in ("tests", "test"):
        folder = root / name
        if folder.is_dir():
            for p in folder.rglob("*.py"):
                if p.name.startswith("test_") or p.name.endswith("_test.py"):
                    return True
    return any(root.glob("test_*.py")) or any(root.glob("*_test.py"))


def _python(root: Path) -> List[Check]:
    out: List[Check] = []
    pyproject = _read(root / "pyproject.toml")
    setup_cfg = _read(root / "setup.cfg")
    tox = _read(root / "tox.ini")
    configured = ((root / "pytest.ini").is_file() or "[tool.pytest" in pyproject or "[tool:pytest]" in setup_cfg or "[pytest]" in tox)
    if configured or ((pyproject or setup_cfg) and _has_tests(root)):
        out.append(Check("test", "python -m pytest -x -q", "pytest.ini" if (root / "pytest.ini").is_file() else "pyproject.toml" if pyproject else "setup.cfg"))
    if "[tool.ruff" in pyproject or (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        out.append(Check("lint", "python -m ruff check .", "ruff.toml" if (root / "ruff.toml").is_file() else "pyproject.toml"))
    elif (root / ".flake8").is_file() or "[flake8]" in setup_cfg or "[flake8]" in tox:
        out.append(Check("lint", "python -m flake8", ".flake8" if (root / ".flake8").is_file() else "setup.cfg"))
    if "[tool.mypy" in pyproject or (root / "mypy.ini").is_file():
        out.append(Check("typecheck", "python -m mypy .", "mypy.ini" if (root / "mypy.ini").is_file() else "pyproject.toml"))
    return out


def _node(root: Path) -> List[Check]:
    text = _read(root / "package.json")
    if not text:
        return []
    try:
        scripts = (json.loads(text) or {}).get("scripts") or {}
    except ValueError:
        return []
    if not isinstance(scripts, dict):
        return []
    runner = "pnpm" if (root / "pnpm-lock.yaml").is_file() else "yarn" if (root / "yarn.lock").is_file() else "npm"
    out: List[Check] = []

    def add(kind: str, names: tuple) -> None:
        for name in names:
            body = scripts.get(name)
            if isinstance(body, str) and body.strip() and not _NPM_PLACEHOLDER.search(body):
                out.append(Check(kind, f"{runner} test" if name == "test" else f"{runner} run {name}", "package.json", _short(body)))
                return
    add("test", ("test",))
    add("lint", ("lint",))
    add("typecheck", ("typecheck", "type-check", "tsc"))
    add("build", ("build",))
    return out


def _cargo(root: Path) -> List[Check]:
    if not (root / "Cargo.toml").is_file():
        return []
    return [Check("test", "cargo test", "Cargo.toml"), Check("lint", "cargo clippy", "Cargo.toml"), Check("build", "cargo build", "Cargo.toml")]


def _go(root: Path) -> List[Check]:
    if not (root / "go.mod").is_file():
        return []
    return [Check("test", "go test ./...", "go.mod"), Check("lint", "go vet ./...", "go.mod"), Check("build", "go build ./...", "go.mod")]


def _make(root: Path) -> List[Check]:
    text = _read(root / "Makefile")
    out: List[Check] = []
    for kind, names in (("test", ("test", "check")), ("lint", ("lint",)), ("build", ("build",))):
        for name in names:
            m = re.search(rf"^{name}\s*:[^=\n]*\n((?:[ \t]+[^\n]*\n?)*)", text, re.MULTILINE)
            if m:
                out.append(Check(kind, f"make {name}", "Makefile", _short(m.group(1))))
                break
    return out


def _cmake(root: Path) -> List[Check]:
    if not (root / "CMakeLists.txt").is_file():
        return []
    return [Check("build", "", "CMakeLists.txt", hint="cmake")]


def detect(folder: Any) -> List[Check]:
    """The checks the folder's files suggest, in a stable order. Reads a few small files; runs nothing."""
    root = Path(folder)
    if not root.is_dir():
        return []
    found: List[Check] = []
    for reader in (_python, _node, _cargo, _go, _make, _cmake):
        found += reader(root)
    seen, out = set(), []
    for c in found:
        key = c.command or c.source
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def is_empty(folder: Any) -> bool:
    """A folder with nothing in it but hidden entries (a new project)."""
    root = Path(folder)
    try:
        return root.is_dir() and not any(p.name not in _SKIP_DIRS and not p.name.startswith(".") for p in root.iterdir())
    except OSError:
        return False
