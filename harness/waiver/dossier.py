"""The evidence dossier written when a level-4 waiver is given.

It is local, encrypted with the device key (AES-256-GCM) and never sent anywhere. What it proves is
limited: whoever controls the PC can alter a file made on it. Stronger proof needs a qualified
electronic signature and a qualified timestamp, which are outside this module.
"""
from __future__ import annotations

import datetime
import getpass
import hashlib
import json
import os
import platform
import secrets
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

MAGIC = b"BDS1"


class DossierUnavailable(RuntimeError):
    """The dossier cannot be encrypted here, so no waiver may be given."""


def _aead():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise DossierUnavailable("level 4 needs the 'cryptography' package to encrypt the evidence dossier "
                                 "(pip install cryptography)") from None
    return AESGCM


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def os_identity() -> Dict[str, Any]:
    sid = ""
    try:
        if os.name == "nt":
            out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10)
            parts = [p.strip('"') for p in out.stdout.strip().split(",")]
            sid = parts[-1] if parts else ""
        else:
            sid = str(os.getuid())
    except Exception:
        sid = ""
    machine = f"{platform.node()}|{uuid.getnode()}|{platform.machine()}"
    return {"user": getpass.getuser(), "sid": sid, "machine_hash": _sha256(machine.encode("utf-8")),
            "os": platform.platform(), "python": platform.python_version()}


def application_hashes(root: Path) -> Dict[str, Any]:
    files: Dict[str, str] = {}
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" not in p.parts:
            files[p.relative_to(root).as_posix()] = _sha256(p.read_bytes())
    return {"files": files, "combined": _sha256(json.dumps(files, sort_keys=True).encode("utf-8"))}


@dataclass
class DossierRef:
    dossier_id: str
    path: Path
    sha256: str


class DossierStore:
    def __init__(self, directory: str | Path, key: bytes) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._key = key

    def build(
        self,
        *,
        card: Dict[str, Any],
        user_name: str,
        phrase_text: str,
        phrase_lang: str,
        amount_text: str,
        per_payment: str,
        cumulative: str,
        currency: str,
        pasted: bool,
        dialog_texts: Dict[str, str],
        retyped_names: Dict[str, str],
        typing_intervals_ms: Optional[List[int]] = None,
        screenshots: Optional[Dict[str, str]] = None,
        app_root: Optional[Path] = None,
        now: Optional[datetime.datetime] = None,
    ) -> Dict[str, Any]:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        local = now.astimezone()
        return {
            "version": 1,
            "identity": os_identity(),
            "time": {"utc": now.isoformat(), "local": local.isoformat(), "network_time_offset_s": None,
                     "qualified_timestamp": None},
            "declaration": {"name": user_name, "text": phrase_text, "language": phrase_lang, "amount_text": amount_text,
                            "pasted": pasted, "typing_intervals_ms": typing_intervals_ms or []},
            "limits": {"per_payment": per_payment, "cumulative": cumulative, "currency": currency.upper()},
            "payment_system": card,
            "dialogs_shown": dialog_texts,
            "names_retyped": retyped_names,
            "screenshots": screenshots or {},
            "application": application_hashes(app_root) if app_root else None,
        }

    def seal(self, record: Dict[str, Any]) -> DossierRef:
        aead = _aead()
        dossier_id = f"ds_{secrets.token_hex(8)}"
        nonce = secrets.token_bytes(12)
        plain = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        blob = MAGIC + nonce + aead(self._key).encrypt(nonce, plain, dossier_id.encode("ascii"))
        path = self.directory / f"{dossier_id}.bin"
        path.write_bytes(blob)
        return DossierRef(dossier_id, path, _sha256(blob))

    def open(self, dossier_id: str) -> Dict[str, Any]:
        """Decrypts a dossier; raises ValueError when it was altered."""
        if not dossier_id.startswith("ds_") or not dossier_id[3:].isalnum():
            raise ValueError("invalid dossier id")
        blob = (self.directory / f"{dossier_id}.bin").read_bytes()
        if blob[:4] != MAGIC:
            raise ValueError("not a dossier file")
        try:
            plain = _aead()(self._key).decrypt(blob[4:16], blob[16:], dossier_id.encode("ascii"))
        except DossierUnavailable:
            raise
        except Exception:
            raise ValueError("the dossier was altered or was made with another device key") from None
        return json.loads(plain.decode("utf-8"))

    def delete(self, dossier_id: str) -> bool:
        path = self.directory / f"{dossier_id}.bin"
        if dossier_id.startswith("ds_") and dossier_id[3:].isalnum() and path.is_file():
            path.unlink()
            return True
        return False
