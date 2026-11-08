"""TaDaMoo's voice settings (`voice.json`) turned into what the voice service uses.

The file is the one serve/voice_settings.py writes:
    {"models_dir": "<absolute path>", "services": {"<service>": {"selected": "<model id>", "device": "auto|cpu|cuda:N", "memory": "vram|ram"}}}
This module reads it without importing `serve` or `tadamoo` (the service has its own interpreter). A missing, unreadable or damaged file,
or a bad value in it, gives the defaults: the service never fails because of its settings.

`overrides()` maps the choices onto callbridge `Config` fields; `helper_env()` carries the GPU number to the helper processes."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from callbridge import models_store as ms

_DEVICE_RE = re.compile(r"^(auto|cpu|cuda:[0-9]{1,2})$")
MEMORY = ("vram", "ram")
# Config field prefix of each TTS engine's (device, memory) pair.
_TTS_KEYS = {"kokoro": "tts", "piper": "piper", "chatterbox": "chatterbox", "xtts": "xtts", "cosyvoice": "cosyvoice",
             "qwen3tts": "qwen3tts", "orpheus": "orpheus", "dia": "dia"}


@dataclass(frozen=True)
class Choice:
    service: str
    model: str            # the catalog id, "" when the service has none
    device: str           # auto | cpu | cuda:N
    memory: str           # vram | ram

    @property
    def engine(self) -> str:
        return ms.MODELS[self.model][1] if self.model in ms.MODELS else ""

    @property
    def cuda_index(self) -> int | None:
        return int(self.device[5:]) if self.device.startswith("cuda:") else None

    @property
    def on_cpu(self) -> bool:
        return self.device == "cpu" or self.memory == "ram"


@dataclass(frozen=True)
class Settings:
    models_dir: str
    services: dict[str, Choice]


def _read(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _choice(service: str, stored) -> Choice:
    stored = stored if isinstance(stored, dict) else {}
    model = stored.get("selected")
    if not (isinstance(model, str) and model in ms.MODELS and ms.MODELS[model][0] == service):
        model = ms.DEFAULTS.get(service, "")
    device, memory = stored.get("device"), stored.get("memory")
    if not (isinstance(device, str) and _DEVICE_RE.match(device)):
        device = "cpu" if service in ms.CPU_SERVICES else "auto"
    if memory not in MEMORY:
        memory = "ram" if device == "cpu" else "vram"
    if device == "cpu" and memory == "vram":
        memory = "ram"
    return Choice(service, model, device, memory)


def load(path, default_models_dir: str = "") -> Settings:
    """The settings in `path`; a value that is missing or invalid takes its default.

    The models folder is the file's `models_dir` when it is an absolute path, else `default_models_dir`, else `voice-models` next to the file."""
    data = _read(path)
    chosen = data.get("models_dir")
    if isinstance(chosen, str) and os.path.isabs(chosen):
        models_dir = chosen
    else:
        models_dir = default_models_dir or str(Path(path).resolve().parent / "voice-models")
    stored = data.get("services") if isinstance(data.get("services"), dict) else {}
    return Settings(models_dir, {s: _choice(s, stored.get(s)) for s in ms.SERVICES})


def _config_device(choice: Choice) -> str:
    return "cpu" if choice.on_cpu else "cuda"


def _config_memory(choice: Choice) -> str:
    return "shared" if choice.memory == "ram" else "dedicated"


def overrides(settings: Settings) -> dict:
    """The `Config` fields these settings set. Only fields that exist in `Config` are used by the caller."""
    root = settings.models_dir
    out: dict = {}
    stt = settings.services["stt"]
    if stt.model in ms.MODELS:
        out["stt_model"] = stt.model.removeprefix("whisper-")
    out["stt_device"], out["stt_gpu_memory"] = _config_device(stt), _config_memory(stt)
    tts = settings.services["tts"]
    if tts.engine in _TTS_KEYS:
        out["tts_engine"] = tts.engine
        prefix = _TTS_KEYS[tts.engine]
        out[f"{prefix}_device"], out[f"{prefix}_gpu_memory"] = _config_device(tts), _config_memory(tts)
        if tts.engine == "qwen3tts":
            out["qwen3tts_size"] = tts.model.removeprefix("qwen3tts-").upper()
    out["kokoro_model_dir"] = ms.folder("tts", "kokoro", root)
    out["piper_model_dir"] = ms.folder("tts", "piper", root)
    out["diarization_model_dir"] = ms.folder("diarization", "sherpa-diarization", root)
    dia = settings.services["diarization"]
    out["diarization_device"] = _config_device(dia)
    vad = ms.folder("vad", "silero-vad-v4", root)
    out["stt_vad_model"] = os.path.join(vad, "silero_vad_v4.onnx")
    return out


def helper_env(settings: Settings) -> dict[str, dict[str, str]]:
    """Per service, the variables its helper processes get: the GPU the person chose (the helper then sees that GPU as cuda:0)."""
    env: dict[str, dict[str, str]] = {}
    for service, choice in settings.services.items():
        if choice.cuda_index is not None and not choice.on_cpu:
            env[service] = {"CUDA_VISIBLE_DEVICES": str(choice.cuda_index)}
    return env


def apply(config, settings: Settings) -> dict:
    """Set the overrides on a `Config` (only the fields it has) and give the helper processes their environment; returns what was set."""
    applied = {k: v for k, v in overrides(settings).items() if hasattr(config, k)}
    for k, v in applied.items():
        setattr(config, k, v)
    os.environ[ms.ROOT_ENV] = settings.models_dir
    for service in ms.SERVICES:
        ms.set_service_env(service, {})
    for service, env in helper_env(settings).items():
        ms.set_service_env(service, env)
    return applied
