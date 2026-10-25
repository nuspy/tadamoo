"""run_code: a snippet of Python, JavaScript or TypeScript run in a separate process.

Each run gets its own process in a job object (memory and process caps, the whole tree killed at the end, on a timeout
and on a cancel), a fresh working folder inside the workspace, an environment without the PC's secrets, and a cap on
the output it returns. This is containment of resources, not an isolation boundary: the code still runs with the
user's file and network rights (deno, when it is used, narrows files to the working folder and the workspace for
reading and the working folder for writing, and has no network). The tool is therefore an irreversible action for the
permission layer, and every decision to run belongs to it.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.sandbox import DEFAULT_MAX_PROCESSES, MAX_TIMEOUT, _Capped, kill_tree, safe_environment
from harness.sandbox.jobs import CREATE_SUSPENDED, CommandJob
from harness.tools.registry import EffectClass

RUN_DIR = Path(".basalt") / "run"
DEFAULT_MAX_OUTPUT = 50_000
DEFAULT_MAX_MEMORY = 2 << 30
KEEP_DAYS = 7
MAX_LISTED_FILES = 20
CODE_MAX_CHARS = 200_000
SCRIPT_NAMES = {"python": "main.py", "javascript": "main.js", "typescript": "main.ts"}


def find_runtimes(python: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """language -> {"kind": ..., "exe": ...}. Python is always there; deno gives all three, node gives JavaScript."""
    exe = python or (shutil.which("python") if getattr(sys, "frozen", False) else sys.executable) or sys.executable
    found: Dict[str, Dict[str, str]] = {"python": {"kind": "python", "exe": exe}}
    deno, node = shutil.which("deno"), shutil.which("node")
    if deno:
        found["javascript"] = found["typescript"] = {"kind": "deno", "exe": deno}
    elif node:
        found["javascript"] = {"kind": "node", "exe": node}
    return found


def build_command(runtime: Dict[str, str], script: Path, workdir: Path, workspace: Path) -> List[str]:
    kind, exe = runtime["kind"], runtime["exe"]
    if kind == "python":
        return [exe, "-I", "-X", "utf8", str(script)]               # -I: no PYTHON* variables, no user site, no script folder on the path
    if kind == "deno":
        return [exe, "run", "--quiet", "--no-prompt", "--no-config", "--no-lock", f"--allow-read={workdir},{workspace}",
                f"--allow-write={workdir}", "--allow-env=BASALT_WORKSPACE", str(script)]
    return [exe, "--no-warnings", str(script)]


def _prune(base: Path) -> None:
    """Working folders that were kept for their files are removed after a while."""
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        for child in base.iterdir():
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass


def _files_left(workdir: Path, script: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for folder, _, names in os.walk(workdir):
        for name in names:
            full = Path(folder) / name
            if full == script:
                continue
            try:
                out.append({"path": full.relative_to(workdir).as_posix(), "bytes": full.stat().st_size})
            except OSError:
                continue
    return sorted(out, key=lambda f: f["path"])[:MAX_LISTED_FILES]


def run_code(language: str, code: str, workspace: str | Path, runtimes: Dict[str, Dict[str, str]], timeout: float = 30.0,
             max_output_bytes: int = DEFAULT_MAX_OUTPUT, max_memory_bytes: Optional[int] = DEFAULT_MAX_MEMORY,
             max_processes: Optional[int] = DEFAULT_MAX_PROCESSES, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"exit_code": -1, "stdout": "", "stderr": "", "success": False, "timed_out": False, "cancelled": False,
                              "truncated": False, "language": language, "seconds": 0.0, "folder": "", "files": []}
    runtime = runtimes.get(language)
    if runtime is None:
        return {**result, "stderr": f"No runtime for '{language}' on this PC. Available: {', '.join(sorted(runtimes))}."}
    if not isinstance(code, str) or not code.strip():
        return {**result, "stderr": "Give the code to run."}
    if len(code) > CODE_MAX_CHARS:
        return {**result, "stderr": f"The code is too long ({len(code)} characters; at most {CODE_MAX_CHARS}). Put it in a file instead."}

    workspace = Path(workspace).resolve()
    base = workspace / RUN_DIR
    base.mkdir(parents=True, exist_ok=True)
    _prune(base)
    workdir = base / secrets.token_hex(4)
    workdir.mkdir()
    script = workdir / SCRIPT_NAMES[language]
    script.write_text(code, encoding="utf-8", newline="\n")
    result["folder"] = (RUN_DIR / workdir.name).as_posix()

    limit = min(max(float(timeout), 1.0), MAX_TIMEOUT)
    popen_args: Dict[str, Any] = {}
    job = CommandJob(max_memory_bytes, max_processes)
    if os.name == "nt":
        popen_args["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                                       | (CREATE_SUSPENDED if job.active else 0))
    else:
        popen_args["start_new_session"] = True
    env = safe_environment({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "BASALT_WORKSPACE": str(workspace)})
    started = time.monotonic()
    try:
        proc = subprocess.Popen(build_command(runtime, script, workdir, workspace), cwd=str(workdir), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_args)
    except Exception as exc:
        job.close()
        shutil.rmtree(workdir, ignore_errors=True)
        return {**result, "folder": "", "stderr": f"The runtime could not be started: {exc}"}
    if job.active:
        job.attach(proc)
    out, err = _Capped(proc.stdout, max_output_bytes), _Capped(proc.stderr, max_output_bytes)
    out.start()
    err.start()
    deadline = started + limit
    while True:
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            result["cancelled"] = True
        elif time.monotonic() >= deadline:
            result["timed_out"] = True
        else:
            continue
        job.kill()
        kill_tree(proc)
        proc.wait()
        break
    job.close()                                       # whatever the code left running ends here
    out.join(5)
    err.join(5)
    stderr = err.data.decode("utf-8", errors="replace")
    if result["timed_out"]:
        stderr = (stderr + f"\nThe code was stopped after {limit:g} seconds").strip()
    if result["cancelled"]:
        stderr = (stderr + "\nThe code was stopped because the run was cancelled").strip()
    code_out = -1 if result["timed_out"] or result["cancelled"] else proc.returncode
    files = _files_left(workdir, script)
    if files:
        script.unlink(missing_ok=True)
    else:
        shutil.rmtree(workdir, ignore_errors=True)
        result["folder"] = ""
    return {**result, "exit_code": code_out, "stdout": out.data.decode("utf-8", errors="replace"), "stderr": stderr,
            "success": code_out == 0, "truncated": out.truncated or err.truncated, "seconds": round(time.monotonic() - started, 2),
            "files": files}


def register_run_code(registry: Any, workspace: str | Path, sandbox: Any = None, python: Optional[str] = None,
                      cancel: Optional[threading.Event] = None) -> List[str]:
    """`run_code` in the tools; returns the languages it offers."""
    runtimes = find_runtimes(python)
    languages = sorted(runtimes)

    def handler(language: str, code: str, timeout: float = 30.0) -> Dict[str, Any]:
        def go() -> Dict[str, Any]:
            return run_code(language, code, workspace, runtimes, timeout=timeout, cancel=cancel)
        if sandbox is None:
            return go()
        with sandbox.watching():                       # code that edits workspace files is reviewed like the file tools
            return go()

    registry.register(
        name="run_code",
        description=("Runs a snippet of code in a separate process and returns its output. Languages: " + ", ".join(languages) + ". "
                     "The working folder is new for each run; the workspace is in the environment variable BASALT_WORKSPACE. "
                     "Python runs in isolated mode: add the workspace to sys.path to import its modules. Files the code leaves in "
                     "its working folder are listed in the result. The code can reach whatever the user can: it is not confined to the workspace."),
        parameters={
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": languages, "description": "Language of the snippet"},
                "code": {"type": "string", "description": "The code to run"},
                "timeout": {"type": "number", "description": "Timeout in seconds (default 30)", "default": 30.0},
            },
            "required": ["language", "code"],
        },
        effect=EffectClass.IRREVERSIBLE,
        handler=handler,
    )
    return languages
