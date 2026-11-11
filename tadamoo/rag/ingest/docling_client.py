"""Running the Docling worker (tadamoo/rag/ingest/docling_worker.py) in the Python of a Docling environment, and reading what it prints.

The environment is the one named by the setting `docling_python`; when there is none, the first Python that is found in `VENV_ROOTS` (the GPU
environment before the CPU one). Nothing is installed from here. The worker is a subprocess: a cancel closes its stdin, a worker that says
nothing for `IDLE_S` seconds or runs longer than the time given is killed, and the blocks it printed so far are never used when it did not finish."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.rag.errors import RagError

WORKER = Path(__file__).with_name("docling_worker.py")
VENV_ROOTS = tuple(p for p in (os.environ.get("TADAMOO_VENVS", ""), "E:/Dev/venvs") if p)
VENV_NAMES = ("docling-gpu", "docling")
DOCLING_FORMATS = (".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".md", ".markdown", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
WINDOW_PAGES = 40
IDLE_S = 900.0                     # a model may take minutes to load the first time
TOTAL_S = 6 * 3600.0
Progress = Callable[[Dict[str, Any]], None]


class Cancelled(RagError):
    def __init__(self) -> None:
        super().__init__("the reading was cancelled", "cancelled")


def _python_of(root: Path) -> Optional[Path]:
    for rel in ("Scripts/python.exe", "bin/python"):
        if (root / rel).is_file():
            return root / rel
    return None


def find_python(configured: str = "", roots: Sequence[str] = VENV_ROOTS) -> str:
    """The Python to run the worker with: the configured one when it is a file, else the first Docling environment under `roots`; "" when none.
    The environment variable `TADAMOO_DOCLING=off` says there is none, whatever the setting."""
    if os.environ.get("TADAMOO_DOCLING", "").lower() == "off":
        return ""
    if configured:
        return configured if Path(configured).is_file() else ""
    for root in roots:
        for name in VENV_NAMES:
            found = _python_of(Path(root) / name)
            if found is not None:
                return str(found)
    return ""


def read(python: str, path: Path, ext: str, progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None, url: str = "",
         window: int = WINDOW_PAGES, idle_s: float = IDLE_S, total_s: float = TOTAL_S, cpu: bool = False,
         command: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """`{blocks, pages, device}` for the file at `path`, read by the worker. Raises RagError with the worker's own words when it fails, `Cancelled`
    when `cancel` is set. `command` replaces the worker's command line (for tests)."""
    argv = list(command) if command else [python, "-X", "utf8", str(WORKER), "--file", str(path), "--format", ext, "--window", str(window)]
    if not command and url:
        argv += ["--url", url]
    if not command and cpu:
        argv.append("--cpu")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=flags)
    lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
    err_tail: List[bytes] = []

    def pump() -> None:
        for raw in proc.stdout:                                   # type: ignore[union-attr]
            lines.put(raw)
        lines.put(None)

    def drain() -> None:
        for raw in proc.stderr:                                   # type: ignore[union-attr]
            err_tail.append(raw)
            del err_tail[:-30]

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=drain, daemon=True).start()
    blocks: List[Dict[str, Any]] = []
    out = {"pages": 0, "device": "cpu"}
    finished = False
    started = last = time.monotonic()
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            now = time.monotonic()
            if now - last > idle_s or now - started > total_s:
                raise RagError("the reader stopped answering and was ended")
            try:
                raw = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if raw is None:
                break
            last = time.monotonic()
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("event")
            if kind == "blocks":
                blocks += [b for b in event.get("blocks", []) if isinstance(b, dict)]
            elif kind in ("start", "progress", "done"):
                out["pages"] = int(event.get("pages") or out["pages"])
                out["device"] = str(event.get("device") or out["device"])
                finished = finished or kind == "done"
                if progress is not None:
                    progress({k: event.get(k) for k in ("event", "page", "pages", "device")})
            elif kind == "error":
                raise RagError("Docling could not read the file: " + str(event.get("message") or "unknown error"))
        if not finished:
            tail = b"".join(err_tail).decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            raise RagError("the reader ended before the document was read" + (f" ({tail[0][:200]})" if tail[0] else ""))
        return {"blocks": blocks, "pages": out["pages"], "device": out["device"]}
    finally:
        try:
            proc.stdin.close()                                    # the worker takes a closed stdin as the order to stop
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
