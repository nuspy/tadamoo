"""Whether an engine can be used for one avatar: do the files it draws from exist. Reads the avatar's folder and the models folder, starts nothing.

An engine says what it looks for (`EngineInfo.requires`); each kind of requirement is a function here that returns the reasons it is not met,
in English and Italian. An engine that is not integrated in this build is never available, whatever the folder holds."""
from __future__ import annotations

import json
from pathlib import Path

from callbridge import avatar_assets, avatar_weights
from callbridge.avatar_store import AvatarStore, StoreError

TRAINED_DIR = "engines"            # <avatar>/engines/<engine id>/trained.json is written when a personalized engine finished training
TRAINED_MARK = "trained.json"


def _missing(folder: Path, files) -> list[str]:
    return [f for f in files if not (folder / f).is_file()]


def _viseme_sprites(info, folder: Path, base) -> list[tuple]:
    if not (folder / "poster.jpg").is_file():
        return [("this avatar has no photo to make mouth shapes from", "questo avatar non ha una foto da cui fare le forme della bocca")]
    if _missing(folder, avatar_assets.VISEME_FILES):
        return [("the mouth sprites are not made yet: they are made from the photo the first time a device asks for the avatar's files",
                 "gli sprite della bocca non sono ancora fatti: si fanno dalla foto la prima volta che un dispositivo chiede i file dell'avatar")]
    return []


def _wav2lip_assets(info, folder: Path, base) -> list[tuple]:
    out: list[tuple] = []
    if not (folder / "poster.jpg").is_file():
        out.append(("this avatar has no photo to make the face from", "questo avatar non ha una foto da cui fare il volto"))
    elif _missing(folder, avatar_assets.WAV2LIP_FILES):
        out.append(("the straightened face and the mouth box are not made yet: they are made from the photo the first time a device asks for the avatar's files",
                    "il volto raddrizzato e il riquadro della bocca non sono ancora fatti: si fanno dalla foto la prima volta che un dispositivo chiede i file dell'avatar"))
    if avatar_assets.model_path(base) is None:
        out.append(("the Wav2Lip model file is not in the models folder (avatar/wav2lip-int8/wav2lip_int8.onnx)",
                    "il file del modello Wav2Lip non è nella cartella dei modelli (avatar/wav2lip-int8/wav2lip_int8.onnx)"))
    return out


def _musetalk_prepared(info, folder: Path, base) -> list[tuple]:
    out: list[tuple] = []
    absent = avatar_weights.missing(base)
    if absent:
        out.append((f"{len(absent)} MuseTalk weight file(s) are missing from the models folder (avatar/musetalk-v15)",
                    f"mancano {len(absent)} file dei pesi di MuseTalk nella cartella dei modelli (avatar/musetalk-v15)"))
    try:
        store = AvatarStore(folder.parent)
        ready = bool(set(store.clips(folder.name)) & set(store.prepared(folder.name)))
    except StoreError:
        ready = False
    if not ready:
        out.append(("no video of this avatar is prepared for MuseTalk", "nessun video di questo avatar è preparato per MuseTalk"))
    return out


def _trained(info, folder: Path, base) -> list[tuple]:
    mark = folder / TRAINED_DIR / info.id / TRAINED_MARK
    try:
        done = isinstance(json.loads(mark.read_text(encoding="utf-8")), dict)
    except (OSError, ValueError):
        done = False
    if done:
        return []
    return [("this avatar has not been trained for this engine", "questo avatar non è stato addestrato per questo motore")]


REQUIREMENTS = {"viseme_sprites": _viseme_sprites, "wav2lip_assets": _wav2lip_assets, "musetalk_prepared": _musetalk_prepared,
                "trained": _trained, "none": lambda info, folder, base: []}


def of(info, avatar_dir, models_base=None) -> dict:
    """{available, reason, reason_it}: the reasons are joined with "; " and empty when the engine can be used."""
    problems: list[tuple] = []
    if not info.integrated:
        problems.append(("its code and weights are not part of this build yet", "il suo codice e i suoi pesi non sono ancora in questa versione"))
    folder = Path(avatar_dir) if avatar_dir else None
    if folder is None or not folder.is_dir():
        problems.append(("no avatar is chosen", "nessun avatar è scelto"))
    else:
        problems += REQUIREMENTS[info.requires](info, folder, models_base)
    if not problems:
        return {"available": True, "reason": "", "reason_it": ""}
    return {"available": False, "reason": "; ".join(p[0] for p in problems), "reason_it": "; ".join(p[1] for p in problems)}
