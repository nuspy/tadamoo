"""Running a picture or video job (tadamoo/media/worker.py) as a subprocess and reading what it prints, like the Docling client does.

The worker is run by the Python that runs this code: it needs only the standard library, the model processes it starts use LTX Studio's own
environments. A cancel closes its stdin (the worker kills the model process and gives the lease back); a worker that says nothing for `idle_s`
seconds or runs longer than `total_s` is ended; the whole process tree is killed if closing stdin is not enough. The result is the file the worker
made, how long it took and the lease it held; a lease the server refused is `Refused` with the broker's reason, so the caller can decide what to do."""
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

from tadamoo.media.errors import Cancelled, MediaError, Refused
from tadamoo.media.settings import VIDEO_MODEL, ltx_dir_of

WORKER = Path(__file__).with_name("worker.py")
TOTAL_S = {"image": 3600.0, "video": 4 * 3600.0, "avatar": 4 * 3600.0}
CHECK_TTL_S = 60.0
Progress = Callable[[Dict[str, Any]], None]
_checked: Dict[Any, Any] = {}


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    from tadamoo.sandbox import kill_tree
    kill_tree(proc)


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def check(settings: Dict[str, Any], command: Optional[Sequence[str]] = None, force: bool = False) -> Dict[str, Any]:
    """Whether each kind can run: `{ltx_dir, image: {ready, missing, ...}, video: {...}}`. Asked of the worker (it reads LTX Studio's own registry), remembered for a minute."""
    folder = ltx_dir_of(settings["ltx_dir"])
    if not folder:
        why = f"the LTX Studio folder {settings['ltx_dir']} does not exist" if settings["ltx_dir"] else "LTX Studio was not found (set its folder)"
        return {"ltx_dir": "", "image": {"ready": False, "missing": [], "error": why}, "video": {"ready": False, "missing": [], "error": why}}
    model = settings["video_model"] or VIDEO_MODEL
    key = (folder, model, tuple(settings[k] for k in ("image_python", "video_python")), tuple(command or ()))
    hit = _checked.get(key)
    if hit and not force and time.monotonic() - hit[0] < CHECK_TTL_S:
        return hit[1]
    argv = list(command) if command else [sys.executable, "-X", "utf8", str(WORKER), "--check", "--ltx-dir", folder, "--video-model", model]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=60, creationflags=_flags(), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        got = json.loads(done.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
        heard = (getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or b"")
        heard = heard.decode("utf-8", "replace") if isinstance(heard, bytes) else str(heard)
        why = f"the check of LTX Studio failed ({type(exc).__name__}; {Path(argv[0]).name})" + (f": {heard.strip()[-200:]}" if heard.strip() else "")
        got = {k: {"ready": False, "missing": [], "error": why} for k in ("image", "video")}
    for kind, setting in (("image", "image_python"), ("video", "video_python")):       # a Python the person named instead of LTX Studio's
        if settings[setting] and isinstance(got.get(kind), dict):
            ok = Path(settings[setting]).is_file()
            got[kind].update(python=settings[setting], python_ok=ok, ready=bool(ok and got[kind].get("script_ok") and not got[kind].get("missing")))
    out = {"ltx_dir": folder, **got}
    _checked[key] = (time.monotonic(), out)
    return out


def forget_checks() -> None:
    _checked.clear()


def run(kind: str, request: Dict[str, Any], output: Path, settings: Dict[str, Any], url: str = "", progress: Optional[Progress] = None,
        cancel: Optional[Callable[[], bool]] = None, idle_s: Optional[float] = None, total_s: Optional[float] = None,
        command: Optional[Sequence[str]] = None, held: bool = False) -> Dict[str, Any]:
    """Makes the file `output` (its folder is the job's working folder: the worker's parameters and the model's own temporary files go there) and returns
    `{path, bytes, seconds, lease}`. Raises `Refused` (the lease), `Cancelled`, or MediaError with the reason in words. `command` replaces the worker's
    command line (for tests); it is given the job file as its last argument. `held`: the caller keeps the local model out of the way across
    several jobs (a swap of its own, docs/heavy-swap.md), so this job asks for the VRAM only."""
    folder = ltx_dir_of(settings["ltx_dir"])
    if not folder and not command:
        raise MediaError("LTX Studio was not found: set its folder in the media settings", "unavailable")
    output.parent.mkdir(parents=True, exist_ok=True)
    job_file = output.parent / "job.json"
    mib = settings[f"{kind}_mib"]
    job_file.write_text(json.dumps({"kind": kind, "ltx_dir": folder, "url": url, "mib": mib, "wait_s": settings["wait_s"], "output": str(output), "request": request, "held": held,
                                    "python": settings.get(f"{kind}_python", ""), "video_model": settings["video_model"] or VIDEO_MODEL}),
                        encoding="utf-8")
    argv = list(command) + [str(job_file)] if command else [sys.executable, "-X", "utf8", str(WORKER), "--job", str(job_file)]
    return _run(argv, kind, progress, cancel, settings["idle_s"] if idle_s is None else idle_s, TOTAL_S[kind] if total_s is None else total_s)


def _run(argv: List[str], kind: str, progress: Optional[Progress], cancel: Optional[Callable[[], bool]], idle_s: float, total_s: float) -> Dict[str, Any]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=_flags())
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
    began = last = time.monotonic()
    lease: Dict[str, Any] = {}
    done: Dict[str, Any] = {}
    refused: Optional[Refused] = None
    error = ""
    try:
        while True:
            if cancel is not None and cancel():
                raise Cancelled()
            now = time.monotonic()
            if now - began > total_s:
                raise MediaError(f"the {kind} took longer than {int(total_s // 60)} minutes and was ended", "timeout")
            if now - last > idle_s:
                raise MediaError(f"the {kind} maker said nothing for {int(idle_s // 60)} minutes and was ended", "timeout")
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
            if not isinstance(event, dict):
                continue
            kind_of = event.get("event")
            if kind_of == "lease":
                lease = {"granted_mib": int(event.get("granted_mib") or 0), "ok": bool(event.get("ok"))}
            elif kind_of == "refused":
                refused = Refused(str(event.get("reason") or "unavailable"), str(event.get("message") or ""), str(event.get("message_it") or ""))
            elif kind_of == "done":
                done = event
            elif kind_of == "error":
                error = str(event.get("message") or "unknown error")
            if progress is not None and kind_of in ("start", "lease", "progress", "log"):
                progress({k: event.get(k) for k in ("event", "step", "steps", "line", "granted_mib", "ok", "reason") if k in event})
    finally:
        try:
            proc.stdin.close()                                    # the worker takes a closed stdin as the order to stop
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait(timeout=10)
        if proc.poll() is None:
            _kill_tree(proc)
    if refused is not None:
        raise refused
    if done:
        return {"path": str(done.get("path") or ""), "bytes": int(done.get("bytes") or 0), "seconds": float(done.get("seconds") or round(time.monotonic() - began, 1)),
                "lease": {**(done.get("lease") or {}), **{k: v for k, v in lease.items() if k not in ("ok",)}}}
    if error:
        raise MediaError(error)
    tail = b"".join(err_tail).decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    raise MediaError(f"the {kind} maker ended before it made a file" + (f" ({tail[0][:200]})" if tail[0] else ""))
