"""The enhancer: between the avatar worker's frames and everything that shows them.

    worker -> AvatarClient._FrameServer -> Enhancer.feed(frame) -> emit(frame)  (AvatarFrames.publish: preview, virtual camera, other code)

With both stages off a frame goes straight through. With a stage on, the frame is upscaled to the target size and then interpolated with the
previous one; the rule that decides what the worker renders and what comes out is in `settings.py`. `feed` never raises: a stage that fails on
a frame is skipped for that frame, three failures in a row switch that stage off with the reason in `status()`, and the frame goes out anyway.

Interpolation is causal: when frame k arrives, `n` frames go out between the previous one and k (the last of them is k itself), spread over
the measured interval between the two arrivals. The first goes out at once, so a frame is shown at most one worker period late."""
from __future__ import annotations

import heapq
import logging
import threading
import time
from collections import deque

from . import providers, rtx_video  # noqa: F401 (rtx_video registers its provider)
from .capability import usable
from .settings import EnhanceSettings, Plan, plan as make_plan

log = logging.getLogger("callbridge.avatar.enhance")

MAX_FAILURES = 3                  # in a row, then the stage is switched off until the settings change
MAX_GAP_S = 0.5                   # no interpolation across a longer pause between two frames
MIN_PERIOD_S, MAX_PERIOD_S = 1 / 60, 0.25
WINDOW = 60                       # frames the timings are averaged over


def _even(n: int) -> int:
    return n + (n % 2)


class _Timer:
    def __init__(self) -> None:
        self.ms: deque[float] = deque(maxlen=WINDOW)

    def add(self, seconds: float) -> None:
        self.ms.append(seconds * 1000.0)

    def mean(self) -> float | None:
        return round(sum(self.ms) / len(self.ms), 3) if self.ms else None


class _Stage:
    """One stage with its provider, its failures and its timing."""

    def __init__(self, stage: str, name: str, provider, reason: str = "") -> None:
        self.stage, self.name, self.provider = stage, name, provider
        self.reason = reason                      # why the stage is not running ("" = it runs)
        self.failures = 0
        self.errors = 0
        self.last_error = ""
        self.timer = _Timer()

    @property
    def on(self) -> bool:
        return self.provider is not None and not self.reason

    def failed(self, error: Exception) -> None:
        self.errors += 1
        self.failures += 1
        self.last_error = str(error) or type(error).__name__
        if self.failures >= MAX_FAILURES:
            self.reason = f"switched off after {MAX_FAILURES} failures in a row: {self.last_error}"
            log.warning("enhance: %s %s %s", self.stage, self.name, self.reason)

    def succeeded(self) -> None:
        self.failures = 0

    def status(self) -> dict:
        return {"provider": self.name, "running": self.on, "reason": self.reason, "errors": self.errors, "last_error": self.last_error, "ms": self.timer.mean()}


class _Pacer:
    """Runs the delayed emissions of one input frame on a thread of its own, in time order."""

    def __init__(self, clock=time.perf_counter) -> None:
        self._clock = clock
        self._cond = threading.Condition()
        self._heap: list = []
        self._seq = 0
        self._thread: threading.Thread | None = None
        self._closed = False

    def schedule(self, delay: float, fn) -> None:
        with self._cond:
            self._seq += 1
            heapq.heappush(self._heap, (self._clock() + delay, self._seq, fn))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="avatar-enhance-pacer", daemon=True)
                self._thread.start()
            self._cond.notify()

    def flush(self) -> None:
        """Send what is still waiting now, in order."""
        with self._cond:
            pending, self._heap = sorted(self._heap), []
        for _due, _seq, fn in pending:
            fn()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._closed and (not self._heap or self._heap[0][0] > self._clock()):
                    self._cond.wait(None if not self._heap else max(0.0, self._heap[0][0] - self._clock()))
                if self._closed:
                    return
                _due, _seq, fn = heapq.heappop(self._heap)
            fn()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._heap = []
            self._cond.notify_all()


