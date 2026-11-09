"""Settings > Voice: where the voice models are kept, which model each service uses and where it is loaded.

`voice.json` in the user data folder holds only what the person chose:
    {"models_dir": "<absolute path>", "services": {"<service>": {"selected": "<model id>", "device": "auto|cpu|cuda:N", "memory": "vram|ram",
                                                                  "enabled": true|false}},
     "brain": {"mode": "direct|harness", "persona": "<slug>", "permission": "plan|read_only|default|auto", "workspace": "<folder>",
               "base_url": "http://127.0.0.1:<port>", "hotkeys": true|false},
     "telephony": {"transport": "off|auto|gsm|android", "outgoing_approval": "manual|auto", "ai_disclosure_on_start": true|false,
                   "incoming_auto_answer": true|false, "on_behalf_of": "<name>"}}
A missing value has a default: the folder is `voice/` inside the first models folder of Basalt (else `voice-models/` in the data folder), a
service uses the default model of serve/voice_catalog.py, on the GPU the service picks (`auto`) in VRAM, or on the CPU in RAM for the
services that run there. Changing the folder moves nothing: a model is looked for where the folder is now. The file is replaced whole."""
from __future__ import annotations

import json
import logging
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
log = logging.getLogger("serve.voice_settings")


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


def ensure_models_dir() -> str:
    """Writes the default models folder into voice.json once a Basalt models folder exists, so the voice service, which reads the file, finds the same folder as the server."""
    data = _read()
    chosen = data.get("models_dir")
    if isinstance(chosen, str) and os.path.isabs(chosen):
        return chosen
    if not models.dirs():
        return default_models_dir()
    data["models_dir"] = default_models_dir()
    _write(data)
    return data["models_dir"]


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


def enabled(service: str) -> bool:
    """Whether the voice service is loaded with the others (Models > Voice services): the stored boolean, else on only for the speech-to-text
    model when it is installed."""
    stored = ((_read().get("services") or {}).get(service) or {}).get("enabled")
    if isinstance(stored, bool):
        return stored
    entry = voice_catalog.find(selected(service))
    return service == "stt" and entry is not None and voice_catalog.installed(entry, models_dir())


def set_enabled(service, on) -> bool:
    _check_service(service)
    if not isinstance(on, bool):
        raise VoiceSettingsError("enabled", "enabled is true or false.", "enabled è true o false.")
    with _lock:
        data = _read()
        data.setdefault("services", {}).setdefault(service, {})["enabled"] = on
        _write(data)
    return on


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


def check_placement(device, memory) -> None:
    if not (isinstance(device, str) and _DEVICE_RE.match(device)):
        raise VoiceSettingsError("device", "The device is auto, cpu or cuda:N (N a GPU number).", "Il dispositivo è auto, cpu o cuda:N (N il numero di una GPU).")
    if memory not in MEMORY:
        raise VoiceSettingsError("memory", "The memory is vram or ram.", "La memoria è vram o ram.")
    if device == "cpu" and memory == "vram":
        raise VoiceSettingsError("cpu_vram", "A model on the CPU lives in RAM, not in VRAM.", "Un modello sulla CPU sta nella RAM, non nella VRAM.")


def set_placement(service, device, memory) -> dict:
    _check_service(service)
    check_placement(device, memory)
    with _lock:
        data = _read()
        data.setdefault("services", {}).setdefault(service, {}).update({"device": device, "memory": memory})
        _write(data)
    return {"device": device, "memory": memory}


# ---------------------------------------------------------------------------------------- the brains of the voice
BRAIN_MODES = ("direct", "harness")
PERMISSIONS = ("plan", "read_only", "default", "auto")        # the Chat's own levels (serve/remote.py MODE_ORDER, the Chat's radio buttons)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
_BRAIN_KEYS = ("mode", "persona", "permission", "workspace", "hotkeys")


def brain() -> dict:
    """What the voice service reads to choose and run its brains. `permission_set` says whether the person ever chose a level for the voice."""
    stored = _read().get("brain")
    stored = stored if isinstance(stored, dict) else {}
    mode, persona, permission = stored.get("mode"), stored.get("persona"), stored.get("permission")
    workspace = stored.get("workspace")
    return {"mode": mode if mode in BRAIN_MODES else "direct",
            "persona": persona if isinstance(persona, str) and _SLUG_RE.match(persona) else "",
            "permission": permission if permission in PERMISSIONS else "default", "permission_set": permission in PERMISSIONS,
            "workspace": workspace if isinstance(workspace, str) and os.path.isabs(workspace) else "",
            "hotkeys": stored.get("hotkeys") is True,
            "base_url": stored.get("base_url") if isinstance(stored.get("base_url"), str) else ""}


