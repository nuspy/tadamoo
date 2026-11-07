"""run_code: a snippet of Python, JavaScript or TypeScript run in a separate process.

Each run gets its own process in a job object (memory and process caps, the whole tree killed at the end, on a timeout
and on a cancel), a fresh working folder inside the workspace, an environment without the PC's secrets, and a cap on
the output it returns. This is containment of resources, not an isolation boundary: the code still runs with the
user's file and network rights, except for the person's secrets (the vault, the payment files, the browser's sign-ins, the
control token, the phone keys). Python refuses those at run time through an audit hook, also in the Python programs it
starts; deno and node narrow files to the working folder and the workspace for reading and the working folder for writing
(deno has no network, node cannot start programs). A program in another language started by Python, or code written to
defeat the hook, is not stopped. The tool is an irreversible action for the permission layer, and every decision to run
belongs to it.
"""
from __future__ import annotations

import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from tadamoo import exec_level
from tadamoo.sandbox import DEFAULT_MAX_PROCESSES, MAX_TIMEOUT, _Capped, guard, kill_tree, safe_environment
from tadamoo.sandbox.jobs import CREATE_SUSPENDED, CommandJob
from tadamoo.tools.registry import EffectClass

RUN_DIR = Path(".tadamoo") / "run"
DEFAULT_MAX_OUTPUT = 50_000
DEFAULT_MAX_MEMORY = 2 << 30
MAX_LISTED_FILES = 20
CODE_MAX_CHARS = 200_000
SCRIPT_NAMES = {"python": "main.py", "javascript": "main.js", "typescript": "main.ts"}


