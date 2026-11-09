"""The voice models TaDaMoo can install on request: speech to text, speech synthesis, avatar, diarization, voice activity, embedding.

Every entry was read from the voice service's own code (voice/callbridge/engines, voice/avatar) and from the download script of the
original project. What that code does not state is not guessed: the entry carries `verify` (the names of the fields to check) and a
download is refused until its sources are complete. A grounded Hugging Face source is pinned to a commit and records the SHA-256 of each
file (`pins`, the LFS id; serve/downloads.py refuses a file that differs); one still at revision "main" is resolved when a download is
planned and is not grounded yet. A URL source needs a fixed SHA-256. Sizes here are for display only.

An entry is installed under <voice models folder>/<service>/<model id>/ (serve/voice_settings.py). A finished download leaves a
marker file listing what it wrote; a model put there by hand is recognised by its `probe` files."""
from __future__ import annotations

import json
import os
import re

from serve import voice_engine_weights as packs

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
    "musetalk": {"name": "MuseTalk: MIT code and weights; the face-parsing weights are non-commercial", "url": "",
                 "en": "MuseTalk's code is MIT and its trained weights may be used for any purpose, commercial use included (its own statement); its "
                       "test data is for non-commercial research only. The weights it relies on follow their own terms: Whisper tiny (Apache-2.0), "
                       "sd-vae-ft-mse (MIT), DWPose (Apache-2.0). The face-parsing network was trained on CelebAMask-HQ, whose agreement allows "
                       "non-commercial research only, so this download is treated as non-commercial; the S3FD face detector and the ResNet-18 "
                       "backbone state no license in what was read. This is a summary, not legal advice: the license texts govern.",
                 "it": "Il codice di MuseTalk è MIT e i suoi pesi addestrati si possono usare per qualunque scopo, anche commerciale (lo dice il "
                       "progetto); i suoi dati di prova sono solo per ricerca non commerciale. I pesi che usa seguono la propria licenza: Whisper "
                       "tiny (Apache-2.0), sd-vae-ft-mse (MIT), DWPose (Apache-2.0). La rete di face parsing è addestrata su CelebAMask-HQ, il cui "
                       "accordo consente solo ricerca non commerciale: questo download è quindi trattato come non commerciale; il rilevatore di "
                       "volti S3FD e la base ResNet-18 non dichiarano una licenza in ciò che è stato letto. È un riassunto, non una consulenza "
                       "legale: fanno fede i testi delle licenze."},
    "piper-voices": {"name": "Piper voices: each voice follows the license of its training data", "url": "",
                     "en": "The four voices installed here come from datasets that allow commercial use: Riccardo (M-AILABS, a BSD-style license: keep "
                           "its copyright notice), Serena (CC BY 4.0: credit the author), and the LJ Speech and LibriVox voices (public domain). Other "
                           "Piper voices differ: several were finetuned from the Lessac voice, whose data is for research only. The runtime package "
                           "piper-tts is GPL-3.0-or-later and is not part of this download. This is a summary, not legal advice.",
                     "it": "Le quattro voci installate qui vengono da dati che consentono l'uso commerciale: Riccardo (M-AILABS, licenza tipo BSD: "
                           "mantenere l'avviso di copyright), Serena (CC BY 4.0: citare l'autore) e le voci LJ Speech e LibriVox (pubblico dominio). "
                           "Altre voci di Piper sono diverse: parecchie derivano dalla voce Lessac, i cui dati sono solo per ricerca. Il pacchetto "
                           "piper-tts è GPL-3.0-or-later e non fa parte di questo download. È un riassunto, non una consulenza legale."},
    "pyannote-camplus": {"name": "pyannote segmentation 3.0 (MIT) and CAM++ (Apache-2.0)", "url": "",
                         "en": "The segmentation model is pyannote's segmentation-3.0 (MIT, CNRS) converted to ONNX by the sherpa-onnx author; the original "
                               "repository asks for a login on Hugging Face, the converted copy installed here does not. The speaker model is the "
                               "3D-Speaker CAM++ (Apache-2.0). Both allow commercial use with their notices. This is a summary, not legal advice.",
                         "it": "Il modello di segmentazione è segmentation-3.0 di pyannote (MIT, CNRS) convertito in ONNX dall'autore di sherpa-onnx; "
                               "il repository originale chiede un accesso su Hugging Face, la copia convertita installata qui no. Il modello di "
                               "impronta vocale è CAM++ di 3D-Speaker (Apache-2.0). Entrambi consentono l'uso commerciale con i loro avvisi. "
                               "È un riassunto, non una consulenza legale."},
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
    "linly-talker": {"name": "Linly-Talker's Hugging Face repository: no licence stated; the Wav2Lip v2 model is non-commercial", "url": "",
                     "en": "The Hugging Face repository these weights come from states no licence on its card, and it gathers files of other projects (SadTalker, Wav2Lip, ER-NeRF, "
                           "MuseTalk) whose own terms apply and were not all checked. The Wav2Lip v2 model derives from Wav2Lip, which allows research and non-commercial use only: "
                           "do not use it commercially. This is a summary, not legal advice.",
                     "it": "Il repository Hugging Face da cui vengono questi pesi non dichiara una licenza nella sua scheda e raccoglie file di altri progetti (SadTalker, Wav2Lip, "
                           "ER-NeRF, MuseTalk) di cui valgono le condizioni, non tutte verificate. Il modello Wav2Lip v2 deriva da Wav2Lip, che consente solo ricerca e uso non "
                           "commerciale: non usarlo commercialmente. È un riassunto, non una consulenza legale."},
    "ultralytics-face": {"name": "YOLOv8n-face: AGPL-3.0 code, weights licence unclear", "url": "",
                         "en": "The ultralytics package that loads this file is AGPL-3.0 code: running it for yourself is allowed, but offering it over a network or distributing it "
                               "brings the AGPL duties. The licence of the face-detection weights themselves is not stated in anything read here. Verify both before you distribute.",
                         "it": "Il pacchetto ultralytics che carica questo file è codice AGPL-3.0: usarlo per sé è permesso, ma offrirlo in rete o distribuirlo comporta gli obblighi "
                               "della AGPL. La licenza dei pesi di rilevamento del volto non è dichiarata in nulla di ciò che è stato letto qui. Verificare entrambe prima di distribuire."},
    "orpheus-llama": {"name": "Orpheus 3B: Apache-2.0 on its card, fine-tuned from Llama 3.2; SNAC codec MIT", "url": "",
                      "en": "The card of the model installed (unsloth's copy of canopylabs/orpheus-3b-0.1-ft) says Apache-2.0. The model was fine-tuned from Meta's "
                            "Llama-3.2-3B-Instruct, so the Llama 3.2 Community License (attribution, an acceptable-use policy) may apply too: that was not checked. The "
                            "SNAC codec is MIT. This is a summary, not legal advice: the license texts govern.",
                      "it": "La scheda del modello installato (la copia di unsloth di canopylabs/orpheus-3b-0.1-ft) dice Apache-2.0. Il modello deriva da Llama-3.2-3B-Instruct "
                            "di Meta, quindi può applicarsi anche la Llama 3.2 Community License (attribuzione, una politica d'uso accettabile): non è stato verificato. Il "
                            "codec SNAC è MIT. È un riassunto, non una consulenza legale: fanno fede i testi delle licenze."},
    "dia-dac": {"name": "Dia 1.6B (Apache-2.0) and the DAC codec (MIT)", "url": "",
                "en": "Dia 1.6B (the 0626 release) is Apache-2.0 on its card. The DAC 44 kHz codec that its processor loads is Descript's: the Hugging Face copy states no "
                      "license (its card says \"More Information Needed\"), the descript-audio-codec repository on GitHub is MIT. This is a summary, not legal advice.",
                "it": "Dia 1.6B (la versione 0626) è Apache-2.0 secondo la sua scheda. Il codec DAC 44 kHz che il suo processore carica è di Descript: la copia su Hugging Face "
                      "non dichiara una licenza (la scheda dice \"More Information Needed\"), il repository descript-audio-codec su GitHub è MIT. È un riassunto, non una consulenza legale."},
    "verify": {"name": "License not verified", "url": "",
               "en": "The license of this model has not been checked yet. Read the model's page on its source before you use it, "
                     "and do not rely on this download for any use the license may forbid.",
               "it": "La licenza di questo modello non è ancora stata verificata. Leggi la pagina del modello alla sua fonte prima di usarlo e "
                     "non fare affidamento su questo download per usi che la licenza potrebbe vietare."},
}

