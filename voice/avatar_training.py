"""Training a personalised avatar engine (Ultralight-Digital-Human, FeatherTalk) from the video of one avatar: a plan the person confirms, one job at a time,
progress from the epochs, cancel, and the result kept in the avatar's folder.

    POST /avatar/train/plan {avatar, engine, epochs?, asr?, fresh?}   what would be done and needs: the video, an estimated duration, the VRAM (a figure only when the
                                                                       upstream documentation states one), what is created, the checks, and a `token`
    POST /avatar/train/start {token, confirm: true}                    starts the plan of that token if it is unchanged and every check is met
    GET  /avatar/train/status                                          the job: state, phase, percent, epoch, eta, the end of the log
    POST /avatar/train/cancel                                          ends the job and every process it started
    POST /avatar/train/setup {engine, asr?, avatar?, probe?}           the Set up panel: each prerequisite with what fixes it, the engines Python and where it came from,
                                                                       with `probe` the packages and CUDA that Python has, and what is trained and exported for `avatar`
    POST /avatar/train/python {path}                                   choose the engines Python ("" forgets the choice; nothing is run)
    POST /avatar/train/export {avatar, engine}                         make the files for a phone again from the trained model; `phone` says whether its package
                                                                       (phone.json, the self-test: phone_export.py) was made or why not

The video is the avatar's training video (voice/avatar_training_video.py) when it has one, else its longest clip; the plan says which in `video_source`.
Nothing starts without `confirm: true` on a token whose plan was looked at. The avatar stays usable with the other engines meanwhile. The files of an engine
are in `<avatar>/engines/<engine>/` (voice/avatar_engines/personalised.py); the end of the log is `log_tail.txt` there."""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import shutil
import threading
import time
from collections import deque
from pathlib import Path

from callbridge import models_store
from callbridge.avatar_store import AvatarStore, StoreError

from voice import functions
from voice.avatar_engines import registry
from voice.avatar_engines.ernerf import ErNerfEngine
from voice.avatar_engines.pack import EngineFailed, EngineNotReady
from voice.avatar_engines import personalised, phone_export
from voice.avatar_engines.personalised import LOG_TAIL, Cancelled, Check, EngineError, PersonalisedEngine, Runner

log = logging.getLogger("voice.avatar_training")

VIDEO_SECONDS = (120.0, 600.0)            # refused outside: under 2 minutes there is too little to learn from, over 10 the preparation grows with no gain stated
# Seconds per frame per epoch is not stated by either project. Until a training has been timed on this PC the estimate uses this range: an assumption made
# here, shown as one. After a run the measured figure replaces it.
ASSUMED_S_PER_FRAME_EPOCH = (0.003, 0.02)
PHASE_SPAN = {"convert": (0.0, 2.0), "preprocess": (2.0, 12.0), "train": (12.0, 95.0), "export": (95.0, 100.0),
              # ER-NeRF's steps (voice/avatar_engines/ernerf.py), spans in the proportion of their weights
              "check": (0.0, 2.0), "prepare": (2.0, 12.0), "head": (12.0, 52.0), "torso": (52.0, 100.0), "done": (100.0, 100.0)}
PLANS_KEPT = 16
TIMING_FILE = "avatar-engine-timing.json"


class TrainingError(Exception):
    """The request is refused: `status` and `code` for the route, `extra` is added to the body (the plan, for example)."""

    def __init__(self, status: int, code: str, message: str, extra: dict | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra or {}


def probe_video(path: Path) -> dict:
    """{seconds, fps, width, height} of a video, or ValueError when it cannot be read."""
    import cv2
    cap = cv2.VideoCapture(str(path))
    try:
        frames, fps = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0), float(cap.get(cv2.CAP_PROP_FPS) or 0)
        width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    finally:
        cap.release()
    if frames <= 0 or fps <= 0:
        raise ValueError("the video cannot be read")
    return {"seconds": round(frames / fps, 1), "fps": round(fps, 2), "width": width, "height": height}


def _t(en: str, it: str) -> dict:
    return {"en": en, "it": it}


