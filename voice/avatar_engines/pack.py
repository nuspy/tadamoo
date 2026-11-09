"""What the PC engines that run upstream code have in common: where their files are, what is missing, one process at a time, and the frames they make.

The upstream code of each engine is vendored in `vendor/<engine>/` (byte for byte, see its VENDORED.md); its weights are not and are installed on request
(serve/voice_catalog.py) in folders under `<models folder>/avatar-engines/<engine>/`. Before a run the vendored code is copied into the run folder
`<models folder>/avatar-engines/<engine>/code/`, where some weights also live because the code reads them by relative path. The engine runs the upstream scripts
through the runners of `runners/` as a subprocess of the Python environment named by TADAMOO_AVATAR_ENGINES_PYTHON: offline, with the run folder as its
working directory, one at a time with every other engine, and the whole process tree killed on stop. Nothing is started, loaded, copied or fetched by
importing this module or by asking an engine for its status."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from callbridge import models_store
from callbridge.avatar_client import Frame, HEADER, MAGIC, MAX_SIDE

from .base import AvatarEngine, EngineInfo
from .python_path import checked_python

ENV_PYTHON = "TADAMOO_AVATAR_ENGINES_PYTHON"
RUNNERS = Path(__file__).resolve().parent / "runners"
VENDOR = Path(__file__).resolve().parent / "vendor"
NOTES = ("VENDORED.md", "LICENSE-NOTE.md")             # the files of a vendor folder that are about the code, not part of it
TAIL_LINES = 12
_PERCENT = re.compile(r"(\d{1,3})%\|")                 # a tqdm bar: " 42%|####   | 21/50"


class EngineBusy(RuntimeError):
    """Another engine process is running: they run one at a time."""


class EngineFailed(RuntimeError):
    """The engine's process failed; the message is what it said."""


class EngineNotReady(RuntimeError):
    """The engine cannot run now; the message says what is missing."""


# ---- where things are
PYTHON_FILE = "python.json"                    # <models folder>/avatar-engines/python.json: the Python of the engines, chosen in the page


def _engines_root(base: str | None = None) -> Path | None:
    where = base if base is not None else models_store.root()
    return Path(where) / "avatar-engines" if where else None


def stored_python(base: str | None = None) -> str:
    root = _engines_root(base)
    try:
        value = json.loads((root / PYTHON_FILE).read_text(encoding="utf-8")).get("python", "") if root else ""
    except (OSError, ValueError, AttributeError):
        return ""
    try:
        return checked_python(value) if isinstance(value, str) else ""         # a kept path is started only while it still passes the check
    except ValueError:
        return ""


def python(base: str | None = None) -> str:
    """The Python of the engine environment: the variable TADAMOO_AVATAR_ENGINES_PYTHON, else the path chosen in the page."""
    return os.environ.get(ENV_PYTHON, "").strip() or stored_python(base)


def python_source(base: str | None = None) -> str:
    return "env" if os.environ.get(ENV_PYTHON, "").strip() else "stored" if stored_python(base) else "none"


def set_python(path: str, base: str | None = None) -> str:
    """Keep the path of the engine Python (an existing file); "" forgets it. ValueError with a plain reason."""
    root = _engines_root(base)
    if root is None:
        raise ValueError("there is no models folder to keep it in: choose one in Settings > Voice")
    path = checked_python(path)
    root.mkdir(parents=True, exist_ok=True)
    (root / PYTHON_FILE).write_text(json.dumps({"python": path}), encoding="utf-8")
    return path


def engine_dir(engine: str, base: str | None = None) -> Path | None:
    """`<models folder>/avatar-engines/<engine>`, or None when there is no models folder."""
    root = _engines_root(base)
    return root / engine if root else None


def code_dir(engine: str, base: str | None = None) -> Path | None:
    """The run folder of the engine's code: the vendored files are copied here by `stage_code`, and the weights the code reads by relative path are installed here."""
    where = engine_dir(engine, base)
    return where / "code" if where else None


def vendor_files(engine: str, vendor: Path | None = None) -> list[Path]:
    """The vendored files of an engine that are part of its code (not the notes about it)."""
    root = (vendor or VENDOR) / engine
    return sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and not (p.parent == root and p.name in NOTES))


