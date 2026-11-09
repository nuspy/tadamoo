"""Settings > Voice: where the voice models are kept, which model each service uses and where it is loaded.

`voice.json` in the user data folder holds only what the person chose:
    {"models_dir": "<absolute path>", "services": {"<service>": {"selected": "<model id>", "device": "auto|cpu|cuda:N", "memory": "vram|ram",
                                                                  "enabled": true|false}},
     "brain": {"mode": "direct|harness", "persona": "<slug>", "permission": "plan|read_only|default|auto", "workspace": "<folder>",
               "base_url": "http://127.0.0.1:<port>", "hotkeys": true|false},
     "meetings": {"speak": true|false, "camera": true|false, "proactive": true|false, "consent_mode": "window|explicit", "consent_window_s": 20..300, "on_behalf_of": "<name>",
                  "display_name": "<name>", "virtual_mic": "<part of a device name>", "wake_phrase": "<phrase>", "autojoin": true|false, "allow_programs_join": true|false,
                  "copilot_replies": 1..6, "copilot_audio_reply": true|false, "copilot_hide_from_capture": true|false},
     "telephony": {"transport": "off|auto|gsm|android", "outgoing_approval": "manual|auto", "ai_disclosure_on_start": true|false,
                   "incoming_auto_answer": true|false, "on_behalf_of": "<name>"},
     "interruptions": {"enabled": true|false, "judge_timeout_ms": 200..5000, "duck_level": 0..0.9, "resume_offer": true|false}}
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


# ---------------------------------------------------------------------------------------- the avatar's acceleration (voice.json avatar.enhance)
# The same lists as voice/callbridge/enhance/settings.py, which reads the file in the voice service (the two cannot import each other);
# voice/tests/test_enhance_settings.py checks that they agree.
ENHANCE_UPSCALE = ("none", "basic", "rtx_video")
ENHANCE_INTERPOLATE = ("none", "basic")
ENHANCE_SCALES = (1.5, 2.0, 3.0, 4.0)
ENHANCE_PROVIDER_SCALES = {"none": ENHANCE_SCALES, "basic": ENHANCE_SCALES, "rtx_video": (2.0, 3.0, 4.0)}
ENHANCE_FACTORS = (2, 3)
ENHANCE_FPS_CAP = (5, 60)
ENHANCE_HEIGHT_CAP = (120, 2160)
ENHANCE_DEFAULT = {"upscale": "none", "scale": 2.0, "interpolate": "none", "factor": 2, "worker_fps_cap": 0, "worker_height_cap": 0, "rtx_video_path": ""}


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_enhance(merged: dict) -> dict:
    """The complete `enhance` object, checked; raises VoiceSettingsError with the field that is wrong."""
    if merged["upscale"] not in ENHANCE_UPSCALE:
        raise VoiceSettingsError("upscale", "upscale is none, basic or rtx_video.", "upscale è none, basic o rtx_video.")
    scale = merged["scale"]
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or float(scale) not in ENHANCE_SCALES:
        raise VoiceSettingsError("scale", "scale is 1.5, 2, 3 or 4.", "scale è 1.5, 2, 3 o 4.")
    if float(scale) not in ENHANCE_PROVIDER_SCALES[merged["upscale"]]:
        allowed = ", ".join(f"{s:g}" for s in ENHANCE_PROVIDER_SCALES[merged["upscale"]])
        raise VoiceSettingsError("scale", f"{merged['upscale']} takes the scales {allowed}.", f"{merged['upscale']} accetta le scale {allowed}.")
    if merged["interpolate"] not in ENHANCE_INTERPOLATE:
        raise VoiceSettingsError("interpolate", "interpolate is none or basic.", "interpolate è none o basic.")
    if not _whole(merged["factor"]) or merged["factor"] not in ENHANCE_FACTORS:
        raise VoiceSettingsError("factor", "factor is 2 or 3.", "factor è 2 o 3.")
    for key, (lo, hi) in (("worker_fps_cap", ENHANCE_FPS_CAP), ("worker_height_cap", ENHANCE_HEIGHT_CAP)):
        value = merged[key]
        if not _whole(value) or not (value == 0 or lo <= value <= hi):
            raise VoiceSettingsError(key, f"{key} is 0 (automatic) or a whole number from {lo} to {hi}.", f"{key} è 0 (automatico) o un numero intero da {lo} a {hi}.")
    path = merged["rtx_video_path"]
    if not isinstance(path, str) or (path and not (os.path.isabs(path) and path.lower().endswith(".dll") and "\0" not in path and len(path) <= 500)):
        raise VoiceSettingsError("rtx_video_path", "rtx_video_path is the full path of the SDK's DLL, or nothing.", "rtx_video_path è il percorso completo della DLL dell'SDK, oppure niente.")
    return {**merged, "scale": float(scale)}


def enhance(stored=None) -> dict:
    """The stored acceleration settings with the defaults filled in; a damaged value is the default."""
    stored = stored if isinstance(stored, dict) else {}
    merged = {k: stored.get(k, v) for k, v in ENHANCE_DEFAULT.items()}
    try:
        return check_enhance(merged)
    except VoiceSettingsError:
        return dict(ENHANCE_DEFAULT)


def merge_enhance(stored, req) -> dict:
    """The stored settings changed by the fields of `req`, the whole checked together (a scale must suit the provider chosen with it)."""
    if not isinstance(req, dict):
        raise VoiceSettingsError("enhance", "enhance is a JSON object.", "enhance è un oggetto JSON.")
    for key in req:
        if key not in ENHANCE_DEFAULT:
            raise VoiceSettingsError("field", f"Unknown field: {key}.", f"Campo sconosciuto: {key}.")
    return check_enhance({**enhance(stored), **req})


# ---------------------------------------------------------------------------------------- the avatar's engines (voice.json avatar.engines, resolution, fps_hint)
# A copy of what a request is checked against from voice/avatar_engines/registry.py (the server cannot import the voice package):
# id -> (targets it runs on, the heights it offers, its default height). voice/tests/test_avatar_engines_selection.py keeps the two equal.
AVATAR_TARGETS = ("pc", "web", "android", "esp32")
AVATAR_ENGINES = {"visemes": (("pc", "web", "android", "esp32"), (96, 144, 192, 240, 288, 384, 480), 240),
                  "wav2lip_gan96": (("pc", "web", "android"), (96, 144, 192, 288, 384, 480), 96),
                  "wav2lip_v288": (("pc", "web"), (144, 192, 288, 384, 480), 288),
                  "ultralight": (("pc", "web", "android"), (160, 256, 384, 480), 256),
                  "feathertalk": (("pc", "web", "android"), (144, 192, 288, 384, 480), 144),
                  "musetalk_15": (("pc", "web"), (256, 384, 480, 720), 480),
                  "sadtalker": (("pc",), (256, 384, 512), 256),
                  "er_nerf": (("pc",), (256, 384, 512), 256)}
AVATAR_DEFAULT_ENGINES = {"pc": "musetalk_15", "web": "visemes", "android": "wav2lip_gan96", "esp32": "visemes"}
AVATAR_DEFAULT_RESOLUTION = {"pc": 480, "web": 240, "android": 96, "esp32": 96}
AVATAR_FPS = (5, 60)
AVATAR_DEFAULT_FPS = 25
AVATAR_ENGINE_KEYS = ("engines", "resolution", "fps_hint")


def _avatar_height(target: str, engine: str, current=None) -> int:
    """The height for `engine` on `target`: `current` when the engine offers it, else the target's default when it does, else the engine's own."""
    _targets, heights, own = AVATAR_ENGINES[engine]
    for height in (current, AVATAR_DEFAULT_RESOLUTION[target]):
        if _whole(height) and height in heights:
            return height
    return own


def avatar_selection(stored=None) -> dict:
    """{engines, resolution, fps_hint} from a stored `avatar` object; each value that is missing or invalid takes its default."""
    stored = stored if isinstance(stored, dict) else {}
    engines_in = stored.get("engines") if isinstance(stored.get("engines"), dict) else {}
    heights_in = stored.get("resolution") if isinstance(stored.get("resolution"), dict) else {}
    engines, heights = {}, {}
    for target in AVATAR_TARGETS:
        wanted = engines_in.get(target)
        ok = isinstance(wanted, str) and wanted in AVATAR_ENGINES and target in AVATAR_ENGINES[wanted][0]
        engines[target] = wanted if ok else AVATAR_DEFAULT_ENGINES[target]
        heights[target] = _avatar_height(target, engines[target], heights_in.get(target))
    fps = stored.get("fps_hint")
    return {"engines": engines, "resolution": heights, "fps_hint": fps if _whole(fps) and AVATAR_FPS[0] <= fps <= AVATAR_FPS[1] else AVATAR_DEFAULT_FPS}


def _check_target(target) -> str:
    if target not in AVATAR_TARGETS:
        raise VoiceSettingsError("target", f"The target is one of {', '.join(AVATAR_TARGETS)}.", f"La destinazione è una tra {', '.join(AVATAR_TARGETS)}.")
    return target


def _check_avatar_engine(target: str, engine) -> str:
    if not isinstance(engine, str) or engine not in AVATAR_ENGINES:
        raise VoiceSettingsError("engine", "This engine is not known.", "Questo motore non è conosciuto.")
    if target not in AVATAR_ENGINES[engine][0]:
        runs = ", ".join(AVATAR_ENGINES[engine][0])
        raise VoiceSettingsError("engine", f"{engine} does not run on {target}: it runs on {runs}.", f"{engine} non gira su {target}: gira su {runs}.")
    return engine


def _check_avatar_height(engine: str, height) -> int:
    heights = AVATAR_ENGINES[engine][1]
    if not _whole(height) or height not in heights:
        shown = ", ".join(map(str, heights))
        raise VoiceSettingsError("resolution", f"{engine} draws at {shown} pixels high.", f"{engine} disegna a {shown} pixel di altezza.")
    return height


def merge_avatar_selection(stored, req: dict) -> dict:
    """The stored selection changed by the fields of `req` (`engines` and `resolution` as {target: value}, `fps_hint`), the whole checked together:
    an engine that does not run on its target or a height it does not offer is refused. A target whose engine changes and whose height is not in
    `req` keeps its height when the new engine offers it, else takes a default."""
    now = avatar_selection(stored)
    engines, heights = dict(now["engines"]), dict(now["resolution"])
    for key in ("engines", "resolution"):
        if key in req and not isinstance(req[key], dict):
            raise VoiceSettingsError(key, f"{key} is an object with a value per target.", f"{key} è un oggetto con un valore per destinazione.")
    for target, engine in (req.get("engines") or {}).items():
        _check_target(target)
        engines[target] = _check_avatar_engine(target, engine)
        heights[target] = _avatar_height(target, engine, heights[target])
    for target, height in (req.get("resolution") or {}).items():
        _check_target(target)
        heights[target] = _check_avatar_height(engines[target], height)
    fps = req.get("fps_hint", now["fps_hint"])
    if not _whole(fps) or not AVATAR_FPS[0] <= fps <= AVATAR_FPS[1]:
        raise VoiceSettingsError("fps_hint", f"fps_hint is a whole number from {AVATAR_FPS[0]} to {AVATAR_FPS[1]}.", f"fps_hint è un numero intero da {AVATAR_FPS[0]} a {AVATAR_FPS[1]}.")
    return {"engines": engines, "resolution": heights, "fps_hint": fps}


def select_avatar_engine(req) -> dict:
    """Set one target's engine (and height); returns the whole selection. `req` is {target, engine, resolution?}."""
    if not isinstance(req, dict) or set(req) - {"target", "engine", "resolution"} or "target" not in req or "engine" not in req:
        raise VoiceSettingsError("body", "Send target, engine and, if you want one, resolution.", "Manda target, engine e, se vuoi, resolution.")
    target = _check_target(req["target"])
    change: dict = {"engines": {target: req["engine"]}}
    if req.get("resolution") is not None:
        change["resolution"] = {target: req["resolution"]}
    with _lock:
        data = _read()
        stored = data.get("avatar") if isinstance(data.get("avatar"), dict) else {}
        merged = merge_avatar_selection(stored, change)
        stored.update(merged)
        data["avatar"] = stored
        _write(data)
    return merged


