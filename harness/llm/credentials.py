"""Where the keys and tokens of the providers are kept: the credential manager of Windows, not a JSON file in clear.

`CredentialStore` is the small interface the registry uses; `WindowsCredentials` talks to the Windows credential manager
(`CredWriteW` and friends, through ctypes: no extra package), `FileCredentials` is the fallback where there is none (a file
that only the user can read) and the one tests use with a folder of their own. A secret is never written to a log, and the
registry's own JSON file only holds a reference to it.
"""
from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Dict, Optional, Protocol

PREFIX = "Basalt/llm/"
MAX_BYTES = 2560                                   # what the Windows credential manager takes for one secret


class CredentialStore(Protocol):
    def read(self, name: str) -> str: ...
    def write(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class CredentialError(OSError):
    pass


class FileCredentials:
    """A JSON file that only this user can read, for machines without the Windows credential manager."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> Dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def read(self, name: str) -> str:
        return self._load().get(name, "")

    def write(self, name: str, value: str) -> None:
        data = self._load()
        data[name] = value
        self._save(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if data.pop(name, None) is not None:
            self._save(data)


class WindowsCredentials:
    """The Windows credential manager, as generic credentials named `Basalt/llm/<name>`, kept for this user on this PC."""

    CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE = 1, 2

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    def __init__(self, prefix: str = PREFIX) -> None:
        if os.name != "nt":
            raise CredentialError("the Windows credential manager is not here")
        self.prefix = prefix
        self._adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self._adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(self._CREDENTIAL))]
        self._adv.CredReadW.restype = wintypes.BOOL
        self._adv.CredWriteW.argtypes = [ctypes.POINTER(self._CREDENTIAL), wintypes.DWORD]
        self._adv.CredWriteW.restype = wintypes.BOOL
        self._adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._adv.CredDeleteW.restype = wintypes.BOOL
        self._adv.CredFree.argtypes = [ctypes.c_void_p]

    def read(self, name: str) -> str:
        pcred = ctypes.POINTER(self._CREDENTIAL)()
        if not self._adv.CredReadW(self.prefix + name, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            if ctypes.get_last_error() == 1168:                      # ERROR_NOT_FOUND
                return ""
            raise CredentialError(f"the credential manager could not read {name}: error {ctypes.get_last_error()}")
        try:
            cred = pcred.contents
            return bytes(ctypes.cast(cred.CredentialBlob, ctypes.POINTER(ctypes.c_ubyte * cred.CredentialBlobSize)).contents).decode("utf-8")
        finally:
            self._adv.CredFree(pcred)

    def write(self, name: str, value: str) -> None:
        blob = value.encode("utf-8")
        if len(blob) > MAX_BYTES:
            raise CredentialError(f"{name} is too long for the credential manager ({len(blob)} bytes, at most {MAX_BYTES})")
        buf = (ctypes.c_ubyte * max(len(blob), 1)).from_buffer_copy(blob or b"\0")
        cred = self._CREDENTIAL(Type=self.CRED_TYPE_GENERIC, TargetName=self.prefix + name, CredentialBlobSize=len(blob),
                                CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), Persist=self.CRED_PERSIST_LOCAL_MACHINE,
                                UserName="basalt")
        if not self._adv.CredWriteW(ctypes.byref(cred), 0):
            raise CredentialError(f"the credential manager could not save {name}: error {ctypes.get_last_error()}")

    def delete(self, name: str) -> None:
        if not self._adv.CredDeleteW(self.prefix + name, self.CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != 1168:
            raise CredentialError(f"the credential manager could not delete {name}: error {ctypes.get_last_error()}")


def default_store(fallback_dir: Optional[Path] = None) -> CredentialStore:
    """The Windows credential manager where there is one; else a private file next to the registry (also when
    BASALT_CREDENTIALS=file: the tests use it so that they never touch the person's own credentials)."""
    if os.name == "nt" and os.environ.get("BASALT_CREDENTIALS") != "file":
        try:
            return WindowsCredentials()
        except (OSError, AttributeError):
            pass
    return FileCredentials((fallback_dir or Path.home() / ".basalt") / "llm_credentials.json")