def stage_code(engine: str, base: str | None = None, vendor: Path | None = None) -> Path:
    """Copy the vendored code of `engine` into its run folder, only the files that differ, and return the folder. EngineNotReady when there is no models
    folder or the vendored code is missing."""
    dest = code_dir(engine, base)
    files = vendor_files(engine, vendor)
    if dest is None:
        raise EngineNotReady("there is no models folder to run the engine in: choose one in Settings > Voice")
    if not files:
        raise EngineNotReady(f"the code of this engine is missing from this installation (voice/avatar_engines/vendor/{engine})")
    root = (vendor or VENDOR) / engine
    for src in files:
        target = dest / src.relative_to(root)
        st = src.stat()
        if target.is_file() and target.stat().st_size == st.st_size and target.stat().st_mtime_ns == st.st_mtime_ns:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return dest


def not_installed(model_ids, base: str | None = None) -> list[tuple]:
    """(en, it) for each catalog entry of `model_ids` that is not in the models folder."""
    out = []
    for model_id in model_ids:
        if models_store.is_installed(model_id, base):
            continue
        name, where = models_store.MODELS[model_id][2], models_store.folder("avatar", model_id, base)
        out.append((f"{name} is not installed: install it from Settings > Voice" + (f" or put it in {where}" if where else ""),
                    f"{name} non è installato: installarlo da Settings > Voice" + (f" o metterlo in {where}" if where else "")))
    return out


def environment_problems() -> list[tuple]:
    out = []
    exe = python()
    if not exe:
        out.append((f"the engine environment is not set: its Python is named by {ENV_PYTHON}", f"l'ambiente dei motori non è impostato: il suo Python è indicato da {ENV_PYTHON}"))
    elif not Path(exe).is_file():
        out.append((f"the engine environment's Python is not there: {exe}", f"il Python dell'ambiente dei motori non c'è: {exe}"))
    if shutil.which("ffmpeg") is None:
        out.append(("ffmpeg is not on the PATH", "ffmpeg non è nel PATH"))
    return out


def vendor_problems(engine: str, vendor: Path | None = None) -> list[tuple]:
    if vendor_files(engine, vendor):
        return []
    return [(f"the code of this engine is missing from this installation (voice/avatar_engines/vendor/{engine})",
             f"il codice di questo motore manca da questa installazione (voice/avatar_engines/vendor/{engine})")]


def photo_problems(avatar_dir: Path | None) -> list[tuple]:
    if avatar_dir is None or not (avatar_dir / "poster.jpg").is_file():
        return [("this avatar has no photo to draw from", "questo avatar non ha una foto da cui disegnare")]
    return []


# ---- the process
_ONE_AT_A_TIME = threading.Lock()                       # the GPU is shared: one engine process at a time, whichever engine


def parse_line(line: str) -> dict | None:
    """The event a line of the process stands for: a JSON object with "event" (the runners' own), or a tqdm bar as a progress event; None otherwise."""
    line = line.strip()
    if line.startswith("{"):
        try:
            msg = json.loads(line)
        except ValueError:
            return None
        return msg if isinstance(msg, dict) and msg.get("event") else None
    found = _PERCENT.search(line)
    if found:
        return {"event": "progress", "phase": "", "percent": min(100, int(found.group(1)))}
    return None


def kill_tree(proc) -> None:
    """End the process and everything it started."""
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15, check=False)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def spawn(cmd, cwd, env) -> subprocess.Popen:
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", bufsize=1, **flags)


def process_env(code: Path, extra: dict | None = None) -> dict:
    """Offline, the models folder known, the environment's own scripts first on the PATH, the run folder importable, and `extra` (an engine's own variables)."""
    env = models_store.helper_env("avatar")
    exe = python()
    if exe:
        env["PATH"] = os.pathsep.join([str(Path(exe).parent), env.get("PATH", "")])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(code), env.get("PYTHONPATH", "")) if p)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra or {})
    return env


