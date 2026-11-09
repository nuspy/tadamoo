"""What the personalised engines (Ultralight-Digital-Human, FeatherTalk) share: they are trained for one person from a video, and their upstream scripts
run in a Python environment of their own.

The upstream scripts of an engine are vendored in `vendor/<engine>/` (byte for byte, see its VENDORED.md); the weights they load are not: they are installed on
request from Settings > Voice (`engine-landmarks`, `feathertalk-weights`, `ultralight-wenet-encoder`) under `<models folder>/avatar-engines/`. Before a script runs, the
vendored folder is copied to `<models folder>/avatar-engines/<engine>/run/` and the weights are copied to the places the scripts read them from (the scripts open them
by relative path); the scripts run there as subprocesses, offline, with the Python named by TADAMOO_AVATAR_ENGINES_PYTHON (nothing installs that Python). What one
avatar gets lives under `<avatar>/engines/<engine>/`:

    dataset/        the video re-encoded at the engine's frame rate, its frames, landmarks and audio features (`ready.json` says what they were made for)
    checkpoints/    what the training saves (`last.pth` and one file per saved epoch)
    export/         what a phone needs (ONNX, and MNN when MNNConvert is set) with `export.json`
    trained.json    written when a training finished: the engine, the audio encoder, the frame rate, the epochs

A subclass says what the upstream scripts are called with (`*_cmd`); the steps, the progress, the status and the export are here."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from callbridge import models_store

from .base import AvatarEngine, EngineInfo
from .status import TRAINED_DIR, TRAINED_MARK

PYTHON_ENV = "TADAMOO_AVATAR_ENGINES_PYTHON"
MNN_ENV = "TADAMOO_MNNCONVERT"
PACKS_DIR = "avatar-engines"
VENDOR = Path(__file__).resolve().parent / "vendor"
LANDMARK_FILES = ("scrfd_2.5g_kps.onnx", "checkpoint_epoch_335.pth.tar")      # the entry `engine-landmarks`; both preprocessing scripts open them from data_utils/
EPOCHS_DEFAULT = 200                    # the epochs of the upstream examples (FeatherTalk README, Ultralight train.py)
EPOCHS_RANGE = (1, 400)
BATCH = 16                              # the batch size of the same examples
SAVE_EVERY = 5                          # the upstream default: a checkpoint is saved every 5 epochs
EXPORT_FRAMES = 100                     # the frames, from the start of the video, a phone gets to draw over
VGG19_FILE = "vgg19-dcbb9e9d.pth"       # the torchvision file both train.py scripts load for their perceptual loss
EPOCH_RE = re.compile(r"Epoch (\d+)/(\d+)")
PERCENT_RE = re.compile(r"(\d{1,3})%\|")
CHECKPOINT_RE = re.compile(r"(\d+)\.pth")
LOG_TAIL = 400


class EngineError(RuntimeError):
    """A step of the engine failed; the message says which and why."""


class Cancelled(Exception):
    """The job was cancelled."""


@dataclass(frozen=True)
class Check:
    id: str
    ok: bool
    en: str
    it: str
    install: str = ""            # the catalog entry whose Install button fixes it ("" = none)
    where: str = ""              # the folder a file goes in when it is put there by hand

    def as_dict(self) -> dict:
        return {"id": self.id, "ok": self.ok, "en": self.en, "it": self.it, **({"install": self.install} if self.install else {}), **({"where": self.where} if self.where else {})}


class BgrFrame:
    """One rendered frame: width, height and bgr() like `avatar_client.Frame`."""

    __slots__ = ("width", "height", "_img")

    def __init__(self, img) -> None:
        self._img = img
        self.height, self.width = img.shape[:2]

    def bgr(self):
        return self._img


# ---------------------------------------------------------------------------------------------- running the upstream scripts
def kill_tree(proc: subprocess.Popen) -> None:
    """Ends a process and everything it started; never leaves one behind."""
    try:
        import psutil
        root = psutil.Process(proc.pid)
        for _ in range(2):                                        # a child started while the first sweep ran is caught by the second
            victims = root.children(recursive=True) + [root]
            for p in victims:
                try:
                    p.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs(victims, timeout=5)
    except Exception:                                             # noqa: BLE001 (no psutil, or the process is already gone)
        try:
            proc.kill()
        except OSError:
            pass


class Runner:
    """Runs commands one after the other and gives every output line to `log`; `cancel()` ends the running command and its children, and every
    later `run` raises Cancelled."""

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self.log = log or (lambda line: None)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        with self._lock:
            self.cancelled.set()
            if self._proc is not None:
                kill_tree(self._proc)

    def run(self, argv: list[str], cwd, env: dict, on_line: Callable[[str], None] | None = None) -> int:
        """The exit code. Output lines end at a newline or a carriage return (a progress bar redraws one line)."""
        with self._lock:
            if self.cancelled.is_set():
                raise Cancelled()
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
            self._proc = proc
        pending = b""
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                pending += chunk
                *lines, pending = re.split(rb"[\r\n]+", pending)
                for raw in lines:
                    self._emit(raw, on_line)
            if pending:
                self._emit(pending, on_line)
            code = proc.wait()
        finally:
            if proc.poll() is None:
                kill_tree(proc)
                proc.wait()
            proc.stdout.close()
            with self._lock:
                self._proc = None
        if self.cancelled.is_set():
            raise Cancelled()
        return code

    def _emit(self, raw: bytes, on_line) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            self.log(text)
            if on_line:
                on_line(text)


# ---------------------------------------------------------------------------------------------- the Python of the engines
PYTHON_FILE = "python.json"          # <models folder>/avatar-engines/python.json: the Python of the engines, chosen in the page (shared by every engine that runs upstream code)


def _python_file(base: str | None) -> Path | None:
    where = base if base is not None else models_store.root()
    return Path(where) / PACKS_DIR / PYTHON_FILE if where else None


def saved_python(base: str | None = None) -> str:
    file = _python_file(base)
    try:
        data = json.loads(file.read_text(encoding="utf-8")) if file else {}
    except (OSError, ValueError):
        return ""
    return str(data.get("python") or "").strip() if isinstance(data, dict) else ""


def save_python(path: str, base: str | None = None) -> str:
    """Keeps the Python the person chose ("" forgets it). It must be a file that exists; nothing is run. Raises ValueError otherwise."""
    file = _python_file(base)
    if file is None:
        raise ValueError("there is no models folder to keep it in")
    path = (path or "").strip().strip('"')
    if path and (not os.path.isabs(path) or not os.path.isfile(path)):
        raise ValueError("send the full path of a Python executable that exists")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({"python": path}), encoding="utf-8")
    return path


def python_choice(python: str | None = None, base: str | None = None) -> tuple[str, str]:
    """(path, where it came from: given, env, settings or none): the argument, else the variable TADAMOO_AVATAR_ENGINES_PYTHON, else the choice made in the page."""
    if python is not None and python.strip():
        return python.strip(), "given"
    env = os.environ.get(PYTHON_ENV, "").strip()
    if env:
        return env, "env"
    saved = saved_python(base)
    return (saved, "settings") if saved else ("", "none")


PROBE_CODE = ("import importlib, json, sys\nout = {}\nfor n in sys.argv[1:]:\n    try:\n        m = importlib.import_module(n)\n        out[n] = str(getattr(m, '__version__', 'ok'))\n"
              "    except Exception:\n        out[n] = None\ntry:\n    import torch\n    out['cuda'] = bool(torch.cuda.is_available())\nexcept Exception:\n    out['cuda'] = None\n"
              "print(json.dumps(out))\n")


def probe_environment(python: str, modules, timeout: float = 180.0) -> dict:
    """{module: version or None, "cuda": bool or None} from the engines Python, or {"error": why}. Starts that Python and imports the packages (torch makes a CUDA check)."""
    try:
        run = subprocess.run([python, "-c", PROBE_CODE, *modules], capture_output=True, text=True, timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        lines = [ln for ln in run.stdout.splitlines() if ln.startswith("{")]
        return json.loads(lines[-1]) if lines else {"error": (run.stderr.strip().splitlines() or ["no answer"])[-1]}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"error": str(e)}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------- the engine
class PersonalisedEngine(AvatarEngine):
    ID = ""                              # the id in registry.py, and the folder under avatar-engines/ and <avatar>/engines/

    def __init__(self, info: EngineInfo) -> None:
        if info.id != self.ID:
            raise ValueError(f"{self.ID}: the descriptor is {info.id}")
        self._info = info
        self.which = shutil.which                  # replaceable: how the PATH is searched
        self.vendor = VENDOR / self.ID             # replaceable: the folder of the vendored scripts
        self._active: set = set()                  # the runners of the processes running for this engine now

    @property
    def info(self) -> EngineInfo:
        return self._info

    # ---- what a subclass says
    def options(self, asr: str | None = None, epochs: int | None = None) -> dict:
        """The options of one training, checked: {asr, fps, epochs, batch}. Raises ValueError with a plain reason."""
        raise NotImplementedError

    def audio_checks(self, base: str, opts: dict) -> list[Check]:
        raise NotImplementedError

    def extra_checks(self, base: str, opts: dict) -> list[Check]:
        return []

    def preprocess_cmd(self, python: str, code: Path, base: str, video: Path, opts: dict) -> tuple[list[str], Path]:
        raise NotImplementedError

    def train_cmd(self, python: str, code: Path, base: str, dataset: Path, ckpt_dir: Path, opts: dict, resume: Path | None) -> tuple[list[str], Path]:
        raise NotImplementedError

    def features_cmd(self, python: str, code: Path, base: str, wav: Path, work: Path, opts: dict) -> tuple[list[str], Path, Path]:
        """(command, cwd, the features file it writes) for the 16 kHz wav `wav`; the file is in `work` or beside the wav."""
        raise NotImplementedError

    def infer_cmd(self, python: str, code: Path, dataset: Path, feat: Path, ckpt: Path, out: Path, opts: dict) -> tuple[list[str], Path]:
        raise NotImplementedError

    def export_cmds(self, python: str, code: Path, base: str, ckpt: Path, out_dir: Path, opts: dict) -> list[tuple[list[str], Path]]:
        raise NotImplementedError

    def export_files(self, base: str, out_dir: Path, opts: dict) -> list[Path]:
        """The model files the export commands made, inside `out_dir`, and the audio encoder file the phone needs (copied there)."""
        raise NotImplementedError

    def device_input(self, opts: dict) -> dict:
        """The `input` of the manifest entry: what the phone feeds the model with."""
        raise NotImplementedError

    # ---- places
    def base_of(self, models_base: str | None) -> str:
        return models_base if models_base is not None else models_store.root()

    def run_dir(self, base: str) -> Path:
        return Path(base) / PACKS_DIR / self.ID / "run"

    def landmark_dir(self, base: str) -> Path:
        return Path(base) / PACKS_DIR / "landmarks"

    def staged_weights(self, base: str, opts: dict | None) -> list[tuple[str, Path]]:
        """(path inside the run folder, the installed weight file) for each weight the scripts open by relative path."""
        return [(f"data_utils/{name}", self.landmark_dir(base) / name) for name in LANDMARK_FILES]

    def stage(self, base: str, opts: dict | None = None) -> Path:
        """The run folder: the vendored scripts and the weights in the places the scripts read them from, copied again only when they differ. Returns its path."""
        run = self.run_dir(base)
        pairs = [(p, run / p.relative_to(self.vendor)) for p in self.vendor.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        pairs += [(src, run / rel) for rel, src in self.staged_weights(base, opts)]
        for src, dest in pairs:
            if not src.is_file():
                raise EngineError(f"{src} is missing")
            st = src.stat()
            if dest.is_file() and dest.stat().st_size == st.st_size and dest.stat().st_mtime_ns == st.st_mtime_ns:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        return run

    def weights_dir(self, base: str) -> Path:
        return Path(base) / PACKS_DIR / self.ID / "weights"

    def engine_dir(self, avatar_dir) -> Path:
        return Path(avatar_dir) / TRAINED_DIR / self.ID

    def env(self, base: str) -> dict:
        env = models_store.helper_env("", None)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "TORCH_HOME": str(Path(base) / PACKS_DIR / "torch-home"),
                    "HF_HOME": str(Path(base) / PACKS_DIR / "hf-home")})
        return env

    @staticmethod
    def python_of(python: str | None = None, base: str | None = None) -> str:
        """The Python of the engines: the one given, else the one named by TADAMOO_AVATAR_ENGINES_PYTHON, else the one chosen in the Set up panel."""
        return python_choice(python, base)[0]

    def unload(self) -> None:
        """What `ActiveEngine` calls when another engine becomes the one in use: ends the work this engine has running."""
        self.stop()

    # ---- what is loaded: nothing stays when an engine is not the one in use
    def stop(self) -> None:
        """Ends every process this engine has running for drawing or exporting, children included. Idempotent; a training is a job of its own and is not ended here."""
        for runner in list(self._active):
            runner.cancel()

    def loaded(self) -> bool:
        """A process of this engine is running now (one exists only during a render or an export: nothing is started before, nothing is left after)."""
        return bool(self._active)

    @contextmanager
    def _tracked(self, runner: "Runner"):
        self._active.add(runner)
        try:
            yield runner
        finally:
            self._active.discard(runner)

    # ---- the environment of the scripts
    MODULES = ("torch", "torchvision", "cv2", "numpy", "onnx", "onnxruntime", "librosa", "soundfile", "tqdm")

    def modules(self, opts: dict | None) -> tuple:
        return self.MODULES

    def environment_checks(self, python: str | None, opts: dict | None, probe: Callable | None = None, base: str | None = None) -> list[Check]:
        """One check per package the scripts import and one for CUDA, from asking the engines Python (it starts that Python and imports the packages: only on request)."""
        py = self.python_of(python, base)
        if not py or not Path(py).is_file():
            return []
        found = (probe or probe_environment)(py, self.modules(opts))
        if found.get("error"):
            return [Check("environment", False, f"the engines Python could not be asked: {found['error']}", f"non si è potuto interrogare il Python dei motori: {found['error']}")]
        out = []
        for name in self.modules(opts):
            v = found.get(name)
            out.append(Check(f"module-{name}", v is not None, f"{name} is installed ({v})" if v is not None else f"{name} is not installed in the engines Python",
                             f"{name} è installato ({v})" if v is not None else f"{name} non è installato nel Python dei motori"))
        cuda = found.get("cuda")
        out.append(Check("cuda", cuda is True, "PyTorch sees a CUDA GPU" if cuda else "PyTorch does not see a CUDA GPU (or torch is missing): the training needs one",
                         "PyTorch vede una GPU CUDA" if cuda else "PyTorch non vede una GPU CUDA (o manca torch): l'addestramento ne richiede una"))
        return out

    def trained(self, avatar_dir) -> dict | None:
        try:
            data = json.loads((self.engine_dir(avatar_dir) / TRAINED_MARK).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ---- checks
    def pack_checks(self, base: str, opts: dict | None, python: str | None) -> list[Check]:
        """What running the scripts needs, whatever the job: the audio encoder when `opts` says which, and the Python."""
        out: list[Check] = []
        if opts is not None:
            out += self.audio_checks(base, opts)
        py = self.python_of(python, base)
        if not py:
            out.append(Check("python", False, f"the Python of the engines is not set: name it in the Set up panel or in {PYTHON_ENV}",
                             f"il Python dei motori non è impostato: indicalo nel pannello Imposta o in {PYTHON_ENV}"))
        elif not Path(py).is_file():
            out.append(Check("python", False, f"the Python of the engines is not there: {py}", f"il Python dei motori non c'è: {py}"))
        else:
            out.append(Check("python", True, "the Python of the engines is set", "il Python dei motori è impostato"))
        return out

    def preflight(self, base: str, opts: dict, python: str | None = None, which: Callable | None = None) -> list[Check]:
        """Every check a training needs before it starts (the GPU is the job manager's)."""
        out = self.pack_checks(base, opts, python)
        land = self.landmark_dir(base) if base else None
        absent = [n for n in LANDMARK_FILES if not (land and (land / n).is_file())]
        out.append(Check("landmarks", not absent, "the face detector and landmark weights are installed" if not absent else
                         f"the face detector and landmark weights are not installed ({', '.join(absent)}): install `engine-landmarks`",
                         "i pesi del rilevatore del volto e dei suoi punti sono installati" if not absent else
                         f"i pesi del rilevatore del volto e dei suoi punti non sono installati ({', '.join(absent)}): installa `engine-landmarks`",
                         install="" if not absent else "engine-landmarks", where=str(land) if land else ""))
        ff = (which or self.which)("ffmpeg") is not None
        out.append(Check("ffmpeg", ff, "ffmpeg is found" if ff else "ffmpeg is not found on the PATH: the upstream scripts and the video conversion need it",
                         "ffmpeg c'è" if ff else "ffmpeg non è nel PATH: gli script originali e la conversione del video ne hanno bisogno"))
        vgg = Path(base) / PACKS_DIR / "torch-home" / "hub" / "checkpoints" / VGG19_FILE if base else None
        have = bool(vgg) and vgg.is_file()
        out.append(Check("vgg19", have, f"the VGG19 file of the perceptual loss is in place ({VGG19_FILE})" if have else
                         f"the training loads torchvision's VGG19 weights and nothing downloads them: put {VGG19_FILE} in {vgg}",
                         f"il file VGG19 della perceptual loss c'è ({VGG19_FILE})" if have else
                         f"l'addestramento carica i pesi VGG19 di torchvision e nessuno li scarica: metti {VGG19_FILE} in {vgg}", where=str(vgg.parent) if vgg else ""))
        return out + self.extra_checks(base, opts)

    def status_of(self, avatar_dir, models_base: str | None = None) -> dict:
        """{available, reason, reason_it, python, trained, checkpoint}: whether this avatar can be drawn by this engine now.
        Reads files and the environment only."""
        base = self.base_of(models_base)
        folder = Path(avatar_dir) if avatar_dir else None
        problems: list[Check] = []
        if folder is None or not folder.is_dir():
            problems.append(Check("avatar", False, "no avatar is chosen", "nessun avatar è scelto"))
            mark = None
        else:
            mark = self.trained(folder)
            if mark is None or not (self.engine_dir(folder) / "checkpoints" / "last.pth").is_file():
                mark = None
                problems.append(Check("trained", False, "this avatar has not been trained for this engine", "questo avatar non è stato addestrato per questo motore"))
        opts = {"asr": mark.get("asr"), "fps": mark.get("fps")} if mark else None
        checks = self.pack_checks(base, opts, None)
        problems += [c for c in checks if not c.ok]
        by_id = {c.id: c.ok for c in checks}
        return {"available": not problems, "reason": "; ".join(c.en for c in problems), "reason_it": "; ".join(c.it for c in problems),
                "python": by_id.get("python", False), "trained": mark is not None,
                "checkpoint": "checkpoints/last.pth" if mark else ""}

    # ---- the training
    def resume_epoch(self, avatar_dir) -> int:
        """The epoch a training would continue from: after the last saved one, 0 when there is none."""
        folder = self.engine_dir(avatar_dir) / "checkpoints"
        if not (folder / "last.pth").is_file() or not self.dataset_ready(avatar_dir):
            return 0
        saved = [int(m.group(1)) for p in folder.glob("*.pth") if (m := CHECKPOINT_RE.fullmatch(p.name))]
        return max(saved) + 1 if saved else 0

    def dataset_ready(self, avatar_dir, opts: dict | None = None, clip: Path | None = None) -> bool:
        """The dataset made for these options and this clip is there."""
        try:
            mark = json.loads((self.engine_dir(avatar_dir) / "dataset" / "ready.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(mark, dict) or not mark.get("frames"):
            return False
        if opts is not None and (mark.get("asr"), mark.get("fps")) != (opts.get("asr"), opts.get("fps")):
            return False
        if clip is not None:
            st = clip.stat()
            return (mark.get("clip"), mark.get("clip_bytes")) == (clip.name, st.st_size)
        return True

    @staticmethod
    def convert_cmd(clip: Path, out: Path, fps: int) -> list[str]:
        """The video at exactly the frame rate the engine's preprocessing requires, with its sound."""
        return ["ffmpeg", "-y", "-i", str(clip), "-r", str(fps), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)]

    @staticmethod
    def audio_cmd(raw: Path, wav: Path) -> list[str]:
        """Speech at 16 kHz mono, what both audio encoders read."""
        return ["ffmpeg", "-y", "-i", str(raw), "-ar", "16000", "-ac", "1", "-f", "wav", str(wav)]

    def prepare(self, avatar_dir: Path, progress: Callable[..., None], *, clip: Path | None = None, options: dict | None = None,
                runner: Runner | None = None, models_base: str | None = None, python: str | None = None, fresh: bool = False) -> dict:
        """Trains this engine for the avatar: converts the video, makes the dataset, trains, exports for a phone and writes trained.json.

        `progress(phase, percent, **info)` with phase `convert`, `preprocess`, `train` or `export` (`epoch` and `epochs` come with `train`). Raises Cancelled
        when the runner was cancelled and EngineError when a step failed. Returns what was written to trained.json."""
        if clip is None or options is None:
            raise ValueError("prepare needs the clip and the options of the plan")
        runner = runner or Runner()
        base, opts, py = self.base_of(models_base), options, self.python_of(python, self.base_of(models_base))
        failed = [c for c in self.preflight(base, opts, python) if not c.ok]
        if failed:
            raise EngineError("; ".join(c.en for c in failed))
        code, env, folder = self.stage(base, opts), self.env(base), self.engine_dir(avatar_dir)
        dataset, ckpts = folder / "dataset", folder / "checkpoints"
        if fresh or not self.dataset_ready(avatar_dir, opts, clip):
            for sub in (dataset, ckpts, folder / "export"):
                shutil.rmtree(sub, ignore_errors=True)
            (folder / TRAINED_MARK).unlink(missing_ok=True)
        dataset.mkdir(parents=True, exist_ok=True)
        if not self.dataset_ready(avatar_dir, opts, clip):
            self._make_dataset(avatar_dir, clip, opts, runner, progress, py, code, base, env)
        resume = ckpts / "last.pth" if self.resume_epoch(avatar_dir) else None
        ckpts.mkdir(parents=True, exist_ok=True)
        argv, cwd = self.train_cmd(py, code, base, dataset, ckpts, opts, resume)
        started = time.time()

        def on_train(line: str) -> None:
            m = EPOCH_RE.search(line)
            if m:
                epoch, epochs = int(m.group(1)), int(m.group(2))
                pc = PERCENT_RE.search(line)
                progress("train", min(100.0, ((epoch - 1) + (int(pc.group(1)) / 100 if pc else 0)) / epochs * 100), epoch=epoch, epochs=epochs)
        self._step(runner, argv, cwd, env, "train", on_train)
        if not (ckpts / "last.pth").is_file():
            raise EngineError("the training ended without saving a checkpoint")
        seconds = round(time.time() - started, 3)
        result = {"engine": self.ID, "asr": opts.get("asr"), "fps": opts["fps"], "epochs": opts["epochs"], "checkpoint": "checkpoints/last.pth",
                  "frames": self.frame_count(dataset), "clip": clip.name, "train_seconds": seconds, "finished": _now(), "export_error": ""}
        mark = folder / TRAINED_MARK
        mark.write_text(json.dumps(result, indent=1), encoding="utf-8")              # the export reads it; a training is kept whatever the export does
        progress("export", 0)
        try:
            self.export_for_device(avatar_dir, "android", runner=runner, models_base=models_base, python=python)
        except Cancelled:
            raise
        except (EngineError, OSError, ValueError) as e:
            result["export_error"] = str(e)
            mark.write_text(json.dumps(result, indent=1), encoding="utf-8")
        progress("export", 100)
        return result

    @staticmethod
    def frame_count(dataset: Path) -> int:
        folder = dataset / "full_body_img"
        return sum(1 for p in folder.glob("*.jpg")) if folder.is_dir() else 0

    def _step(self, runner: Runner, argv: list[str], cwd, env: dict, name: str, on_line=None) -> None:
        code = runner.run(argv, cwd, env, on_line)
        if code != 0:
            raise EngineError(f"{name} failed (exit code {code}): see the end of the log")

    def _make_dataset(self, avatar_dir, clip: Path, opts: dict, runner: Runner, progress, py: str, code: Path, base: str, env: dict) -> None:
        dataset = self.engine_dir(avatar_dir) / "dataset"
        video = dataset / "train.mp4"
        progress("convert", 0)
        self._step(runner, self.convert_cmd(clip, video, opts["fps"]), dataset, env, "the video conversion")
        progress("convert", 100)
        marks = (("extract audio", 10), ("extracting images", 25), ("extracted", 40), ("detecting landmarks", 45), ("extracting audio feature", 90))
        progress("preprocess", 0)
        argv, cwd = self.preprocess_cmd(py, code, base, video, opts)

        def on_pre(line: str) -> None:
            for text, pct in marks:
                if text in line:
                    progress("preprocess", pct)
        self._step(runner, argv, cwd, env, "the preprocessing", on_pre)
        frames = self.frame_count(dataset)
        lms = sum(1 for _ in (dataset / "landmarks").glob("*.lms")) if (dataset / "landmarks").is_dir() else 0
        if not frames or lms != frames:
            raise EngineError(f"the preprocessing made {frames} frames and {lms} landmark files: they must be the same number, and not zero")
        video.unlink(missing_ok=True)
        st = clip.stat()
        (dataset / "ready.json").write_text(json.dumps({"engine": self.ID, "asr": opts.get("asr"), "fps": opts["fps"], "frames": frames,
                                                        "clip": clip.name, "clip_bytes": st.st_size}), encoding="utf-8")
        progress("preprocess", 100)

    # ---- rendering
    def render(self, pcm16: bytes, sample_rate: int, resolution: int, fps: int, *, avatar_dir: Path | None = None, models_base: str | None = None,
               python: str | None = None, runner: Runner | None = None) -> Iterator[BgrFrame]:
        """Frames for this speech, made by the upstream inference script (it loads its model for every call: this is for a sentence at a time, not live).

        The frames are `resolution` pixels high and come at `fps` (the model's own rate is picked from by nearest frame)."""
        if avatar_dir is None:
            raise ValueError("render needs the avatar_dir keyword")
        st = self.status_of(avatar_dir, models_base)
        if not st["available"]:
            raise EngineError(st["reason"])
        import tempfile

        import cv2
        base, folder, runner = self.base_of(models_base), self.engine_dir(avatar_dir), runner or Runner()
        mark = self.trained(avatar_dir) or {}
        opts = {"asr": mark.get("asr"), "fps": mark["fps"]}
        code, env, py = self.stage(base, opts), self.env(base), self.python_of(python, base)
        with self._tracked(runner), tempfile.TemporaryDirectory(prefix="tadamoo-render-") as tmp:
            work = Path(tmp)
            raw, wav, video = work / "speech.wav", work / "speech16k.wav", work / "out.mp4"
            with wave.open(str(raw), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(pcm16)
            self._step(runner, self.audio_cmd(raw, wav), work, env, "the audio conversion")
            argv, cwd, feat = self.features_cmd(py, code, base, wav, work, opts)
            self._step(runner, argv, cwd, env, "the audio features")
            argv, cwd = self.infer_cmd(py, code, folder / "dataset", feat, folder / "checkpoints" / "last.pth", video, opts)
            self._step(runner, argv, cwd, env, "the rendering")
            cap = cv2.VideoCapture(str(video))
            try:
                frames = []
                while True:
                    ok, img = cap.read()
                    if not ok:
                        break
                    frames.append(img)
            finally:
                cap.release()
        if not frames:
            raise EngineError("the inference script made no frames")
        count = max(1, round(len(frames) * fps / opts["fps"]))
        for i in range(count):
            img = frames[min(len(frames) - 1, int(i * opts["fps"] / fps))]
            if img.shape[0] != resolution:
                img = cv2.resize(img, (max(2, round(img.shape[1] * resolution / img.shape[0]) // 2 * 2), resolution), interpolation=cv2.INTER_AREA)
            yield BgrFrame(img)

    # ---- the files for a device
    def export_for_device(self, avatar_dir: Path, target: str, *, runner: Runner | None = None, models_base: str | None = None,
                          python: str | None = None) -> list[Path]:
        """ONNX (and MNN when MNNConvert is set) for `target`, the audio encoder file and the first frames with their landmarks, in
        `<avatar>/engines/<id>/export/`; `export.json` lists them with their SHA-256 and the input the phone feeds the model (see docs/voice/gateway.md)."""
        if target == "pc":
            return []
        if target not in self.info.runs_on:
            raise ValueError(f"{self.info.name} does not run on {target}")
        mark = self.trained(avatar_dir)
        folder = self.engine_dir(avatar_dir)
        if mark is None or not (folder / "checkpoints" / "last.pth").is_file():
            raise EngineError("this avatar has not been trained for this engine")
        base, runner = self.base_of(models_base), runner or Runner()
        failed = [c for c in self.pack_checks(base, mark, python) if not c.ok]
        if failed:
            raise EngineError("; ".join(c.en for c in failed))
        code, env, py, out = self.stage(base, mark), self.env(base), self.python_of(python, base), folder / "export"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        for argv, cwd in self.export_cmds(py, code, base, folder / "checkpoints" / "last.pth", out, mark):
            with self._tracked(runner):
                self._step(runner, argv, cwd, env, "the export")
        files = self.export_files(base, out, mark)
        missing = [p.name for p in files if not p.is_file()]
        if missing:
            raise EngineError("the export did not make: " + ", ".join(missing))
        dataset = folder / "dataset"
        for sub, ext in (("frames", ".jpg"), ("lms", ".lms")):
            (out / sub).mkdir()
            src = dataset / ("full_body_img" if sub == "frames" else "landmarks")
            for i in range(min(EXPORT_FRAMES, self.frame_count(dataset))):
                shutil.copyfile(src / f"{i}{ext}", out / sub / f"{i}{ext}")
                files.append(out / sub / f"{i}{ext}")
        listing = {p.relative_to(avatar_dir).as_posix(): sha256_file(p) for p in files}
        (out / "export.json").write_text(json.dumps({"engine": self.ID, "created": _now(), "files": listing, "input": self.device_input(mark),
                                                     "epochs": mark.get("epochs")}, indent=1), encoding="utf-8")
        return files + [out / "export.json"]