# Whisper checkpoints as openai-whisper 20250625 names them: the SHA-256 is the folder of each address (the same table as the tag's
# whisper/__init__.py) and the sizes are the Content-Length of each address.
_WHISPER_HOST = "https://openaipublic.azureedge.net/main/whisper/models/"
_WHISPER = {
    "tiny": "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
    "base": "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e",
    "small": "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794",
    "medium": "345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1",
    "large-v3": "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb",
}
_WHISPER_BYTES = {"tiny": 75_572_083, "base": 145_262_807, "small": 483_617_219, "medium": 1_528_008_539, "large-v3": 3_087_371_615}
_WHISPER_VENV = _t("Runs inside the voice service with torch (openai-whisper); a CUDA build of torch is needed to use the GPU.",
                   "Gira dentro il servizio vocale con torch (openai-whisper); per usare la GPU serve la build CUDA di torch.")

_KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
_SILERO = "https://raw.githubusercontent.com/snakers4/silero-vad/915dd3d639b8333a52e001af095f87c5b7f1e0ac/files/silero_vad.onnx"     # the v4.0 tag
_CAMPP = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
_SEG_DIR = "sherpa-onnx-pyannote-segmentation-3-0"


def _hf(repo: str, files=None, subdir: str = "", strip: str = "") -> dict:
    """`strip` is a prefix taken off each file's path in the repository, so a file lands in the folder of its entry."""
    return {"type": "hf", "repo": repo, "revision": "main", "files": files, "subdir": subdir, "strip": strip, "pins": {}}


def _pinned(repo: str, revision: str, files, subdir: str = "", strip: str = "") -> dict:
    """A checked Hugging Face source: `revision` is a commit and `files` are (path, SHA-256) pairs, the LFS id or the hash of the small file.
    A (path, SHA-256, size) triple also records the size in bytes (`sizes`), the sum of which is the entry's size."""
    src = {"type": "hf", "repo": repo, "revision": revision, "files": [f[0] for f in files], "subdir": subdir, "strip": strip,
           "pins": {f[0]: f[1] for f in files}}
    if files and all(len(f) == 3 for f in files):
        src["sizes"] = {f[0]: f[2] for f in files}
    return src


def _total(*sets) -> int:
    """The bytes of one or more lists of (path, SHA-256, size) triples."""
    return sum(f[2] for files in sets for f in files)


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


def _whisper(size: str) -> dict:
    name = size + ".pt"
    return _entry(f"whisper-{size}", "stt", "whisper", f"Whisper {size}", "whisper", "mit",
                  [_url(_WHISPER_HOST + _WHISPER[size] + "/" + name, name, _WHISPER[size], _WHISPER_BYTES[size])],
                  size=_WHISPER_BYTES[size], venv=_WHISPER_VENV, probe=[name],
                  notes=_t("OpenAI's checkpoint of the " + size + " size, the one the voice service loads by name.",
                           "Il checkpoint OpenAI della taglia " + size + ", quello che il servizio vocale carica per nome."))


def _qwen(size: str) -> dict:
    srcs = [_pinned(f"Qwen/Qwen3-TTS-12Hz-{size}-{kind}", commit, files + _QWEN_COMMON, subdir="base" if kind == "Base" else "custom-voice")
            for kind, (commit, files) in _QWEN[size].items()]
    return _entry(f"qwen3tts-{size.lower()}", "tts", "qwen3tts", f"Qwen3-TTS {size}", "qwen3-tts", "apache-2.0", srcs,
                  size=sum(_total(f, _QWEN_COMMON) for _, f in _QWEN[size].values()),
                  languages=("it", "en", "fr", "de", "es", "pt", "ru", "ja", "ko", "zh"),
                  venv=_t("Own Python environment (tts-venvs/dia, shared with Dia), torch with CUDA, the faster-qwen3-tts runtime.",
                          "Ambiente Python proprio (tts-venvs/dia, condiviso con Dia), torch con CUDA, il runtime faster-qwen3-tts."),
                  notes=_t("Voice cloning (the Base model) and nine preset voices (the CustomVoice model); both are installed, each at one commit with the files the "
                           "repository holds besides its README, the speech tokenizer included. Streaming.",
                           "Clonazione di voce (modello Base) e nove voci preimpostate (modello CustomVoice); si installano entrambi, ciascuno a un commit con i file "
                           "che il repository ha oltre al README, il tokenizzatore vocale compreso. Streaming."))


# The weights of the PC avatar engines Wav2Lip v2, SadTalker and ER-NeRF. Their code is vendored (voice/avatar_engines/vendor/), so nothing here installs code.
# The files of Kedreamix/Linly-Talker on Hugging Face (the repository its README names) are pinned to one commit; each size and SHA-256 is the LFS id of that tree
# (the hash of the 144-byte config.yaml, which is not in LFS, was computed from the file and its git blob id matches the tree's). Licence (to confirm, notes in the
# code only): that repository states none; see voice/avatar_engines/vendor/*/LICENSE-NOTE.md.
_LINLY = "Kedreamix/Linly-Talker"
_LINLY_REV = "de2ee105d8fbe279e973b79db24009e803845b2f"
_LINLY_WEIGHTS = {                                   # path in the repository: (SHA-256, bytes)
    "checkpoints/wav2lipv2.pth": ("b22d7ac86295df667644b17254dc71250c2600b89e20403e90e58812450bc173", 214_670_409),
    "checkpoints/weights/yolov8n-face/yolov8n-face.pt": ("d545bf1add5aa736a4febac4f4f9245a6d596cd0fe70d5d57989fe0cb9e626ca", 6_389_512),
    "checkpoints/weights/wflw/hrnet18_256x256_p1/config.yaml": ("a998bb71186699585fa23b279cc4b1435e222ba39e6525dd897c74053ff57d7e", 144),
    "checkpoints/weights/wflw/hrnet18_256x256_p1/model.pth": ("a0d0f89d3830954b6adb8d2e9e67fc656636db61c612a47b935b444f3ce69504", 39_273_976),
    "checkpoints/SadTalker_V0.0.2_256.safetensors": ("c211f5d6de003516bf1bbda9f47049a4c9c99133b1ab565c6961e5af16477bff", 725_066_984),
    "checkpoints/mapping_00229-model.pth.tar": ("62a1e06006cc963220f6477438518ed86e9788226c62ae382ddc42fbcefb83f1", 155_521_183),
    "gfpgan/weights/alignment_WFLW_4HG.pth": ("bbfd137307a4c7debd5c283b9b0ce539466cee417ac0a155e184d857f9f2899c", 193_670_248),
    "gfpgan/weights/detection_Resnet50_Final.pth": ("6d1de9c2944f2ccddca5f5e010ea5ae64a39845a86311af6fdf30841b0a5a16d", 109_497_761),
    "checkpoints/audio_visual_encoder.pth": ("1f75cdb5556938590c64221f6c47a8d20ab3e368d53c166023eedb12279db8c4", 11_300_706),
    "MuseTalk/face-parse-bisent/79999_iter.pth": ("468e13ca13a9b43cc0881a9f99083a430e9c0a38abd935431d1c28ee94b26567", 53_289_463),
    "MuseTalk/face-parse-bisent/resnet18-5c106cde.pth": ("5c106cde386e87d4033832f2996f5493238eda96ccf559d1d62760c4de0613f8", 46_827_520),
}
# AD-NeRF's 3DMM folder at one GitHub commit, the files ER-NeRF's README downloads from it: (path, SHA-256, bytes) computed from the raw files at that commit
# (each git blob id matches the contents listing). AD-NeRF's repository is MIT per GitHub.
_ADNERF, _ADNERF_COMMIT = "YudongGuo/AD-NeRF", "9fdcd7e1352aa74e26baa8e60c862f9fbd7933bf"
_ADNERF_3DMM = (
    ("data_util/face_tracking/3DMM/exp_info.npy", "c3196029ed038eb9a461df6c782125fc4d1ec1545f2e5f361891471136b6cbb6", 33_264_853),
    ("data_util/face_tracking/3DMM/keys_info.npy", "028d3c383bae129c4bdcac880e22c551a5d2436ec7db7a26f5c57148d12469e6", 7_375),
    ("data_util/face_tracking/3DMM/sub_mesh.obj", "1234e8e8d48617d4be869fde4d8d7485f27164351f085cc2c491e9badb48a47c", 2_338_406),
    ("data_util/face_tracking/3DMM/topology_info.npy", "2edff6a6ad574d2dddf0d0815e0beabfea9369d7c2d6e53e0ba81f809b81e963", 4_145_201),
)
_GPU_PY = ("PyTorch with CUDA, torchvision, opencv, numpy, scipy, scikit-image, librosa, yacs, tqdm; ffmpeg on the PATH.",
           "PyTorch con CUDA, torchvision, opencv, numpy, scipy, scikit-image, librosa, yacs, tqdm; ffmpeg nel PATH.")
