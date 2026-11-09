"""Settings > Voice > Import from callAPIcall: the plan and the run of voice/importer.py with TaDaMoo's own personalities and knowledge archives.

Both routes come from this PC only. The folder is the one the person names; the server never looks in a default place. The plan reads and writes
nothing of the person's data; the run needs `confirm: true` and, for an item that holds personal data, that item's own `confirm_personal`."""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Any, Dict

from serve import userdata, voice_settings
from tadamoo import personas as pe

_lock = threading.Lock()
_module = None


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def importer():
    """voice/importer.py, loaded by its path: the folder `voice/` is not put on the import path of the server."""
    global _module
    if _module is None:
        path = Path(__file__).resolve().parents[1] / "voice" / "importer.py"
        spec = importlib.util.spec_from_file_location("tadamoo_voice_importer", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _module = module
    return _module


class PersonaPort:
    """The personality store as the importer uses it, with a way to check fields without writing them."""

    def __init__(self, store: pe.PersonaStore) -> None:
        self.store = store
        self.exists, self.create, self.update, self.memory = store.exists, store.create, store.update, store.memory

    @staticmethod
    def check(data: Dict[str, Any]) -> None:
        pe.clean(data)


def _dirs() -> tuple[Path, Path, list]:
    return userdata.path("voice") / "avatars", Path(voice_settings.models_dir()) / "tts" / "voices", [userdata.user_data_dir()]


def _source(req: Any) -> str:
    if not isinstance(req, dict) or not isinstance(req.get("source"), str):
        raise Refused(400, "source: the full path of the callAPIcall folder")
    return req["source"]


def plan(req: Any, store: pe.PersonaStore, rag: Any) -> Dict[str, Any]:
    avatars, voices, forbidden = _dirs()
    mod = importer()
    try:
        return mod.plan(_source(req), PersonaPort(store), rag, voices, forbidden)
    except mod.SourceError as e:
        raise Refused(400, str(e)) from None


def run(req: Any, store: pe.PersonaStore, rag: Any) -> Dict[str, Any]:
    source = _source(req)
    if req.get("confirm") is not True or set(req) - {"source", "confirm", "items"}:
        raise Refused(400, "send {source, confirm: true, items}: the run changes data and needs the confirmation")
    avatars, voices, forbidden = _dirs()
    mod = importer()
    if not _lock.acquire(blocking=False):
        raise Refused(409, "an import is already running")
    try:
        return mod.run(source, req.get("items"), PersonaPort(store), rag, avatars, voices, forbidden)
    except mod.SourceError as e:
        raise Refused(400, str(e)) from None
    finally:
        _lock.release()