class Job:
    """One run of one command: reads its output as events, keeps the last lines for an error message, and can be stopped."""

    def __init__(self, cmd: list, cwd: Path, env: dict, label: str, progress: Callable[[dict], None] | None = None, spawner=spawn,
                 needs_done: bool = True) -> None:
        """`needs_done` is False for an upstream script that prints no "done" event: its exit code 0 is then the end."""
        self.cmd, self.cwd, self.env, self.label = cmd, cwd, env, label
        self.progress, self._spawn, self.needs_done = progress, spawner, needs_done
        self.proc = None
        self.stopped = False
        self.done: dict | None = None
        self.error = ""
        self.tail: list[str] = []

    def run(self) -> dict:
        """Run to the end and return the "done" event; EngineBusy when another engine is running, EngineFailed when this one failed or was stopped."""
        if not _ONE_AT_A_TIME.acquire(blocking=False):
            raise EngineBusy("another avatar engine is running: they run one at a time")
        try:
            if self.stopped:
                raise EngineFailed(f"{self.label} was stopped")
            self.proc = self._spawn(self.cmd, self.cwd, self.env)
            readers = [threading.Thread(target=self._read, args=(stream,), daemon=True) for stream in (self.proc.stdout, self.proc.stderr)]
            for t in readers:
                t.start()
            code = self.proc.wait()
            for t in readers:
                t.join(timeout=5)
        finally:
            _ONE_AT_A_TIME.release()
        if self.stopped:
            raise EngineFailed(f"{self.label} was stopped")
        if code != 0 or (self.done is None and self.needs_done):
            raise EngineFailed(self.error or "; ".join(self.tail[-3:]) or f"{self.label} ended with code {code}")
        return self.done or {"event": "done"}

    def stop(self) -> None:
        self.stopped = True
        if self.proc is not None:
            kill_tree(self.proc)

    def _read(self, stream) -> None:
        for line in stream:
            event = parse_line(line)
            if event is None:
                if line.strip():
                    self.tail = (self.tail + [line.strip()])[-TAIL_LINES:]
                continue
            if event["event"] == "done":
                self.done = event
            elif event["event"] == "error":
                self.error = str(event.get("message") or "the engine reported an error")
            elif self.progress is not None:
                self.progress(event)


@dataclass(frozen=True)
class Stage:
    """One step of a training: a command, where it runs, its share of the progress bar, and what to do with its "done" event."""
    id: str
    en: str
    it: str
    argv: tuple
    cwd: Path
    weight: int
    needs_done: bool = True
    after: Callable[[dict], None] | None = None

    def as_dict(self) -> dict:
        return {"id": self.id, "en": self.en, "it": self.it, "weight": self.weight}


# ---- the frames
def read_frames(path: str | os.PathLike) -> Iterator[Frame]:
    """The frames of a file a runner wrote (runners/_frames.py)."""
    with open(path, "rb") as fh:
        while True:
            head = fh.read(HEADER.size)
            if not head:
                return
            if len(head) < HEADER.size:
                raise EngineFailed("the frames file is cut short")
            magic, w, h = HEADER.unpack(head)
            if magic != MAGIC or not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE) or w % 2 or h % 2:
                raise EngineFailed("the frames file has a bad header")
            body = fh.read(w * h * 3 // 2)
            if len(body) < w * h * 3 // 2:
                raise EngineFailed("the frames file is cut short")
            yield Frame(w, h, body)


def feed_frames(frames, sink: Callable[[Frame], None], fps: int, sleep: Callable[[float], None], stop: threading.Event | None = None) -> int:
    """Hand each frame to `sink` (the enhancer's `feed`, or `AvatarFrames.publish`) at `fps`; returns how many were handed over before the end or a stop."""
    gap, sent = 1.0 / max(1, fps), 0
    for frame in frames:
        if stop is not None and stop.is_set():
            break
        sink(frame)
        sent += 1
        sleep(gap)
    return sent


def write_wav(path: Path, pcm16: bytes, sample_rate: int) -> None:
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)


