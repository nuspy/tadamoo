"""Energy of the GPUs during one task: the power limit, and the power read at intervals added up over the time.

The readings come from a `source()` that returns one dict per GPU with `power` (watts, now) and `power_limit` (watts);
the server's own telemetry gives exactly that. The figure is the energy of the whole board(s) while the task ran, so it
includes anything else that used the GPU at the same time; it is an estimate from samples, not a measurement of the task.
Without readings (no NVIDIA driver, a telemetry that could not read power) the result is empty.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

NOTE = "energy of the whole GPU board(s) while the task ran, estimated from power samples"


class EnergyMeter:
    def __init__(self, source: Callable[[], List[Dict[str, Any]]], interval: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.source, self.interval, self.clock = source, interval, clock
        self._points: List[tuple] = []            # (time, total watts)
        self._limit_w: Optional[float] = None
        self._gpus = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def sample(self, now: Optional[float] = None) -> None:
        when = self.clock() if now is None else now
        try:
            gpus = [g for g in self.source() or [] if isinstance(g, dict)]
        except Exception:                          # a reading that fails is a missing sample
            return
        powers = [g.get("power") for g in gpus]
        limits = [g.get("power_limit") for g in gpus]
        if powers and all(isinstance(p, (int, float)) for p in powers):
            self._points.append((when, float(sum(powers))))
            self._gpus = len(gpus)
        if limits and all(isinstance(l, (int, float)) for l in limits):
            self._limit_w = float(sum(limits))

    def start(self) -> "EnergyMeter":
        self.sample()
        self._thread = threading.Thread(target=self._loop, name="energy-meter", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.sample()

    def stop(self) -> Dict[str, Any]:
        """{"gpu_power_limit_w", "avg_power_w", "peak_power_w", "energy_wh", "seconds", "samples", "gpus", "note"}; {} without readings."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.interval + 2)
        self.sample()
        out: Dict[str, Any] = {}
        if self._limit_w is not None:
            out["gpu_power_limit_w"] = round(self._limit_w, 1)
        if len(self._points) >= 2:
            span = self._points[-1][0] - self._points[0][0]
            joules = sum((b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(self._points, self._points[1:]))
            if span > 0:
                out.update(avg_power_w=round(joules / span, 1), peak_power_w=round(max(p for _, p in self._points), 1),
                           energy_wh=round(joules / 3600, 3), seconds=round(span, 1), samples=len(self._points), gpus=self._gpus)
        if out:
            out["note"] = NOTE
        return out
