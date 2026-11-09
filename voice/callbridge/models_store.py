"""Where the voice models are read from, and the rule that nothing is ever fetched.

Models live in `<models folder>/<service>/<model id>/`. A finished download (TaDaMoo's Settings > Voice) leaves `.basalt-model.json`
there, listing the files and their sizes; a model put there by hand is recognised by its probe files. The folder comes from
`BASALT_VOICE_MODELS_DIR`, set by the service for itself and for every helper process it starts. A loader that finds its model missing
raises `ModelNotInstalled`; none of them downloads anything.

The ids and probe files below mirror serve/voice_catalog.py (the service runs in its own interpreter and cannot import it); a test
in serve/tests keeps the two equal."""
from __future__ import annotations

import json
import os

ROOT_ENV = "BASALT_VOICE_MODELS_DIR"
MARKER = ".basalt-model.json"
VOICES = "voices"
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}

SERVICES = ("stt", "tts", "avatar", "diarization", "vad", "embedding")
DEFAULTS = {"stt": "whisper-small", "tts": "kokoro", "avatar": "musetalk-v15", "diarization": "sherpa-diarization",
            "vad": "silero-vad-v4", "embedding": ""}
CPU_SERVICES = ("diarization", "vad", "embedding")

_SEG = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
_CAMPP = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
# id -> (service, engine, name, probe files)
MODELS = {
    "whisper-tiny": ("stt", "whisper", "Whisper tiny", ("tiny.pt",)),
    "whisper-base": ("stt", "whisper", "Whisper base", ("base.pt",)),
    "whisper-small": ("stt", "whisper", "Whisper small", ("small.pt",)),
    "whisper-medium": ("stt", "whisper", "Whisper medium", ("medium.pt",)),
    "whisper-large-v3": ("stt", "whisper", "Whisper large-v3", ("large-v3.pt",)),
    "kokoro": ("tts", "kokoro", "Kokoro", ("kokoro-v1.0.fp16.onnx", "voices-v1.0.bin")),
    "piper": ("tts", "piper", "Piper", ()),
    "chatterbox": ("tts", "chatterbox", "Chatterbox Multilingual", ()),
    "xtts-v2": ("tts", "xtts", "XTTS v2", ()),
    "cosyvoice-3": ("tts", "cosyvoice", "CosyVoice 3 (0.5B)", ()),
    "qwen3tts-0.6b": ("tts", "qwen3tts", "Qwen3-TTS 0.6B", ()),
    "qwen3tts-1.7b": ("tts", "qwen3tts", "Qwen3-TTS 1.7B", ()),
    "orpheus-3b": ("tts", "orpheus", "Orpheus 3B", ()),
    "dia-1.6b": ("tts", "dia", "Dia 1.6B", ()),
    "musetalk-v15": ("avatar", "musetalk", "MuseTalk v1.5 weights", ()),
    "wav2lip-int8": ("avatar", "wav2lip", "Wav2Lip INT8 (for the phones)", ("wav2lip_int8.onnx",)),
    "ultralight-wenet-encoder": ("avatar", "ultralight", "Ultralight wenet audio encoder", ("encoder.onnx",)),
    "engine-landmarks": ("avatar", "ultralight", "Face detector and landmark weights", ("scrfd_2.5g_kps.onnx", "checkpoint_epoch_335.pth.tar")),
    "feathertalk-weights": ("avatar", "feathertalk", "FeatherHuBERT weights", ("feather_hubert.pth",)),
    "wav2lipv2-code": ("avatar", "wav2lipv2", "Wav2Lip v2 code (Linly-Talker)", ("TFG/Wav2Lipv2.py", "src/modelsv2/wav2lip_v2.py")),
    "wav2lipv2-weights": ("avatar", "wav2lipv2", "Wav2Lip v2 288 px weights", ("wav2lipv2.pth",)),
    "wav2lipv2-yolo-face": ("avatar", "wav2lipv2", "YOLOv8n-face weights", ("yolov8n-face.pt",)),
    "wav2lipv2-landmarks": ("avatar", "wav2lipv2", "WFLW HRNet18 landmark weights", ("config.yaml", "model.pth")),
    "sadtalker-code": ("avatar", "sadtalker", "SadTalker code (Linly-Talker)", ("TFG/SadTalker.py", "src/config/facerender.yaml")),
    "sadtalker-weights": ("avatar", "sadtalker", "SadTalker 256 px weights", ("SadTalker_V0.0.2_256.safetensors", "mapping_00229-model.pth.tar")),
    "sadtalker-gfpgan": ("avatar", "sadtalker", "SadTalker face detector and aligner weights", ("alignment_WFLW_4HG.pth", "detection_Resnet50_Final.pth")),
    "ernerf-code": ("avatar", "ernerf", "ER-NeRF code (Linly-Talker)", ("TFG/NeRFTalk.py", "NeRF/nerf_triplane/network.py")),
    "ernerf-audio-encoder": ("avatar", "ernerf", "ER-NeRF audio encoder weights", ("audio_visual_encoder.pth",)),
    "ernerf-bfm": ("avatar", "ernerf", "ER-NeRF 3DMM face model files", ("3DMM_info.npy", "keys_info.npy", "topology_info.npy")),
    "ernerf-face-parsing": ("avatar", "ernerf", "ER-NeRF face parsing weights", ("79999_iter.pth",)),
    "sherpa-diarization": ("diarization", "sherpa-onnx", "sherpa-onnx diarization", (_SEG, _CAMPP)),
    "silero-vad-v4": ("vad", "silero", "Silero VAD v4", ("silero_vad_v4.onnx",)),
}

