"""SadTalker as an engine: Linly-Talker's `SadTalker.test2` run on the avatar's photo and the audio, at 256 px with the cropped preprocessing.

The code is vendored in `vendor/sadtalker/` and copied into the run folder before a run; the weights are installed on request (catalog ids `sadtalker-*`). It makes
a video with head motion from one image and loads all its models on every call, so it draws a sentence at a time, not live.

Licence (to confirm): see vendor/sadtalker/LICENSE-NOTE.md. Nothing about it is shown in the GUI."""
from __future__ import annotations

from .pack import PackEngine


class SadTalkerEngine(PackEngine):
    ID = "sadtalker"
    key = "sadtalker"
    runner = "sadtalker_run.py"
    models = ("sadtalker-weights", "sadtalker-gfpgan")
    fps_default = 20
