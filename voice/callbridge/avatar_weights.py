"""The MuseTalk weights the avatar worker reads, and which of them are missing.

All of them live under `<models folder>/avatar/musetalk-v15/` and nowhere else; nothing here, and nothing in the worker, fetches a file.
TaDaMoo's download installs all of them (serve/voice_catalog.py); a person may also put them there by hand. Until every file is there the avatar
stays unloaded and says which file is absent."""
from __future__ import annotations

import os

from . import models_store

MODEL_ID = "musetalk-v15"

#: relative path -> where it comes from (what the message tells the person)
REQUIRED = {
    "musetalkV15/musetalk.json": "MuseTalk v1.5 (TMElyralab/MuseTalk)",
    "musetalkV15/unet.pth": "MuseTalk v1.5 (TMElyralab/MuseTalk)",
    "sd-vae/config.json": "the SD VAE (stabilityai/sd-vae-ft-mse)",
    "sd-vae/diffusion_pytorch_model.bin": "the SD VAE (stabilityai/sd-vae-ft-mse)",
    "whisper/config.json": "the audio encoder (openai/whisper-tiny)",
    "whisper/pytorch_model.bin": "the audio encoder (openai/whisper-tiny)",
    "whisper/preprocessor_config.json": "the audio encoder (openai/whisper-tiny)",
    "dwpose/dw-ll_ucoco_384.pth": "DWPose (yzd-v/DWPose)",
    "face-parse-bisent/79999_iter.pth": "face parsing (BiSeNet)",
    "face-parse-bisent/resnet18-5c106cde.pth": "face parsing (ResNet-18 backbone)",
    "s3fd/s3fd-619a316812.pth": "the face detector S3FD",
}


def folder(base: str | None = None) -> str:
    """`<models folder>/avatar/musetalk-v15`, or "" when there is no models folder."""
    return models_store.folder("avatar", MODEL_ID, base)


def missing(base: str | None = None) -> list[str]:
    """The required files that are not there, as paths relative to the model folder; all of them when there is no models folder."""
    where = folder(base)
    return [rel for rel in REQUIRED if not (where and os.path.isfile(os.path.join(where, *rel.split("/"))))]


def reason(base: str | None = None) -> str:
    """Why the avatar cannot load, in plain words; "" when every weight is in place."""
    absent = missing(base)
    if not absent:
        return ""
    where = folder(base) or "<models folder>/avatar/" + MODEL_ID
    shown = ", ".join(absent[:4]) + (f" and {len(absent) - 4} more" if len(absent) > 4 else "")
    return f"The avatar is missing {len(absent)} weight file(s) in {where}: {shown}. Install the MuseTalk model from Settings > Voice."


def env(base: str | None = None) -> dict[str, str]:
    """What the worker is told about where its weights are."""
    return {"MUSETALK_MODELS": folder(base)}