_LINLY_ENV = _t("Run by the voice service in a Python environment of your own (TADAMOO_AVATAR_ENGINES_PYTHON); nothing here installs it. ",
                "Eseguito dal servizio vocale in un ambiente Python tuo (TADAMOO_AVATAR_ENGINES_PYTHON); qui non si installa nulla. ")


def _linly_weights(id_: str, engine: str, name: str, paths, strip: str, folder: str, notes: dict, need: tuple | None = None, license_: str = "linly-talker", **kw) -> dict:
    """Weights of the Linly-Talker Hugging Face repository at the pinned commit; `strip` is the folder of the repository they are taken out of."""
    files = [(p, *_LINLY_WEIGHTS[p]) for p in paths]
    venv = _t(_LINLY_ENV["en"] + need[0], _LINLY_ENV["it"] + need[1]) if need else _t("Read by the engine's scripts with PyTorch.", "Letto dagli script del motore con PyTorch.")
    return _entry(id_, "avatar", engine, name, engine, license_, [_pinned(_LINLY, _LINLY_REV, files, strip=strip)], size=_total(files),
                  probe=[p[len(strip):] for p in paths], selectable=False, kind="engine-pack", folder=folder, venv=venv, notes=notes, **kw)


ENGINE_PACKS = [
    _linly_weights("wav2lipv2-weights", "wav2lipv2", "Wav2Lip v2 288 px weights (wav2lipv2.pth)", ["checkpoints/wav2lipv2.pth"], "checkpoints/",
                   "avatar-engines/wav2lipv2/checkpoints", noncommercial=True, verify=("license",),
                   need=(_GPU_PY[0] + " Also ultralytics (AGPL-3.0).", _GPU_PY[1] + " Anche ultralytics (AGPL-3.0)."),
                   notes=_t("The checkpoint the Wav2Lipv2 script loads by default (checkpoints/wav2lipv2.pth), from the Hugging Face repository Linly-Talker's README names, at one "
                            "commit. Non-commercial: it derives from Wav2Lip, which is for research use only.",
                            "Il checkpoint che lo script Wav2Lipv2 carica per default (checkpoints/wav2lipv2.pth), dal repository Hugging Face che il README di Linly-Talker nomina, a un "
                            "commit fisso. Non commerciale: deriva da Wav2Lip, che è solo per ricerca.")),
    _linly_weights("wav2lipv2-yolo-face", "wav2lipv2", "YOLOv8n-face weights (yolov8n-face.pt)", ["checkpoints/weights/yolov8n-face/yolov8n-face.pt"],
                   "checkpoints/weights/yolov8n-face/", "avatar-engines/wav2lipv2/models/yolov8n-face", license_="ultralytics-face", verify=("license",),
                   notes=_t("The face detector the script loads from weights/yolov8n-face/, taken from the same Hugging Face repository and commit as the checkpoint. The "
                            "ultralytics code that reads it is AGPL-3.0 and the licence of the weights is not stated.",
                            "Il rilevatore del volto che lo script carica da weights/yolov8n-face/, preso dallo stesso repository Hugging Face e commit del checkpoint. Il codice "
                            "ultralytics che lo legge è AGPL-3.0 e la licenza dei pesi non è dichiarata.")),
    _linly_weights("wav2lipv2-landmarks", "wav2lipv2", "WFLW HRNet18 landmark weights (config.yaml, model.pth)",
                   ["checkpoints/weights/wflw/hrnet18_256x256_p1/config.yaml", "checkpoints/weights/wflw/hrnet18_256x256_p1/model.pth"],
                   "checkpoints/weights/wflw/hrnet18_256x256_p1/", "avatar-engines/wav2lipv2/models/wflw/hrnet18_256x256_p1", verify=("license",),
                   notes=_t("The landmark net the script loads from weights/wflw/hrnet18_256x256_p1/, taken from the same Hugging Face repository and commit as the checkpoint. "
                            "Where the files first come from is not stated there.",
                            "La rete di punti del volto che lo script carica da weights/wflw/hrnet18_256x256_p1/, presa dallo stesso repository Hugging Face e commit del checkpoint. "
                            "Da dove i file vengano in origine non è dichiarato lì.")),
    _linly_weights("sadtalker-weights", "sadtalker", "SadTalker 256 px weights", ["checkpoints/SadTalker_V0.0.2_256.safetensors", "checkpoints/mapping_00229-model.pth.tar"],
                   "checkpoints/", "avatar-engines/sadtalker/checkpoints", verify=("license",),
                   need=(_GPU_PY[0] + " Also safetensors, facexlib, gfpgan, imageio, pydub, kornia, face_alignment.",
                         _GPU_PY[1] + " Anche safetensors, facexlib, gfpgan, imageio, pydub, kornia, face_alignment."),
                   notes=_t("The two files the wrapper reads for the 256 px model with the cropped preprocessing, from the Hugging Face repository Linly-Talker's README names, at one commit.",
                            "I due file che il wrapper legge per il modello a 256 px con il ritaglio, dal repository Hugging Face che il README di Linly-Talker nomina, a un commit fisso.")),
    _linly_weights("sadtalker-gfpgan", "sadtalker", "SadTalker face detector and aligner weights", ["gfpgan/weights/alignment_WFLW_4HG.pth", "gfpgan/weights/detection_Resnet50_Final.pth"],
                   "gfpgan/weights/", "avatar-engines/sadtalker/code/gfpgan/weights", verify=("license",),
                   notes=_t("What the keypoint extractor reads from gfpgan/weights (it would otherwise fetch them from the network). They are installed inside the engine's run "
                            "folder because the code looks for them there.",
                            "Ciò che l'estrattore dei punti chiave legge da gfpgan/weights (altrimenti li prenderebbe dalla rete). Si installano dentro la cartella di lavoro "
                            "del motore perché il codice li cerca lì.")),
    _linly_weights("ernerf-audio-encoder", "ernerf", "ER-NeRF audio encoder weights (audio_visual_encoder.pth)", ["checkpoints/audio_visual_encoder.pth"], "checkpoints/",
                   "avatar-engines/ernerf/code/checkpoints", verify=("license",),
                   need=("PyTorch with CUDA, PyTorch3D and the compiled CUDA modules freqencoder, gridencoder, raymarching and shencoder (built on this PC from the sources in the "
                         "engine's run folder), plus the packages of TFG/requirements_nerf.txt; ffmpeg on the PATH. It breaks with the versions of Python, PyTorch, CUDA and the compiler.",
                         "PyTorch con CUDA, PyTorch3D e i moduli CUDA compilati freqencoder, gridencoder, raymarching e shencoder (compilati su questo PC dai sorgenti nella cartella "
                         "di lavoro del motore), più i pacchetti di TFG/requirements_nerf.txt; ffmpeg nel PATH. Si rompe con le versioni di Python, PyTorch, CUDA e del compilatore."),
                   notes=_t("What NeRFTalk reads from ./checkpoints to turn the speech into features, from the Hugging Face repository Linly-Talker's README names, at one commit.",
                            "Ciò che NeRFTalk legge da ./checkpoints per trasformare il parlato in feature, dal repository Hugging Face che il README di Linly-Talker nomina, a un commit fisso.")),
    _entry("ernerf-bfm", "avatar", "ernerf", "ER-NeRF 3DMM files (AD-NeRF: exp_info, keys_info, sub_mesh, topology_info)", "ernerf", "mit",
           [_url(f"{packs.HOST}{_ADNERF}/{_ADNERF_COMMIT}/{path}", path.rsplit("/", 1)[1], sha, size) for path, sha, size in _ADNERF_3DMM],
           size=_total(_ADNERF_3DMM), probe=["exp_info.npy", "keys_info.npy", "sub_mesh.obj", "topology_info.npy"], selectable=False, kind="engine-pack",
           folder="avatar-engines/ernerf/code/NeRF/data_utils/face_tracking/3DMM", venv=_t("Read by the engine's scripts.", "Letto dagli script del motore."),
           notes=_t("The four files ER-NeRF's README downloads from AD-NeRF (github.com/YudongGuo/AD-NeRF, MIT), at one commit. They are only part of the 3DMM folder: the Basel Face "
                    "Model (01_MorphableModel.mat, which needs a registration with the university) has to be placed beside them by hand and convert_BFM.py run to make "
                    "3DMM_info.npy. Needed only to train a new person.",
                    "I quattro file che il README di ER-NeRF scarica da AD-NeRF (github.com/YudongGuo/AD-NeRF, MIT), a un commit fisso. Sono solo una parte della cartella 3DMM: il Basel "
                    "Face Model (01_MorphableModel.mat, che richiede una registrazione presso l'università) va messo a mano accanto e va eseguito convert_BFM.py per fare "
                    "3DMM_info.npy. Serve solo per addestrare una persona nuova.")),
    _linly_weights("ernerf-face-parsing", "ernerf", "ER-NeRF face parsing weights (79999_iter.pth)", ["MuseTalk/face-parse-bisent/79999_iter.pth"], "MuseTalk/face-parse-bisent/",
                   "avatar-engines/ernerf/code/NeRF/data_utils/face_parsing", noncommercial=True, verify=("license",),
                   notes=_t("The default of the data preparation's face-parsing step (data_utils/face_parsing/79999_iter.pth). ER-NeRF's README takes it from AD-NeRF; this copy is the "
                            "one in the Linly-Talker Hugging Face repository (its MuseTalk folder), which has the same name and the same size (53,289,463 bytes) as the AD-NeRF file; "
                            "the two were not compared byte for byte. The network was trained on CelebAMask-HQ (non-commercial research data). The face-parsing code also needs "
                            "resnet18-5c106cde.pth (the entry ernerf-resnet18). Needed only to train a new person.",
                            "Il valore predefinito del passo di face parsing della preparazione dati (data_utils/face_parsing/79999_iter.pth). Il README di ER-NeRF lo prende da AD-NeRF; "
                            "questa copia è quella del repository Hugging Face di Linly-Talker (cartella MuseTalk), con lo stesso nome e la stessa dimensione (53.289.463 byte) del file "
                            "di AD-NeRF; i due non sono stati confrontati byte per byte. La rete è addestrata su CelebAMask-HQ (dati di ricerca non commerciali). Il codice di face "
                            "parsing serve anche resnet18-5c106cde.pth (la voce ernerf-resnet18). Serve solo per addestrare una persona nuova.")),
    _linly_weights("ernerf-resnet18", "ernerf", "ResNet-18 weights for the face-parsing net (resnet18-5c106cde.pth)", ["MuseTalk/face-parse-bisent/resnet18-5c106cde.pth"],
                   "MuseTalk/face-parse-bisent/", "avatar-engines/ernerf/torch-home/hub/checkpoints", verify=("license",),
                   notes=_t("What the face-parsing code would fetch from download.pytorch.org each time it builds its network (torchvision's resnet18 ImageNet weights). It goes in the "
                            "PyTorch hub cache folder of the engine (torch-home/hub/checkpoints), which the engine sets as TORCH_HOME for its processes, so nothing is fetched at run time. "
                            "The SHA-256 of the file starts with 5c106cde, as torchvision's file name says, and was computed from the file. Needed only to train a new person.",
                            "Ciò che il codice di face parsing scaricherebbe da download.pytorch.org ogni volta che costruisce la sua rete (i pesi ImageNet di resnet18 di torchvision). "
                            "Va nella cartella della cache di PyTorch hub del motore (torch-home/hub/checkpoints), che il motore imposta come TORCH_HOME per i suoi processi, così a "
                            "run time non si scarica nulla. Lo SHA-256 del file inizia con 5c106cde, come dice il nome del file di torchvision, ed è stato calcolato dal file. "
                            "Serve solo per addestrare una persona nuova.")),
]

