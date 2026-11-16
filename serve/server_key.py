"""The server's key, changed from a client and applied at once.

`--api-key` is a command-line option (a key may not be written in the --config file). A key changed from a page, the desktop app or the phone app is kept in a file of the data folder
(`server_key.json`, readable by the owner only where the system allows it) and is the key the server uses from then on, also after a restart: it wins over `--api-key`. Changing it takes the
current key (a client that knows it may change it) and the new one; the server applies the new key to its own requests without a restart. The previous key is still believed from this machine
(loopback) until the server is started again, so that conversations already running here, which hold it in their environment, are not cut off; from the network it stops at once.

Routes:

  GET  /api/server-key           {set, kept, note}: whether the server asks for a key and whether the one in force was set from a client; never the key
  POST /api/server-key           {"current": ..., "new": ...} -> {changed, applied, note}; from this PC the key may also be set when none was (a server open to the network cannot be locked from outside)
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict

from serve.security import api_key_of, key_matches

KEY_MAX = 200
_lock = threading.Lock()


class KeyChangeRefused(ValueError):
    """A change that is refused: the HTTP status, a code and a sentence."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


class KeyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def stored(self) -> str:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        key = data.get("key") if isinstance(data, dict) else ""
        return key if isinstance(key, str) else ""

    def write(self, key: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".server-key-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"key": key}, f)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def clean_new(value: Any) -> str:
    """The new key as a client can send it in a header: one line of printable characters, up to KEY_MAX."""
    if not isinstance(value, str):
        raise KeyChangeRefused(400, "bad_key", "the new key is a text")
    try:
        key = api_key_of(value)
    except ValueError:
        key = ""
    if not key:
        raise KeyChangeRefused(400, "bad_key", "give the new key: it cannot be empty")
    if len(key) > KEY_MAX or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise KeyChangeRefused(400, "bad_key", f"the new key is one line of at most {KEY_MAX} characters")
    return key


def view(svc: Any, store: KeyStore) -> Dict[str, Any]:
    return {"set": bool(svc.api_key), "kept": bool(store.stored()),
            "note": "A key changed from a client is kept in the data folder and wins over --api-key at the next start."}


def change(svc: Any, store: KeyStore, current: Any, new: Any, local: bool, holders=()) -> Dict[str, Any]:
    """Applies a new key. `holders` are objects with an `api_key` attribute that hand it to the processes they start (the conversations, the agent runs)."""
    with _lock:
        key = clean_new(new)
        old = svc.api_key or ""
        if old:
            if not isinstance(current, str) or not key_matches(current, old):
                raise KeyChangeRefused(403, "wrong_key", "the current key is not right")
        elif not local:
            raise KeyChangeRefused(403, "forbidden", "a key can be set from the PC the server runs on only, while the server has none")
        if key == old:
            raise KeyChangeRefused(400, "same_key", "the new key is the one in force")
        store.write(key)
        retired = getattr(svc, "retired_keys", None)
        if retired is None:
            retired = svc.retired_keys = set()
        if old:
            retired.add(old)
        retired.discard(key)
        svc.api_key = key
        for h in holders:
            if h is not None:
                h.api_key = key
        if os.environ.get("TADAMOO_API_KEY"):
            os.environ["TADAMOO_API_KEY"] = key
        return {"changed": True, "applied": True, "note": "The server asks for the new key now: every client must use it."}
