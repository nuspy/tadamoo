"""The voice models TaDaMoo can install on request: speech to text, speech synthesis, avatar, diarization, voice activity, embedding.

Every entry was read from the voice service's own code (voice/callbridge/engines, voice/avatar) and from the download script of the
original project. What that code
does not state is not guessed: the entry carries `verify` (the names of the fields to check) and a download is refused until its
sources are complete. A Hugging Face source stays at revision "main"; serve/downloads.py resolves the commit and takes each file's size
and SHA-256 from Hugging Face when a download is planned. A URL source needs a fixed SHA-256. Sizes here are for display only.

An entry is installed under <voice models folder>/<service>/<model id>/ (serve/voice_settings.py). A finished download leaves a
marker file listing what it wrote; a model put there by hand is recognised by its `probe` files."""
from __future__ import annotations

import json
import os
import re

SERVICES = ("stt", "tts", "avatar", "diarization", "vad", "embedding")
DEFAULTS = {"stt": "whisper-small", "tts": "kokoro", "avatar": "musetalk-v15", "diarization": "sherpa-diarization", "vad": "silero-vad-v4",
            "embedding": ""}
MARKER = ".basalt-model.json"
GIB = 2 ** 30
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,40}$")


def _t(en: str, it: str) -> dict:
    return {"en": en, "it": it}


LICENSES = {
    "mit": {"name": "MIT", "url": "",
            "en": "Free to use, change and share, commercial use included, as long as the copyright notice stays. No warranty.",
            "it": "Uso, modifica e condivisione liberi, anche commerciali, purché resti l'avviso di copyright. Nessuna garanzia."},
    "apache-2.0": {"name": "Apache License 2.0", "url": "",
                   "en": "Free to use, change and share, commercial use included, with the notices and the patent terms of the license. No warranty.",
                   "it": "Uso, modifica e condivisione liberi, anche commerciali, con gli avvisi e le clausole sui brevetti della licenza. Nessuna garanzia."},
    "cpml-1.0": {"name": "Coqui Public Model License 1.0", "url": "",
                 "en": "Non-commercial use only. Coqui has closed and there is no commercial license to buy. Use it for personal and research "
                       "work only. This is a summary, not legal advice: the license text governs.",
                 "it": "Solo uso non commerciale. Coqui ha chiuso e non esiste una licenza commerciale da acquistare. Usalo solo per lavoro "
                       "personale e di ricerca. È un riassunto, non una consulenza legale: fa fede il testo della licenza."},
    "musetalk": {"name": "MuseTalk: MIT code, weights free for any purpose", "url": "",
                 "en": "MuseTalk's code is MIT and its trained weights may be used for any purpose, commercial use included. The test data of the "
                       "project is not. The third-party models it relies on (Whisper, sd-vae, DWPose, face parsing, SyncNet) each follow "
                       "their own license. This is a summary, not legal advice: the license texts govern.",
                 "it": "Il codice di MuseTalk è MIT e i suoi pesi addestrati si possono usare per qualunque scopo, anche commerciale. I dati di "
                       "prova del progetto no. I modelli di terzi che usa (Whisper, sd-vae, DWPose, face parsing, SyncNet) seguono ciascuno la "
                       "propria licenza. È un riassunto, non una consulenza legale: fanno fede i testi delle licenze."},
    "verify": {"name": "License not verified", "url": "",
               "en": "The license of this model has not been checked yet. Read the model's page on its source before you use it, "
                     "and do not rely on this download for any use the license may forbid.",
               "it": "La licenza di questo modello non è ancora stata verificata. Leggi la pagina del modello alla sua fonte prima di usarlo e "
                     "non fare affidamento su questo download per usi che la licenza potrebbe vietare."},
}

