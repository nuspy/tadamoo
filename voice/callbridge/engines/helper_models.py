"""Where the TTS helper processes read their weights and voice samples from; the helpers never fetch anything.

The helpers run as scripts in their own Python environments, so this module imports only the standard library. The models folder is
`BASALT_VOICE_MODELS_DIR` (set by the service for every helper); the rule for what counts as installed is that of models_store.py,
which is loaded from its file when the package is not importable here."""
from __future__ import annotations

import importlib.util
import os
import sys

_CLONE_EXTS = (".wav", ".mp3", ".flac", ".ogg")


def _store():
    mod = sys.modules.get("callbridge.models_store") or sys.modules.get("_basalt_models_store")
    if mod is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models_store.py")
        spec = importlib.util.spec_from_file_location("_basalt_models_store", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_basalt_models_store"] = mod
        spec.loader.exec_module(mod)
    return mod


def require(model_id: str, sub: str = "") -> str:
    """The folder of an installed model (or of its `sub` folder), or `ModelNotInstalled`: call it before importing any heavy library."""
    where = _store().require(model_id)
    return os.path.join(where, *sub.split("/")) if sub else where


def whisper_small() -> str:
    """The local Whisper small checkpoint (used to transcribe a voice sample), or `ModelNotInstalled`."""
    return _store().whisper_checkpoint("small")


def voice_sample(voice: str) -> str | None:
    """The audio file of a cloned voice in `<models folder>/tts/voices`, or None."""
    base = _store().voices_dir()
    name = (voice or "").strip()
    if not base or not name or os.path.basename(name) != name:
        return None
    for ext in _CLONE_EXTS:
        path = os.path.join(base, name + ext)
        if os.path.isfile(path):
            return path
    return None