# Where an engine pack goes when it is not `<service>/<id>` (serve/voice_catalog.py `folder`).
FOLDERS = {
    "ultralight-wenet-encoder": "avatar-engines/ultralight/weights",
    "engine-landmarks": "avatar-engines/landmarks",
    "feathertalk-weights": "avatar-engines/feathertalk/weights",
    "wav2lipv2-code": "avatar-engines/wav2lipv2/code",
    "wav2lipv2-weights": "avatar-engines/wav2lipv2/checkpoints",
    "wav2lipv2-yolo-face": "avatar-engines/wav2lipv2/models/yolov8n-face",
    "wav2lipv2-landmarks": "avatar-engines/wav2lipv2/models/wflw/hrnet18_256x256_p1",
    "sadtalker-code": "avatar-engines/sadtalker/code",
    "sadtalker-weights": "avatar-engines/sadtalker/checkpoints",
    "sadtalker-gfpgan": "avatar-engines/sadtalker/code/gfpgan/weights",
    "ernerf-code": "avatar-engines/ernerf/code",
    "ernerf-audio-encoder": "avatar-engines/ernerf/code/checkpoints",
    "ernerf-bfm": "avatar-engines/ernerf/code/NeRF/data_utils/face_tracking/3DMM",
    "ernerf-face-parsing": "avatar-engines/ernerf/code/NeRF/data_utils/face_parsing",
}


class ModelNotInstalled(RuntimeError):
    """A model the service was asked to load is not in the models folder."""

    def __init__(self, model: str, service: str = "", detail: str = ""):
        self.model, self.service = model, service
        name = MODELS[model][2] if model in MODELS else model
        text = f"{name} is not installed: install it from Settings > Voice."
        super().__init__(text + (f" ({detail})" if detail else ""))


def root() -> str:
    """The models folder of this process, or "" when none was given."""
    return os.environ.get(ROOT_ENV, "").strip()


def folder(service: str, model_id: str, base: str | None = None) -> str:
    """`<models folder>/<service>/<model id>`, or "" when there is no models folder."""
    base = root() if base is None else base
    if not base:
        return ""
    return os.path.join(base, *FOLDERS[model_id].split("/")) if model_id in FOLDERS else os.path.join(base, service, model_id)


def _probes(model_id: str) -> tuple:
    return MODELS[model_id][3] if model_id in MODELS else ()


def is_installed(model_id: str, base: str | None = None) -> bool:
    """True when the folder holds what a finished download recorded (each file there, of the recorded size), else every probe file."""
    if model_id not in MODELS:
        return False
    where = folder(MODELS[model_id][0], model_id, base)
    if not where:
        return False
    try:
        with open(os.path.join(where, MARKER), encoding="utf-8") as fh:
            files = json.load(fh).get("files")
    except (OSError, ValueError, AttributeError):
        files = None
    if isinstance(files, list) and files:
        try:
            return all(isinstance(f, dict) and os.path.getsize(os.path.join(where, *str(f["name"]).split("/"))) == f["size"] for f in files)
        except (OSError, KeyError, TypeError):
            return False
    probes = _probes(model_id)
    if probes:
        return all(os.path.isfile(os.path.join(where, *p.split("/"))) for p in probes)
    return False


def reason(model_id: str, base: str | None = None) -> str:
    """Why a model counts as missing, in plain words; "" when it is installed."""
    if model_id not in MODELS:
        return "no model is chosen" if not model_id else "this model is not in the catalog"
    if is_installed(model_id, base):
        return ""
    return str(ModelNotInstalled(model_id, MODELS[model_id][0]))


def require(model_id: str, base: str | None = None) -> str:
    """The model's folder, or `ModelNotInstalled`."""
    if not is_installed(model_id, base):
        raise ModelNotInstalled(model_id, MODELS.get(model_id, ("",))[0])
    return folder(MODELS[model_id][0], model_id, base)


def whisper_checkpoint(size: str, base: str | None = None) -> str:
    """The checkpoint file of a Whisper size from the models folder, or `ModelNotInstalled`."""
    return os.path.join(require("whisper-" + size, base), size + ".pt")


def voices_dir(base: str | None = None) -> str:
    """The folder of cloned-voice samples, `<models folder>/tts/voices`; "" when there is no models folder."""
    base = root() if base is None else base
    return os.path.join(base, "tts", VOICES) if base else ""


def piper_voices(base: str | None = None) -> str:
    """The folder of Piper voices; it need not exist, a voice that is missing is reported by the loader."""
    return folder("tts", "piper", base)


def silero_path(base: str | None = None) -> str:
    """The Silero VAD file when it is installed, else ""."""
    where = folder("vad", "silero-vad-v4", base)
    path = os.path.join(where, "silero_vad_v4.onnx") if where else ""
    return path if path and os.path.isfile(path) else ""


# Environment of the helper processes of each service ({service: {name: value}}), set once by the service.
_service_env: dict[str, dict[str, str]] = {}


def set_service_env(service: str, env: dict[str, str]) -> None:
    _service_env[service] = dict(env)


def helper_env(service: str = "", base: dict | None = None) -> dict:
    """The environment for a helper process: offline, the models folder, and the GPU the person chose for that service."""
    env = dict(os.environ if base is None else base)
    env.update(_service_env.get(service, {}))
    if root():
        env[ROOT_ENV] = root()
    env.update(OFFLINE_ENV)
    return env


def apply_offline() -> None:
    """Make this process offline too (the service calls it at start)."""
    os.environ.update(OFFLINE_ENV)
