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
STALLED = 125                 # the program showed no sign of life in the time given for it to start

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
                should_stop: Optional[Callable[[], bool]] = None, log_path: Optional[Path] = None, stall_after: Optional[float] = None,
                alive: Optional[Callable[[], bool]] = None, on_stall: Optional[Callable[[int], None]] = None) -> Result:
    """Runs `args` until it ends, `timeout` seconds pass (exit 124) or `should_stop()` turns true (exit 130, the process tree is killed). The output of the
    program is written to `log_path` as it comes; the result holds its last part. When `alive()` is still false `stall_after` seconds after the start, the
    program has not started: `on_stall(pid)` is called to record where it stands, the tree is killed and the exit is 125."""
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
        watching = stall_after is not None and alive is not None
        while code is None:
            try:
                code = proc.wait(POLL_S)
            except subprocess.TimeoutExpired:
                if should_stop is not None and should_stop():
                    _kill(proc)
                    return Result(STOPPED, _tail(log_path), "stopped on request")
                if watching and time.monotonic() - started > stall_after:
                    if alive():
                        watching = False
                    else:
                        if on_stall is not None:
                            on_stall(proc.pid)
                        _kill(proc)
                        return Result(STALLED, _tail(log_path), f"{args[0]}: no sign of life after {stall_after:g} s")
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


def dump_stacks(pid: int, path: Path) -> None:
    """Writes where the Python programs of the tree under `pid` are standing (py-spy), for a program that does not start. Never raises."""
    import shutil
    spy = shutil.which("py-spy")
    lines = []
    try:
        import psutil
        tree = [psutil.Process(pid)]
        tree += tree[0].children(recursive=True)
        for p in tree:
            lines.append(f"== {p.pid} {' '.join(p.cmdline())[:200]}")
            if spy and "python" in p.name().lower():
                done = subprocess.run([spy, "dump", "--pid", str(p.pid)], capture_output=True, timeout=30)
                lines.append((done.stdout or done.stderr).decode("utf-8", errors="replace"))
    except Exception as exc:                                       # the dump is a help for the person; its failure changes nothing
        lines.append(f"no dump: {type(exc).__name__}: {exc}")
    try:
        Path(path).write_text(os.linesep.join(lines), encoding="utf-8")
    except OSError:
        pass
