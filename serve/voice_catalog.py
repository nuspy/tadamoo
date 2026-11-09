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

from serve import voice_engine_weights as packs
from serve import voice_linly_packs as linly

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
    "wav2lip-research": {"name": "Wav2Lip: research and non-commercial use", "url": "",
                         "en": "The original Wav2Lip license allows research and non-commercial use only. Where this file comes from is not declared, "
                               "so its terms are unknown: do not hand it to anyone else or use it commercially before that is settled. "
                               "This is a summary, not legal advice.",
                         "it": "La licenza originale di Wav2Lip consente solo uso di ricerca e non commerciale. Non è dichiarato da dove venga questo file, "
                               "quindi le sue condizioni sono ignote: non darlo ad altri né usarlo commercialmente prima di chiarirlo. "
                               "È un riassunto, non una consulenza legale."},
    "apache-2.0-feathertalk": {"name": "Apache License 2.0 (FeatherTalk's LICENSE file)", "url": "",
                              "en": "FeatherTalk's repository carries the Apache 2.0 text as its LICENSE file (it is in voice/avatar_engines/vendor/feathertalk with the code). The copyright "
                                    "line of that file is the unfilled template, so it names no owner. The license of the weights is not stated apart from the repository's. This is a "
                                    "summary, not legal advice: the license text governs.",
                              "it": "Il repository di FeatherTalk ha come LICENSE il testo dell'Apache 2.0 (sta in voice/avatar_engines/vendor/feathertalk con il codice). La riga del "
                                    "copyright di quel file è il modello non compilato, quindi non nomina un titolare. La licenza dei pesi non è dichiarata oltre a quella del repository. "
                                    "È un riassunto, non una consulenza legale: fa fede il testo della licenza.",
                              "text": packs.FEATHERTALK_LICENSE_TEXT},
    "linly-talker": {"name": "Linly-Talker code (MIT); the Wav2Lip v2 model is non-commercial", "url": "",
                     "en": "Linly-Talker's own files are MIT and its LICENSE file is installed beside the code. The Wav2Lip v2 model derives from Wav2Lip, which allows research and "
                           "non-commercial use only: do not use it commercially. This is a summary, not legal advice.",
                     "it": "I file propri di Linly-Talker sono MIT e il suo file LICENSE si installa accanto al codice. Il modello Wav2Lip v2 deriva da Wav2Lip, che consente solo "
                           "ricerca e uso non commerciale: non usarlo commercialmente. È un riassunto, non una consulenza legale."},
    "ultralytics-face": {"name": "YOLOv8n-face: AGPL-3.0 code, weights licence unclear", "url": "",
                         "en": "The ultralytics package that loads this file is AGPL-3.0 code: running it for yourself is allowed, but offering it over a network or distributing it "
                               "brings the AGPL duties. The licence of the face-detection weights themselves is not stated in anything read here. Verify both before you distribute.",
                         "it": "Il pacchetto ultralytics che carica questo file è codice AGPL-3.0: usarlo per sé è permesso, ma offrirlo in rete o distribuirlo comporta gli obblighi "
                               "della AGPL. La licenza dei pesi di rilevamento del volto non è dichiarata in nulla di ciò che è stato letto qui. Verificare entrambe prima di distribuire."},
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


def _hf(repo: str, files=None, subdir: str = "", strip: str = "") -> dict:
    """`strip` is a prefix taken off each file's path in the repository, so a file lands in the folder of its entry."""
    return {"type": "hf", "repo": repo, "revision": "main", "files": files, "subdir": subdir, "strip": strip}


def _url(url: str, name: str, sha256: str | None = None, size: int | None = None, archive: str = "") -> dict:
    return {"type": "url", "url": url, "name": name, "sha256": sha256, "size": size, "archive": archive}


