"""X11: runs the small command-line tools (xclip, xsel, xdotool, xprop)."""

from __future__ import annotations

import subprocess

TIMEOUT_S = 2.0


class X11Error(RuntimeError):
    """A tool failed, timed out or is not installed."""


def run_tool(cmd: list[str], data: bytes | None = None) -> bytes:
    """Runs `cmd` and returns its stdout. With `data` (a write), nothing is
    captured: xclip and xsel stay in the background to serve the selection and
    must not keep our pipes open."""
    reading = data is None
    try:
        done = subprocess.run(
            cmd, input=data, check=True, timeout=TIMEOUT_S,
            stdout=subprocess.PIPE if reading else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise X11Error(f"{cmd[0]} failed: {e}") from e
    return done.stdout or b""
