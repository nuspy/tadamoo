"""TaDaMoo's voice settings (`voice.json`) turned into what the voice service uses.

The file is the one serve/voice_settings.py writes:
    {"models_dir": "<absolute path>", "services": {"<service>": {"selected": "<model id>", "device": "auto|cpu|cuda:N", "memory": "vram|ram",
                                                                  "enabled": true|false}},
     "brain": {"mode": "direct|harness", "persona": "<slug>", "workspace": "<folder>",
               "base_url": "http://127.0.0.1:<port>", "hotkeys": true|false},
     "telephony": {"transport": "off|auto|gsm|android", "outgoing_approval": "manual|auto", "ai_disclosure_on_start": true|false,
                   "incoming_auto_answer": true|false, "on_behalf_of": "<name>"},
     "meetings": {"speak": true|false, "camera": true|false, "proactive": true|false, "consent_mode": "window|explicit", "consent_window_s": 20..300, "on_behalf_of": "<name>",
                  "display_name": "<name>", "virtual_mic": "<part of a device name>", "wake_phrase": "<phrase>", "autojoin": true|false, "allow_programs_join": true|false,
                  "copilot_replies": 1..6, "copilot_audio_reply": true|false, "copilot_hide_from_capture": true|false},
     "avatar": {"virtual_camera": true|false, "enhance": {"upscale": "none|basic|rtx_video", "scale": 2, "interpolate": "none|basic", "factor": 2,
                "worker_fps_cap": 0, "worker_height_cap": 0, "rtx_video_path": "<DLL>"},
                "engines": {"pc|web|android|esp32": "<engine id>"}, "resolution": {"pc|web|android|esp32": <height>}, "fps_hint": 5..60},
     "interruptions": {"enabled": true|false, "judge_timeout_ms": 200..5000, "duck_level": 0..0.9, "resume_offer": true|false}}
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
from callbridge.call_policy import APPROVAL_MODES, TRANSPORT_MODES
from callbridge.meetbot import consent
from callbridge.enhance import settings as enhance_settings
from callbridge.interruption import Interruptions
from voice.avatar_engines import selection as engine_selection

_DEVICE_RE = re.compile(r"^(auto|cpu|cuda:[0-9]{1,2})$")
BRAIN_MODES = ("direct", "harness")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
_BASE_URL_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}/?$")
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
    enabled: bool | None = None   # the stored boolean, None when the file has none (see is_enabled)

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
class Brain:
    """How the voice talks to TaDaMoo: which brain answers by default, as which personality, and where the server is. The permission level of
    the voice is the server's own: it applies it to every turn the voice sends."""
    mode: str = "direct"
    persona: str = ""
    workspace: str = ""
    base_url: str = ""          # TaDaMoo's address on this PC; "" keeps the config's default
    hotkeys: bool = False       # whether the service registers the global push-to-talk hotkeys


@dataclass(frozen=True)
class Telephony:
    """How calls are placed and answered. Everything starts off or on the careful side: telephony off, the agent's calls asked, the AI
    said first, no call answered by itself."""
    transport: str = "off"
    outgoing_approval: str = "manual"
    ai_disclosure_on_start: bool = True
    incoming_auto_answer: bool = False
    on_behalf_of: str = ""


@dataclass(frozen=True)
class Meetings:
    """How the meeting bot and the copilot behave. Everything starts on the careful side: the bot does not speak, answers only when addressed, asks for
    consent with a window, and the calendar does not join by itself."""
    speak: bool = False
    camera: bool = False
    proactive: bool = False
    consent_mode: str = "window"
    consent_window_s: int = consent.WINDOW_DEFAULT_S
    on_behalf_of: str = ""
    display_name: str = "TaDaMoo AI assistant"
    virtual_mic: str = ""
    wake_phrase: str = ""
    autojoin: bool = False
    copilot_replies: int = 3
    copilot_audio_reply: bool = False
    copilot_hide_from_capture: bool = True