def _entry(id_: str, service: str, engine: str, name: str, family: str, license_: str, sources: list, *, size=None, languages=(), notes=None,
           venv=None, verify=(), noncommercial=False, probe=(), selectable=True, sha256=None, kind="model", folder="") -> dict:
    """`selectable` is False for a file the phones use, which the function of its service does not load; `sha256` is that of a file whose
    place is known but whose source is not (it is put there by hand). `kind` is "engine-pack" for the code or weights of a personalised
    avatar engine (not a model the function loads); `folder` is where it goes when that is not `<service>/<id>`."""
    return {"id": id_, "service": service, "engine": engine, "name": name, "family": family, "size": size, "license": license_,
            "noncommercial": noncommercial, "sources": sources, "languages": list(languages), "notes": notes or _t("", ""),
            "venv": venv or _t("", ""), "verify": tuple(verify), "probe": list(probe), "selectable": selectable, "sha256": sha256, "kind": kind, "folder": folder}


def _pack(repo: str, commit: str, files) -> list:
    """The URL sources of an engine pack: the raw address of each file at the pinned commit, its blob SHA-256 and size."""
    return [_url(f"{packs.HOST}{repo}/{commit}/{path}", path, sha, size) for path, sha, size in files]


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


_LINLY = "Kedreamix/Linly-Talker"
_LINLY_ENV = _t("Run by the voice service in a Python environment of your own (TADAMOO_AVATAR_ENGINES_PYTHON); nothing here installs it. ",
                "Eseguito dal servizio vocale in un ambiente Python tuo (TADAMOO_AVATAR_ENGINES_PYTHON); qui non si installa nulla. ")


def _linly_pack(id_: str, engine: str, name: str, files, probe, folder: str, need: tuple, notes: dict, **kw) -> dict:
    """The Linly-Talker code an engine runs: the raw address of each file at the pinned commit with its blob SHA-256 and size."""
    return _entry(id_, "avatar", engine, name, engine, "linly-talker", _pack(linly.REPO, linly.COMMIT, files), size=sum(f[2] for f in files), probe=probe,
                  selectable=False, kind="engine-pack", folder=folder, venv=_t(_LINLY_ENV["en"] + need[0], _LINLY_ENV["it"] + need[1]), notes=notes,
                  verify=kw.pop("verify", ()), **kw)


def _linly_weights(id_: str, engine: str, name: str, files, strip: str, probe, folder: str, notes: dict, **kw) -> dict:
    """Weights of the Linly-Talker Hugging Face repository (the one its README names): size and SHA-256 come from Hugging Face when a download is planned."""
    return _entry(id_, "avatar", engine, name, engine, "linly-talker", [_hf(_LINLY, files, strip=strip)], probe=probe, selectable=False, kind="engine-pack",
                  folder=folder, verify=kw.pop("verify", ("source", "size")), venv=_t("Read by the engine's scripts with PyTorch.", "Letto dagli script del motore con PyTorch."),
                  notes=notes, **kw)


def _no_source(id_: str, engine: str, name: str, license_: str, probe, folder: str, notes: dict, **kw) -> dict:
    """A file the code needs and the project names no source for: placed by hand, never downloaded."""
    return _entry(id_, "avatar", engine, name, engine, license_, [], probe=probe, selectable=False, kind="engine-pack", folder=folder,
                  verify=kw.pop("verify", ("source", "sha256", "size", "license")), venv=_t("Read by the engine's scripts.", "Letto dagli script del motore."), notes=notes, **kw)


_GPU_PY = ("PyTorch with CUDA, torchvision, opencv, numpy, scipy, scikit-image, librosa, yacs, tqdm; ffmpeg on the PATH.",
           "PyTorch con CUDA, torchvision, opencv, numpy, scipy, scikit-image, librosa, yacs, tqdm; ffmpeg nel PATH.")
_NOT_LISTED = ("The Linly-Talker README names this repository and lists its checkpoints folder, but its file tree does not list this file: the download plan "
               "stops with a clear error if the repository does not have it.",
               "Il README di Linly-Talker nomina questo repository ed elenca la sua cartella checkpoints, ma il suo albero di file non elenca questo file: il piano di "
               "download si ferma con un errore chiaro se il repository non lo ha.")
