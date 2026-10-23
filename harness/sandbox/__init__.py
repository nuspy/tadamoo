"""Execution sandbox for subprocess containment and Windows security rules."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_DENY_COMMANDS = {"reg", "schtasks", "sc", "netsh", "runas"}


class ProcessSandbox:
    def __init__(self, workspace_root: str | Path, timeout: float = 30.0) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.timeout = timeout

    def run(self, command: str, extra_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Runs command inside workspace with command inspection and environment scrubbing."""
        tokens = command.strip().split()
        if not tokens:
            return {"exit_code": 0, "stdout": "", "stderr": "", "blocked": False}

        binary_name = Path(tokens[0]).stem.lower()
        if binary_name in DEFAULT_DENY_COMMANDS:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command '{binary_name}' is blocked by sandbox security policy",
                "blocked": True,
            }

        # Clean environment: keep safe standard variables only
        safe_keys = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT", "PYTHONPATH"}
        env = {k: v for k, v in os.environ.items() if k.upper() in safe_keys}
        if extra_env:
            env.update(extra_env)

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.workspace_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                env=env,
            )
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", errors="replace"),
                "stderr": proc.stderr.decode("utf-8", errors="replace"),
                "blocked": False,
            }
        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Execution timed out after {self.timeout}s",
                "blocked": False,
            }
        except Exception as exc:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "blocked": False,
            }
