"""The device key: 32 random bytes kept in the user's data folder, wrapped with Windows DPAPI when available.

It encrypts the evidence dossier and signs the payment state. It protects the files at rest and
against edits by anything that is not this user; code running as this same user can still ask the
operating system to unwrap it, so the strong guarantee comes from running the agent as another user.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

KEY_BYTES = 32


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise OSError("DPAPI could not process the device key")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _wrap(data: bytes) -> bytes:
    return _dpapi(data, True) if os.name == "nt" else data


def _unwrap(data: bytes) -> bytes:
    return _dpapi(data, False) if os.name == "nt" else data


def load_or_create_key(path: str | Path) -> bytes:
    path = Path(path)
    if path.is_file():
        key = _unwrap(path.read_bytes())
        if len(key) != KEY_BYTES:
            raise ValueError(f"the device key file {path} is damaged")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(_wrap(key))
    return key