# Whisper checkpoints as openai-whisper 20250625 names them: the SHA-256 is the folder of each address.
_WHISPER_HOST = "https://openaipublic.azureedge.net/main/whisper/models/"
_WHISPER = {
    "tiny": "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
    "base": "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e",
    "small": "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794",
    "medium": "345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1",
    "large-v3": "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb",
}
_WHISPER_BYTES = {"large-v3": 2_900_000_000}          # the original download script: "~2,9 GB"
_WHISPER_VENV = _t("Runs inside the voice service with torch (openai-whisper); a CUDA build of torch is needed to use the GPU.",
                   "Gira dentro il servizio vocale con torch (openai-whisper); per usare la GPU serve la build CUDA di torch.")

_KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
_CAMPP = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
_SEG_DIR = "sherpa-onnx-pyannote-segmentation-3-0"


def _hf(repo: str, files=None, subdir: str = "") -> dict:
    return {"type": "hf", "repo": repo, "revision": "main", "files": files, "subdir": subdir}


def _url(url: str, name: str, sha256: str | None = None, size: int | None = None, archive: str = "") -> dict:
    return {"type": "url", "url": url, "name": name, "sha256": sha256, "size": size, "archive": archive}


def _entry(id_: str, service: str, engine: str, name: str, family: str, license_: str, sources: list, *, size=None, languages=(), notes=None,
           venv=None, verify=(), noncommercial=False, probe=()) -> dict:
    return {"id": id_, "service": service, "engine": engine, "name": name, "family": family, "size": size, "license": license_,
            "noncommercial": noncommercial, "sources": sources, "languages": list(languages), "notes": notes or _t("", ""),
            "venv": venv or _t("", ""), "verify": tuple(verify), "probe": list(probe)}


def _whisper(size: str) -> dict:
    name = size + ".pt"
    unknown = size not in _WHISPER_BYTES
    return _entry(f"whisper-{size}", "stt", "whisper", f"Whisper {size}", "whisper", "verify",
                  [_url(_WHISPER_HOST + _WHISPER[size] + "/" + name, name, _WHISPER[size])],
                  size=_WHISPER_BYTES.get(size), venv=_WHISPER_VENV, verify=("license",) + (("size",) if unknown else ()), probe=[name],
                  notes=_t("OpenAI's checkpoint of the " + size + " size, the one the voice service loads by name.",
                           "Il checkpoint OpenAI della taglia " + size + ", quello che il servizio vocale carica per nome."))


def _qwen(size: str) -> dict:
    return _entry(f"qwen3tts-{size.lower()}", "tts", "qwen3tts", f"Qwen3-TTS {size}", "qwen3-tts", "apache-2.0",
                  [_hf(f"Qwen/Qwen3-TTS-12Hz-{size}-Base", subdir="base"), _hf(f"Qwen/Qwen3-TTS-12Hz-{size}-CustomVoice", subdir="custom-voice")],
                  languages=("it", "en", "fr", "de", "es", "pt", "ru", "ja", "ko", "zh"), verify=("size", "license-card"),
                  venv=_t("Own Python environment (tts-venvs/dia, shared with Dia), torch with CUDA.",
                          "Ambiente Python proprio (tts-venvs/dia, condiviso con Dia), torch con CUDA."),
                  notes=_t("Voice cloning (the Base model) and nine preset voices (the CustomVoice model); both are installed. Streaming.",
                           "Clonazione di voce (modello Base) e nove voci preimpostate (modello CustomVoice); si installano entrambi. Streaming."))


