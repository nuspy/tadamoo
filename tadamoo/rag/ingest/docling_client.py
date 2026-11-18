"""Running the Docling worker (tadamoo/rag/ingest/docling_worker.py) in the Python of a Docling environment, and reading what it prints.

The environment is the one named by the setting `docling_python`; when there is none, the first Python that is found in `VENV_ROOTS` (the GPU
environment before the CPU one). Nothing is installed from here. The worker is a subprocess: a cancel closes its stdin, a worker that says
nothing for `IDLE_S` seconds or runs longer than the time given is killed. One that dies part way is started again where it stopped, and what it
had finished is kept (see `read`)."""
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


def _own_venv() -> Optional[Path]:
    """Our own embeddings environment (tadamoo/rag/embed_env.py): it already has torch, transformers and docling, so it reads documents too. The same
    fallback embed_client.py uses for embeddings, so installing that one environment covers both without a second, separate install."""
    from tadamoo.rag import embed_env
    return embed_env.shared()._venv_python()


def find_python(configured: str = "", roots: Sequence[str] = VENV_ROOTS, own: Optional[Callable[[], Optional[Path]]] = _own_venv) -> str:
    """The Python to run the worker with: the configured one when it is a file, else our own embeddings environment when it is installed, else the
    first Docling environment under `roots`; "" when none. The environment variable `TADAMOO_DOCLING=off` says there is none, whatever the setting."""
    if os.environ.get("TADAMOO_DOCLING", "").lower() == "off":
        return ""
    if configured:
        return configured if Path(configured).is_file() else ""
    found_own = own() if own is not None else None
    if found_own is not None:
        return str(found_own)
    for root in roots:
        for name in VENV_NAMES:
            found = _python_of(Path(root) / name)
            if found is not None:
                return str(found)
    return ""


class _Run:
    """What one run of the worker printed: the blocks and skipped pages, how far it got, and why it stopped when it did not finish."""

    def __init__(self) -> None:
        self.blocks: List[Dict[str, Any]] = []
        self.skipped: Dict[int, str] = {}
        self.empty: Dict[int, str] = {}
        self.figures: List[Dict[str, Any]] = []
        self.pages, self.device, self.done_to = 0, "cpu", 0
        self.started = self.finished = False
        self.error = ""


def _run(argv: List[str], progress: Optional[Progress], cancel: Optional[threading.Event], idle_s: float, total_s: float, began: float) -> _Run:
    """Runs the worker once. Raises `Cancelled`, and a RagError when the whole reading has run longer than `total_s`; any other end is in `.error`."""
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
    run = _Run()
    last = time.monotonic()
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            now = time.monotonic()
            if now - began > total_s:
                raise RagError("the reading took too long and was ended")
            if now - last > idle_s:
                run.error = "the reader stopped answering and was ended"
                return run
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
                run.blocks += [b for b in event.get("blocks", []) if isinstance(b, dict)]
            elif kind == "skipped":
                run.skipped[int(event.get("page") or 0)] = str(event.get("message") or "")
            elif kind == "figures":
                run.figures += [f for f in event.get("figures", []) if isinstance(f, dict)]
            elif kind == "empty":
                run.empty[int(event.get("page") or 0)] = str(event.get("reason") or "unknown")
            elif kind in ("start", "progress", "done"):
                run.pages = int(event.get("pages") or run.pages)
                run.device = str(event.get("device") or run.device)
                run.started = run.started or kind == "start"
                run.finished = run.finished or kind == "done"
                if kind == "progress":
                    run.done_to = max(run.done_to, int(event.get("page") or 0))
                if progress is not None:
                    progress({k: event.get(k) for k in ("event", "page", "pages", "device")})
            elif kind == "error":
                run.error = "Docling could not read the file: " + str(event.get("message") or "unknown error")
        if not run.finished and not run.error:
            tail = b"".join(err_tail).decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            run.error = "the reader ended before the document was read" + (f" ({tail[0][:200]})" if tail[0] else "")
        return run
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


def read(python: str, path: Path, ext: str, progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None, url: str = "",
         window: int = WINDOW_PAGES, idle_s: float = IDLE_S, total_s: float = TOTAL_S, cpu: bool = False,
         command: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """`{blocks, pages, device, skipped, empty, figures}` for the file at `path`, read by the worker. Raises RagError with the worker's own words when it fails before it
    has started (nothing about the file is known then), `Cancelled` when `cancel` is set. `command` replaces the worker's command line (for tests).

    A worker that dies or hangs part way is started again where the work stopped and the pages already read are kept: the second try reads page by
    page, the third page by page on the CPU, and a page that still fails is skipped, with a block in its place and its number in `skipped` (page ->
    why) so the person can take care of it by hand."""
    base = list(command) if command else [python, "-X", "utf8", str(WORKER), "--file", str(path), "--format", ext, "--window", str(window)]
    if not command and url:
        base += ["--url", url]
    began = time.monotonic()
    blocks: List[Dict[str, Any]] = []
    skipped: Dict[int, str] = {}
    empty: Dict[int, str] = {}
    figures: List[Dict[str, Any]] = []
    out = {"pages": 0, "device": "cpu"}
    resume, fails, extra = 1, 0, []
    while True:
        argv = base + (["--cpu"] if cpu and not command and "--cpu" not in extra else []) + extra
        if resume > 1:
            argv += ["--from-page", str(resume)]
        if skipped:
            argv += ["--skip", ",".join(str(n) for n in sorted(skipped))]
        run = _run(argv, progress, cancel, idle_s, total_s, began)
        out["pages"] = run.pages or out["pages"]
        out["device"] = run.device if run.started else out["device"]
        skipped.update(run.skipped)
        empty.update(run.empty)
        if run.finished:
            blocks += run.blocks
            figures += run.figures
            return {"blocks": _with_gaps(blocks, skipped), "pages": out["pages"], "device": out["device"], "skipped": skipped, "empty": empty, "figures": figures}
        if not run.started or not run.pages:
            raise RagError(run.error)                              # no page was ever reached: the file or the environment is at fault, not a page
        blocks += [b for b in run.blocks if int(b.get("page") or 0) <= run.done_to]      # what the run printed past its last finished page is read again
        figures += [f for f in run.figures if int(f.get("page") or 0) <= run.done_to]
        position = max(run.done_to, resume - 1) + 1
        fails = fails + 1 if position == resume else 1
        resume = position
        if fails == 1:
            extra = ["--paged"]
        elif fails == 2:
            extra = ["--paged", "--cpu"]
        else:
            skipped[resume] = run.error
            resume, fails, extra = resume + 1, 0, ["--paged"]
        if resume > out["pages"]:
            return {"blocks": _with_gaps(blocks, skipped), "pages": out["pages"], "device": out["device"], "skipped": skipped, "empty": empty, "figures": figures}


def _with_gaps(blocks: List[Dict[str, Any]], skipped: Dict[int, str]) -> List[Dict[str, Any]]:
    """The blocks with a marker where a page is missing, so the text itself says so."""
    out = list(blocks)
    for number in sorted(skipped):
        gap = [{"type": "page_break", "text": "", "page": number},
               {"type": "paragraph", "text": f"[Page {number} of this document could not be read automatically and is missing from this text.]", "page": number}]
        at = next((i for i, b in enumerate(out) if int(b.get("page") or 0) > number), len(out))
        out[at:at] = gap
    return out