def _node_permission(node: str) -> str:
    """The flag that turns on node's permission model, or '' when this node has none."""
    for flag in ("--permission", "--experimental-permission"):
        try:
            if subprocess.run([node, flag, "-e", "0"], capture_output=True, timeout=15).returncode == 0:
                return flag
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def find_runtimes(python: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """language -> {"kind": ..., "exe": ...}. Python is always there; deno gives all three, node gives JavaScript."""
    exe = python or (shutil.which("python") if getattr(sys, "frozen", False) else sys.executable) or sys.executable
    found: Dict[str, Dict[str, str]] = {"python": {"kind": "python", "exe": exe}}
    deno, node = shutil.which("deno"), shutil.which("node")
    if deno:
        found["javascript"] = found["typescript"] = {"kind": "deno", "exe": deno}
    elif node:
        found["javascript"] = {"kind": "node", "exe": node, "permission": _node_permission(node)}
    return found


def build_command(runtime: Dict[str, str], script: Path, workdir: Path, workspace: Path, places: Sequence[str] = ()) -> List[str]:
    """The command for a snippet. `places` are the person's secrets: Python is started through the guard that refuses them, deno is told to
    deny them, node reads only the working folder and the workspace."""
    kind, exe = runtime["kind"], runtime["exe"]
    if kind == "python":
        # -I: no PYTHON* variables, no user site, no script folder on the path
        return [exe, "-I", "-X", "utf8", *([str(guard.LAUNCHER)] if places else []), str(script)]
    if kind == "deno":
        return [exe, "run", "--quiet", "--no-prompt", "--no-config", "--no-lock", f"--allow-read={workdir},{workspace}",
                f"--allow-write={workdir}", "--allow-env=TADAMOO_WORKSPACE", *guard.deno_flags(places), str(script)]
    if runtime.get("permission"):
        return [exe, "--no-warnings", runtime["permission"], f"--allow-fs-read={workdir}", f"--allow-fs-read={workspace}",
                f"--allow-fs-write={workdir}", str(script)]
    return [exe, "--no-warnings", str(script)]


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


def _existing_workspace_path(stderr: str, workspace: Path) -> str:
    """The note for a missing `.tadamoo/...` path that does exist under the workspace: a script starts in its own folder, not in the workspace."""
    if "No such file" not in stderr and "ENOENT" not in stderr and "cannot find" not in stderr.lower():
        return ""
    for token in re.findall(r"\.tadamoo[\\/][^\s'\"]+", stderr):
        relative = re.sub(r"[\\/]+", "/", token)
        found = workspace / relative
        if not relative.startswith(RUN_DIR.as_posix() + "/") and found.exists():
            return f"\nThat path exists at {found}. A script starts in its own new folder; the workspace is in TADAMOO_WORKSPACE."
    return ""


def run_code(language: str, code: str, workspace: str | Path, runtimes: Dict[str, Dict[str, str]], timeout: Optional[float] = None,
             max_output_bytes: int = DEFAULT_MAX_OUTPUT, max_memory_bytes: Optional[int] = DEFAULT_MAX_MEMORY,
             max_processes: Optional[int] = DEFAULT_MAX_PROCESSES, cancel: Optional[threading.Event] = None,
             broker: Any = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"exit_code": -1, "stdout": "", "stderr": "", "success": False, "timed_out": False, "cancelled": False,
                              "truncated": False, "language": language, "seconds": 0.0, "folder": "", "files": []}
    runtime = runtimes.get(language)
    if runtime is None:
        return {**result, "stderr": f"No runtime for '{language}' on this PC. Available: {', '.join(sorted(runtimes))}."}
    if not isinstance(code, str) or not code.strip():
        return {**result, "stderr": "Give the code to run."}
    if len(code) > CODE_MAX_CHARS:
        return {**result, "stderr": f"The code is too long ({len(code)} characters; at most {CODE_MAX_CHARS}). Put it in a file instead."}

    free = not exec_level.contained()
    hit = broker.protected_hit(code) if broker is not None and not free else ""
    if hit:
        return {**result, "blocked": True, "stderr": f"The code cannot name '{hit}': the vault, the payment files, the browser's sign-ins, "
                                                      "the control token and the phone keys are the person's, and they are read in the Wallet."}
    places = broker.places() if broker is not None and not free else []
    workspace = Path(workspace).resolve()
    base = workspace / RUN_DIR
    base.mkdir(parents=True, exist_ok=True)
    workdir = base / secrets.token_hex(4)
    workdir.mkdir()
    script = workdir / SCRIPT_NAMES[language]
    script.write_text(code, encoding="utf-8", newline="\n")
    result["folder"] = (RUN_DIR / workdir.name).as_posix()

    if timeout is None and not free:
        timeout = 30.0
    limit = math.inf if timeout is None else max(float(timeout), 1.0) if free else min(max(float(timeout), 1.0), MAX_TIMEOUT)
    popen_args: Dict[str, Any] = {}
    job = CommandJob(None, None) if free else CommandJob(max_memory_bytes, max_processes)
    if os.name == "nt":
        popen_args["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                                       | (CREATE_SUSPENDED if job.active else 0))
    else:
        popen_args["start_new_session"] = True
    own = {"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "TADAMOO_WORKSPACE": str(workspace)}
    if free:                                          # the person's own environment, and the values of their vault ready in it
        env = {**os.environ, **(broker.environment() if broker is not None else {}), **own}
    else:
        env = safe_environment({**own, **guard.environment(places, os.environ.get("PYTHONPATH", ""))})
    started = time.monotonic()
    try:
        proc = subprocess.Popen(build_command(runtime, script, workdir, workspace, places), cwd=str(workdir), env=env, stdin=subprocess.DEVNULL,
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
    stderr += _existing_workspace_path(stderr, workspace)
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
                      cancel: Optional[threading.Event] = None, broker: Any = None) -> List[str]:
    """`run_code` in the tools; returns the languages it offers."""
    runtimes = find_runtimes(python)
    languages = sorted(runtimes)

    def handler(language: str, code: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        def go() -> Dict[str, Any]:
            return run_code(language, code, workspace, runtimes, timeout=timeout, cancel=cancel, broker=broker)
        if sandbox is None:
            return go()
        with sandbox.watching():                       # code that edits workspace files is reviewed like the file tools
            return go()

    registry.register(
        name="run_code",
        description=("Runs a snippet of code in a separate process and returns its output. Languages: " + ", ".join(languages) + ". "
                     "The working folder is new for each run, so relative paths start there; the workspace is in the environment variable TADAMOO_WORKSPACE "
                     "(saved results are in TADAMOO_WORKSPACE/.tadamoo/artifacts/). "
                     "Python runs in isolated mode: add the workspace to sys.path to import its modules. Files the code leaves in "
                     "its working folder are listed in the result. Python can reach whatever the user can, except the vault, payment files, browser "
                     "sign-ins, control token and phone keys, which it cannot open. JavaScript and TypeScript read only the working folder and the "
                     "workspace and write only the working folder."),
        parameters={
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": languages, "description": "Language of the snippet"},
                "code": {"type": "string", "description": "The code to run"},
                "timeout": {"type": "number", "description": "Timeout in seconds; when it is left out the code runs until it ends (30 seconds when the person keeps code contained)"},
            },
            "required": ["language", "code"],
        },
        effect=EffectClass.IRREVERSIBLE,
        handler=handler,
    )
    return languages
