"""ER-NeRF as an engine: a NeRF trained for one person from a video, drawn by Linly-Talker's `NeRFTalk` wrapper.

The code (with its CUDA sources) and the weights are installed on request (catalog ids `ernerf-*`); the four CUDA modules are built by the person in the
engine environment and checked here. What one avatar gets lives under `<avatar>/engines/er_nerf/`:

    data/          the video and what the data preparation made from it (frames, parsing, landmarks, tracking, audio features, transforms)
    head/, torso/  the workspaces of the two training stages (their checkpoints)
    trained.json   written when the training finished: the checkpoint, the pose file and the audio model they were trained with

A training is four steps, each its own subprocess: the build check (once), the upstream data preparation (data_utils/process.py), the head and the
torso (runners/ernerf_train.py). `training_plan` lists them and what is missing without starting anything."""
from __future__ import annotations

import glob
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .pack import (EngineFailed, EngineNotReady, Job, PackEngine, RUNNERS, Stage, code_dir, engine_dir, environment_problems, not_installed, process_env,
                   python, spawn)
from .status import TRAINED_DIR, TRAINED_MARK

AUDIO_MODEL = "deepspeech"          # what the data preparation's default makes features with (NeRF/data_utils/process.py)
ITERS = 200000                      # `iters` of the wrapper's Options (TFG/NeRFTalk.py)
ITERS_RANGE = (1000, 1_000_000)
MODULES_MARK = "modules.json"       # written when the build check passed: <models folder>/avatar-engines/ernerf/modules.json
VIDEO = "video.mp4"
TRAIN_MODELS = ("ernerf-bfm", "ernerf-face-parsing")


