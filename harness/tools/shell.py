"""Shell command tool: runs through the process sandbox, with an optional secrets broker."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from harness.sandbox import ProcessSandbox, command_words
from harness.secrets_broker import NETWORK_TOOLS, SecretsBroker, url_hosts
from harness.vault import VaultError


def execute_shell_command(
    command: str,
    cwd: str | Path,
    timeout: float = 30.0,
    max_output_bytes: int = 50_000,
    secrets: Optional[SecretsBroker] = None,
) -> Dict[str, Any]:
    """Runs `command` in `cwd`. {{secret:NAME}} placeholders are filled in only here, at the boundary,
    and the secret values are masked again in everything returned to the model."""
    sandbox = ProcessSandbox(cwd, timeout=timeout, max_output_bytes=max_output_bytes)
    if secrets and secrets.has_references(command):
        try:
            secrets.check_destinations(command, url_hosts(command), [w for w in command_words(command) if w in NETWORK_TOOLS])
        except PermissionError as exc:
            return {"exit_code": -1, "stdout": "", "stderr": str(exc), "success": False, "blocked": True,
                    "timed_out": False, "truncated": False}
    try:
        to_run = secrets.resolve_tokens(command) if secrets else command
    except VaultError as exc:
        return {"exit_code": -1, "stdout": "", "stderr": str(exc), "success": False, "blocked": True, "timed_out": False, "truncated": False}
    res = sandbox.run(to_run, timeout=timeout)
    if secrets:
        res["stdout"] = secrets.mask_secrets(res["stdout"])
        res["stderr"] = secrets.mask_secrets(res["stderr"])
    return {k: res[k] for k in ("exit_code", "stdout", "stderr", "success", "blocked", "timed_out", "truncated")}
