"""The engines TaDaMoo knows and where each runs by default.

Only facts are written as numbers: the size of the Wav2Lip file, the figures a project states (named as such), the heights this build offers.
Anything not measured or not settled is None or says so, and a licence that is not settled has `verify`. `selection.py` turns the person's
choices into what the gateway and the pages use."""
from __future__ import annotations

from pathlib import Path

from .base import AvatarEngine, EngineInfo, License, Training


def _t(en: str, it: str) -> dict:
    return {"en": en, "it": it}


NOT_MEASURED = _t("Not measured here.", "Non misurato qui.")
_VIDEO_NEEDS = _t("A video of the person to learn from, 3 to 5 minutes.", "Un video della persona da cui imparare, da 3 a 5 minuti.")
_GPU_TRAIN = _t("A GPU for the training, once per person.", "Una GPU per l'addestramento, una volta per persona.")

# (output heights, default) shared by the engines that draw a face crop and are scaled by the enhance stage
_SMALL = ((96, 144, 192, 240, 288, 384, 480), 240)

INFOS: tuple[EngineInfo, ...] = (
    EngineInfo(
        id="visemes", name="Visemes", kind="generic", runs_on=("pc", "web", "android", "esp32"), weight_mb=0,
        performance=_t("No model: a timeline of mouth shapes from the speech engine and 15 sprites made from the photo, drawn on the CPU. Speed not measured here.",
                       "Nessun modello: una linea del tempo di forme della bocca dal motore vocale e 15 sprite fatti dalla foto, disegnati dalla CPU. Velocità non misurata qui."),
        native_resolution=None, resolutions=_SMALL[0], default_resolution=_SMALL[1],
        license=License("own", False, False, _t("TaDaMoo's own code.", "Codice di TaDaMoo.")),
        training=Training(False, None, False), needs=(_t("A photo of the person.", "Una foto della persona."),),
        mode="visemes", requires="viseme_sprites", integrated=True),
    EngineInfo(
        id="wav2lip_gan96", name="Wav2Lip GAN 96 px", kind="generic", runs_on=("pc", "web", "android"), weight_mb=37,
        performance=_t("Runs an INT8 ONNX model (36,664,106 bytes) on the device, on the straightened 96 px face. Speed on a phone not measured here.",
                       "Esegue un modello ONNX INT8 (36.664.106 byte) sul dispositivo, sul volto raddrizzato a 96 px. Velocità su un telefono non misurata qui."),
        native_resolution=96, resolutions=(96, 144, 192, 288, 384, 480), default_resolution=96,
        license=License("wav2lip-research", True, True,
                        _t("Non-commercial: the weights are from the original authors, for research. The ONNX file has no declared provenance: verify it before giving it to others.",
                           "Non commerciale: i pesi sono degli autori originali, per la ricerca. Il file ONNX non ha una provenienza dichiarata: verificarla prima di darlo ad altri.")),
        training=Training(False, None, False),
        needs=(_t("A photo of the person.", "Una foto della persona."), _t("The model file in the models folder.", "Il file del modello nella cartella dei modelli.")),
        mode="wav2lip", requires="wav2lip_assets", integrated=True),
    EngineInfo(
        id="wav2lip_v288", name="Wav2Lip v2 288 px (Linly-Talker)", kind="generic", runs_on=("pc", "web", "android"), weight_mb=None,
        performance=NOT_MEASURED, native_resolution=288, resolutions=(144, 192, 288, 384, 480), default_resolution=288,
        license=License("unverified", False, True,
                        _t("A newly trained model: its licence and the provenance of its weights are not verified. Treat it as non-commercial until they are.",
                           "Un modello addestrato di recente: licenza e provenienza dei pesi non sono verificate. Trattarlo come non commerciale finché non lo sono.")),
        training=Training(False, None, False),
        needs=(_t("A photo of the person.", "Una foto della persona."), _t("Its weights, which are not in this build.", "I suoi pesi, che non sono in questa versione.")),
        mode="wav2lip", requires="wav2lip_assets", integrated=False),
    EngineInfo(
        id="ultralight", name="Ultralight-Digital-Human", kind="personalized", runs_on=("pc", "web", "android"), weight_mb=None,
        performance=NOT_MEASURED, native_resolution=None, resolutions=(160, 256, 384, 480), default_resolution=256,
        license=License("Apache-2.0", False, False,
                        _t("Apache 2.0. The audio encoder (wenet or hubert) has its own licence.", "Apache 2.0. Il codificatore audio (wenet o hubert) ha una licenza propria.")),
        training=Training(True, (3, 5), True),
        needs=(_VIDEO_NEEDS, _GPU_TRAIN, _t("An audio encoder, wenet or hubert.", "Un codificatore audio, wenet o hubert.")),
        mode=None, requires="trained", integrated=False),
    EngineInfo(
        id="feathertalk", name="FeatherTalk", kind="personalized", runs_on=("pc", "web", "android"), weight_mb=None,
        performance=_t("Draws a 144x144 face with FeatherHuBERT (3.36M parameters), on the CPU through MNN. Speed not measured here.",
                       "Disegna un volto 144x144 con FeatherHuBERT (3,36 milioni di parametri), sulla CPU con MNN. Velocità non misurata qui."),
        native_resolution=144, resolutions=(144, 192, 288, 384, 480), default_resolution=144,
        license=License("Apache-2.0", False, False, _t("Apache 2.0.", "Apache 2.0.")),
        training=Training(True, (3, 5), True),
        needs=(_VIDEO_NEEDS, _GPU_TRAIN,
               _t("FeatherHuBERT has Chinese-only, incomplete weights: the quality in Italian is unverified.",
                  "FeatherHuBERT ha pesi solo cinesi e incompleti: la resa in italiano non è verificata.")),
        mode=None, requires="trained", integrated=False),
    EngineInfo(
        id="musetalk_15", name="MuseTalk 1.5", kind="generic", runs_on=("pc", "web"), weight_mb=None,
        performance=_t("Over 30 fps on a GPU, per the project; not measured here. It uses the PC's VRAM.",
                       "Oltre 30 fps su una GPU, secondo il progetto; non misurato qui. Usa la VRAM del PC."),
        native_resolution=256, resolutions=(256, 384, 480, 720), default_resolution=480,
        license=License("MIT", False, False,
                        _t("MIT, and the weights for any purpose. The third-party models it uses (Whisper, SD VAE, DWPose, face parsing, S3FD) keep their own licences.",
                           "MIT, e i pesi per qualunque scopo. I modelli di terzi che usa (Whisper, SD VAE, DWPose, face parsing, S3FD) seguono ciascuno la propria licenza.")),
        training=Training(False, None, False),
        needs=(_t("A GPU on this PC.", "Una GPU su questo PC."), _t("A video of the person prepared for it.", "Un video della persona preparato per lui."),
               _t("Its weights in the models folder.", "I suoi pesi nella cartella dei modelli.")),
        mode="video", requires="musetalk_prepared", integrated=True),
    EngineInfo(
        id="sadtalker", name="SadTalker", kind="generic", runs_on=("pc",), weight_mb=None,
        performance=_t("Makes a video from one image, with head motion. Not measured here.", "Fa un video da una sola immagine, con il movimento della testa. Non misurato qui."),
        native_resolution=None, resolutions=(256, 384, 512), default_resolution=256,
        license=License("unverified", False, True, _t("The licence of the code and the weights is to verify before use.", "La licenza del codice e dei pesi è da verificare prima dell'uso.")),
        training=Training(False, None, False),
        needs=(_t("A GPU on this PC.", "Una GPU su questo PC."), _t("Its weights, which are not in this build.", "I suoi pesi, che non sono in questa versione.")),
        mode="video", requires="none", integrated=False),
    EngineInfo(
        id="er_nerf", name="ER-NeRF", kind="personalized", runs_on=("pc",), weight_mb=None,
        performance=_t("Not measured here. Training takes hours for each person.", "Non misurato qui. L'addestramento richiede ore per ogni persona."),
        native_resolution=None, resolutions=(256, 384, 512), default_resolution=256,
        license=License("unverified", False, True, _t("The licence of the code and the weights is to verify before use.", "La licenza del codice e dei pesi è da verificare prima dell'uso.")),
        training=Training(True, None, True),
        needs=(_t("A GPU on this PC, and hours of training for each person.", "Una GPU su questo PC e ore di addestramento per ogni persona."),
               _t("PyTorch3D and CUDA modules compiled on this PC.", "PyTorch3D e moduli CUDA compilati su questo PC."),
               _t("Very fragile: it breaks with the versions of Python, PyTorch and CUDA, and with the compiler.",
                  "Molto fragile: si rompe con le versioni di Python, PyTorch e CUDA e con il compilatore.")),
        mode="video", requires="trained", integrated=False),
)


