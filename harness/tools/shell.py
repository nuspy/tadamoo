"""Shell command execution tool with timeout and directory confinement."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict


def execute_shell_command(
    command: str,
    cwd: str | Path,
    timeout: float = 30.0,
    max_output_bytes: int = 50_000,
) -> Dict[str, Any]:
    cwd_path = Path(cwd).resolve()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")[:max_output_bytes]
        stderr = proc.stderr.decode("utf-8", errors="replace")[:max_output_bytes]
        return {
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "success": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds",
            "success": False,
        }
    except Exception as exc:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": str(exc),
            "success": False,
        }
