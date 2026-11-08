"""Settings > Voice: where the voice models are kept, which model each service uses and where it is loaded.

`voice.json` in the user data folder holds only what the person chose:
    {"models_dir": "<absolute path>", "services": {"<service>": {"selected": "<model id>", "device": "auto|cpu|cuda:N", "memory": "vram|ram"}}}
A missing value has a default: the folder is `voice/` inside the first models folder of Basalt (else `voice-models/` in the data folder), a
service uses the default model of serve/voice_catalog.py, on the GPU the service picks (`auto`) in VRAM, or on the CPU in RAM for the
services that run there. Changing the folder moves nothing: a model is looked for where the folder is now. The file is replaced whole."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from serve import models, userdata, voice_catalog

FILE = "voice.json"
MEMORY = ("vram", "ram")
_DEVICE_RE = re.compile(r"^(auto|cpu|cuda:[0-9]{1,2})$")
CPU_SERVICES = ("diarization", "vad", "embedding")          # these run on the CPU unless the person says otherwise
_lock = threading.Lock()


class VoiceSettingsError(ValueError):
    """A refused value, with its code and its text in both languages."""

    def __init__(self, code: str, en: str, it: str):
        super().__init__(en)
        self.code, self.en, self.it = code, en, it

    def body(self) -> dict:
        return {"type": "voice_settings", "code": self.code, "message": self.en, "message_it": self.it}


def _file() -> Path:
    return userdata.path(FILE)


def _read() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    target = _file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def default_models_dir() -> str:
    dirs = models.dirs()
    return str(Path(dirs[0]) / "voice") if dirs else str(userdata.path("voice-models"))


def models_dir() -> str:
    chosen = _read().get("models_dir")
    return chosen if isinstance(chosen, str) and os.path.isabs(chosen) else default_models_dir()


def check_models_dir(raw) -> str:
    """The folder as it will be stored, or a refusal: an absolute path, an existing folder or one that can be created."""
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise VoiceSettingsError("path", "Give a folder's path.", "Scrivi il percorso di una cartella.")
    text = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
    if not os.path.isabs(text):
        raise VoiceSettingsError("path_relative", "Give a full path, e.g. D:\\Models\\voice or /home/name/models/voice.",
                                 "Serve un percorso completo, per esempio D:\\Modelli\\voice o /home/nome/models/voice.")
    path = os.path.normpath(text)
    if os.path.dirname(path) == path:
        raise VoiceSettingsError("path_root", "Choose a folder, not a whole disk.", "Scegli una cartella, non un disco intero.")
    if os.path.exists(path):
        if not os.path.isdir(path):
            raise VoiceSettingsError("path_file", "This is a file, not a folder.", "È un file, non una cartella.")
        if not os.access(path, os.W_OK):
            raise VoiceSettingsError("path_readonly", "Nothing can be written in this folder.", "In questa cartella non si può scrivere.")
        return path
    parent = os.path.dirname(path)
    while not os.path.exists(parent) and os.path.dirname(parent) != parent:
        parent = os.path.dirname(parent)
    if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
        raise VoiceSettingsError("path_uncreatable", "This folder does not exist and cannot be created here.",
                                 "Questa cartella non esiste e non si può creare qui.")
    return path


def set_models_dir(raw) -> str:
    path = check_models_dir(raw)
    with _lock:
        data = _read()
        data["models_dir"] = path
        _write(data)
    return path


def _check_service(service) -> str:
    if service not in voice_catalog.SERVICES:
        raise VoiceSettingsError("service", "Unknown voice service.", "Servizio vocale sconosciuto.")
    return service


def selected(service: str) -> str:
    """The model id a service uses: the one chosen if it is still in the catalog, else the service's default."""
    chosen = ((_read().get("services") or {}).get(service) or {}).get("selected")
    if isinstance(chosen, str) and (e := voice_catalog.find(chosen)) is not None and e["service"] == service:
        return chosen
    return voice_catalog.DEFAULTS.get(service, "")


def placement(service: str) -> dict:
    stored = (_read().get("services") or {}).get(service) or {}
    cpu = service in CPU_SERVICES
    device, memory = stored.get("device"), stored.get("memory")
    if not (isinstance(device, str) and _DEVICE_RE.match(device)):
        device = "cpu" if cpu else "auto"
    if memory not in MEMORY:
        memory = "ram" if device == "cpu" else "vram"
    return {"device": device, "memory": memory}


def select(service, model) -> str:
    _check_service(service)
    entry = voice_catalog.find(model)
    if entry is None or entry["service"] != service:
        raise VoiceSettingsError("model", "This model is not in the catalog of that service.", "Questo modello non è nel catalogo di quel servizio.")
    with _lock:
        data = _read()
        data.setdefault("services", {}).setdefault(service, {})["selected"] = model
        _write(data)
    return model


def set_placement(service, device, memory) -> dict:
    _check_service(service)
    if not (isinstance(device, str) and _DEVICE_RE.match(device)):
        raise VoiceSettingsError("device", "The device is auto, cpu or cuda:N (N a GPU number).", "Il dispositivo è auto, cpu o cuda:N (N il numero di una GPU).")
    if memory not in MEMORY:
        raise VoiceSettingsError("memory", "The memory is vram or ram.", "La memoria è vram o ram.")
    if device == "cpu" and memory == "vram":
        raise VoiceSettingsError("cpu_vram", "A model on the CPU lives in RAM, not in VRAM.", "Un modello sulla CPU sta nella RAM, non nella VRAM.")
    with _lock:
        data = _read()
        data.setdefault("services", {}).setdefault(service, {}).update({"device": device, "memory": memory})
        _write(data)
    return {"device": device, "memory": memory}