# ---------------------------------------------------------------------------------------- meetings and the copilot
CONSENT_MODES = ("window", "explicit")
CONSENT_WINDOW = (20, 300, 45)                  # lowest, highest, default seconds; voice/callbridge/meetbot/consent.py holds the same numbers (a test compares)
MEETINGS_DEFAULT = {"speak": False, "camera": False, "proactive": False, "consent_mode": "window", "consent_window_s": CONSENT_WINDOW[2], "on_behalf_of": "",
                    "display_name": "TaDaMoo AI assistant", "virtual_mic": "", "wake_phrase": "", "autojoin": False, "allow_programs_join": False, "copilot_replies": 3,
                    "copilot_audio_reply": False, "copilot_hide_from_capture": True}
MEETINGS_BOOLS = ("speak", "camera", "proactive", "autojoin", "allow_programs_join", "copilot_audio_reply", "copilot_hide_from_capture")
MEETINGS_TEXT = {"on_behalf_of": 80, "display_name": 40, "virtual_mic": 80, "wake_phrase": 40}


def _whole_in(value, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _text_ok(value, limit: int) -> bool:
    return isinstance(value, str) and len(value.strip()) <= limit and not any(ord(c) < 32 for c in value)


def meetings() -> dict:
    """The rules of the meeting bot and the copilot as the voice service reads them: a missing or bad value is the careful default (the bot does not
    speak, asks for consent with a window, nothing joins by itself, a program may not start the bot)."""
    stored = _read().get("meetings")
    stored = stored if isinstance(stored, dict) else {}
    out = dict(MEETINGS_DEFAULT)
    for key in MEETINGS_BOOLS:
        if isinstance(stored.get(key), bool):
            out[key] = stored[key]
    if stored.get("consent_mode") in CONSENT_MODES:
        out["consent_mode"] = stored["consent_mode"]
    if _whole_in(stored.get("consent_window_s"), CONSENT_WINDOW[0], CONSENT_WINDOW[1]):
        out["consent_window_s"] = stored["consent_window_s"]
    if _whole_in(stored.get("copilot_replies"), 1, 6):
        out["copilot_replies"] = stored["copilot_replies"]
    for key, limit in MEETINGS_TEXT.items():
        if _text_ok(stored.get(key), limit) and (stored[key].strip() or key not in ("display_name",)):
            out[key] = stored[key].strip()
    return out


def check_meetings(req) -> dict:
    """The fields of a request to change, checked; only the ones named are returned."""
    if not isinstance(req, dict):
        raise VoiceSettingsError("body", "Send a JSON object.", "Manda un oggetto JSON.")
    for key in req:
        if key not in MEETINGS_DEFAULT:
            raise VoiceSettingsError("field", f"Unknown field: {key}.", f"Campo sconosciuto: {key}.")
    out: dict = {}
    for key in MEETINGS_BOOLS:
        if key in req:
            if not isinstance(req[key], bool):
                raise VoiceSettingsError(key, f"{key} is true or false.", f"{key} è true o false.")
            out[key] = req[key]
    if "consent_mode" in req:
        if req["consent_mode"] not in CONSENT_MODES:
            raise VoiceSettingsError("consent_mode", "The consent mode is window or explicit.", "La modalità del consenso è window o explicit.")
        out["consent_mode"] = req["consent_mode"]
    if "consent_window_s" in req:
        if not _whole_in(req["consent_window_s"], CONSENT_WINDOW[0], CONSENT_WINDOW[1]):
            raise VoiceSettingsError("consent_window_s", f"The consent window is a whole number of seconds from {CONSENT_WINDOW[0]} to {CONSENT_WINDOW[1]}.",
                                     f"La finestra del consenso è un numero intero di secondi da {CONSENT_WINDOW[0]} a {CONSENT_WINDOW[1]}.")
        out["consent_window_s"] = req["consent_window_s"]
    if "copilot_replies" in req:
        if not _whole_in(req["copilot_replies"], 1, 6):
            raise VoiceSettingsError("copilot_replies", "The copilot shows 1 to 6 replies.", "Il copilota mostra da 1 a 6 risposte.")
        out["copilot_replies"] = req["copilot_replies"]
    for key, limit in MEETINGS_TEXT.items():
        if key in req:
            if not _text_ok(req[key], limit) or (key == "display_name" and not req[key].strip()):
                raise VoiceSettingsError(key, f"{key} is a text of 1 to {limit} characters." if key == "display_name" else f"{key} is a text of at most {limit} characters, or nothing.",
                                         f"{key} è un testo da 1 a {limit} caratteri." if key == "display_name" else f"{key} è un testo di al massimo {limit} caratteri, oppure niente.")
            out[key] = req[key].strip()
    return out


def set_meetings(req) -> dict:
    changes = check_meetings(req)
    with _lock:
        data = _read()
        before = meetings()
        stored = data.get("meetings") if isinstance(data.get("meetings"), dict) else {}
        stored.update(changes)
        data["meetings"] = stored
        _write(data)
    for key in ("speak", "proactive", "autojoin", "allow_programs_join"):
        if changes.get(key) is True and not before[key]:
            log.info("meetings: %s was turned on by the person", key)
    return meetings()


# ---------------------------------------------------------------------------------------- speaking over the avatar
INTERRUPTIONS_DEFAULT = {"enabled": True, "judge_timeout_ms": 800, "duck_level": 0.3, "resume_offer": True}    # voice/callbridge/interruption.py holds the same (a test compares)
JUDGE_TIMEOUT_MS = (200, 5000)
DUCK_LEVEL = (0.0, 0.9)


def _duck_ok(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and DUCK_LEVEL[0] <= value <= DUCK_LEVEL[1]


def interruptions() -> dict:
    """What the avatar does when the person speaks over it, as the voice service reads it: a missing or bad value is the default."""
    stored = _read().get("interruptions")
    stored = stored if isinstance(stored, dict) else {}
    out = dict(INTERRUPTIONS_DEFAULT)
    for key in ("enabled", "resume_offer"):
        if isinstance(stored.get(key), bool):
            out[key] = stored[key]
    if _whole_in(stored.get("judge_timeout_ms"), *JUDGE_TIMEOUT_MS):
        out["judge_timeout_ms"] = stored["judge_timeout_ms"]
    if _duck_ok(stored.get("duck_level")):
        out["duck_level"] = float(stored["duck_level"])
    return out


def check_interruptions(req) -> dict:
    """The fields of a request to change, checked; only the ones named are returned."""
    if not isinstance(req, dict):
        raise VoiceSettingsError("body", "Send a JSON object.", "Manda un oggetto JSON.")
    for key in req:
        if key not in INTERRUPTIONS_DEFAULT:
            raise VoiceSettingsError("field", f"Unknown field: {key}.", f"Campo sconosciuto: {key}.")
    out: dict = {}
    for key in ("enabled", "resume_offer"):
        if key in req:
            if not isinstance(req[key], bool):
                raise VoiceSettingsError(key, f"{key} is true or false.", f"{key} è true o false.")
            out[key] = req[key]
    if "judge_timeout_ms" in req:
        if not _whole_in(req["judge_timeout_ms"], *JUDGE_TIMEOUT_MS):
            raise VoiceSettingsError("judge_timeout_ms", f"The time to judge is a whole number of milliseconds from {JUDGE_TIMEOUT_MS[0]} to {JUDGE_TIMEOUT_MS[1]}.",
                                     f"Il tempo per giudicare è un numero intero di millisecondi da {JUDGE_TIMEOUT_MS[0]} a {JUDGE_TIMEOUT_MS[1]}.")
        out["judge_timeout_ms"] = req["judge_timeout_ms"]
    if "duck_level" in req:
        if not _duck_ok(req["duck_level"]):
            raise VoiceSettingsError("duck_level", f"The lowered voice is a number from {DUCK_LEVEL[0]} to {DUCK_LEVEL[1]} (the share of the normal volume).",
                                     f"La voce abbassata è un numero da {DUCK_LEVEL[0]} a {DUCK_LEVEL[1]} (la parte del volume normale).")
        out["duck_level"] = float(req["duck_level"])
    return out


def set_interruptions(req) -> dict:
    changes = check_interruptions(req)
    with _lock:
        data = _read()
        stored = data.get("interruptions") if isinstance(data.get("interruptions"), dict) else {}
        stored.update(changes)
        data["interruptions"] = stored
        _write(data)
    return interruptions()
