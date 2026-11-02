"""The secret a Basalt server writes for the programs on its own PC: what a change through /api/* needs, next to the API key.

The server saves it in the user's data folder, one file for each port. A program that talks to a server on this PC reads it from
there; for any other address there is nothing to send.
"""
from __future__ import annotations

from typing import Dict
from urllib.parse import urlsplit

HEADER = "X-Basalt-Control"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def header_for(url: str) -> Dict[str, str]:
    """{HEADER: secret} for a server on this PC whose secret file exists, else {}."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return {}
    if host not in LOOPBACK:
        return {}
    try:
        from serve.userdata import user_data_dir
        secret = (user_data_dir() / f"control-{port}.token").read_text(encoding="ascii").strip()
    except (OSError, ImportError, ValueError):
        return {}
    return {HEADER: secret} if secret else {}
