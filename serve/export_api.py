"""The route logic of the conversation export: POST /api/export turns the page's snapshot into a file, and a PDF made by the desktop
app is held for a moment so the app's hidden window can load it by an address that only this PC can use (GET /api/export/held/<token>).

The conversion itself is in serve/exportdoc.py and serve/exportoffice.py."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from serve import exportdoc
from serve.exportdoc import ExportError

BODY_MAX = 96 << 20                 # the most an export request may hold: a long conversation with its pictures
HOLD_SECONDS = 300
HOLD_MAX = 4
HELD_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"


@dataclass
class Result:
    data: bytes
    content_type: str
    name: str
    token: str = ""                 # set when the file is held for the desktop app


class ExportApi:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: Dict[str, Tuple[bytes, str, str, float]] = {}

    def run(self, req: Any, local: bool) -> Result:
        """The file for a request. With "hold": true (the desktop app prints a PDF itself) the page to print is held for one fetch
        and its token returned; only the PC the server runs on may ask for that."""
        if not isinstance(req, dict):
            raise ExportError("Send a JSON object.")
        hold = req.get("hold") is True
        if hold and not local:
            raise PermissionError("a file can be held only for the app on the PC the server runs on")
        data, ctype, name = exportdoc.export(req)
        if not hold:
            return Result(data, ctype, name)
        return Result(b"", ctype, name, token=self._put(data, ctype, name))

    def _put(self, data: bytes, ctype: str, name: str) -> str:
        token = secrets.token_hex(16)
        now = time.monotonic()
        with self._lock:
            for key in [k for k, v in self._held.items() if v[3] < now]:
                del self._held[key]
            while len(self._held) >= HOLD_MAX:
                del self._held[min(self._held, key=lambda k: self._held[k][3])]
            self._held[token] = (data, ctype, name, now + HOLD_SECONDS)
        return token

    def take(self, token: str) -> Optional[Tuple[bytes, str, str]]:
        """The held file, once: it is forgotten as it is handed over."""
        with self._lock:
            entry = self._held.pop(token, None)
        if entry is None or entry[3] < time.monotonic():
            return None
        return entry[0], entry[1], entry[2]


def disposition(name: str) -> str:
    ascii_name = "".join(c if c.isascii() and c.isalnum() or c in " ._-()" else "_" for c in name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
