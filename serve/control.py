"""The secret of the control routes (/api/control/*): the server writes it to the user's data folder at start, and the
scripts that stop the server read it from there. An application that only knows the API key cannot unload the model."""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from serve import userdata


def token_path(port: int) -> Path:
    """One file for each port, so two servers on one PC keep their own secrets."""
    return userdata.path(f"control-{port}.token")


def write_control_token(port: int) -> str:
    token = secrets.token_hex(24)
    try:
        path = token_path(port)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(token, encoding="ascii")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[server] warning: the control secret could not be saved ({exc}); the model cannot be unloaded "
              "through /api/control", file=sys.stderr, flush=True)
    return token