# ---- an engine made of a runner
class PackEngine(AvatarEngine):
    """An engine that renders by running one runner of runners/ on the audio and, for most, a photo of the avatar. A subclass names its runner, its
    folder under avatar-engines/ and the catalog entries that must be installed; `problems` lists, in words, what stops it from running."""

    ID = ""                                             # the id in registry.py
    key = ""                                            # the engine's folder under avatar-engines/ and the prefix of its catalog ids
    runner = ""                                         # the file in runners/
    models: tuple = ()                                  # catalog ids (weights) that must be installed to render; the code is vendored
    needs_photo = True
    fps_default = 25

    def __init__(self, info: EngineInfo, spawner=spawn) -> None:
        if info.id != self.ID:
            raise ValueError(f"{self.ID}: the descriptor is {info.id}")
        self._info = info
        self._spawn = spawner
        self._avatar: Path | None = None
        self._job: Job | None = None
        self.last_progress: dict | None = None

    @property
    def info(self) -> EngineInfo:
        return self._info

    # ---- what is missing
    def problems(self, avatar_dir, base: str | None = None) -> list[tuple]:
        """(en, it) for each thing that stops this engine from drawing this avatar: the environment, the vendored code, the weights, the photo."""
        folder = Path(avatar_dir) if avatar_dir else None
        out = environment_problems() + vendor_problems(self.key) + not_installed(self.models, base)
        if self.needs_photo and folder is not None and folder.is_dir():
            out += photo_problems(folder)
        return out

    def status_of(self, avatar_dir, models_base: str | None = None) -> dict:
        """{available, reason, reason_it}. Reads files and the environment only: nothing is started or loaded."""
        folder = Path(avatar_dir) if avatar_dir else None
        problems = [] if folder is not None and folder.is_dir() else [("no avatar is chosen", "nessun avatar è scelto")]
        problems += self.problems(folder, models_base)
        if not problems:
            return {"available": True, "reason": "", "reason_it": ""}
        return {"available": False, "reason": "; ".join(p[0] for p in problems), "reason_it": "; ".join(p[1] for p in problems)}

    # ---- the commands
    def base_args(self, base: str | None = None) -> list[str]:
        return [python(), str(RUNNERS / self.runner), "--pack", str(code_dir(self.key, base))]

    def env_extra(self, base: str | None = None) -> dict:
        """Variables only this engine's processes get, on top of the common offline environment."""
        return {}

    def check_command(self, base: str | None = None) -> list[str]:
        return self.base_args(base) + ["--engine-dir", str(engine_dir(self.key, base)), "--check"]

    def render_command(self, base: str | None, out: Path, wav: Path, resolution: int, fps: int, avatar_dir: Path) -> list[str]:
        return self.base_args(base) + ["--engine-dir", str(engine_dir(self.key, base)), "--image", str(avatar_dir / "poster.jpg"), "--audio", str(wav),
                                       "--frames-out", str(out), "--height", str(int(resolution)), "--fps", str(int(fps))]

    def _run(self, cmd: list, label: str, progress: Callable[[dict], None] | None = None) -> dict:
        def relay(event: dict) -> None:
            self.last_progress = event
            if progress is not None:
                progress(event)
        code = stage_code(self.key)
        self._job = Job(cmd, code, process_env(code, self.env_extra()), label, relay, self._spawn)
        try:
            return self._job.run()
        finally:
            self._job = None

    # ---- the setup: what the engine needs on this PC, and the explicit check that loads it once
    mark_name = "check.json"                            # <models folder>/avatar-engines/<engine>/: written when the check passed
    train_models: tuple = ()                            # catalog ids only a training needs

    def check_mark(self, base: str | None = None) -> dict | None:
        root = engine_dir(self.key, base)
        try:
            data = json.loads((root / self.mark_name).read_text(encoding="utf-8")) if root else None
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write_check_mark(self, base: str | None, done: dict) -> dict:
        from datetime import datetime, timezone
        mark = {**{k: v for k, v in done.items() if k != "event"}, "checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        root = engine_dir(self.key, base)
        root.mkdir(parents=True, exist_ok=True)
        (root / self.mark_name).write_text(json.dumps(mark), encoding="utf-8")
        return mark

    def setup_checks(self, base: str | None = None) -> list[dict]:
        """One item per prerequisite, each {id, ok, en, it} and, for a catalog entry, `model`: the Python, ffmpeg, each file, and whether the check passed."""
        def item(id_: str, ok: bool, en: str, it: str, **extra) -> dict:
            return {"id": id_, "ok": ok, "en": en, "it": it, **extra}
        exe = python(base)
        py_ok = bool(exe) and Path(exe).is_file()
        have_ffmpeg = shutil.which("ffmpeg") is not None
        vendored = not vendor_problems(self.key)
        out = [item("code", vendored, f"the engine code is part of this installation (vendor/{self.key})" if vendored else vendor_problems(self.key)[0][0],
                    f"il codice del motore è parte di questa installazione (vendor/{self.key})" if vendored else vendor_problems(self.key)[0][1]),
               item("python", py_ok, f"the engine Python is set ({exe})" if py_ok else f"the engine Python is not set or not there: choose it here or set {ENV_PYTHON}",
                    f"il Python dei motori è impostato ({exe})" if py_ok else f"il Python dei motori non è impostato o non c'è: sceglilo qui o imposta {ENV_PYTHON}"),
               item("ffmpeg", have_ffmpeg, "ffmpeg is on the PATH" if have_ffmpeg else "ffmpeg is not on the PATH", "ffmpeg è nel PATH" if have_ffmpeg else "ffmpeg non è nel PATH")]
        for model_id in self.models + self.train_models:
            have = models_store.is_installed(model_id, base)
            name = models_store.MODELS[model_id][2]
            out.append(item(model_id, have, f"{name} is installed" if have else f"{name} is not installed", f"{name} è installato" if have else f"{name} non è installato",
                            model=model_id, scope="training" if model_id in self.train_models else "render"))
        return out + self.check_items(base, item)

    def check_items(self, base, item) -> list[dict]:
        mark = self.check_mark(base)
        when = (mark or {}).get("checked", "")
        return [item("check", mark is not None, f"the environment loaded the engine on {when}" if mark else
                     "the environment has not loaded the engine yet: press Check (it loads the models once)",
                     f"l'ambiente ha caricato il motore il {when}" if mark else "l'ambiente non ha ancora caricato il motore: premi Controlla (carica i modelli una volta)")]

    def check_environment(self, progress: Callable[[str, float], None] | None = None, base: str | None = None) -> dict:
        """Load the engine once in its environment to prove it (the person asks for this) and record that it passed. EngineNotReady when something is missing,
        EngineFailed with what the process said when it does not load."""
        problems = environment_problems() + vendor_problems(self.key) + not_installed(self.models, base)
        if problems:
            raise EngineNotReady("; ".join(p[0] for p in problems))
        relay = (lambda e: progress(str(e.get("phase") or "loading"), float(e.get("percent") or 0.0))) if progress else None
        return self.write_check_mark(base, self._run(self.check_command(base), f"{self.info.name} check", relay))

    # ---- the interface
    def prepare(self, avatar_dir: Path, progress: Callable[[str, float], None]) -> None:
        """Choose the avatar to draw and load the engine's models once to prove the environment (this is the explicit request to load them)."""
        problems = self.problems(avatar_dir)
        if problems:
            raise EngineNotReady("; ".join(p[0] for p in problems))
        self.check_environment(progress)
        self._avatar = Path(avatar_dir)
        progress("ready", 100.0)

    def render(self, pcm16: bytes, sample_rate: int, resolution: int, fps: int, *, avatar_dir: Path | None = None) -> Iterator[Frame]:
        """Frames for this audio, drawn for `avatar_dir` (or the avatar `prepare` chose). Never scaled up: the enhance stage does that."""
        folder = Path(avatar_dir) if avatar_dir else self._avatar
        if folder is None:
            raise EngineNotReady(f"{self.info.name}: choose an avatar first")
        problems = self.problems(folder)
        if problems:
            raise EngineNotReady("; ".join(p[0] for p in problems))
        return self._frames(pcm16, sample_rate, resolution, fps or self.fps_default, folder)

    def _frames(self, pcm16: bytes, sample_rate: int, resolution: int, fps: int, folder: Path) -> Iterator[Frame]:
        with tempfile.TemporaryDirectory(prefix="tadamoo-engine-") as tmp:
            wav, out = Path(tmp) / "in.wav", Path(tmp) / "frames.i420"
            write_wav(wav, pcm16, sample_rate)
            self._run(self.render_command(None, out, wav, resolution, fps, folder), f"{self.info.name} render")
            yield from read_frames(out)

    def stop(self) -> None:
        """Stop what is running now, process tree included."""
        job = self._job
        if job is not None:
            job.stop()

    def unload(self) -> None:
        """Stop what is running and forget the avatar: a pack engine keeps no process or model between calls, so this is all there is to free."""
        self.stop()
        self._avatar = None