@dataclass(frozen=True)
class Avatar:
    """What the person chose about the avatar's output besides the browser: the virtual camera, off until they turn it on, and the acceleration of
    the video (everything off until they turn it on)."""
    virtual_camera: bool = False
    enhance: enhance_settings.EnhanceSettings = enhance_settings.EnhanceSettings()
    engines: engine_selection.Selection = engine_selection.parse(None)


@dataclass(frozen=True)
class Settings:
    models_dir: str
    services: dict[str, Choice]
    brain: Brain = Brain()
    telephony: Telephony = Telephony()
    audit_file: str = ""
    avatar: Avatar = Avatar()
    avatars_dir: str = ""
    meetings: Meetings = Meetings()
    meetings_audit_file: str = ""
    interruptions: Interruptions = Interruptions()


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
    enabled = stored.get("enabled")
    return Choice(service, model, device, memory, enabled if isinstance(enabled, bool) else None)


def _brain(stored) -> Brain:
    stored = stored if isinstance(stored, dict) else {}
    mode, persona = stored.get("mode"), stored.get("persona")
    workspace, base_url, hotkeys = stored.get("workspace"), stored.get("base_url"), stored.get("hotkeys")
    return Brain(mode if mode in BRAIN_MODES else "direct",
                 persona if isinstance(persona, str) and _SLUG_RE.match(persona) else "",
                 workspace if isinstance(workspace, str) and os.path.isabs(workspace) else "",
                 base_url.rstrip("/") if isinstance(base_url, str) and _BASE_URL_RE.match(base_url) else "",
                 hotkeys is True)


def _telephony(stored) -> Telephony:
    stored = stored if isinstance(stored, dict) else {}
    transport, approval, who = stored.get("transport"), stored.get("outgoing_approval"), stored.get("on_behalf_of")
    return Telephony(transport if transport in TRANSPORT_MODES else "off",
                     approval if approval in APPROVAL_MODES else "manual",
                     stored.get("ai_disclosure_on_start") is not False,
                     stored.get("incoming_auto_answer") is True,
                     who.strip()[:80] if isinstance(who, str) and not any(ord(c) < 32 for c in who) else "")


def _text(value, limit: int, default: str = "") -> str:
    return value.strip()[:limit] if isinstance(value, str) and not any(ord(c) < 32 for c in value) else default


def _meetings(stored) -> Meetings:
    stored = stored if isinstance(stored, dict) else {}
    replies = stored.get("copilot_replies")
    return Meetings(stored.get("speak") is True, stored.get("camera") is True, stored.get("proactive") is True,
                    stored.get("consent_mode") if stored.get("consent_mode") in consent.MODES else "window", consent.window_of(stored.get("consent_window_s")),
                    _text(stored.get("on_behalf_of"), 80), _text(stored.get("display_name"), 40, Meetings.display_name) or Meetings.display_name,
                    _text(stored.get("virtual_mic"), 80), _text(stored.get("wake_phrase"), 40), stored.get("autojoin") is True,
                    replies if isinstance(replies, int) and not isinstance(replies, bool) and 1 <= replies <= 6 else 3,
                    stored.get("copilot_audio_reply") is True, stored.get("copilot_hide_from_capture") is not False)


def _avatar(stored) -> Avatar:
    stored = stored if isinstance(stored, dict) else {}
    return Avatar(stored.get("virtual_camera") is True, enhance_settings.parse(stored.get("enhance")), engine_selection.parse(stored))


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
    return Settings(models_dir, {s: _choice(s, stored.get(s)) for s in ms.SERVICES}, _brain(data.get("brain")), _telephony(data.get("telephony")),
                    str(Path(path).resolve().parent / "call-audit.jsonl"), _avatar(data.get("avatar")),
                    str(Path(path).resolve().parent / "voice" / "avatars"),
                    _meetings(data.get("meetings")), str(Path(path).resolve().parent / "meetings-audit.jsonl"), Interruptions.parse(data.get("interruptions")))