# Piper voices of rhasspy/piper-voices at one commit: (name, directory, .onnx SHA-256, .onnx.json SHA-256, size of both files).
_PIPER, _PIPER_REV = "rhasspy/piper-voices", "e7229f7d33a10e145e109b886d661984201ac3c4"
_PIPER_VOICES = [
    ("it_IT-riccardo-x_low", "it/it_IT/riccardo/x_low", "1368de15f123275a7ef951c9e5e30be0f58a032daa14a0da44037443c1d1d21b",
     "146ab9c634afe524e9fb7530f2510df7a42fb1db56b52658ca1fb3d98001a62a", 28_130_791 + 4_161),
    ("it_IT-serena-medium", "it/it_IT/serena/medium", "fe4e26b2c1236e2a44d2e295cad564d94a0d8b0a9fca1ccdfd70dd4af3116eb5",
     "14cb4995874138b2792ae07753b5540942124761af9ae6609f9b520f28837881", 63_516_051 + 5_254),
    ("en_US-ljspeech-medium", "en/en_US/ljspeech/medium", "6f52a751e2349abe7a76735eb09dc1875298c77ea2342ffd2fef79ff81b87f22",
     "141d612cc0a95ed7efc1ca936b845c2364967f2e9217c5dbfcf69fc4d6c65860", 63_531_379 + 4_972),
    ("en_GB-cori-medium", "en/en_GB/cori/medium", "1899f98e5fb8310154f3c2973f4b8a929ba7245e722b3d3a85680b833d95f10d",
     "e262c16d7f192f69d4edd6b4ef8a5915379e67495fcc402f1ab15eeb33da3d36", 63_531_379 + 4_966),
]
_PIPER_SOURCES = [_pinned(_PIPER, _PIPER_REV, [(f"{d}/{n}.onnx", h1), (f"{d}/{n}.onnx.json", h2)], strip=d + "/") for n, d, h1, h2, _ in _PIPER_VOICES]

# The files of the TTS entries below: (path, SHA-256, size), the LFS id or the hash of the small file, read from the repository's tree at that commit.
# ResembleAI/chatterbox at 5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18
_CHATTERBOX_FILES = [
    ("ve.pt", "4b16d836bc598509860f6fa068165a8bb5e9ac84f05582dfcf278a5a372879f1", 5_698_626),
    ("t3_mtl23ls_v2.safetensors", "b1237586127ce98e7800a68e49938eb5092846862aabcb6e17b2fda7889a6c75", 2_143_989_752),
    ("s3gen.pt", "9b9ff07e60b20c136e2b1b3d7563a24604e8d2c4c267888d1ee929dd0151d2a3", 1_057_165_844),
    ("grapheme_mtl_merged_expanded_v1.json", "69632f47220a788a52ce2661d096453c5655e9bf25289d89a8d832c46ee07dbf", 69_989),
    ("conds.pt", "6552d70568833628ba019c6b03459e77fe71ca197d5c560cef9411bee9d87f4e", 107_374),
    ("Cangjie5_TC.json", "7073fd9de919443ae88e0bd2449917a65fe54898a4413ed1edcc4b67f28bce8c", 1_920_163),
]

# coqui/XTTS-v2 at 6c2b0d75eae4b7047358e3b6bd9325f857d43f77
_XTTS_FILES = [
    ("LICENSE.txt", "190f6d7c19b8984f91b97712b94ce92d2b2e640fc677dacab966e955ece9d043", 4_014),
    ("config.json", "ef262b1454dd2a77e1461b0b2cd53e19b8a7624cc131b837d36df67356bc75e8", 4_368),
    ("dvae.pth", "b29bc227d410d4991e0a8c09b858f77415013eeb9fba9650258e96095557d97a", 210_514_388),
    ("mel_stats.pth", "1f69422a8a8f344c4fca2f0c6b8d41d2151d6615b7321e48e6bb15ae949b119c", 1_067),
    ("model.pth", "c7ea20001c6a0a841c77e252d8409f6a74fb423e79b3206a0771ba5989776187", 1_867_929_118),
    ("speakers_xtts.pth", "f0f6137c19a4eab0cbbe4c99b5babacf68b1746e50da90807708c10e645b943b", 7_754_818),
    ("vocab.json", "928260878a59da8a72a2a5b7687fea29d5106137669d90945430fe17e415304a", 361_219),
]

