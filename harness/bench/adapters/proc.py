"""Running an outside program for an adapter: its output goes to a log file, a stop request or a time limit ends it, and a program that is missing is a
failed result and not an exception. The same call shape is faked in the tests."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from harness.bench.importer import Result

POLL_S = 1.0
STOPPED = 130
TIMED_OUT = 124

# (args, cwd, env, timeout, should_stop, log_path) -> Result
LongRunner = Callable[..., Result]


def _tail(path: Optional[Path], limit: int = 20000) -> str:
    if path is None:
        return ""
    try:
        data = Path(path).read_bytes()[-limit:]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def long_runner(args: Sequence[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
                should_stop: Optional[Callable[[], bool]] = None, log_path: Optional[Path] = None) -> Result:
    """Runs `args` until it ends, `timeout` seconds pass (exit 124) or `should_stop()` turns true (exit 130, the process tree is killed). The output of the
    program is written to `log_path` as it comes; the result holds its last part."""
    log = None
    try:
        if log_path is not None:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            log = open(log_path, "wb")
        try:
            proc = subprocess.Popen(list(args), cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log or subprocess.PIPE, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            return Result(127, "", f"{args[0]}: not found")
        except OSError as exc:
            return Result(126, "", str(exc))
        started = time.monotonic()
        code: Optional[int] = None
        while code is None:
            try:
                code = proc.wait(POLL_S)
            except subprocess.TimeoutExpired:
                if should_stop is not None and should_stop():
                    _kill(proc)
                    return Result(STOPPED, _tail(log_path), "stopped on request")
                if timeout is not None and time.monotonic() - started > timeout:
                    _kill(proc)
                    return Result(TIMED_OUT, _tail(log_path), f"{args[0]}: timed out after {timeout:g} s")
        text = _tail(log_path) if log_path is not None else (proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else "")
        return Result(code, text, "")
    finally:
        if log is not None:
            log.close()


def _kill(proc: "subprocess.Popen[bytes]") -> None:
    if os.name == "nt":                                            # the whole tree: Harbor starts docker compose under itself
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        pass