class DescribedEngine(AvatarEngine):
    """An engine that has a descriptor and no code of its own yet: every interface method raises NotImplementedForNow."""

    def __init__(self, info: EngineInfo) -> None:
        self._info = info

    @property
    def info(self) -> EngineInfo:
        return self._info


for _info in INFOS:
    _info.validate()
ENGINES: dict[str, AvatarEngine] = {i.id: DescribedEngine(i) for i in INFOS}

# What a target uses when the person has chosen nothing, and the output height it draws at. The PC's default is MuseTalk, the video for
# the PC's own features; the web preview and the ESP32 use the visemes; the Android app uses Wav2Lip.
DEFAULT_ENGINES = {"pc": "musetalk_15", "web": "visemes", "android": "wav2lip_gan96", "esp32": "visemes"}
DEFAULT_RESOLUTION = {"pc": 480, "web": 240, "android": 96, "esp32": 96}
# The engine reported for a mode when the configured one does not draw it: the integrated engine of that mode.
MODE_ENGINE = {"visemes": "visemes", "wav2lip": "wav2lip_gan96", "video": "musetalk_15"}


def get(engine_id) -> AvatarEngine | None:
    return ENGINES.get(engine_id) if isinstance(engine_id, str) else None


def info_of(engine_id) -> EngineInfo | None:
    engine = get(engine_id)
    return engine.info if engine else None


def for_target(target: str) -> list[EngineInfo]:
    """The descriptors of the engines that run on `target`, in table order."""
    return [i for i in INFOS if target in i.runs_on]


def describe(avatar_dir=None, models_base: str | None = None) -> list[dict]:
    """Every engine as a dict, with its availability for the avatar in `avatar_dir` (None = no avatar)."""
    path = Path(avatar_dir) if avatar_dir else None
    return [{**i.as_dict(), "status": i.status_of(path, models_base)} for i in INFOS]