# FunAudioLLM/Fun-CosyVoice3-0.5B-2512 at 29e01c4e8d000f4bcd70751be16fa94bf3d85a18
_COSYVOICE_FILES = [
    ("cosyvoice3.yaml", "f5a6b2c6f05139d0f18861a1fe506f751e787026b77c05f7e8fef9f8a4405965", 6_934),
    ("campplus.onnx", "a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73", 28_303_423),
    ("speech_tokenizer_v3.onnx", "23236a74175dbdda47afc66dbadd5bcb41303c467a57c261cb8539ad9db9208d", 969_451_503),
    ("flow.pt", "a6fab32a7825e5b0bc855ddd948f8db9370b0a786fbc249caa4595e95b608e4b", 1_329_116_148),
    ("hift.pt", "b279d7641eb97ae55b3b540cfba4f953c26492a2df758328a89a4d007ab87a65", 83_202_622),
    ("llm.pt", "69f43bd545131c30e98947fb360ea8b4dc9916d8e83dded7757c7ea4f5a24970", 2_024_669_519),
    ("CosyVoice-BlankEN/config.json", "168aa1bd401abc3bc262ba15ba4e499627a8b4e006e9d050b47c22de20660185", 659),
    ("CosyVoice-BlankEN/generation_config.json", "e558847a8b4402616f1273797b015104dc266fe4b520056fca88823ba8f8ebe6", 242),
    ("CosyVoice-BlankEN/merges.txt", "ac8ff86a72bee70828fbc1119bc4398c6f3a9a6e490d7b0dbe917be025478bd0", 1_402_109),
    ("CosyVoice-BlankEN/model.safetensors", "130282af0dfa9fe5840737cc49a0d339d06075f83c5a315c3372c9a0740d0b96", 988_097_824),
    ("CosyVoice-BlankEN/tokenizer_config.json", "482bd979881423375ca5414e4e0d94cd7c5349dbb17fffd46b4d36d71e62a1bc", 1_287),
    ("CosyVoice-BlankEN/vocab.json", "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910", 2_776_833),
]

# Qwen3-TTS-12Hz: the files every one of the four repositories has alike, then each repository's commit, config.json and model.safetensors.
_QWEN_COMMON = [
    ("generation_config.json", "f1b90b4513f3b34c62851049e2492d7b4c5940daf1276f89c82b8ef04127f3aa", 245),
    ("merges.txt", "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3", 1_671_839),
    ("preprocessor_config.json", "efdde1022ea9d76928bf7a9cd53139138f5ba2e466e837f08f6105ab1af1c119", 127),
    ("speech_tokenizer/config.json", "ee65bb901c876664ab8707c487157aa1a6ee57c65969b28fb5ec9dc211e68167", 2_336),
    ("speech_tokenizer/configuration.json", "6bc26d64eb5024b4d1dab5a52371958b429256d6c9d59787f1f5294a54e0cebd", 76),
    ("speech_tokenizer/model.safetensors", "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258", 682_293_092),
    ("speech_tokenizer/preprocessor_config.json", "fcb3805e597e786d4067706e602f6688524640f8d3396790e2e09b5942fcbdfb", 234),
    ("tokenizer_config.json", "dc3c31c3bdaedd5016382bb3cbe07323026775ad51f5a4fb564505992ae4a670", 7_344),
    ("vocab.json", "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910", 2_776_833),
]
_QWEN = {
    "0.6B": {
        "Base": ("5d83992436eae1d760afd27aff78a71d676296fc", [("config.json", "2e714c787c8edb98b05432685cddb634add2de4d4e645f653d68251ef72ba011", 4_494),
                    ("model.safetensors", "180b3b10eb1c9f1b4db7806d5475bae3071c0243c299d49926bab1da3b6946f6", 1_829_344_272)]),
        "CustomVoice": ("85e237c12c027371202489a0ec509ded67b5e4b5", [("config.json", "81aca2b6fac304944d8acf345272d8a9a727d5fc2e2e66b222ab4729340c7455", 4_908),
                    ("model.safetensors", "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb", 1_811_626_576)]),
    },
    "1.7B": {
        "Base": ("fd4b254389122332181a7c3db7f27e918eec64e3", [("config.json", "b4f01752d15a488abde3e1ab44723ae4f4b9e68a4037257b098b3737893cc1f9", 4_494),
                    ("model.safetensors", "38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c", 3_857_413_744)]),
        "CustomVoice": ("0c0e3051f131929182e2c023b9537f8b1c68adfe", [("config.json", "17a07f527a1c25ea30b4e023a184482a23d3e279d697b1dc81b1bde498d29cf9", 4_908),
                    ("model.safetensors", "38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137", 3_833_402_552)]),
    },
}

# unsloth/orpheus-3b-0.1-ft at eae2b6e5e429c81b95ac42a883ac64f126583d43
_ORPHEUS_FILES = [
    ("config.json", "48720eff2b6096b5b332b87f4a2c23f1c073059c4c34571ba2a0c857487511f1", 890),
    ("generation_config.json", "7743d26d00c238b85ae33b7c4bd0064c5dc7aceb6b28aae7e70e173ba668298a", 230),
    ("model-00001-of-00002.safetensors", "1064bbba044a8ba0f5e1c88a9f301399c958875ca0db5fb216b7c7d39c91a6ca", 4_991_037_968),
    ("model-00002-of-00002.safetensors", "6fac70f10fa2d2c37e06952366d43a0a4363b7e9e5fe48572b29f4a21b3479a9", 1_610_725_592),
    ("model.safetensors.index.json", "38b3093cc8266199752dbd76dda1ccd5bc0702d0d6b75f25a2286536f709163a", 20_919),
    ("special_tokens_map.json", "f17390e64274b3117a7f99afa87eb1de93097b820842c68bd626aff53d73d7f5", 508),
    ("tokenizer.json", "fc3fecb199b4170636dbfab986d25f628157268d37b861f9cadaca60b1353bce", 22_849_547),
    ("tokenizer_config.json", "d670e34e9ea7f2e30083a94cc83f1aefc3baa4c93116616fde446322514f5894", 5_407_514),
    ("chat_template.jinja", "5816fce10444e03c2e9ee1ef8a4a1ea61ae7e69e438613f3b17b69d0426223a4", 3_827),
]
# hubertsiuzdak/snac_24khz at d73ad176a12188fcf4f360ba3bf2c2fbbe8f58ec
_SNAC_FILES = [
    ("config.json", "e119b9366d4f5e73c6ca5f31137c4ff361578bbb132953a5203afe037c4012be", 300),
    ("pytorch_model.bin", "4b8164cc6606bfa627f1a784734c1e539891518f1191ed9194fe1e3b9b4bff40", 79_488_254),
]
# nari-labs/Dia-1.6B-0626 at ef2795fcc29c5abe6ffc91fd33808588b49bbc66
_DIA_FILES = [
    ("config.json", "4f4e9c50e6898fa79d6fe16e9991bebef07a4d3dd6926eee5eb7f7e39c88e04f", 1_396),
    ("generation_config.json", "df7c1f7e4dc1c3e35811b7b2e49abd89d8a8b9599ec732eb6b0d17d678548ac4", 238),
    ("model-00001-of-00002.safetensors", "9cf8f82d9f87d408f5da9cc0d73a3e9c4d9ca3633a4df3be4d283277b7ff1b1a", 4_993_046_400),
    ("model-00002-of-00002.safetensors", "54bf3a47ac13e28ba6193659d57bc63a46875685b211b071f782c32efc834343", 1_451_637_544),
    ("model.safetensors.index.json", "89664ffd667582a41e71c9aa1a4ba9de8187ec9dcd1e7711d97ec57d6e32dec5", 30_833),
    ("preprocessor_config.json", "7dfa85b765a7c5b3fb3907abf43ece88cd9edcbf39402e3e32e056b3275e5433", 243),
    ("audio_tokenizer_config.json", "1552f8bdccdfe0a98a8e93f9eb369de315e66a07b1530eba0be41513e2ee8f5e", 98),
    ("special_tokens_map.json", "4373f3b7455d5a34196fca520de10a0af2af9d7cc05f6177b4be73a8bcfb3f74", 277),
    ("tokenizer_config.json", "6c84315ff3c5214a5846bc3c08cb23fe01146906e4abf395519316732d602e64", 803),
]
# descript/dac_44khz at c1bc521685adf9cfe247bc39a5ca58917eda1ac4
_DAC_FILES = [
    ("config.json", "4eb55fb9af1990b8d608184ad29b70e358589719af7ea8d3c06998f7c2264a64", 541),
    ("model.safetensors", "6128ebff483a41422b0164d079a3773b0d8d82e64c4293d775994cbf8baf913a", 306_507_276),
    ("preprocessor_config.json", "c7d295758ce5777d6d88fef1996e94adc8ef3e2237ddfc5ecc24d1407aaddd7d", 206),
]

