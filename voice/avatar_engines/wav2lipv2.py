"""Wav2Lip v2 (Linly-Talker's 288 px model) as an engine: Linly-Talker's `Wav2Lipv2` script run on the avatar's photo and the audio.

The code, the checkpoint and the two face models are installed on request (catalog ids `wav2lipv2-*`); nothing is bundled. It is a PC engine: the frames
it makes go to the avatar's frame path (preview, camera, the web and the enhance stage), and the model draws at its own size."""
from __future__ import annotations

from .pack import PackEngine


class Wav2LipV2Engine(PackEngine):
    ID = "wav2lip_v288"
    key = "wav2lipv2"
    runner = "wav2lipv2_run.py"
    models = ("wav2lipv2-code", "wav2lipv2-weights", "wav2lipv2-yolo-face", "wav2lipv2-landmarks")