class ErNerfEngine(PackEngine):
    ID = "er_nerf"
    key = "ernerf"
    runner = "ernerf_run.py"
    models = ("ernerf-code", "ernerf-audio-encoder")
    needs_photo = False
    fps_default = 25

    def __init__(self, info, spawner=spawn) -> None:
        super().__init__(info, spawner)
        self._cancelled = False

    # ---- places and marks
    def folder(self, avatar_dir) -> Path:
        return Path(avatar_dir) / TRAINED_DIR / self.ID

    def trained(self, avatar_dir) -> dict | None:
        try:
            data = json.loads((self.folder(avatar_dir) / TRAINED_MARK).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def modules_checked(self, base: str | None = None) -> bool:
        root = engine_dir(self.key, base)
        return root is not None and (root / MODULES_MARK).is_file()

    def _artifacts(self, avatar_dir, mark: dict) -> tuple[Path, Path]:
        return self.folder(avatar_dir) / str(mark.get("checkpoint", "")), self.folder(avatar_dir) / str(mark.get("pose", ""))

    # ---- what is missing
    def problems(self, avatar_dir, base: str | None = None) -> list[tuple]:
        out = environment_problems() + not_installed(self.models, base)
        if not self.modules_checked(base):
            out.append(("the CUDA modules (freqencoder, gridencoder, raymarching, shencoder) and PyTorch3D have not been checked on this PC: a training checks them first",
                        "i moduli CUDA (freqencoder, gridencoder, raymarching, shencoder) e PyTorch3D non sono stati controllati su questo PC: un addestramento li controlla per primi"))
        folder = Path(avatar_dir) if avatar_dir else None
        if folder is not None and folder.is_dir():
            mark = self.trained(folder)
            if mark is None:
                out.append(("this avatar has not been trained for this engine", "questo avatar non è stato addestrato per questo motore"))
            elif not all(p.is_file() for p in self._artifacts(folder, mark)):
                out.append(("the trained checkpoint or its pose file of this avatar is missing", "il checkpoint addestrato o il suo file di posa di questo avatar manca"))
        return out

    # ---- drawing
    def render_command(self, base, out: Path, wav: Path, resolution: int, fps: int, avatar_dir: Path) -> list[str]:
        mark = self.trained(avatar_dir) or {}
        checkpoint, pose = self._artifacts(avatar_dir, mark)
        return self.base_args(base) + ["--checkpoint", str(checkpoint), "--pose", str(pose), "--asr-model", str(mark.get("asr_model") or AUDIO_MODEL),
                                       "--audio", str(wav), "--frames-out", str(out), "--height", str(int(resolution)), "--fps", str(int(fps))]

    # ---- the training
    def training_options(self, options: dict | None = None) -> dict:
        """{iters_head, iters_torso} checked; ValueError with a plain reason."""
        options = options or {}
        out = {}
        for key in ("iters_head", "iters_torso"):
            value = options.get(key, ITERS)
            if isinstance(value, bool) or not isinstance(value, int) or not ITERS_RANGE[0] <= value <= ITERS_RANGE[1]:
                raise ValueError(f"{key} is a whole number from {ITERS_RANGE[0]} to {ITERS_RANGE[1]}")
            out[key] = value
        return out

    def training_checks(self, avatar_dir, clip: Path | None, base: str | None, options: dict) -> list[dict]:
        def check(id_: str, ok: bool, en: str, it: str) -> dict:
            return {"id": id_, "ok": ok, "en": en, "it": it}
        have = clip is not None and clip.is_file()
        out = [check("clip", have, "the video of the person is there" if have else "this avatar has no video to train from",
                     "il video della persona c'è" if have else "questo avatar non ha un video da cui addestrare")]
        data = self.folder(avatar_dir) / "data"
        spaced = " " in str(data)
        out.append(check("path", not spaced, "the data folder path has no space" if not spaced else
                         f"the path {data} has a space and the upstream data preparation does not quote it: use an avatars folder without one",
                         "il percorso della cartella dei dati non ha spazi" if not spaced else
                         f"il percorso {data} ha uno spazio e la preparazione dati originale non lo racchiude tra virgolette: usare una cartella degli avatar senza"))
        for problem in environment_problems() + not_installed(self.models + TRAIN_MODELS, base):
            out.append(check("setup", False, problem[0], problem[1]))
        return out

    def training_plan(self, avatar_dir, clip: Path | None = None, options: dict | None = None, base: str | None = None) -> dict:
        """What a training would do and what is missing, without starting anything: {engine, options, stages, checks, ready, note}."""
        opts = self.training_options(options)
        checks = self.training_checks(avatar_dir, clip, base, opts)
        stages = self.training_stages(avatar_dir, clip, opts, base, dry=True)
        return {"engine": self.ID, "options": opts, "stages": [s.as_dict() for s in stages], "checks": checks, "ready": all(c["ok"] for c in checks),
                "note": {"en": "The training takes hours and is not measured here; it needs the GPU of this PC and the CUDA modules built for it.",
                         "it": "L'addestramento richiede ore e non è misurato qui; usa la GPU di questo PC e i moduli CUDA compilati per essa."}}

    def training_stages(self, avatar_dir, clip: Path | None, opts: dict, base: str | None, dry: bool = False) -> list[Stage]:
        code, work = code_dir(self.key, base), self.folder(avatar_dir)
        data, head, torso = work / "data", work / "head", work / "torso"
        py, stages = python(), []
        if not self.modules_checked(base):
            stages.append(Stage("check", "Checking the CUDA modules and PyTorch3D", "Controllo dei moduli CUDA e di PyTorch3D", tuple(self.check_command(base)), code, 2,
                                after=lambda done: self._mark_modules(base, done)))
        stages.append(Stage("prepare", "Preparing the data from the video", "Preparazione dei dati dal video",
                            (py, "data_utils/process.py", str(data / VIDEO), "--task", "-1", "--asr", AUDIO_MODEL), code / "NeRF", 10, needs_done=False))
        train = (py, str(RUNNERS / "ernerf_train.py"), "--pack", str(code), "--data", str(data), "--asr-model", AUDIO_MODEL)
        stages.append(Stage("head", "Training the head", "Addestramento della testa",
                            train + ("--stage", "head", "--workspace", str(head), "--iters", str(opts["iters_head"])), code, 40))
        stages.append(Stage("torso", "Training the torso", "Addestramento del busto",
                            train + ("--stage", "torso", "--workspace", str(torso), "--head-workspace", str(head), "--iters", str(opts["iters_torso"])), code, 48))
        if not dry:
            data.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(clip, data / VIDEO)
        return stages

    def _mark_modules(self, base: str | None, done: dict) -> None:
        root = engine_dir(self.key, base)
        (root / MODULES_MARK).write_text(json.dumps({"modules": done.get("modules", {}), "checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}), encoding="utf-8")

    def finish_training(self, avatar_dir, opts: dict) -> dict:
        """Write trained.json from what the stages made; EngineFailed when a file is missing."""
        work = self.folder(avatar_dir)
        made = sorted(glob.glob(str(work / "torso" / "checkpoints" / "ngp_ep*.pth")))
        pose = work / "data" / "transforms_train.json"
        if not made or not pose.is_file():
            raise EngineFailed("the training ended without a torso checkpoint or a pose file (data/transforms_train.json)")
        mark = {"engine": self.ID, "checkpoint": Path(made[-1]).relative_to(work).as_posix(), "pose": pose.relative_to(work).as_posix(), "asr_model": AUDIO_MODEL,
                "iters": opts, "finished": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        (work / TRAINED_MARK).write_text(json.dumps(mark, indent=1), encoding="utf-8")
        return mark

    def prepare(self, avatar_dir: Path, progress: Callable[..., None], *, clip: Path | None = None, options: dict | None = None,
                models_base: str | None = None) -> dict:
        """Train this engine for the avatar, step by step; `progress(phase, percent)` with the id of the step. EngineNotReady when something is missing,
        EngineFailed when a step failed or `stop` was called."""
        opts = self.training_options(options)
        missing = [c for c in self.training_checks(avatar_dir, clip, models_base, opts) if not c["ok"]]
        if missing:
            raise EngineNotReady("; ".join(c["en"] for c in missing))
        self._cancelled = False
        stages = self.training_stages(avatar_dir, clip, opts, models_base)
        total, done_weight = sum(s.weight for s in stages), 0
        code = code_dir(self.key, models_base)
        for stage in stages:
            if self._cancelled:
                raise EngineFailed("the training was stopped")
            base_pct = 100.0 * done_weight / total

            def relay(event: dict, stage=stage, base_pct=base_pct) -> None:
                self.last_progress = event
                part = float(event.get("percent") or 0.0) / 100.0 * stage.weight / total * 100.0
                progress(stage.id, min(100.0, base_pct + part))
            progress(stage.id, base_pct)
            self._job = Job(list(stage.argv), stage.cwd, process_env(code), stage.id, relay, self._spawn, stage.needs_done)
            try:
                done = self._job.run()
            finally:
                self._job = None
            if stage.after is not None:
                stage.after(done)
            done_weight += stage.weight
        mark = self.finish_training(avatar_dir, opts)
        progress("done", 100.0)
        return mark

    def stop(self) -> None:
        self._cancelled = True
        super().stop()