CATALOG = [_whisper(s) for s in _WHISPER] + [
    _entry("kokoro", "tts", "kokoro", "Kokoro", "kokoro", "apache-2.0",
           [_url(_KOKORO_BASE + "kokoro-v1.0.fp16.onnx", "kokoro-v1.0.fp16.onnx", "c1610a859f3bdea01107e73e50100685af38fff88f5cd8e5c56df109ec880204", 177_464_787),
            _url(_KOKORO_BASE + "voices-v1.0.bin", "voices-v1.0.bin", "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d", 28_214_398)],
           size=205_679_185, languages=("it", "en", "fr", "es", "pt", "ja", "zh", "hi"), verify=("sha256",),
           probe=["kokoro-v1.0.fp16.onnx", "voices-v1.0.bin"],
           venv=_t("Runs inside the voice service with onnxruntime; no extra environment.",
                   "Gira dentro il servizio vocale con onnxruntime; nessun ambiente in più."),
           notes=_t("The fastest engine, 17 fixed voices in 8 languages, Italian included. The ONNX files are the kokoro-onnx conversion (MIT code) of "
                    "hexgrad's Kokoro-82M v1.0, whose weights are Apache-2.0. The release publishes no digest: the two SHA-256 are the Hugging Face LFS "
                    "ids that independent mirrors on Hugging Face give for files of these exact sizes, and a download is refused if they differ.",
                    "Il motore più veloce, 17 voci fisse in 8 lingue, italiano compreso. I file ONNX sono la conversione kokoro-onnx (codice MIT) di "
                    "Kokoro-82M v1.0 di hexgrad, i cui pesi sono Apache-2.0. Il rilascio non pubblica un digest: i due SHA-256 sono gli id LFS di Hugging "
                    "Face che mirror indipendenti su Hugging Face danno per file di queste dimensioni esatte, e un download è rifiutato se differiscono.")),
    _entry("piper", "tts", "piper", "Piper", "piper", "piper-voices", _PIPER_SOURCES, languages=("it", "en"), size=sum(v[4] for v in _PIPER_VOICES),
           probe=[v[0] + ".onnx" for v in _PIPER_VOICES],
           venv=_t("Runs inside the voice service with the piper-tts package, version 1.3 or newer (GPL-3.0-or-later, with its own espeak-ng data); no extra environment.",
                   "Gira dentro il servizio vocale con il pacchetto piper-tts, versione 1.3 o successiva (GPL-3.0-or-later, con i propri dati espeak-ng); nessun ambiente in più."),
           notes=_t("Four voices from the rhasspy/piper-voices repository, a .onnx file and its .onnx.json each: Italian Riccardo (x_low) and Serena (medium), "
                    "English US LJ Speech (medium) and English GB Cori (medium). The voice name is the file's base name. Other voices (Hungarian, for one) are put "
                    "in the same folder by hand. Italian Paola and English Alba are left out on purpose: they were finetuned from the Lessac voice, whose data is for research only.",
                    "Quattro voci dal repository rhasspy/piper-voices, un file .onnx e il suo .onnx.json ciascuna: italiano Riccardo (x_low) e Serena (medium), "
                    "inglese US LJ Speech (medium) e inglese GB Cori (medium). Il nome della voce è il nome base del file. Altre voci (l'ungherese, per esempio) si mettono "
                    "a mano nella stessa cartella. Paola (italiano) e Alba (inglese) mancano di proposito: derivano dalla voce Lessac, i cui dati sono solo per ricerca.")),
    _entry("chatterbox", "tts", "chatterbox", "Chatterbox Multilingual", "chatterbox", "mit",
           [_pinned("ResembleAI/chatterbox", "5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18", _CHATTERBOX_FILES)], size=_total(_CHATTERBOX_FILES),
           languages=("it", "en", "fr", "es", "ru", "de", "pt"),
           venv=_t("Own Python environment (chatterbox-venv with the chatterbox-tts package, 0.1.7 installed), torch with CUDA. The package also pulls resemble-perth, "
                   "which watermarks the audio it makes.",
                   "Ambiente Python proprio (chatterbox-venv con il pacchetto chatterbox-tts, installato la 0.1.7), torch con CUDA. Il pacchetto porta anche resemble-perth, "
                   "che mette una filigrana nell'audio prodotto."),
           notes=_t("Voice cloning, high quality, slow. The six files are those that ChatterboxMultilingualTTS.from_local of chatterbox-tts 0.1.7 reads from the folder "
                    "(its own download list for ResembleAI/chatterbox); the repository's newer v3 and the English-only files are not installed. The package supports 23 "
                    "languages, the voice service lists seven. Cangjie5_TC.json is only for Chinese and 0.1.7 asks Hugging Face for it by itself; a later package version "
                    "may want other files.",
                    "Clonazione di voce, qualità alta, lento. I sei file sono quelli che ChatterboxMultilingualTTS.from_local di chatterbox-tts 0.1.7 legge dalla cartella "
                    "(la sua lista di download di ResembleAI/chatterbox); i v3 più recenti del repository e i file solo inglese non si installano. Il pacchetto supporta 23 "
                    "lingue, il servizio vocale ne elenca sette. Cangjie5_TC.json serve solo al cinese e la 0.1.7 lo chiede da sé a Hugging Face; una versione più recente "
                    "del pacchetto potrebbe volere altri file.")),
    _entry("xtts-v2", "tts", "xtts", "XTTS v2", "xtts", "cpml-1.0",
           [_pinned("coqui/XTTS-v2", "6c2b0d75eae4b7047358e3b6bd9325f857d43f77", _XTTS_FILES)], size=_total(_XTTS_FILES), noncommercial=True,
           languages=("it", "en", "fr", "es", "ru", "de", "pt", "pl", "tr", "nl", "cs", "ar", "zh", "ja", "ko", "hu", "hi"),
           venv=_t("Own Python environment (tts-venvs/xtts), torch with CUDA, the coqui-tts package with transformers older than 5.",
                   "Ambiente Python proprio (tts-venvs/xtts), torch con CUDA, il pacchetto coqui-tts con transformers precedente alla 5."),
           notes=_t("Zero-shot voice cloning, 17 languages and 58 built-in speakers. Non-commercial use only (the license text, LICENSE.txt, is installed with it). "
                    "The seven files are what the repository's main branch holds for running it, at one commit; its sample WAV files and hash.md5 are left out.",
                    "Clonazione di voce zero-shot, 17 lingue e 58 voci integrate. Solo uso non commerciale (il testo della licenza, LICENSE.txt, si installa con il modello). "
                    "I sette file sono quelli che il ramo main del repository ha per farlo girare, a un commit fisso; gli WAV di esempio e hash.md5 non ci sono.")),
    _entry("cosyvoice-3", "tts", "cosyvoice", "CosyVoice 3 (0.5B)", "cosyvoice", "apache-2.0",
           [_pinned("FunAudioLLM/Fun-CosyVoice3-0.5B-2512", "29e01c4e8d000f4bcd70751be16fa94bf3d85a18", _COSYVOICE_FILES)], size=_total(_COSYVOICE_FILES),
           languages=("it", "en", "fr", "es", "ru", "de", "ja", "ko", "zh"),
           venv=_t("Own Python environment (tts-venvs/cosyvoice, torch with CUDA) and the CosyVoice code in tts-venvs/cosyvoice-repo (FunAudioLLM/CosyVoice, Apache-2.0), "
                   "neither part of this download.",
                   "Ambiente Python proprio (tts-venvs/cosyvoice, torch con CUDA) e il codice di CosyVoice in tts-venvs/cosyvoice-repo (FunAudioLLM/CosyVoice, Apache-2.0), "
                   "nessuno dei due fa parte di questo download."),
           notes=_t("Voice cloning, streaming; the best rendering of Italian of the engines here. No Hungarian. The twelve files are what CosyVoice3 reads with "
                    "load_trt and load_vllm off (the helper's setting): the YAML, the language model, flow and vocoder weights, the speaker and speech tokenizers and the "
                    "Qwen2 skeleton in CosyVoice-BlankEN. Left out: the TensorRT estimator and the batch tokenizer (not loaded), llm.rl.pt (the reinforcement-trained "
                    "variant, which the code does not load), the optional ttsfrd text normaliser. The list comes from the code at the commit the helper uses, not from a run.",
                    "Clonazione di voce, streaming; la miglior resa dell'italiano tra i motori qui. Niente ungherese. I dodici file sono quelli che CosyVoice3 legge con "
                    "load_trt e load_vllm spenti (l'impostazione dell'helper): il file YAML, i pesi del modello di linguaggio, del flow e del vocoder, i tokenizzatori di "
                    "voce e di parlante e lo scheletro Qwen2 in CosyVoice-BlankEN. Esclusi: lo stimatore TensorRT e il tokenizzatore batch (non caricati), llm.rl.pt (la "
                    "variante addestrata con rinforzo, che il codice non carica), il normalizzatore di testo ttsfrd facoltativo. L'elenco viene dal codice al commit che usa "
                    "l'helper, non da una prova.")),
    _qwen("0.6B"), _qwen("1.7B"),
    _entry("orpheus-3b", "tts", "orpheus", "Orpheus 3B", "orpheus", "orpheus-llama",
           [_pinned("unsloth/orpheus-3b-0.1-ft", "eae2b6e5e429c81b95ac42a883ac64f126583d43", _ORPHEUS_FILES),
            _pinned("hubertsiuzdak/snac_24khz", "d73ad176a12188fcf4f360ba3bf2c2fbbe8f58ec", _SNAC_FILES, subdir="snac_24khz")],
           size=_total(_ORPHEUS_FILES, _SNAC_FILES), languages=("en",), verify=("license",),
           venv=_t("Own Python environment (tts-venvs/orpheus), torch with CUDA, transformers and the snac package.",
                   "Ambiente Python proprio (tts-venvs/orpheus), torch con CUDA, transformers e il pacchetto snac."),
           notes=_t("Eight fixed voices and expressive tags such as <laugh>; English only. It also needs the SNAC audio codec, installed with it in snac_24khz/. "
                    "The official canopylabs repositories are gated on Hugging Face (a login and accepted terms, and they list no file hashes), so the entry uses unsloth's "
                    "open copy of orpheus-3b-0.1-ft, as the helper does. The multilingual research releases (canopylabs/3b-es_it-ft-research_release for Italian, and the "
                    "French, German, Korean, Chinese and Hindi ones) are gated too and are not in the catalog.",
                    "Otto voci fisse e tag espressivi come <laugh>; solo inglese. Serve anche il codec audio SNAC, installato insieme in snac_24khz/. "
                    "I repository ufficiali di canopylabs sono ad accesso controllato su Hugging Face (un accesso e termini accettati, e non elencano gli hash dei file), "
                    "quindi la voce usa la copia aperta di unsloth di orpheus-3b-0.1-ft, come fa l'helper. Le versioni multilingue di ricerca (canopylabs/3b-es_it-ft-research_release "
                    "per l'italiano, e quelle per francese, tedesco, coreano, cinese e hindi) sono ad accesso controllato anch'esse e non sono nel catalogo.")),
    _entry("dia-1.6b", "tts", "dia", "Dia 1.6B", "dia", "dia-dac",
           [_pinned("nari-labs/Dia-1.6B-0626", "ef2795fcc29c5abe6ffc91fd33808588b49bbc66", _DIA_FILES),
            _pinned("descript/dac_44khz", "c1bc521685adf9cfe247bc39a5ca58917eda1ac4", _DAC_FILES, subdir="dac_44khz")],
           size=_total(_DIA_FILES, _DAC_FILES), languages=("en",), verify=("dac-local",),
           venv=_t("Own Python environment (tts-venvs/dia, shared with Qwen3-TTS), torch with CUDA, a transformers release that has DiaForConditionalGeneration.",
                   "Ambiente Python proprio (tts-venvs/dia, condiviso con Qwen3-TTS), torch con CUDA, una versione di transformers che ha DiaForConditionalGeneration."),
           notes=_t("Dialogue with non-verbal markers; English only. The model is the Hugging Face (transformers) format of the 0626 release; the repository's duplicate "
                    "dia-v1.pth and pytorch_model.bin (6.4 GB each) are left out. Its weights are stored in 32 bits and the helper loads them in 16. The processor also needs "
                    "Descript's DAC 44 kHz codec: audio_tokenizer_config.json names it as descript/dac_44khz on the Hub, so it is installed in dac_44khz/, but the helper "
                    "loads the processor with local_files_only and does not point it to that folder yet (to do in the engine, not proven).",
                    "Dialogo con marcatori non verbali; solo inglese. Il modello è il formato Hugging Face (transformers) della versione 0626; dia-v1.pth e pytorch_model.bin "
                    "del repository (6,4 GB ciascuno, doppioni) non ci sono. I pesi sono in 32 bit e l'helper li carica in 16. Il processore ha bisogno anche del codec DAC 44 kHz "
                    "di Descript: audio_tokenizer_config.json lo nomina come descript/dac_44khz sull'Hub, quindi si installa in dac_44khz/, ma l'helper carica il processore con "
                    "local_files_only e non lo punta ancora a quella cartella (da fare nel motore, non provato).")),
    _entry("musetalk-v15", "avatar", "musetalk", "MuseTalk v1.5 weights", "musetalk", "musetalk",
           [_pinned("TMElyralab/MuseTalk", "2bcb936e2fddb4d86db4c62fd45b387d0c061571",
                    [("musetalkV15/musetalk.json", "5b6923aee04d71692e0e9846c471e0a4ea07a4f686d39545e472bd4ba17e1b47"),
                     ("musetalkV15/unet.pth", "7ebf6c98c181e20838e4c0054e96e944ac60d5d692cc01db42839fe11b787007")]),
            _pinned("stabilityai/sd-vae-ft-mse", "31f26fdeee1355a5c34592e401dd41e45d25a493",
                    [("config.json", "92d3dfb746fca211a2c9e019e285f8597412211728dce3c5bcf4eda0f2d62e7e"),
                     ("diffusion_pytorch_model.bin", "1b4889b6b1d4ce7ae320a02dedaeff1780ad77d415ea0d744b476155c6377ddc")], subdir="sd-vae"),
            _pinned("openai/whisper-tiny", "169d4a4341b33bc18d8881c4b69c2e104e1cc0af",
                    [("config.json", "ffdccec4f3211f4c63310f2b7098f309fe70f3952cedc5e4d11e43f5b2379b98"),
                     ("pytorch_model.bin", "9607f98a2b22d9e229ae43c52ecea79dcede9e0c5cfae67e8da6eda86d8aac1d"),
                     ("preprocessor_config.json", "9b5cd03a36fbb8a627c64d98a5b5b126ead95a77720723944487311f0110b666")], subdir="whisper"),
            _pinned("yzd-v/DWPose", "1a7144101628d69ee7a3768d1ee3a094070dc388",
                    [("dw-ll_ucoco_384.pth", "0d9408b13cd863c4e95a149dd31232f88f2a12aa6cf8964ed74d7d97748c7a07")], subdir="dwpose"),
            _pinned("vivym/face-parsing-bisenet", "768606b84908769d31ddd78b2e1105319839edfa",
                    [("79999_iter.pth", "468e13ca13a9b43cc0881a9f99083a430e9c0a38abd935431d1c28ee94b26567")], subdir="face-parse-bisent"),
            _url("https://download.pytorch.org/models/resnet18-5c106cde.pth", "face-parse-bisent/resnet18-5c106cde.pth",
                 "5c106cde386e87d4033832f2996f5493238eda96ccf559d1d62760c4de0613f8", 46_827_520),
            _url("https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth", "s3fd/s3fd-619a316812.pth",
                 "619a31681264d3f7f7fc7a16a42cbbe8b23f31a256f75a366e5a1bcd59b33543", 89_843_225)],
           size=4_482_904_130, noncommercial=True, verify=("sha256", "license"),
           venv=_t("Own Python environment (avatar venv: Python 3.10, torch with CUDA, mmcv built from source).",
                   "Ambiente Python proprio (venv dell'avatar: Python 3.10, torch con CUDA, mmcv compilato dai sorgenti)."),
           notes=_t("The eleven files the avatar worker loads, each pinned to a commit with its SHA-256: the v1.5 unet, the SD VAE, Whisper tiny, DWPose, the face-parsing "
                    "network and its ResNet-18 backbone, and the S3FD face detector. The face-parsing file comes from a Hugging Face copy of the original (its Google Drive "
                    "link is not fetchable); the ResNet-18 and S3FD files come from their authors' own servers, with the SHA-256 that Hugging Face copies give for files of "
                    "the same size. SyncNet is not here: only MuseTalk's training reads it. Non-commercial: the face-parsing network was trained on CelebAMask-HQ.",
                    "Gli undici file che il worker dell'avatar carica, ciascuno fissato a un commit con il suo SHA-256: l'unet v1.5, il VAE SD, Whisper tiny, DWPose, la rete di "
                    "face parsing e la sua base ResNet-18, e il rilevatore di volti S3FD. Il file di face parsing viene da una copia su Hugging Face dell'originale (il suo link "
                    "Google Drive non si può scaricare); i file ResNet-18 e S3FD vengono dai server dei loro autori, con lo SHA-256 che le copie su Hugging Face danno per file "
                    "della stessa dimensione. SyncNet non c'è: lo legge solo l'addestramento di MuseTalk. Non commerciale: la rete di face parsing è addestrata su CelebAMask-HQ.")),
    _entry("wav2lip-int8", "avatar", "wav2lip", "Wav2Lip INT8 (for the phones)", "wav2lip", "wav2lip-research", [], size=36_664_106,
           noncommercial=True, verify=("license", "source"), probe=["wav2lip_int8.onnx"], selectable=False,
           sha256="2e297b310dcd397ac94e467814570eaf18edec322c29abd14565dcd3b6896f97",
           venv=_t("Not run on the PC: the phone runs it with ONNX Runtime.", "Non gira sul PC: lo fa il telefono con ONNX Runtime."),
           notes=_t("The lip-sync model the Android app downloads from this PC (GET /avatar/models/wav2lip.onnx). It is put in the folder by hand "
                    "(or by the importer): nothing downloads it, because its source is not known. Searched for a public file of this size and SHA-256 "
                    "among about a hundred Hugging Face repositories about Wav2Lip and the GitHub repositories and releases of the Wav2Lip ONNX projects "
                    "(instant-high/wav2lip-onnx-256 publishes 214 MB and 107 MB files and states no license): none matches.",
                    "Il modello di lip-sync che l'app Android scarica da questo PC (GET /avatar/models/wav2lip.onnx). Si mette nella cartella a mano "
                    "(o con l'importatore): nessuno lo scarica, perché la sua fonte non è nota. Cercato un file pubblico con questa dimensione e questo SHA-256 "
                    "in un centinaio di repository Hugging Face su Wav2Lip e nei repository e rilasci GitHub dei progetti Wav2Lip ONNX "
                    "(instant-high/wav2lip-onnx-256 pubblica file da 214 MB e 107 MB e non dichiara una licenza): nessuno corrisponde.")),
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
    _entry("sherpa-diarization", "diarization", "sherpa-onnx", "sherpa-onnx diarization (pyannote segmentation + CAM++)", "sherpa-onnx", "pyannote-camplus",
           [_pinned("csukuangfj/sherpa-onnx-pyannote-segmentation-3-0", "9403a6902bb58e3d5ae8c7e77c3422de279db2e0",
                    [("model.onnx", "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079")], subdir=_SEG_DIR),
            _url(_SHERPA + "speaker-recongition-models/" + _CAMPP, _CAMPP, "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2", 28_281_164)],
           size=34_274_077, verify=("sha256",), probe=[_SEG_DIR + "/model.onnx", _CAMPP],
           venv=_t("Runs inside the voice service with the sherpa-onnx package (CPU).", "Gira dentro il servizio vocale con il pacchetto sherpa-onnx (CPU)."),
           notes=_t("Who speaks when: a segmentation model and a speaker embedding model. The segmentation is the Hugging Face copy of the sherpa-onnx author (the "
                    "release archive would need unpacking), pinned to a commit; it is the conversion of pyannote/segmentation-3.0, which is gated on Hugging Face "
                    "(the original needs a login and accepted terms, this copy does not). The CAM++ file comes from the sherpa-onnx release, whose SHA-256 is the one "
                    "four Hugging Face copies give for a file of the same size.",
                    "Chi parla quando: un modello di segmentazione e uno di impronta vocale. La segmentazione è la copia su Hugging Face dell'autore di sherpa-onnx (l'archivio "
                    "del rilascio andrebbe estratto), fissata a un commit; è la conversione di pyannote/segmentation-3.0, che su Hugging Face è ad accesso controllato "
                    "(l'originale chiede un accesso e termini accettati, questa copia no). Il file CAM++ viene dal rilascio di sherpa-onnx, il cui SHA-256 è quello che "
                    "quattro copie su Hugging Face danno per un file della stessa dimensione.")),
    _entry("silero-vad-v4", "vad", "silero", "Silero VAD v4", "silero", "mit",
           [_url(_SILERO, "silero_vad_v4.onnx", "a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28", 1_807_522)],
           size=1_807_522, verify=("sha256",), probe=["silero_vad_v4.onnx"],
           venv=_t("Runs inside the voice service with onnxruntime.", "Gira dentro il servizio vocale con onnxruntime."),
           notes=_t("Voice activity detection. The file is files/silero_vad.onnx of the v4.0 tag of snakers4/silero-vad (MIT), the one with the h and c states the voice code feeds, "
                    "saved here as silero_vad_v4.onnx. The repository publishes no digest: the SHA-256 is the one four Hugging Face copies give for a file of the same size.",
                    "Rilevamento dell'attività vocale. Il file è files/silero_vad.onnx del tag v4.0 di snakers4/silero-vad (MIT), quello con gli stati h e c che il codice vocale "
                    "usa, salvato qui come silero_vad_v4.onnx. Il repository non pubblica un digest: lo SHA-256 è quello che quattro copie su Hugging Face danno per un file della stessa dimensione.")),
    _entry("multilingual-e5-small", "embedding", "e5", "Multilingual E5 small", "e5", "mit",
           [_pinned("intfloat/multilingual-e5-small", "614241f622f53c4eeff9890bdc4f31cfecc418b3",
                    [("onnx/model.onnx", "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"),
                     ("onnx/config.json", "bbb7c1333fc4b3e27fbc9cd5d2070aabcc1d4dfb99917c3633e772f97545a6b6"),
                     ("onnx/tokenizer.json", "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
                     ("onnx/tokenizer_config.json", "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b"),
                     ("onnx/special_tokens_map.json", "d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7"),
                     ("onnx/sentencepiece.bpe.model", "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865"),
                     ("1_Pooling/config.json", "987f7a67a38fa564c849bb5d277c52ab9088a84368fc0be31a354125aebb12a0"),
                     ("sentence_bert_config.json", "948201d8329907aae938fa62f9ceeed53f5694dacc2b87b9f3b78b37ee986529")])],
           size=492_421_811, languages=("it", "en"), probe=["onnx/model.onnx", "onnx/tokenizer.json"],
           venv=_t("Runs on the CPU with onnxruntime and a tokenizer reading tokenizer.json; the text must start with \"query: \" or \"passage: \", the output is mean-pooled and normalised "
                   "(384 numbers, at most 512 tokens).",
                   "Gira sulla CPU con onnxruntime e un tokenizzatore che legge tokenizer.json; il testo deve iniziare con \"query: \" o \"passage: \", l'uscita si media e si normalizza "
                   "(384 numeri, al massimo 512 token)."),
           notes=_t("The embedding model proposed for the internal RAG (nothing uses it yet): small, multilingual with Italian and English, MIT, trained for retrieval. Chosen over "
                    "paraphrase-multilingual-MiniLM-L12-v2 (Apache-2.0, the same size but cut at 128 tokens and trained for paraphrase, not retrieval) and BAAI/bge-m3 (MIT, "
                    "about 2.3 GB of weights). The full-precision ONNX file is installed; the same repository has a 118 MB int8 file for CPUs with AVX-512 VNNI.",
                    "Il modello di embedding proposto per il RAG interno (per ora nulla lo usa): piccolo, multilingue con italiano e inglese, MIT, addestrato per il recupero. Scelto al posto di "
                    "paraphrase-multilingual-MiniLM-L12-v2 (Apache-2.0, stessa dimensione ma tagliato a 128 token e addestrato per parafrasi, non per recupero) e di BAAI/bge-m3 (MIT, "
                    "circa 2,3 GB di pesi). Si installa il file ONNX a piena precisione; lo stesso repository ha un file int8 da 118 MB per CPU con AVX-512 VNNI.")),
]
CATALOG += ENGINE_PACKS
# The voice code names no embedding model (it asks the LLM server for embeddings); the one entry is a proposal for the internal RAG, which does not read it yet.
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