def is_enabled(choice: Choice, models_dir: str) -> bool:
    """Whether the function is loaded with the others: the stored boolean, else on for an installed STT only (read when asked, so a model
    installed after the file was read counts)."""
    if choice.enabled is not None:
        return choice.enabled
    return choice.service == "stt" and ms.is_installed(choice.model, models_dir)


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
    b = settings.brain
    out["brain_mode"], out["brain_persona"], out["brain_workspace"] = b.mode, b.persona, b.workspace
    if b.base_url:
        out["tadamoo_base_url"] = b.base_url
    t = settings.telephony
    out["call_transport"], out["outgoing_approval"], out["ai_disclosure_on_start"] = t.transport, t.outgoing_approval, t.ai_disclosure_on_start
    out["incoming_auto_answer"], out["call_on_behalf_of"], out["call_audit_file"] = t.incoming_auto_answer, t.on_behalf_of, settings.audit_file
    out["avatar_enabled"] = out["avatar_preload"] = is_enabled(settings.services["avatar"], root)       # the worker starts with the function, not otherwise
    out["avatar_virtual_camera"], out["avatars_dir"] = settings.avatar.virtual_camera, settings.avatars_dir
    out["avatar_enhance"] = settings.avatar.enhance.as_dict()
    out["avatar_engines"], out["avatar_resolution"] = dict(settings.avatar.engines.engines), dict(settings.avatar.engines.resolution)
    out["avatar_fps_hint"] = settings.avatar.engines.fps_hint
    i = settings.interruptions
    out.update(interruptions_enabled=i.enabled, interruption_judge_timeout_ms=i.judge_timeout_ms, interruption_duck_level=i.duck_level,
               interruption_resume_offer=i.resume_offer)
    m = settings.meetings
    out.update(meetings_speak=m.speak, meetings_camera=m.camera, meetings_proactive=m.proactive, meetings_consent_mode=m.consent_mode, meetings_consent_window_s=m.consent_window_s,
               meetings_on_behalf_of=m.on_behalf_of, meetings_display_name=m.display_name, meetings_virtual_mic=m.virtual_mic, meetings_wake_phrase=m.wake_phrase,
               meetings_autojoin=m.autojoin, meetings_audit_file=settings.meetings_audit_file, copilot_num_responses=m.copilot_replies,
               copilot_audio_reply=m.copilot_audio_reply, copilot_hide_from_capture=m.copilot_hide_from_capture)
    return out


def helper_env(settings: Settings) -> dict[str, dict[str, str]]:
    """Per service, the variables its helper processes get: the GPU the person chose (the helper then sees that GPU as cuda:0)."""
    env: dict[str, dict[str, str]] = {}
    for service, choice in settings.services.items():
        if choice.cuda_index is not None and not choice.on_cpu:
            env[service] = {"CUDA_VISIBLE_DEVICES": str(choice.cuda_index)}
    return env


_TTS_FIELDS = ("tts_engine", "qwen3tts_size", "kokoro_model_dir", "piper_model_dir")


def tts_snapshot(config) -> dict:
    """What decides how the TTS engines run: the chosen engine, each engine's placement, the model folders and the GPU the helpers get."""
    snap = {k: getattr(config, k, None) for k in _TTS_FIELDS}
    for engine, prefix in _TTS_KEYS.items():
        snap[engine] = (getattr(config, f"{prefix}_device", None), getattr(config, f"{prefix}_gpu_memory", None))
    snap["gpu"] = dict(ms._service_env.get("tts", {}))
    return snap


def tts_changes(before: dict, after: dict) -> tuple[set | None, bool]:
    """(the engines whose settings changed, None for all of them; whether the GPU of the helpers changed)."""
    if before["gpu"] != after["gpu"]:
        return None, True
    if before["kokoro_model_dir"] != after["kokoro_model_dir"] or before["piper_model_dir"] != after["piper_model_dir"]:
        return None, False
    changed = {e for e in _TTS_KEYS if before[e] != after[e]}
    if before["qwen3tts_size"] != after["qwen3tts_size"]:
        changed.add("qwen3tts")
    return changed, False


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