def _clock(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


class TrainingJobs:
    def __init__(self, store: AvatarStore, *, models_base=None, python: str | None = None, probe=probe_video, free_vram=functions.free_vram_mib,
                 gpu: int = 0, which=shutil.which, clock=time.time, probe_env=None) -> None:
        self.store = store
        self.probe_env = probe_env                           # replaceable: (python, modules) -> what personalised.probe_environment answers
        self.models_base = models_base or models_store.root
        self.python, self.probe, self.free_vram, self.gpu, self.which, self.clock = python, probe, free_vram, gpu, which, clock
        self._lock = threading.Lock()
        self._plans: dict[str, dict] = {}
        self._runner: Runner | None = None
        self._engine: ErNerfEngine | None = None                 # the running engine when it stops itself (it has no Runner)
        self._thread: threading.Thread | None = None
        self._tail: deque = deque(maxlen=LOG_TAIL)
        self._state: dict = {"state": "idle"}
        self._train_mark: tuple | None = None
        atexit.register(self.shutdown)

    # ---- the plan
    def engine_of(self, engine) -> PersonalisedEngine | ErNerfEngine:
        found = registry.get(engine)
        if not isinstance(found, (PersonalisedEngine, ErNerfEngine)):
            raise TrainingError(400, "engine", "Send the id of a personalised engine: ultralight, feathertalk or er_nerf.")
        return found

    def _video(self, avatar: str) -> tuple[Path | None, dict | None, list[dict], str]:
        """(the file, what is known of it, the reasons it cannot be used, its source): the avatar's training video when it has one, else its longest clip."""
        own = self.store.training_video(avatar)
        if own is not None:
            try:
                info = self.probe(own)
            except (ValueError, OSError):
                return own, None, [{"code": "training_video_unreadable", **_t("The training video of this avatar cannot be read: remove it and add another.",
                                                                              "Il video di addestramento di questo avatar non si può leggere: toglilo e aggiungine un altro.")}], "training"
            return own, info, self._length_problems(info["seconds"], "The training video", "Il video di addestramento", False), "training"
        best: tuple[Path, dict] | None = None
        for name in self.store.clips(avatar):
            path = self.store.clip_path(avatar, name)
            try:
                info = self.probe(path)
            except (ValueError, OSError):
                continue
            if best is None or info["seconds"] > best[1]["seconds"]:
                best = (path, info)
        if best is None:
            return None, None, [{"code": "no_video", **_t("This avatar has no video that can be read: add a training video of 3 to 5 minutes in the Train section.",
                                                          "Questo avatar non ha un video leggibile: aggiungi un video di addestramento di 3-5 minuti nella sezione Addestramento.")}], "clip"
        path, info = best
        return path, info, self._length_problems(info["seconds"], "The video", "Il video", True), "clip"

    @staticmethod
    def _length_problems(s: float, en: str, it: str, hint: bool) -> list[dict]:
        low, high = VIDEO_SECONDS
        long = _clock(s)
        add_en = " Add a training video of 3 to 5 minutes in the Train section." if hint else ""
        add_it = " Aggiungi un video di addestramento di 3-5 minuti nella sezione Addestramento." if hint else ""
        if s < low:
            return [{"code": "too_short", **_t(f"{en} is {s:.0f} s long ({long}): at least {low / 60:.0f} minutes are needed, and 3 to 5 are best.{add_en}",
                                               f"{it} dura {s:.0f} s ({long}): servono almeno {low / 60:.0f} minuti, e da 3 a 5 vanno meglio.{add_it}")}]
        if s > high:
            return [{"code": "too_long", **_t(f"{en} is {s:.0f} s long ({long}): at most {high / 60:.0f} minutes are accepted, and 3 to 5 are best.{add_en}",
                                              f"{it} dura {s:.0f} s ({long}): se ne accettano al massimo {high / 60:.0f} minuti, e da 3 a 5 vanno meglio.{add_it}")}]
        return []

    def video_file(self, avatar: str, source: str, name: str) -> Path:
        """The file a plan named: the training video, or the clip `name`."""
        if source == "training":
            path = self.store.training_video(avatar)
            if path is None or path.name != name:
                raise TrainingError(409, "plan_changed", "The training video changed since the plan was shown: look at the plan again.")
            return path
        return self.store.clip_path(avatar, name)

    def _timing(self) -> dict:
        try:
            data = json.loads((Path(self.store.root).parent / TIMING_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _record_timing(self, engine: str, seconds: float, frames: int, epochs: float) -> None:
        if seconds <= 0 or frames <= 0 or epochs < 1:
            return
        data = self._timing()
        data[engine] = {"s_per_frame_epoch": seconds / (frames * epochs), "frames": frames, "epochs": epochs, "measured": time.strftime("%Y-%m-%d")}
        try:
            (Path(self.store.root).parent / TIMING_FILE).write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("training timing not saved: %s", e)

    def _gpu_check(self) -> Check:
        free = self.free_vram(self.gpu)
        if free is None:
            return Check("gpu", False, "the free memory of the GPU cannot be read (no NVML and no nvidia-smi): the training needs a GPU",
                         "la memoria libera della GPU non si può leggere (né NVML né nvidia-smi): l'addestramento richiede una GPU")
        return Check("gpu", True, f"GPU {self.gpu} has {free} MiB free (what the training needs is not measured)",
                     f"la GPU {self.gpu} ha {free} MiB liberi (quanto serve all'addestramento non è misurato)")

    def plan(self, avatar, engine, epochs=None, asr=None, fresh=False) -> dict:
        """What training `engine` for `avatar` would do. Raises TrainingError for an unknown engine, avatar or option; everything else is in the answer
        (`problems` for the video, `checks` for the environment, `ok` when there is none)."""
        eng = self.engine_of(engine)
        try:
            folder = self.store.existing(avatar)
        except StoreError as e:
            raise TrainingError(404, "not_found", str(e)) from None
        if isinstance(eng, ErNerfEngine):
            return self._plan_ernerf(eng, avatar, folder, epochs, asr)
        try:
            opts = eng.options(asr, epochs)
        except ValueError as e:
            raise TrainingError(400, "options", str(e)) from None
        fresh = fresh is True
        base = self.models_base()
        clip, video, problems, source = self._video(avatar)
        running = self.busy()
        checks = [Check("idle", not running, "no training is running" if not running else "another training is running: wait for it or cancel it",
                        "nessun addestramento è in corso" if not running else "un altro addestramento è in corso: aspettalo o annullalo")]
        checks += eng.preflight(base, opts, self.python, self.which) + [self._gpu_check()]
        resume = 0 if fresh or clip is None or not eng.dataset_ready(folder, opts, clip) else eng.resume_epoch(folder)
        out = {"avatar": avatar, "engine": eng.ID, "name": eng.info.name, "options": opts, "fresh": fresh, "video": None, "video_source": source, "problems": problems,
               "checks": [c.as_dict() for c in checks], "resume_epoch": resume, "license": eng.info.as_dict()["license"]}
        if video is not None:
            out["video"] = {"clip": clip.name, **video}
            out["estimate"] = self._estimate(eng.ID, int(video["seconds"] * opts["fps"]), max(0, opts["epochs"] - resume))
        out["vram"] = {"mib": None, "free_mib": self.free_vram(self.gpu),
                       **_t("Not measured: the upstream documentation states no figure. A GPU with room for a PyTorch training is needed.",
                            "Non misurata: la documentazione originale non dà una cifra. Serve una GPU con spazio per un addestramento PyTorch.")}
        rel = f"engines/{eng.ID}"
        out["creates"] = [
            _t(f"{rel}/dataset: the video at {opts['fps']} frames per second, its frames, landmarks and audio features",
               f"{rel}/dataset: il video a {opts['fps']} fotogrammi al secondo, i suoi fotogrammi, i punti del volto e le caratteristiche audio"),
            _t(f"{rel}/checkpoints: what the training saves (every 5 epochs)", f"{rel}/checkpoints: ciò che l'addestramento salva (ogni 5 epoche)"),
            _t(f"{rel}/export: the files for a phone (ONNX; MNN when MNNConvert is set)", f"{rel}/export: i file per un telefono (ONNX; MNN se MNNConvert è impostato)"),
            _t(f"{rel}/trained.json and log_tail.txt: the result and the end of the log", f"{rel}/trained.json e log_tail.txt: il risultato e la fine del log")]
        out["ok"] = not problems and all(c.ok for c in checks)
        out["token"] = hashlib.sha256(json.dumps([avatar, eng.ID, opts, fresh, None if clip is None else [source, clip.name, clip.stat().st_size, clip.stat().st_mtime_ns], resume],
                                                 sort_keys=True).encode()).hexdigest()
        with self._lock:
            self._plans[out["token"]] = {"avatar": avatar, "engine": eng.ID, "epochs": epochs, "asr": asr, "fresh": fresh}
            while len(self._plans) > PLANS_KEPT:
                self._plans.pop(next(iter(self._plans)))
        return out

    def _plan_ernerf(self, eng: ErNerfEngine, avatar: str, folder: Path, epochs, asr) -> dict:
        """The plan of an ER-NeRF training: the same answer as the others', with the steps in `stages` and no epochs or audio model to choose."""
        if epochs is not None or asr is not None:
            raise TrainingError(400, "options", "ER-NeRF takes no epochs or audio model here: its steps are the build check, the data preparation, the head and the torso.")
        clip, video, problems, source = self._video(avatar)
        running = self.busy()
        made = eng.training_plan(folder, clip, None, self.models_base())
        checks = [Check("idle", not running, "no training is running" if not running else "another training is running: wait for it or cancel it",
                        "nessun addestramento è in corso" if not running else "un altro addestramento è in corso: aspettalo o annullalo")]
        checks += [Check(c["id"], c["ok"], c["en"], c["it"]) for c in made["checks"] if c["id"] != "clip"] + [self._gpu_check()]
        rel = f"engines/{eng.ID}"
        out = {"avatar": avatar, "engine": eng.ID, "name": eng.info.name, "options": made["options"], "fresh": False, "video": None, "video_source": source, "problems": problems,
               "checks": [c.as_dict() for c in checks], "resume_epoch": 0, "license": eng.info.as_dict()["license"], "stages": made["stages"], "note": made["note"]}
        if video is not None:
            out["video"] = {"clip": clip.name, **video}
        out["vram"] = {"mib": None, "free_mib": self.free_vram(self.gpu),
                       **_t("Not measured: the upstream documentation states no figure. A GPU with room for a NeRF training is needed.",
                            "Non misurata: la documentazione originale non dà una cifra. Serve una GPU con spazio per un addestramento NeRF.")}
        out["creates"] = [_t(f"{rel}/data: the video and what the data preparation makes from it", f"{rel}/data: il video e ciò che la preparazione dei dati ne ricava"),
                          _t(f"{rel}/head and {rel}/torso: the checkpoints of the two training steps", f"{rel}/head e {rel}/torso: i checkpoint dei due passi di addestramento"),
                          _t(f"{rel}/trained.json: the result", f"{rel}/trained.json: il risultato")]
        out["ok"] = not problems and all(c.ok for c in checks)
        out["token"] = hashlib.sha256(json.dumps([avatar, eng.ID, made["options"], None if clip is None else [source, clip.name, clip.stat().st_size, clip.stat().st_mtime_ns]],
                                                 sort_keys=True).encode()).hexdigest()
        with self._lock:
            self._plans[out["token"]] = {"avatar": avatar, "engine": eng.ID, "epochs": None, "asr": None, "fresh": False}
            while len(self._plans) > PLANS_KEPT:
                self._plans.pop(next(iter(self._plans)))
        return out

    def _estimate(self, engine: str, frames: int, epochs: int) -> dict:
        measured = self._timing().get(engine)
        rate = measured.get("s_per_frame_epoch") if isinstance(measured, dict) else None
        if isinstance(rate, (int, float)) and rate > 0:
            low = high = rate
            basis = "measured"
            note = _t(f"Estimate, not a promise: {frames} frames for {epochs} epochs, at the speed of the last training timed on this PC ({measured.get('measured', '')}). "
                      "The preparation of the video before the training is not included.",
                      f"Stima, non una promessa: {frames} fotogrammi per {epochs} epoche, alla velocità dell'ultimo addestramento misurato su questo PC ({measured.get('measured', '')}). "
                      "La preparazione del video prima dell'addestramento non è inclusa.")
        else:
            low, high = ASSUMED_S_PER_FRAME_EPOCH
            basis = "assumed"
            note = _t(f"Rough guess, not a promise: {frames} frames for {epochs} epochs at an assumed {low} to {high} seconds per frame per epoch. Neither project states a speed and "
                      "none has been measured on this PC yet; the first training replaces the guess with a measurement. The preparation of the video is not included.",
                      f"Stima grossolana, non una promessa: {frames} fotogrammi per {epochs} epoche a {low}-{high} secondi supposti per fotogramma per epoca. Nessuno dei due progetti dichiara "
                      "una velocità e su questo PC non è ancora stata misurata; il primo addestramento sostituisce la stima con una misura. La preparazione del video non è inclusa.")
        return {"basis": basis, "frames": frames, "epochs": epochs, "frame_epochs": frames * epochs, "seconds": [round(frames * epochs * low), round(frames * epochs * high)], "note": note}

    # ---- the Set up panel
    def setup(self, engine, asr=None, avatar=None, probe=False) -> dict:
        """What the engine needs, item by item, and where it stands for `avatar`. Reads files; with `probe` it also asks the engines Python what it has (that starts it)."""
        eng = self.engine_of(engine)
        try:
            opts = eng.options(asr, None)
        except ValueError as e:
            raise TrainingError(400, "options", str(e)) from None
        base = self.models_base()
        items = eng.preflight(base, opts, self.python, self.which)
        path, source = personalised.python_choice(self.python, base)
        out = {"engine": eng.ID, "name": eng.info.name, "options": opts, "python": {"path": path, "source": source, "env": personalised.PYTHON_ENV},
               "items": [c.as_dict() for c in items], "ok": all(c.ok for c in items), "environment": None, "trained": None}
        if probe:
            env = eng.environment_checks(self.python, opts, self.probe_env, base)
            out["environment"] = [c.as_dict() for c in env]
            out["ok"] = out["ok"] and bool(env) and all(c.ok for c in env)
        if avatar is not None:
            try:
                folder = self.store.existing(avatar)
            except StoreError as e:
                raise TrainingError(404, "not_found", str(e)) from None
            mark = eng.trained(folder)
            if mark is not None:
                export = folder / "engines" / eng.ID / "export" / "export.json"
                out["trained"] = {"epochs": mark.get("epochs"), "finished": mark.get("finished"), "export_error": mark.get("export_error", ""), "exported": export.is_file()}
        return out

    def set_python(self, path) -> dict:
        if not isinstance(path, str):
            raise TrainingError(400, "python", "Send the full path of the Python, or an empty text to forget it.")
        try:
            saved = personalised.save_python(path, self.models_base())
        except ValueError as e:
            raise TrainingError(400, "python", str(e)) from None
        effective, source = personalised.python_choice(self.python, self.models_base())
        return {"path": saved, "effective": effective, "source": source}

    def export(self, avatar, engine) -> dict:
        """The files for a phone again, from the trained model. One thing at a time: it is refused while a training runs."""
        eng = self.engine_of(engine)
        try:
            folder = self.store.existing(avatar)
        except StoreError as e:
            raise TrainingError(404, "not_found", str(e)) from None
        if self.busy():
            raise TrainingError(409, "busy", "A training is running: export when it ends.")
        try:
            files = eng.export_for_device(folder, "android", models_base=self.models_base(), python=self.python)
        except (EngineError, ValueError, OSError) as e:
            raise TrainingError(422, "export_failed", str(e)) from None
        return {"avatar": avatar, "engine": eng.ID, "files": len(files), **({"phone": self._phone_package(eng, folder)} if eng.ID in phone_export.ENGINES else {})}

    @staticmethod
    def _phone_package(eng, folder: Path) -> dict:
        """The phone's package (voice/avatar_engines/phone_export.py) after an export. A failure is a note in the answer, not an error: the training and the ONNX files stay."""
        try:
            return phone_export.package(folder, eng.ID)
        except phone_export.PhoneExportError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:                                   # noqa: BLE001 (onnxruntime or OpenCV failing on a file; the export itself is done)
            log.exception("avatar phone package")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- the job
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def busy_with(self, avatar: str) -> bool:
        return self.busy() and self._state.get("avatar") == avatar

    def start(self, token, confirm) -> dict:
        known = self._plans.get(token) if isinstance(token, str) else None
        if known is None:
            raise TrainingError(409, "unknown_plan", "That plan is not known: ask for the plan again.")
        p = self.plan(**known)
        if confirm is not True:
            raise TrainingError(409, "confirm", "A training starts only after you confirm its plan: send confirm: true with its token.", {"plan": p})
        if self.busy():
            raise TrainingError(409, "busy", "A training is already running.")
        if p["token"] != token:
            raise TrainingError(409, "plan_changed", "The plan changed since it was shown (the video or the options): look at it again.", {"plan": p})
        if not p["ok"]:
            bad = [x["en"] for x in p["problems"]] + [c["en"] for c in p["checks"] if not c["ok"]]
            raise TrainingError(422, "not_possible", "; ".join(bad), {"plan": p})
        eng = self.engine_of(p["engine"])
        folder = self.store.existing(p["avatar"])
        clip = self.video_file(p["avatar"], p["video_source"], p["video"]["clip"])
        with self._lock:
            if self.busy():
                raise TrainingError(409, "busy", "A training is already running.")
            self._tail.clear()
            self._train_mark = None
            self._runner = Runner(self._log)
            self._engine = eng if isinstance(eng, ErNerfEngine) else None
            self._state = {"state": "running", "avatar": p["avatar"], "engine": p["engine"], "name": p["name"], "started": self.clock(), "finished": None,
                           "phase": "convert", "phase_percent": 0.0, "percent": 0.0, "epoch": 0, "epochs": p["options"].get("epochs", 0), "eta_s": None,
                           "cancelling": False, "message": "", "error": ""}
            self._thread = threading.Thread(target=self._run, args=(eng, folder, clip, p), name="avatar-training", daemon=True)
            self._thread.start()
        return self.status()

    def _log(self, line: str) -> None:
        self._tail.append(line)

    def _progress(self, phase: str, percent: float, **info) -> None:
        low, high = PHASE_SPAN[phase]
        with self._lock:
            st = self._state
            st.update(phase=phase, phase_percent=round(percent, 1), percent=round(low + (high - low) * percent / 100, 1))
            if "epoch" in info:
                st.update(epoch=info["epoch"], epochs=info["epochs"])
                done = percent / 100 * info["epochs"]                 # epochs finished, counting the part of the current one
                if self._train_mark is None:
                    self._train_mark = (self.clock(), done)
                else:
                    t0, d0 = self._train_mark
                    if done > d0:
                        st["eta_s"] = int((self.clock() - t0) / (done - d0) * (info["epochs"] - done))

    def _run(self, eng: PersonalisedEngine, folder: Path, clip: Path, plan: dict) -> None:
        runner, opts = self._runner, plan["options"]
        state, message, error, phone = "failed", "", "", {}
        try:
            result = eng.prepare(folder, self._progress, clip=clip, options=opts, runner=runner, models_base=self.models_base(), python=self.python, fresh=plan["fresh"])
            state = "done"
            first = self._train_mark[1] if self._train_mark else 0
            self._record_timing(eng.ID, result["train_seconds"], result["frames"], opts.get("epochs", 0) - first)
            message = result["export_error"] and f"trained; the export for a phone failed: {result['export_error']}" or ("trained" if eng.ID == "er_nerf" else "trained and exported")
            if not result["export_error"] and eng.ID in phone_export.ENGINES:
                phone = self._phone_package(eng, folder)
        except (EngineFailed, EngineNotReady) as e:
            if "stopped" in str(e):
                state, message = "cancelled", "cancelled; what the steps made stays"
            else:
                error = str(e)
        except Cancelled:
            state, message = "cancelled", "cancelled; the checkpoints and the dataset stay, and a new training continues from them"
        except EngineError as e:
            error = str(e)
        except Exception as e:                                   # noqa: BLE001 (any failure ends the job with its reason, never a thread that dies unseen)
            log.exception("avatar training")
            error = f"{type(e).__name__}: {e}"
        folder_out = eng.folder(folder) if isinstance(eng, ErNerfEngine) else eng.engine_dir(folder)
        try:
            folder_out.mkdir(parents=True, exist_ok=True)
            (folder_out / "log_tail.txt").write_text("\n".join(self._tail), encoding="utf-8")
        except OSError as e:
            log.warning("training log not saved: %s", e)
        with self._lock:
            self._state.update(state=state, finished=self.clock(), message=message, error=error, cancelling=False, eta_s=0 if state == "done" else None, **({"phone": phone} if phone else {}))
            if state == "done":
                self._state.update(percent=100.0)

    def status(self) -> dict:
        with self._lock:
            return {**self._state, "log": list(self._tail)[-30:]}

    def cancel(self) -> dict:
        with self._lock:
            runner, running, own = self._runner, self.busy(), self._engine
            if running:
                self._state["cancelling"] = True
        if running and own is not None:
            own.stop()
        elif running and runner is not None:
            runner.cancel()
        return {"cancelling": running}

    def shutdown(self) -> None:
        """Ends a running job and its processes (the service stops)."""
        runner, own = self._runner, self._engine
        if self.busy() and own is not None:
            own.stop()
            self._thread.join(timeout=10)
        elif self.busy() and runner is not None:
            runner.cancel()
            self._thread.join(timeout=10)