ENGINE_PACKS = [
    _linly_pack("wav2lipv2-code", "wav2lipv2", "Wav2Lip v2 code (Linly-Talker, commit dc831b3)", linly.WAV2LIPV2_FILES,
                ["TFG/Wav2Lipv2.py", "src/modelsv2/wav2lip_v2.py"], "avatar-engines/wav2lipv2/code",
                (_GPU_PY[0] + " Also ultralytics (AGPL-3.0).", _GPU_PY[1] + " Anche ultralytics (AGPL-3.0)."), noncommercial=True,
                notes=_t("Linly-Talker's Wav2Lipv2 script and what it imports (the 288 px model, the WFLW landmark net, the face-detection package), at one fixed commit. Non-commercial: "
                         "the model derives from Wav2Lip, which is for research use only.",
                         "Lo script Wav2Lipv2 di Linly-Talker e ciò che importa (il modello a 288 px, la rete di punti del volto WFLW, il pacchetto di rilevamento del volto), a un "
                         "commit fisso. Non commerciale: il modello deriva da Wav2Lip, che è solo per ricerca.")),
    _linly_weights("wav2lipv2-weights", "wav2lipv2", "Wav2Lip v2 288 px weights (wav2lipv2.pth)", ["checkpoints/wav2lipv2.pth"], "checkpoints/", ["wav2lipv2.pth"],
                   "avatar-engines/wav2lipv2/checkpoints", noncommercial=True,
                   notes=_t("The checkpoint the Wav2Lipv2 script loads by default (checkpoints/wav2lipv2.pth), from the Hugging Face repository the README names. " + _NOT_LISTED[0]
                            + " Non-commercial: it derives from Wav2Lip, which is for research use only.",
                            "Il checkpoint che lo script Wav2Lipv2 carica per default (checkpoints/wav2lipv2.pth), dal repository Hugging Face che il README nomina. " + _NOT_LISTED[1]
                            + " Non commerciale: deriva da Wav2Lip, che è solo per ricerca.")),
    _no_source("wav2lipv2-yolo-face", "wav2lipv2", "YOLOv8n-face weights (yolov8n-face.pt)", "ultralytics-face", ["yolov8n-face.pt"],
               "avatar-engines/wav2lipv2/models/yolov8n-face",
               notes=_t("The script loads weights/yolov8n-face/yolov8n-face.pt and neither its README nor its scripts say where the file comes from: put it there by hand. "
                        "The ultralytics code that reads it is AGPL-3.0 and the licence of the weights is not stated.",
                        "Lo script carica weights/yolov8n-face/yolov8n-face.pt e né il README né gli script dicono da dove venga il file: metterlo lì a mano. Il codice "
                        "ultralytics che lo legge è AGPL-3.0 e la licenza dei pesi non è dichiarata.")),
    _no_source("wav2lipv2-landmarks", "wav2lipv2", "WFLW HRNet18 landmark weights (config.yaml, model.pth)", "verify", ["config.yaml", "model.pth"],
               "avatar-engines/wav2lipv2/models/wflw/hrnet18_256x256_p1",
               notes=_t("The script loads weights/wflw/hrnet18_256x256_p1/ (a config.yaml and a model.pth) and nothing in Linly-Talker says where they come from: put them there by "
                        "hand. Without them Wav2Lip v2 cannot start.",
                        "Lo script carica weights/wflw/hrnet18_256x256_p1/ (un config.yaml e un model.pth) e nulla in Linly-Talker dice da dove vengano: metterli lì a mano. "
                        "Senza di essi Wav2Lip v2 non parte.")),
    _linly_pack("sadtalker-code", "sadtalker", "SadTalker code (Linly-Talker, commit dc831b3)", linly.SADTALKER_FILES,
                ["TFG/SadTalker.py", "src/config/facerender.yaml"], "avatar-engines/sadtalker/code",
                (_GPU_PY[0] + " Also safetensors, facexlib, gfpgan, imageio, pydub, kornia, face_alignment.",
                 _GPU_PY[1] + " Anche safetensors, facexlib, gfpgan, imageio, pydub, kornia, face_alignment."), verify=("requirements",),
                notes=_t("Linly-Talker's SadTalker wrapper and what it imports (audio to 3DMM coefficients, the face renderer, the preprocessing), with the configs, at one fixed "
                         "commit. The wrapper's README step scripts/sadtalker_download_models.sh is not in that commit; the weights are listed in the other entries.",
                         "Il wrapper SadTalker di Linly-Talker e ciò che importa (da audio a coefficienti 3DMM, il renderer del volto, la preparazione), con le configurazioni, a un "
                         "commit fisso. Il passo del README del wrapper scripts/sadtalker_download_models.sh non è in quel commit; i pesi sono nelle altre voci.")),
    _linly_weights("sadtalker-weights", "sadtalker", "SadTalker 256 px weights", ["checkpoints/SadTalker_V0.0.2_256.safetensors", "checkpoints/mapping_00229-model.pth.tar"],
                   "checkpoints/", ["SadTalker_V0.0.2_256.safetensors", "mapping_00229-model.pth.tar"], "avatar-engines/sadtalker/checkpoints",
                   notes=_t("The two files the wrapper reads for the 256 px model with the cropped preprocessing, named in the README's tree of the Hugging Face repository.",
                            "I due file che il wrapper legge per il modello a 256 px con il ritaglio, nominati nell'albero del README del repository Hugging Face.")),
    _linly_weights("sadtalker-gfpgan", "sadtalker", "SadTalker face detector and aligner weights", ["gfpgan/weights/alignment_WFLW_4HG.pth", "gfpgan/weights/detection_Resnet50_Final.pth"],
                   "gfpgan/weights/", ["alignment_WFLW_4HG.pth", "detection_Resnet50_Final.pth"], "avatar-engines/sadtalker/code/gfpgan/weights",
                   notes=_t("What the keypoint extractor reads from gfpgan/weights (it would otherwise fetch them from the network), named in the README's tree. They are installed "
                            "inside the code folder because the code looks for them there.",
                            "Ciò che l'estrattore dei punti chiave legge da gfpgan/weights (altrimenti li prenderebbe dalla rete), nominati nell'albero del README. Si installano dentro "
                            "la cartella del codice perché il codice li cerca lì.")),
    _linly_pack("ernerf-code", "ernerf", "ER-NeRF code (Linly-Talker, commit dc831b3)", linly.ERNERF_FILES, ["TFG/NeRFTalk.py", "NeRF/nerf_triplane/network.py"],
                "avatar-engines/ernerf/code",
                ("PyTorch with CUDA, PyTorch3D and the compiled CUDA modules freqencoder, gridencoder, raymarching and shencoder (built on this PC), plus the packages of "
                 "TFG/requirements_nerf.txt; ffmpeg on the PATH. It breaks with the versions of Python, PyTorch, CUDA and the compiler.",
                 "PyTorch con CUDA, PyTorch3D e i moduli CUDA compilati freqencoder, gridencoder, raymarching e shencoder (compilati su questo PC), più i pacchetti di "
                 "TFG/requirements_nerf.txt; ffmpeg nel PATH. Si rompe con le versioni di Python, PyTorch, CUDA e del compilatore."), verify=("requirements",),
                notes=_t("Linly-Talker's NeRF folder (model, renderer, trainer, data preparation and the CUDA sources) and its NeRFTalk wrapper, at one fixed commit. ER-NeRF is trained for each "
                         "person; nothing in this pack is a person's model.",
                         "La cartella NeRF di Linly-Talker (modello, renderer, addestratore, preparazione dei dati e sorgenti CUDA) e il suo wrapper NeRFTalk, a un commit fisso. ER-NeRF si "
                         "addestra per ogni persona; niente in questo pacchetto è il modello di una persona.")),
    _linly_weights("ernerf-audio-encoder", "ernerf", "ER-NeRF audio encoder weights (audio_visual_encoder.pth)", ["checkpoints/audio_visual_encoder.pth"], "checkpoints/",
                   ["audio_visual_encoder.pth"], "avatar-engines/ernerf/code/checkpoints",
                   notes=_t("What NeRFTalk reads from ./checkpoints to turn the speech into features; named in the README's tree of the Hugging Face repository.",
                            "Ciò che NeRFTalk legge da ./checkpoints per trasformare il parlato in feature; nominato nell'albero del README del repository Hugging Face.")),
    _no_source("ernerf-bfm", "ernerf", "ER-NeRF 3DMM face model files", "verify", ["3DMM_info.npy", "keys_info.npy", "topology_info.npy"],
               "avatar-engines/ernerf/code/NeRF/data_utils/face_tracking/3DMM",
               notes=_t("The face tracker of the data preparation reads a 3DMM folder that convert_BFM.py builds from the Basel Face Model (01_MorphableModel.mat, which needs a "
                        "registration with the university). Nothing here can fetch it: build it by hand. Needed only to train a new person.",
                        "Il tracciatore del volto della preparazione dati legge una cartella 3DMM che convert_BFM.py costruisce dal Basel Face Model (01_MorphableModel.mat, che "
                        "richiede una registrazione presso l'università). Qui non si può scaricare: costruirla a mano. Serve solo per addestrare una persona nuova.")),
    _no_source("ernerf-face-parsing", "ernerf", "ER-NeRF face parsing weights (79999_iter.pth)", "verify", ["79999_iter.pth"],
               "avatar-engines/ernerf/code/NeRF/data_utils/face_parsing",
               notes=_t("The default of the data preparation's face-parsing step (data_utils/face_parsing/79999_iter.pth). The README lists a file of the same name for MuseTalk "
                        "but does not say it is the same one: no source is stated here. Needed only to train a new person.",
                        "Il valore predefinito del passo di face parsing della preparazione dati (data_utils/face_parsing/79999_iter.pth). Il README elenca un file con lo stesso nome "
                        "per MuseTalk ma non dice che sia lo stesso: qui non è dichiarata una fonte. Serve solo per addestrare una persona nuova.")),
]

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
    _entry("wav2lip-int8", "avatar", "wav2lip", "Wav2Lip INT8 (for the phones)", "wav2lip", "wav2lip-research", [], size=36_664_106,
           noncommercial=True, verify=("license", "source"), probe=["wav2lip_int8.onnx"], selectable=False,
           sha256="2e297b310dcd397ac94e467814570eaf18edec322c29abd14565dcd3b6896f97",
           venv=_t("Not run on the PC: the phone runs it with ONNX Runtime.", "Non gira sul PC: lo fa il telefono con ONNX Runtime."),
           notes=_t("The lip-sync model the Android app downloads from this PC (GET /avatar/models/wav2lip.onnx). It is put in the folder by hand "
                    "(or by the importer): nothing downloads it, because its source is not known.",
                    "Il modello di lip-sync che l'app Android scarica da questo PC (GET /avatar/models/wav2lip.onnx). Si mette nella cartella a mano "
                    "(o con l'importatore): nessuno lo scarica, perché la sua fonte non è nota.")),
    _entry("ultralight-wenet-encoder", "avatar", "ultralight", "Ultralight wenet audio encoder (encoder.onnx)", "ultralight", "verify", [], size=None,
           verify=("source", "sha256", "size", "license"), probe=["encoder.onnx"], selectable=False, kind="engine-pack",
           folder="avatar-engines/ultralight/weights",
           venv=_t("Read by Ultralight's wenet_infer.py with onnxruntime (the engine copies it next to the vendored code before it runs).",
                   "Letto da wenet_infer.py di Ultralight con onnxruntime (il motore lo copia accanto al codice incluso prima di eseguirlo)."),
           notes=_t("Its README says to download encoder.onnx separately (a Google Drive link, file 1e4Z9zS053JEWl6Mj3W9Lbc9GDtzHIg6b). No fixed "
                    "address, SHA-256, size or license is stated, so this entry cannot be downloaded: put the file in its folder by hand. It is only needed with the wenet audio encoder.",
                    "Il suo README dice di scaricare encoder.onnx a parte (un link Google Drive, file 1e4Z9zS053JEWl6Mj3W9Lbc9GDtzHIg6b). Non sono "
                    "dichiarati indirizzo fisso, SHA-256, dimensione né licenza, quindi questa voce non si può scaricare: mettere il file nella sua cartella a mano. Serve solo con il codificatore audio wenet.")),
    _entry("engine-landmarks", "avatar", "ultralight", "Face detector and landmark weights (SCRFD, PFLD)", "ultralight", "verify",
           [_url(f"{packs.HOST}{packs.FEATHERTALK_REPO}/{packs.FEATHERTALK_COMMIT}/{path}", path.split("/")[-1], sha, size) for path, sha, size in packs.LANDMARK_FILES],
           size=sum(f[2] for f in packs.LANDMARK_FILES),
           verify=("license",), probe=[f[0].split("/")[-1] for f in packs.LANDMARK_FILES], selectable=False, kind="engine-pack", folder="avatar-engines/landmarks",
           venv=_t("Read by the preprocessing of Ultralight and FeatherTalk (OpenCV and PyTorch); the engine copies them next to the vendored code before it runs.",
                   "Letti dal preprocessing di Ultralight e FeatherTalk (OpenCV e PyTorch); il motore li copia accanto al codice incluso prima di eseguirlo."),
           notes=_t("The two files both engines load to find the face and its landmarks: scrfd_2.5g_kps.onnx and checkpoint_epoch_335.pth.tar. They are byte for byte the same in both "
                    "repositories. Where they come from and under what license is not stated in either repository: verify before you share anything made with them.",
                    "I due file che entrambi i motori caricano per trovare il volto e i suoi punti: scrfd_2.5g_kps.onnx e checkpoint_epoch_335.pth.tar. Sono identici byte per byte nei due "
                    "repository. Da dove vengano e con quale licenza non è dichiarato in nessuno dei due: verificare prima di condividere qualcosa fatto con essi.")),
    _entry("feathertalk-weights", "avatar", "feathertalk", "FeatherHuBERT weights (feather_hubert.pth)", "feathertalk", "apache-2.0-feathertalk",
           [_url(f"{packs.HOST}{packs.FEATHERTALK_REPO}/{packs.FEATHERTALK_COMMIT}/{packs.FEATHERTALK_WEIGHTS[0]}", packs.FEATHERTALK_WEIGHTS[0],
                 packs.FEATHERTALK_WEIGHTS[1], packs.FEATHERTALK_WEIGHTS[2])],
           size=packs.FEATHERTALK_WEIGHTS[2], probe=["feather_hubert.pth"], selectable=False, kind="engine-pack", folder="avatar-engines/feathertalk/weights",
           sha256=packs.FEATHERTALK_WEIGHTS[1], languages=("zh",), verify=("license-weights", "italian"),
           venv=_t("Read by FeatherTalk's scripts with PyTorch.", "Letto dagli script di FeatherTalk con PyTorch."),
           notes=_t("The audio encoder FeatherHuBERT (3.36M parameters), generic across people. Its authors say the published weights were trained on Chinese speech only "
                    "and that the training is incomplete: the quality in Italian is unverified. The license of the weights is not stated apart from the repository's.",
                    "Il codificatore audio FeatherHuBERT (3,36 milioni di parametri), valido per ogni persona. Gli autori dicono che i pesi pubblicati sono addestrati solo su "
                    "voce cinese e che l'addestramento è incompleto: la resa in italiano non è verificata. La licenza dei pesi non è dichiarata oltre a quella del repository.")),
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
CATALOG += ENGINE_PACKS
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
    return entry["folder"] or f"{entry['service']}/{entry['id']}"


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
            "selectable": entry["selectable"], "sha256": entry["sha256"], "kind": entry["kind"], "folder": folder_of(entry), "installed": installed(entry, root), "verify": bool(entry["verify"]), "verify_fields": list(entry["verify"]),
            "downloadable": ok, "not_downloadable": why, "languages": entry["languages"], "notes": entry["notes"], "venv": entry["venv"]}
