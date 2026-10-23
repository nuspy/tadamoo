"""Process runner for the agent's shell commands.

This is containment of resources and of the environment, not an isolation boundary: a command still
runs with the user's file and network rights. The deny list stops the obvious system-changing tools
wherever they appear as a command word (after `&&`, `|`, `cmd /c`, `powershell -c`, ...), and every
decision to run a command belongs to the permission layer in front of it.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # keeps children from outliving the process on Windows
    from serve.util.winjob import contain as _contain
except Exception:  # pragma: no cover - the harness also runs without the server package
    _contain = None

DEFAULT_DENY_COMMANDS = {"reg", "regedit", "schtasks", "sc", "netsh", "runas", "bcdedit", "diskpart", "vssadmin", "wmic"}
WRAPPERS = {"cmd", "powershell", "pwsh", "bash", "sh", "wsl", "start", "call", "env", "sudo", "nohup", "command", "time"}
SAFE_ENV_KEYS = {
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT", "PYTHONPATH", "USERPROFILE", "HOME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "LANG", "LC_ALL", "TERM",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
}
MAX_TIMEOUT = 600.0
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n\r`<>]|\$\(")


def safe_environment(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_KEYS}
    if extra:
        env.update(extra)
    return env


def command_words(command: str) -> List[str]:
    """Lower-cased stems of every word that can be a program name in the command line."""
    words: List[str] = []
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = [t.strip("\"'()") for t in segment.split()]
        wrapped = False
        for i, tok in enumerate(tokens):
            stem = Path(tok.replace("\\", "/")).stem.lower()
            if i == 0 or wrapped:
                words.append(stem)
            if stem in WRAPPERS:
                wrapped = True
    return words


def blocked_reason(command: str) -> Optional[str]:
    for word in command_words(command):
        if word in DEFAULT_DENY_COMMANDS:
            return f"Command '{word}' is blocked by sandbox security policy"
    return None


def kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


class _Capped(threading.Thread):
    """Drains a pipe, keeping at most `limit` bytes so a noisy command cannot fill memory."""

    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream, self.limit = stream, limit
        self.data = bytearray()
        self.truncated = False

    def run(self) -> None:
        for chunk in iter(lambda: self.stream.read(8192), b""):
            room = self.limit - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > max(room, 0):
                self.truncated = True


class ProcessSandbox:
    def __init__(self, workspace_root: str | Path, timeout: float = 30.0, max_output_bytes: int = 50_000) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    def run(
        self,
        command: str,
        extra_env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {"exit_code": 0, "stdout": "", "stderr": "", "success": True, "blocked": False,
                                  "timed_out": False, "truncated": False}
        if not command.strip():
            return result
        reason = blocked_reason(command)
        if reason:
            return {**result, "exit_code": -1, "stderr": reason, "success": False, "blocked": True}

        limit = min(max(float(timeout if timeout is not None else self.timeout), 1.0), MAX_TIMEOUT)
        popen_args: Dict[str, Any] = {}
        if os.name == "nt":
            popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            popen_args["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                command, shell=True, cwd=str(self.workspace_root), env=safe_environment(extra_env),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_args,
            )
        except Exception as exc:
            return {**result, "exit_code": -1, "stderr": str(exc), "success": False}
        if _contain is not None:
            try:
                _contain(proc)
            except Exception:
                pass
        out = _Capped(proc.stdout, self.max_output_bytes)
        err = _Capped(proc.stderr, self.max_output_bytes)
        out.start()
        err.start()
        try:
            proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            result["timed_out"] = True
            proc.wait()
        out.join(5)
        err.join(5)
        stderr = err.data.decode("utf-8", errors="replace")
        if result["timed_out"]:
            stderr = (stderr + f"\nCommand timed out after {limit:g} seconds").strip()
        code = -1 if result["timed_out"] else proc.returncode
        return {**result, "exit_code": code, "stdout": out.data.decode("utf-8", errors="replace"), "stderr": stderr,
                "success": code == 0, "truncated": out.truncated or err.truncated}
