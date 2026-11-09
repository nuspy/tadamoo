"""The client of the avatar worker (voice/avatar/avatar_worker.py): starts it offline from the models folder and talks to it in JSON lines.

The worker is MuseTalk in an environment of its own. The client puts the vendored MuseTalk code on its PYTHONPATH, tells it where the weights are
(`<models folder>/avatar/musetalk-v15`, nothing is ever fetched) and where the avatars are (`avatar_store.py`), and refuses to start it when a
weight is missing, naming the file. The Python of that environment is `avatar_python` of the config, or the variable TADAMOO_AVATAR_PYTHON.

The rendered frames come back over a local socket and are handed to whoever subscribed to `AvatarClient.frames` (an `AvatarFrames`):
the browser preview, the virtual camera, the phone gateway. With no subscriber the worker is told to send nothing.

    unsubscribe = client.frames.subscribe(lambda frame: ...)       # frame.width, frame.height, frame.i420 (bytes), frame.bgr(), frame.jpeg()
"""
from __future__ import annotations

import json
import logging
import os
import socket
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import avatar_weights, models_store
from .avatar_store import PREPARED_HEIGHT, AvatarStore, StoreError
from .config import Config
from .enhance.pipeline import Enhancer
from .enhance.settings import EnhanceSettings

log = logging.getLogger("callbridge.avatar")

HERE = Path(__file__).resolve().parent
WORKER = HERE.parent / "avatar" / "avatar_worker.py"
CODE_DIR = HERE.parent / "avatar" / "musetalk"
PYTHON_ENV = "TADAMOO_AVATAR_PYTHON"
MAGIC = b"AVI4"                                   # the frame header of avatar_worker.py and avatar_webrtc.py
HEADER = struct.Struct("<4sII")
MAX_SIDE = 4096


class PrepareBusy(RuntimeError):
    """A preparation is already running: they run one at a time."""