class Enhancer:
    """`feed(frame)` in, `emit(frame)` out. `pacing=False` sends every frame at once (tests and benchmarks)."""

    def __init__(self, emit, clock=time.perf_counter, pacing: bool = True) -> None:
        self._emit = emit
        self._clock = clock
        self._pacer = _Pacer(clock) if pacing else None
        self._lock = threading.Lock()
        self.settings = EnhanceSettings()
        self.plan: Plan = make_plan(self.settings, 25, 480)         # what was asked for
        self.target_fps, self.target_height = 25, 480
        self._up: _Stage | None = None
        self._interp: _Stage | None = None
        self._running: tuple[int, int] | None = None               # (height, fps) the running worker was started with, None = no worker
        self._prev = None
        self._prev_at = 0.0
        self._period = 1 / 25
        self.frames_in = self.frames_out = 0
        self.passthrough = 0
        self._total = _Timer()
        self._convert = _Timer()
        self.last_error = ""

    # ---- configuration
    def configure(self, settings: EnhanceSettings, report: dict, target_fps: int, target_height: int) -> Plan:
        """Apply the person's choice. A provider that the capability `report` calls unusable is not run, and the worker is not asked to
        render less for it: the returned plan is that of the stages that can run."""
        up, up_reason = settings.upscale, ""
        if up != "none" and not usable(report, "upscale", up):
            up_reason = report["upscale"].get(up, {}).get("reason") or "not available"
        interp, interp_reason = settings.interpolate, ""
        if interp != "none" and not usable(report, "interpolate", interp):
            interp_reason = report["interpolate"].get(interp, {}).get("reason") or "not available"
        effective = EnhanceSettings(settings.upscale if not up_reason else "none", settings.scale, settings.interpolate if not interp_reason else "none",
                                    settings.factor, settings.worker_fps_cap, settings.worker_height_cap, settings.rtx_video_path)
        with self._lock:
            self.settings, self.target_fps, self.target_height = settings, int(target_fps), int(target_height)
            self.plan = make_plan(effective, target_fps, target_height)
            self._up = self._stage("upscale", settings.upscale, effective.upscale, settings, up_reason)
            self._interp = self._stage("interpolate", settings.interpolate, effective.interpolate, settings, interp_reason)
            self._prev = None
            self.last_error = ""
        return self.plan

    @staticmethod
    def _stage(stage: str, asked: str, effective: str, settings, reason: str) -> _Stage | None:
        if asked == "none":
            return None
        if reason:
            return _Stage(stage, asked, None, reason)
        try:
            return _Stage(stage, asked, providers.make(stage, effective, settings))
        except Exception as e:                      # noqa: BLE001 (a provider that cannot be built is reported, not raised)
            return _Stage(stage, asked, None, str(e) or type(e).__name__)

    def worker_started(self, height: int, fps: int) -> None:
        """The worker was started rendering `height` rows (0 = its own) at `fps`."""
        with self._lock:
            self._running = (int(height), int(fps))
            self._prev = None

    def worker_stopped(self) -> None:
        with self._lock:
            self._running = None
            self._prev = None

    def close(self) -> None:
        if self._pacer is not None:
            self._pacer.close()

    # ---- the frame path
    def feed(self, frame) -> None:
        """Take a frame from the worker and send out what the settings make of it. Never raises."""
        try:
            self._process(frame)
        except Exception as e:                      # noqa: BLE001 (the picture must keep moving)
            self.last_error = str(e) or type(e).__name__
            log.warning("enhance: a frame went through unchanged: %s", self.last_error)
            self._send(frame)

    def _send(self, frame) -> None:
        self.frames_out += 1
        self._emit(frame)

    def _out_per_in(self) -> int:
        if self._interp is None or not self._interp.on:
            return 1
        if self._running is not None and self._running[1] > 0:
            return max(1, min(4, -(-self.target_fps // self._running[1])))
        return self.plan.out_per_in

    def _process(self, frame) -> None:
        self.frames_in += 1
        with self._lock:
            up, interp = (self._up if self._up and self._up.on else None), (self._interp if self._interp and self._interp.on else None)
            if up is None and interp is None:
                self.passthrough += 1
                self._send(frame)
                return
            n = self._out_per_in()
            target_h = self.plan.out_height
            if self._pacer is not None:
                self._pacer.flush()
            start = self._clock()
            img = self._bgr(frame)
            if up is not None and frame.height < target_h:
                img = self._upscale(up, img, frame, target_h)
            outs = self._interpolate(interp, img, n, start) if interp is not None and n > 1 else [(0.0, img)]
            if interp is not None:
                self._prev, self._prev_at = img, start
            t0 = self._clock()
            frames = [(delay, self._frame(out)) for delay, out in outs]
            self._convert.add(self._clock() - t0)
            self._total.add(self._clock() - start)
        for delay, out in frames:
            if delay <= 0 or self._pacer is None:
                self._send(out)
            else:
                self._pacer.schedule(delay, lambda f=out: self._send(f))

    @staticmethod
    def _bgr(frame):
        return frame.bgr()

    def _upscale(self, stage: _Stage, img, frame, target_h: int):
        out_w, out_h = _even(round(frame.width * target_h / frame.height)), target_h
        t0 = self._clock()
        try:
            out = stage.provider.upscale(img, out_w, out_h)
            if out.shape[0] != out_h or out.shape[1] != out_w:
                raise providers.ProviderError("the provider returned a frame of the wrong size")
        except Exception as e:                      # noqa: BLE001
            stage.failed(e)
            return img
        stage.succeeded()
        stage.timer.add(self._clock() - t0)
        return out

    def _interpolate(self, stage: _Stage, img, n: int, now: float) -> list:
        """[(delay in seconds, picture)]: `n` pictures from the previous one to this one; just this one when there is nothing to interpolate."""
        prev, gap = self._prev, now - self._prev_at
        if prev is None or prev.shape != img.shape or gap > MAX_GAP_S:
            self._period = min(MAX_PERIOD_S, max(MIN_PERIOD_S, self._period))
            return [(0.0, img)]
        self._period = min(MAX_PERIOD_S, max(MIN_PERIOD_S, 0.7 * self._period + 0.3 * gap))
        t0 = self._clock()
        try:
            middle = stage.provider.interpolate(prev, img, [(j + 1) / n for j in range(n - 1)])
            if len(middle) != n - 1:
                raise providers.ProviderError("the provider returned the wrong number of frames")
        except Exception as e:                      # noqa: BLE001
            stage.failed(e)
            return [(0.0, img)]
        stage.succeeded()
        stage.timer.add(self._clock() - t0)
        return [(j * self._period / n, pic) for j, pic in enumerate(middle + [img])]

    @staticmethod
    def _frame(img):
        import cv2
        from callbridge.avatar_client import Frame
        h, w = img.shape[:2]
        return Frame(w, h, cv2.cvtColor(img, cv2.COLOR_BGR2YUV_I420).tobytes())

    # ---- what it measured
    def status(self) -> dict:
        with self._lock:
            total = self._total.mean()
            budget = 1000.0 / self._running[1] if self._running and self._running[1] > 0 else 1000.0 / max(1, self.plan.worker_fps or self.target_fps)
            pending = bool(self.plan.active and self._running is not None and
                           ((self.plan.worker_height and self._running[0] != self.plan.worker_height) or (self.plan.worker_fps and self._running[1] != self.plan.worker_fps)))
            pending = pending or bool(not self.plan.active and self._running is not None and self._running[0] > 0 and self._running[0] < self.target_height)
            return {"settings": self.settings.as_dict(), "plan": self.plan.as_dict(), "target": {"fps": self.target_fps, "height": self.target_height},
                    "worker": {"running": self._running is not None, "height": self._running[0] if self._running else None,
                               "fps": self._running[1] if self._running else None},
                    "restart_needed": pending,
                    "upscale": self._up.status() if self._up else None, "interpolate": self._interp.status() if self._interp else None,
                    "timing": {"frames_in": self.frames_in, "frames_out": self.frames_out, "passthrough": self.passthrough,
                               "convert_ms": self._convert.mean(), "total_ms": total, "budget_ms": round(budget, 3),
                               "over_budget": bool(total is not None and total > budget)},
                    "last_error": self.last_error}