def check_brain(req) -> dict:
    """The fields of a request to change, checked; only the ones named are returned."""
    if not isinstance(req, dict):
        raise VoiceSettingsError("body", "Send a JSON object.", "Manda un oggetto JSON.")
    out: dict = {}
    for key in req:
        if key not in _BRAIN_KEYS:
            raise VoiceSettingsError("field", f"Unknown field: {key}.", f"Campo sconosciuto: {key}.")
    if "mode" in req:
        if req["mode"] not in BRAIN_MODES:
            raise VoiceSettingsError("mode", "The mode is direct or harness.", "La modalità è direct o harness.")
        out["mode"] = req["mode"]
    if "permission" in req:
        if req["permission"] not in PERMISSIONS:
            raise VoiceSettingsError("permission", "The permission is plan, read_only, default or auto.", "Il permesso è plan, read_only, default o auto.")
        out["permission"] = req["permission"]
    if "persona" in req:
        persona = req["persona"]
        if not (persona == "" or (isinstance(persona, str) and _SLUG_RE.match(persona))):
            raise VoiceSettingsError("persona", "Give the personality's id, or nothing.", "Scrivi l'id della personalità, oppure niente.")
        out["persona"] = persona
    if "workspace" in req:
        folder = req["workspace"]
        if folder != "" and not (isinstance(folder, str) and "\0" not in folder and os.path.isabs(folder) and os.path.isdir(folder)):
            raise VoiceSettingsError("workspace", "Give the full path of a folder that exists, or nothing.", "Scrivi il percorso completo di una cartella che esiste, oppure niente.")
        out["workspace"] = os.path.normpath(folder) if folder else ""
    if "hotkeys" in req:
        if not isinstance(req["hotkeys"], bool):
            raise VoiceSettingsError("hotkeys", "hotkeys is true or false.", "hotkeys è true o false.")
        out["hotkeys"] = req["hotkeys"]
    return out


def set_brain(req) -> dict:
    changes = check_brain(req)
    with _lock:
        data = _read()
        stored = data.get("brain") if isinstance(data.get("brain"), dict) else {}
        stored.update(changes)
        data["brain"] = stored
        _write(data)
    return brain()


def write_server_address(port: int) -> None:
    """Where this server listens, for the voice service: written at every start, and only when it changed."""
    url = f"http://127.0.0.1:{int(port)}"
    with _lock:
        data = _read()
        stored = data.get("brain") if isinstance(data.get("brain"), dict) else {}
        if stored.get("base_url") == url:
            return
        stored["base_url"] = url
        data["brain"] = stored
        _write(data)


# ---------------------------------------------------------------------------------------- telephony
TRANSPORTS = ("off", "auto", "gsm", "android")
APPROVALS = ("manual", "auto")                   # who approves a call the agent or a program starts: the person, or nobody
_TELEPHONY_KEYS = ("transport", "outgoing_approval", "ai_disclosure_on_start", "incoming_auto_answer", "on_behalf_of")
ON_BEHALF_MAX = 80


def telephony() -> dict:
    """The rules of phone calls as the voice service reads them (voice/settings_bridge.py): a missing or bad value is the careful default."""
    stored = _read().get("telephony")
    stored = stored if isinstance(stored, dict) else {}
    transport, approval, who = stored.get("transport"), stored.get("outgoing_approval"), stored.get("on_behalf_of")
    return {"transport": transport if transport in TRANSPORTS else "off",
            "outgoing_approval": approval if approval in APPROVALS else "manual",
            "ai_disclosure_on_start": stored.get("ai_disclosure_on_start") is not False,
            "incoming_auto_answer": stored.get("incoming_auto_answer") is True,
            "on_behalf_of": who.strip()[:ON_BEHALF_MAX] if isinstance(who, str) and not any(ord(c) < 32 for c in who) else ""}


def check_telephony(req) -> dict:
    """The fields of a request to change, checked; only the ones named are returned."""
    if not isinstance(req, dict):
        raise VoiceSettingsError("body", "Send a JSON object.", "Manda un oggetto JSON.")
    for key in req:
        if key not in _TELEPHONY_KEYS:
            raise VoiceSettingsError("field", f"Unknown field: {key}.", f"Campo sconosciuto: {key}.")
    out: dict = {}
    if "transport" in req:
        if req["transport"] not in TRANSPORTS:
            raise VoiceSettingsError("transport", "The transport is off, auto, gsm or android.", "Il trasporto è off, auto, gsm o android.")
        out["transport"] = req["transport"]
    if "outgoing_approval" in req:
        if req["outgoing_approval"] not in APPROVALS:
            raise VoiceSettingsError("outgoing_approval", "The approval is manual or auto.", "L'approvazione è manual o auto.")
        out["outgoing_approval"] = req["outgoing_approval"]
    for key in ("ai_disclosure_on_start", "incoming_auto_answer"):
        if key in req:
            if not isinstance(req[key], bool):
                raise VoiceSettingsError(key, f"{key} is true or false.", f"{key} è true o false.")
            out[key] = req[key]
    if "on_behalf_of" in req:
        who = req["on_behalf_of"]
        if not isinstance(who, str) or len(who.strip()) > ON_BEHALF_MAX or any(ord(c) < 32 for c in who):
            raise VoiceSettingsError("on_behalf_of", f"Give a name of at most {ON_BEHALF_MAX} characters, or nothing.",
                                     f"Scrivi un nome di al massimo {ON_BEHALF_MAX} caratteri, oppure niente.")
        out["on_behalf_of"] = who.strip()
    return out


def set_telephony(req) -> dict:
    changes = check_telephony(req)
    with _lock:
        data = _read()
        stored = data.get("telephony") if isinstance(data.get("telephony"), dict) else {}
        before = telephony()
        stored.update(changes)
        data["telephony"] = stored
        _write(data)
    if changes.get("ai_disclosure_on_start") is False and before["ai_disclosure_on_start"]:
        log.info("telephony: the AI disclosure at the start of calls was turned off by the person")
    return telephony()