class Frame:
    """One rendered frame, as the worker sent it: I420, even sides."""

    __slots__ = ("width", "height", "i420")

    def __init__(self, width: int, height: int, i420: bytes) -> None:
        self.width, self.height, self.i420 = width, height, i420

    def bgr(self):
        import cv2
        import numpy as np
        yuv = np.frombuffer(self.i420, np.uint8).reshape(self.height * 3 // 2, self.width)
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)

    def jpeg(self, quality: int = 80) -> bytes:
        import cv2
        ok, data = cv2.imencode(".jpg", self.bgr(), [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise ValueError("the frame could not be encoded")
        return data.tobytes()


class AvatarFrames:
    """The frames the avatar renders, for any number of subscribers. A callback runs on the receiving thread: it must return quickly (take the
    frame and go); one that raises is logged and kept. `on_change(count)` is called when the number of subscribers changes."""

    def __init__(self, on_change=None) -> None:
        self._subs: dict[int, object] = {}
        self._next = 0
        self._lock = threading.Lock()
        self._on_change = on_change

    @property
    def count(self) -> int:
        return len(self._subs)

    def subscribe(self, callback):
        """Start delivering frames to `callback(frame)`; returns the function that stops it."""
        with self._lock:
            self._next += 1
            key = self._next
            self._subs[key] = callback
            n = len(self._subs)
        self._changed(n)

        def unsubscribe() -> None:
            with self._lock:
                gone = self._subs.pop(key, None) is not None
                n2 = len(self._subs)
            if gone:
                self._changed(n2)
        return unsubscribe

    def _changed(self, n: int) -> None:
        if self._on_change is not None:
            try:
                self._on_change(n)
            except Exception as e:                # noqa: BLE001 (a worker that is not there yet is told when it starts)
                log.debug("frames: change not passed on: %s", e)

    def publish(self, frame: Frame) -> None:
        with self._lock:
            subs = list(self._subs.values())
        for cb in subs:
            try:
                cb(frame)
            except Exception as e:                # noqa: BLE001
                log.warning("frames: a subscriber failed: %s", e)


class _FrameServer:
    """Listens on loopback for the worker's frame connection and reads its frames; each goes to `intake` (the enhancer) or, with none, to the subscribers."""

    def __init__(self, frames: AvatarFrames, intake=None) -> None:
        self.frames = frames
        self._deliver = intake or frames.publish
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(2)
        self.port = self._sock.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._accept, name="avatar-frames", daemon=True).start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._read, args=(conn,), name="avatar-frames-read", daemon=True).start()

    def _read(self, conn: socket.socket) -> None:
        conn.settimeout(None)
        try:
            while True:
                head = self._exact(conn, HEADER.size)
                magic, w, h = HEADER.unpack(head)
                if magic != MAGIC or not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE) or w % 2 or h % 2:
                    log.warning("frames: a connection sent a bad header; closed")
                    return
                self._deliver(Frame(w, h, self._exact(conn, w * h * 3 // 2)))
        except (OSError, EOFError):
            pass
        finally:
            conn.close()

    @staticmethod
    def _exact(conn: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(min(1 << 20, n - len(buf)))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


class AvatarClient:
    """Runs the avatar worker and the avatars store."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._persona_ready = threading.Event()
        self._persona_error = threading.Event()
        self._playing = threading.Event()
        self._speaking = threading.Event()
        self._spoken = threading.Event()
        self._spoken.set()
        self.preparazione_s: float = 0.0       # seconds the worker took to get the last sentence ready (the true lead-in of the lip sync)
        self._active_persona: str | None = None
        self.last_error: str | None = None
        self.preparing: str | None = None      # the avatar being loaded by start_async
        self._start_thread: threading.Thread | None = None
        self.progress: dict | None = None      # the last progress event of the worker: {job, phase, done, total, percent, eta_s}
        self.frames = AvatarFrames(self._frames_changed)
        self.enhancer = Enhancer(self.frames.publish)         # between the worker's frames and the subscribers: passes them through while both stages are off
        self._started_with: tuple[int, int] = (0, 0)
        self._frame_server: _FrameServer | None = None
        self._job: dict | None = None          # the preparation job, running or the last one finished
        self._job_lock = threading.Lock()

    # ---- the store
    @property
    def store(self) -> AvatarStore:
        return AvatarStore(self.cfg.avatars_dir or "")

    def personas(self) -> list[str]:
        return self.store.names() if self.cfg.avatars_dir else []

    def voice_for(self, persona: str) -> dict:
        """{"engine", "voice"} the avatar was made with, {} when none."""
        try:
            return self.store.voice_of(persona)
        except StoreError:
            return {}

    # ---- the worker's life
    def start(self, persona: str) -> bool:
        """Start the worker if needed and load the avatar `persona`; blocks until it is ready (preparing a clip the first time takes minutes, and
        `progress` shows it). False with `last_error` when it cannot: suspended, a missing weight or environment, an avatar without a clip, a
        worker that died or made no progress for `avatar_prepare_stall_s`."""
        from .sospensione import MSG_SOSPESO, in_sospensione
        if in_sospensione():
            self.last_error = MSG_SOSPESO
            return False
        try:
            folder = self.store.existing(persona)
        except StoreError as e:
            self.last_error = str(e)
            return False
        if not self.store.clips(persona):
            self.last_error = f"the avatar {persona} has no video clip to animate: add one"
            return False
        with self._lock:
            self.last_error = None
            if not self._ensure_worker():
                self.last_error = self.last_error or "the avatar worker did not start (see the log)"
                return False
            self._persona_ready.clear()
            self._persona_error.clear()
            self.progress = None
            self._send({"cmd": "persona", "dir": str(folder)})
        stall_s = getattr(self.cfg, "avatar_prepare_stall_s", 300.0)
        last_progress, last_change = self.progress, time.monotonic()
        while True:
            if self._persona_ready.wait(timeout=0.5):
                self._active_persona = persona
                return True
            if self._persona_error.is_set():
                log.error("avatar %s failed: %s", persona, self.last_error)
                return False
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self.last_error = "the avatar worker ended while it was preparing"
                log.error("avatar %s: %s", persona, self.last_error)
                return False
            if self.progress != last_progress:
                last_progress, last_change = self.progress, time.monotonic()
            elif stall_s > 0 and time.monotonic() - last_change > stall_s:
                self.last_error = f"the avatar preparation made no progress for {int(stall_s)} s"
                log.error("avatar %s: %s", persona, self.last_error)
                return False

    def start_async(self, persona: str) -> bool:
        """`start` on a thread; progress shows in `preparing`, `progress` and `last_error`. False when another one is running."""
        from .sospensione import MSG_SOSPESO, in_sospensione
        if in_sospensione():
            self.last_error = MSG_SOSPESO
            return False
        if self._start_thread is not None and self._start_thread.is_alive():
            return False
        self.preparing = persona                  # set before the thread runs: the first poll already sees it

        def _run() -> None:
            try:
                self.start(persona)
            finally:
                self.preparing = None

        self._start_thread = threading.Thread(target=_run, name="avatar-start", daemon=True)
        self._start_thread.start()
        return True

    def ensure_worker(self) -> bool:
        """Start the worker without loading an avatar (the function is on, an avatar comes later)."""
        with self._lock:
            self.last_error = None
            ok = self._ensure_worker()
            if not ok:
                self.last_error = self.last_error or "the avatar worker did not start (see the log)"
            return ok

    def speak_wav(self, wav_path: str, mute: bool = False) -> None:
        """Play the sentence (lip sync and sound) without waiting. With `mute` the lips move and nothing sounds here: the voice goes elsewhere."""
        with self._lock:
            if self._proc is None:
                log.warning("avatar: no worker, the sentence is dropped")
                return
            self._spoken.clear()                  # marked at once: a wait_until_spoken before the "speaking" event must not return
            self._playing.clear()
            self._send({"cmd": "speak", "wav": wav_path, "mute": bool(mute)})

    def wait_until_playing(self, timeout: float) -> bool:
        """Wait until the frames start to move (the moment a voice sent elsewhere should start)."""
        return self._playing.wait(timeout)

    def wait_until_spoken(self, timeout: float) -> bool:
        return self._spoken.wait(timeout)

    def is_ready(self, persona: str | None = None) -> bool:
        if self._proc is None or self._proc.poll() is not None or not self._persona_ready.is_set():
            return False
        return persona is None or self._active_persona == persona

    def interrupt(self, svuota: bool = False) -> None:
        """Cut the sentence that is playing; with `svuota` the queued ones too."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return
            self._send({"cmd": "interrupt", "svuota": bool(svuota)})

    def set_stream(self, on: bool, port: int = 0) -> bool:
        """Send the video to a consumer's own port (the phone gateway). False when the worker is not running."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return False
            self._send({"cmd": "stream", "on": bool(on), "port": int(port or 0)})
            return True

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._send({"cmd": "quit"})
                    self._proc.wait(timeout=5)
                except Exception:                 # noqa: BLE001
                    self._proc.kill()
                    self._proc.wait(timeout=5)
                self._proc = None
            self._ready.clear()
            self._persona_ready.clear()
            self._active_persona = None
            self._speaking.clear()
            self._spoken.set()
            self._playing.clear()
        self.enhancer.worker_stopped()
        server, self._frame_server = self._frame_server, None
        if server is not None:
            server.close()
        with self._job_lock:
            if self._job and self._job["state"] == "running":
                self._job.update(state="failed", error="the avatar worker stopped")

    # ---- preparing the clips of an avatar
    def prepare(self, name: str, clip: str) -> dict:
        """Prepare one clip of an avatar in the worker (frames, face landmarks, latents, masks); one job at a time. Returns the job."""
        folder = self.store.existing(name)
        self.store.clip_path(name, clip)
        with self._job_lock:
            if self._job and self._job["state"] == "running":
                raise PrepareBusy(f"{self._job['avatar']}/{self._job['clip']} is being prepared: cancel it or wait")
            if self._proc is None or self._proc.poll() is not None or not self._ready.is_set():
                raise RuntimeError("the avatar is not loaded: load the avatar function first")
            job = {"id": uuid.uuid4().hex[:12], "avatar": name, "clip": clip, "state": "running", "progress": None, "error": "", "started": time.time()}
            self._job = job
            self.progress = None
        with self._lock:
            self._send({"cmd": "prepare", "dir": str(folder), "clip": clip, "id": job["id"]})
        return dict(job)

    def cancel_prepare(self) -> bool:
        """Ask the worker to stop the running preparation; False when none is running."""
        with self._job_lock:
            if not self._job or self._job["state"] != "running":
                return False
            job_id = self._job["id"]
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._send({"cmd": "cancel", "id": job_id})
        return True

    def prepare_state(self) -> dict | None:
        with self._job_lock:
            return dict(self._job) if self._job else None

    # ---- frames
    def configure_enhance(self, settings: EnhanceSettings, report: dict):
        """Apply the acceleration settings: what the next worker is asked to render, and what the enhancer does with the frames now."""
        return self.enhancer.configure(settings, report, int(getattr(self.cfg, "avatar_fps", 0) or 25), int(getattr(self.cfg, "avatar_output_height", 0) or 480))

    def _frames_changed(self, count: int) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._send_frames(count > 0)

    def _send_frames(self, on: bool) -> None:
        if on and self._frame_server is None:
            self._frame_server = _FrameServer(self.frames, self.enhancer.feed)
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._send({"cmd": "frames", "on": on, "port": self._frame_server.port if on and self._frame_server else 0})

    # ---- inside
    def _worker_env(self) -> dict:
        env = models_store.helper_env("avatar")
        env.update(avatar_weights.env())
        env["AVATAR_CODE_DIR"] = str(CODE_DIR)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(CODE_DIR), env.get("PYTHONPATH", "")) if p)
        env["PYTHONIOENCODING"] = "utf-8"
        if self.cfg.avatars_dir:
            env["AVATAR_STATE_DIR"] = str(Path(self.cfg.avatars_dir).parent / "avatar-state")
        if self.cfg.avatar_audio_device is not None:
            env["AVATAR_AUDIO_DEVICE"] = str(self.cfg.avatar_audio_device)
        plan = self.enhancer.plan
        fps = int(plan.worker_fps or getattr(self.cfg, "avatar_fps", 0) or 0)
        if fps > 0:
            env["AVATAR_FPS"] = str(fps)            # with interpolation on, fewer than the outputs get: the enhancer restores the rate
        if plan.worker_height:
            env["AVATAR_OUTPUT_HEIGHT"] = str(plan.worker_height)       # with upscaling on, smaller than the outputs get
        self._started_with = (plan.worker_height, fps)
        if getattr(self.cfg, "avatar_profile", False):
            env["AVATAR_PROFILE"] = "1"
        if self.cfg.avatar_gpu_index is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(self.cfg.avatar_gpu_index)
        return env

    def _python(self) -> str:
        return str(getattr(self.cfg, "avatar_python", "") or os.environ.get(PYTHON_ENV, "")).strip()

    def _check_ready_to_start(self) -> str:
        """"" when the worker can start, else why not."""
        python = self._python()
        if not python:
            return f"the avatar environment is not set: its Python is named by {PYTHON_ENV} (the installer sets it)"
        if not Path(python).is_file():
            return f"the avatar environment's Python is not there: {python}"
        script = Path(getattr(self.cfg, "avatar_worker", "") or WORKER)
        if not script.is_file():
            return f"the avatar worker is missing: {script}"
        return avatar_weights.reason()

    def _kill_orphans(self, script: Path) -> None:
        """A service restarted while its worker lived leaves the worker running; two workers fill the VRAM, so the old one goes."""
        try:
            import psutil
            for p in psutil.process_iter(["pid", "cmdline"]):
                try:
                    if p.pid != os.getpid() and str(script) in (p.info.get("cmdline") or []):
                        p.kill()
                        log.warning("avatar: stale worker %d ended", p.pid)
                except Exception:                 # noqa: BLE001 (gone already, or not ours to touch)
                    continue
        except Exception as e:                    # noqa: BLE001 (no psutil: go on)
            log.debug("avatar: stale workers not looked for: %s", e)

    def _ensure_worker(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        problem = self._check_ready_to_start()
        if problem:
            self.last_error = problem
            log.error("avatar: %s", problem)
            return False
        script = Path(getattr(self.cfg, "avatar_worker", "") or WORKER)
        self._kill_orphans(script)
        log.info("avatar: starting the worker (loading the models takes about a minute)")
        self._ready.clear()
        self._proc = subprocess.Popen(
            [self._python(), str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", bufsize=1, cwd=str(CODE_DIR), env=self._worker_env())
        self.enhancer.worker_started(*self._started_with)
        threading.Thread(target=self._reader, args=(self._proc,), daemon=True).start()
        threading.Thread(target=self._stderr_reader, args=(self._proc,), daemon=True).start()
        if not self._ready.wait(timeout=getattr(self.cfg, "avatar_start_timeout_s", 180.0)):
            log.error("avatar: the worker was not ready in time")
            self.last_error = "the avatar worker was not ready in time"
            return False
        if self.frames.count:
            self._reply_frames_on()
        return True

    def _reply_frames_on(self) -> None:
        if self._frame_server is None:
            self._frame_server = _FrameServer(self.frames, self.enhancer.feed)
        self._send({"cmd": "frames", "on": True, "port": self._frame_server.port})

    def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _reader(self, proc) -> None:
        assert proc.stdout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._event(msg)
        self._worker_gone(proc)

    def _worker_gone(self, proc) -> None:
        if proc is self._proc:
            self.enhancer.worker_stopped()
        with self._job_lock:
            if self._job and self._job["state"] == "running":
                self._job.update(state="failed", error="the avatar worker ended during the preparation")
        self._spoken.set()

    def _event(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "ready":
            self._ready.set()
        elif ev == "progress":
            self.progress = {k: msg.get(k) for k in ("job", "phase", "done", "total", "percent", "eta_s")}
            with self._job_lock:
                if self._job and self._job["state"] == "running" and msg.get("job") == self._job["id"]:
                    self._job["progress"] = self.progress
        elif ev == "persona_ready":
            log.info("avatar: ready (clip %s, %s frames)", msg.get("clip"), msg.get("frames"))
            self.progress = None
            self._persona_ready.set()
        elif ev == "prepared":
            job = self.prepare_state()
            self._job_ended(msg, "done", frames=msg.get("frames"))
            try:
                if job is not None:
                    self.store.record_prepared(job["avatar"], msg.get("clip"), int(msg.get("frames") or 0), PREPARED_HEIGHT)
            except (StoreError, TypeError, ValueError, OSError) as e:
                log.warning("avatar: the prepared clip was not recorded: %s", e)
        elif ev == "prepare_failed":
            self._job_ended(msg, "failed", error=str(msg.get("message") or "the preparation failed"))
        elif ev == "cancelled":
            self._job_ended(msg, "cancelled")
        elif ev == "error":
            self.last_error = str(msg.get("message") or "the worker reported an error")
            self._persona_error.set()
            log.error("avatar: %s", self.last_error)
        elif ev == "speaking":
            self._spoken.clear()
            self._speaking.set()
        elif ev == "playing":
            self._playing.set()
            try:
                self.preparazione_s = float(msg.get("preparazione_s") or 0.0)
            except (TypeError, ValueError):
                self.preparazione_s = 0.0
        elif ev in ("idle", "interrupted"):
            self._speaking.clear()
            self._spoken.set()
        else:
            log.debug("avatar: event %s", msg)

    def _job_ended(self, msg: dict, state: str, **extra) -> None:
        with self._job_lock:
            if self._job and msg.get("id") in (None, self._job["id"]):
                self._job.update(state=state, **extra)
                if state == "done":
                    self._job["progress"] = None
        self.progress = None

    def _stderr_reader(self, proc) -> None:
        assert proc.stderr
        for line in proc.stderr:
            text = line.rstrip()
            if not text:
                continue
            (log.debug if "%|" in text or "it/s]" in text else log.info)("avatar[stderr] %s", text)