CATALOG = [_whisper(s) for s in _WHISPER] + [
    _entry("kokoro", "tts", "kokoro", "Kokoro", "kokoro", "verify",
           [_url(_KOKORO_BASE + "kokoro-v1.0.fp16.onnx", "kokoro-v1.0.fp16.onnx"), _url(_KOKORO_BASE + "voices-v1.0.bin", "voices-v1.0.bin")],
           size=340_000_000, languages=("it", "en", "fr", "es", "pt", "ja", "zh", "hi"), verify=("license", "sha256"),
           probe=["kokoro-v1.0.fp16.onnx", "voices-v1.0.bin"],
           venv=_t("Runs inside the voice service with onnxruntime; no extra environment.",
                   "Gira dentro il servizio vocale con onnxruntime; nessun ambiente in più."),
           notes=_t("The fastest engine, 17 fixed voices in 8 languages, Italian included.",
                    "Il motore più veloce, 17 voci fisse in 8 lingue, italiano compreso.")),
    _entry("piper", "tts", "piper", "Piper", "piper", "verify", [], languages=(), verify=("license", "source", "size"),
           venv=_t("Runs inside the voice service with piper-tts; no extra environment.",
                   "Gira dentro il servizio vocale con piper-tts; nessun ambiente in più."),
           notes=_t("One .onnx file per voice from the rhasspy/piper-voices repository, for languages Kokoro lacks (Hungarian, for one). The "
                    "files of each voice are not listed yet, so this entry cannot be downloaded.",
                    "Un file .onnx per voce dal repository rhasspy/piper-voices, per le lingue che Kokoro non ha (per esempio l'ungherese). "
                    "I file di ogni voce non sono ancora elencati, quindi questa voce non si può scaricare.")),
    _entry("chatterbox", "tts", "chatterbox", "Chatterbox Multilingual", "chatterbox", "mit", [], verify=("source", "size"),
           languages=("it", "en", "fr", "es", "ru", "de", "pt"),
           venv=_t("Own Python environment (chatterbox-venv with the chatterbox-tts package), torch with CUDA.",
                   "Ambiente Python proprio (chatterbox-venv con il pacchetto chatterbox-tts), torch con CUDA."),
           notes=_t("Voice cloning, high quality, slow. The package fetches its own weights; their repository is not named in the voice "
                    "code, so this entry cannot be downloaded yet.",
                    "Clonazione di voce, qualità alta, lento. Il pacchetto scarica da sé i suoi pesi; il loro repository non è nominato nel "
                    "codice vocale, quindi questa voce non si può ancora scaricare.")),
    _entry("xtts-v2", "tts", "xtts", "XTTS v2", "xtts", "cpml-1.0", [_hf("coqui/XTTS-v2")], size=1_800_000_000, noncommercial=True,
           languages=("it", "en", "fr", "es", "ru", "de", "pt", "pl", "tr", "nl", "cs", "ar", "zh", "ja", "ko", "hu", "hi"),
           venv=_t("Own Python environment (tts-venvs/xtts), torch with CUDA.", "Ambiente Python proprio (tts-venvs/xtts), torch con CUDA."),
           notes=_t("Zero-shot voice cloning, 17 languages and 58 built-in speakers. Non-commercial use only.",
                    "Clonazione di voce zero-shot, 17 lingue e 58 voci integrate. Solo uso non commerciale.")),
    _entry("cosyvoice-3", "tts", "cosyvoice", "CosyVoice 3 (0.5B)", "cosyvoice", "apache-2.0", [_hf("FunAudioLLM/Fun-CosyVoice3-0.5B-2512")],
           languages=("it", "en", "fr", "es", "ru", "de", "ja", "ko", "zh"), verify=("size",),
           venv=_t("Own Python environment (tts-venvs/cosyvoice), torch with CUDA.", "Ambiente Python proprio (tts-venvs/cosyvoice), torch con CUDA."),
           notes=_t("Voice cloning, streaming; the best rendering of Italian of the engines here. No Hungarian.",
                    "Clonazione di voce, streaming; la miglior resa dell'italiano tra i motori qui. Niente ungherese.")),
    _qwen("0.6B"), _qwen("1.7B"),
    _entry("orpheus-3b", "tts", "orpheus", "Orpheus 3B", "orpheus", "verify", [_hf("unsloth/orpheus-3b-0.1-ft"), _hf("hubertsiuzdak/snac_24khz", subdir="snac_24khz")],
           languages=("en",), verify=("license", "size"),
           venv=_t("Own Python environment (tts-venvs/orpheus), torch with CUDA.", "Ambiente Python proprio (tts-venvs/orpheus), torch con CUDA."),
           notes=_t("Eight fixed voices and expressive tags such as <laugh>; English only. It also needs the SNAC audio codec, installed with it.",
                    "Otto voci fisse e tag espressivi come <laugh>; solo inglese. Serve anche il codec audio SNAC, installato insieme.")),
    _entry("dia-1.6b", "tts", "dia", "Dia 1.6B", "dia", "verify", [_hf("nari-labs/Dia-1.6B-0626")], languages=("en",), verify=("license", "size"),
           venv=_t("Own Python environment (tts-venvs/dia, shared with Qwen3-TTS), torch with CUDA.",
                   "Ambiente Python proprio (tts-venvs/dia, condiviso con Qwen3-TTS), torch con CUDA."),
           notes=_t("Dialogue with non-verbal markers; English only.", "Dialogo con marcatori non verbali; solo inglese.")),
    _entry("musetalk-v15", "avatar", "musetalk", "MuseTalk v1.5 weights", "musetalk", "musetalk",
           [_hf("TMElyralab/MuseTalk", ["musetalkV15/musetalk.json", "musetalkV15/unet.pth"]),
            _hf("stabilityai/sd-vae-ft-mse", ["config.json", "diffusion_pytorch_model.bin"], "sd-vae"),
            _hf("openai/whisper-tiny", ["config.json", "pytorch_model.bin", "preprocessor_config.json"], "whisper"),
            _hf("yzd-v/DWPose", ["dw-ll_ucoco_384.pth"], "dwpose"),
            _hf("ByteDance/LatentSync", ["latentsync_syncnet.pt"], "syncnet")],
           size=7_000_000_000, verify=("size", "face-parse"),
           venv=_t("Own Python environment (avatar venv: Python 3.10, torch with CUDA, mmcv built from source).",
                   "Ambiente Python proprio (venv dell'avatar: Python 3.10, torch con CUDA, mmcv compilato dai sorgenti)."),
           notes=_t("The five weight sets the avatar worker loads. The face-parsing weights (a Google Drive file and a torchvision "
                    "ResNet-18 file) have no fixed source and are not part of this download; the worker needs them too.",
                    "I cinque gruppi di pesi che il worker dell'avatar carica. I pesi di face parsing (un file su Google Drive e un file "
                    "ResNet-18 di torchvision) non hanno una fonte fissa e non fanno parte di questo download; servono anche al worker.")),
    _entry("sherpa-diarization", "diarization", "sherpa-onnx", "sherpa-onnx diarization (pyannote segmentation + CAM++)", "sherpa-onnx", "verify",
           [_url(_SHERPA + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", _SEG_DIR + ".tar.bz2", archive="tar.bz2"),
            _url(_SHERPA + "speaker-recongition-models/" + _CAMPP, _CAMPP)],
           size=35_000_000, verify=("license", "sha256"), probe=[_SEG_DIR + "/model.onnx", _CAMPP],
           venv=_t("Runs inside the voice service with the sherpa-onnx package (CPU).", "Gira dentro il servizio vocale con il pacchetto sherpa-onnx (CPU)."),
           notes=_t("Who speaks when: a segmentation model and a speaker embedding model. The segmentation comes as an archive that needs unpacking.",
                    "Chi parla quando: un modello di segmentazione e uno di impronta vocale. La segmentazione è un archivio che va estratto.")),
    _entry("silero-vad-v4", "vad", "silero", "Silero VAD v4", "silero", "verify", [], verify=("license", "source", "size"), probe=["silero_vad_v4.onnx"],
           venv=_t("Runs inside the voice service with onnxruntime.", "Gira dentro il servizio vocale con onnxruntime."),
           notes=_t("Voice activity detection. The voice code names the file but not where it comes from, so this entry cannot be downloaded yet.",
                    "Rilevamento dell'attività vocale. Il codice vocale nomina il file ma non da dove viene, quindi questa voce non si può ancora scaricare.")),
]
# No embedding model is named in the voice code (it asks the LLM server for embeddings): the service has no entry yet.
BY_ID = {e["id"]: e for e in CATALOG}
assert len(BY_ID) == len(CATALOG) and all(_ID_RE.match(e["id"]) and e["service"] in SERVICES for e in CATALOG)


def find(model_id: str) -> dict | None:
    return BY_ID.get(model_id) if isinstance(model_id, str) else None


def models_of(service: str) -> list[dict]:
    return [e for e in CATALOG if e["service"] == service]


def license_of(entry: dict) -> dict:
    return LICENSES[entry["license"]]


def folder_of(entry: dict) -> str:
    return f"{entry['service']}/{entry['id']}"


def downloadable(entry: dict) -> tuple[bool, str]:
    """(True, "") when every source is complete, else (False, why): no source, a URL without a SHA-256, an archive."""
    if not entry["sources"]:
        return False, "no_source"
    for s in entry["sources"]:
        if s["type"] == "url":
            if not s["sha256"]:
                return False, "no_sha256"
            if s["archive"]:
                return False, "archive"
    return True, ""


_WHY = {"no_source": _t("the files of this model are not listed yet (to verify)", "i file di questo modello non sono ancora elencati (da verificare)"),
        "no_sha256": _t("a file of this model has no fixed SHA-256 yet (to verify)", "un file di questo modello non ha ancora uno SHA-256 fissato (da verificare)"),
        "archive": _t("this model comes as an archive and unpacking is not built yet (to verify)",
                      "questo modello è un archivio e l'estrazione non è ancora costruita (da verificare)")}


def download_entry(model_id: str) -> dict | None:
    """The entry in the shape serve/downloads.py plans from, or None for an id that is not a voice model."""
    e = find(model_id)
    if e is None:
        return None
    ok, why = downloadable(e)
    return {"id": e["id"], "name": e["name"], "kind": "voice", "convert": "ok", "gated": False, "uncensored": False, "repo": "",
            "revision": "main", "files": ["*"], "folder": folder_of(e), "sources": e["sources"], "license_info": license_of(e),
            "noncommercial": e["noncommercial"], "marker": True, "why_not": {"code": why, **_WHY[why]} if not ok else None}


def installed(entry: dict, root: str) -> bool:
    """True when the folder holds what a finished download recorded (every file there, of the recorded size), else when it holds every probe file."""
    folder = os.path.join(root, *folder_of(entry).split("/"))
    try:
        with open(os.path.join(folder, MARKER), encoding="utf-8") as fh:
            files = json.load(fh).get("files")
    except (OSError, ValueError, AttributeError):
        files = None
    if isinstance(files, list) and files:
        try:
            return all(isinstance(f, dict) and os.path.getsize(os.path.join(folder, *str(f["name"]).split("/"))) == f["size"] for f in files)
        except (OSError, KeyError, TypeError):
            return False
    return bool(entry["probe"]) and all(os.path.isfile(os.path.join(folder, *p.split("/"))) for p in entry["probe"])


def public(entry: dict, root: str) -> dict:
    ok, why = downloadable(entry)
    lic = license_of(entry)
    return {"id": entry["id"], "name": entry["name"], "engine": entry["engine"], "family": entry["family"], "size": entry["size"],
            "license": {"id": entry["license"], "name": lic["name"]}, "noncommercial": entry["noncommercial"],
            "installed": installed(entry, root), "verify": bool(entry["verify"]), "verify_fields": list(entry["verify"]),
            "downloadable": ok, "not_downloadable": why, "languages": entry["languages"], "notes": entry["notes"], "venv": entry["venv"]}
