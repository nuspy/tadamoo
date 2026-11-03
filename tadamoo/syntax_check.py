"""A syntax check of a file's new text, run after an edit or a write.

Python, JSON, TOML (when `tomllib` or `tomli` exists) and YAML (when PyYAML exists) are read in process; JavaScript only when `node` is
on the PATH. A format that cannot be checked here is reported as fine. `regression` tells of a file that parsed before and no longer does.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tempfile
import warnings
from pathlib import Path
from typing import Callable, Dict, Optional

MAX_CHECKED_BYTES = 2_000_000
NODE_TIMEOUT_S = 10.0
GUARD_LEVELS = ("off", "warn", "block")
DEFAULT_GUARD = "warn"


def _python(text: str) -> Optional[str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(text)
    except SyntaxError as exc:
        where = f"line {exc.lineno}" + (f", column {exc.offset}" if exc.offset else "") if exc.lineno else "unknown position"
        return f"{where}: {exc.msg}"
    except ValueError as exc:
        return str(exc)
    return None


def _json(text: str) -> Optional[str]:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
    return None


def _toml(text: str) -> Optional[str]:
    try:
        import tomllib as parser
    except ImportError:
        try:
            import tomli as parser                    # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        parser.loads(text)
    except Exception as exc:                          # the parser's own error type differs between the two modules
        return str(exc)
    return None


def _yaml(text: str) -> Optional[str]:
    try:
        import yaml
    except ImportError:
        return None
    try:
        for _ in yaml.compose_all(text):             # composing reads the syntax without building objects, so custom tags are fine
            pass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        what = getattr(exc, "problem", None) or str(exc)
        return f"line {mark.line + 1}, column {mark.column + 1}: {what}" if mark is not None else str(what)
    return None


def _node(text: str, suffix: str = ".js") -> Optional[str]:
    node = shutil.which("node")
    if not node:
        return None
    with tempfile.TemporaryDirectory(prefix="tadamoo-syntax-") as folder:
        target = Path(folder) / ("check" + suffix)
        target.write_text(text, encoding="utf-8")
        try:
            done = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, timeout=NODE_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return None
    if done.returncode == 0:
        return None
    lines = (done.stderr or "").splitlines()
    where = next((m.group(1) for m in (re.search(r":(\d+)$", ln) for ln in lines[:3]) if m), "")
    caret = next((ln for ln in lines if ln.strip().startswith("^")), "")
    message = next((ln for ln in lines if "Error" in ln and not ln.startswith(("file:", "node:"))), "SyntaxError")
    return (f"line {where}" + (f", column {len(caret) - len(caret.lstrip()) + 1}" if caret else "") + ": " if where else "") + message.strip()


_CHECKERS: Dict[str, Callable[[str], Optional[str]]] = {
    ".py": _python, ".pyw": _python, ".pyi": _python, ".json": _json, ".toml": _toml, ".yaml": _yaml, ".yml": _yaml,
    ".js": _node, ".mjs": _node, ".cjs": _node,
}


def check(path: str, text: str) -> Optional[str]:
    """None when `text` parses as the file type of `path` (or the type is not checked); else the error with its line and column."""
    suffix = Path(path).suffix.lower()
    checker = _CHECKERS.get(suffix)
    if checker is None or len(text) > MAX_CHECKED_BYTES:
        return None
    return _node(text, suffix) if checker is _node else checker(text)


def regression(path: str, old: Optional[str], new: str) -> Optional[str]:
    """The syntax error of `new` when the file parsed before (`old` is None for a new file) and no longer does."""
    error = check(path, new)
    if error is None or (old is not None and check(path, old) is not None):
        return None
    return error
