"""VRAM Explorer: who holds the GPU memory, read with the collector of gputaskman (serve/gputaskman).

  VramExplorer.gpus()               the adapters that can be shown, [{"index", "name", "vendor"}]
  VramExplorer.snapshot(i)          one reading of adapter i as the JSON dict of gputaskman's report, plus "gpus"
  VramExplorer.start()              begins recording every adapter once per HISTORY_STEP_S (the first request of the page does it)
  VramExplorer.processes(i, range)  the memory of each process over the last `range` seconds, from what was recorded
  VramExplorer.close()              stop recording and release every collector

A collector is made at the first request for an adapter and closed after IDLE_CLOSE_S without one; the recorder asks every
HISTORY_STEP_S, so once recording has started the collectors stay. Requests within MIN_INTERVAL_S share one reading.
"""
from __future__ import annotations

import collections
import threading
import time

MIN_INTERVAL_S = 1.0
IDLE_CLOSE_S = 120.0
HISTORY_STEP_S = 10
HISTORY_POINTS = 24 * 3600 // HISTORY_STEP_S           # a day of readings
SHOWN = 240                                            # most buckets one answer carries
SERIES_SHOWN = 8                                       # processes drawn one by one; the rest are summed
MIN_PROCESS_BYTES = 4 << 20                            # a smaller holder is left out of the record


def _load():
    """The gputaskman pieces this module uses, or the reason they cannot be loaded."""
    from serve.gputaskman import collector, report
    return collector, report


class VramExplorer:
    def __init__(self, loader=_load, clock=time.monotonic, wall=time.time):
        self._loader = loader
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._open: dict[int, dict] = {}        # adapter index -> {"collector", "seen", "at", "result"}
        self._gpus: list[dict] | None = None
        self._hlock = threading.Lock()
        self._hist: dict[int, collections.deque] = {}      # adapter index -> (time, used, total, attributed, {name: bytes})
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def gpus(self) -> list[dict]:
        if self._gpus is None:
            collector, _ = self._loader()
            found = collector.ordered_adapters(collector.enumerate_adapters())
            self._gpus = [{"index": i, "name": a.name, "vendor": a.vendor} for i, a in enumerate(found)]
        return self._gpus

    def snapshot(self, index: int = 0) -> dict:
        with self._lock:
            now = self._clock()
            self._close_idle(now)
            try:
                entry = self._open.get(index)
                if entry is not None and now - entry["at"] < MIN_INTERVAL_S and entry["result"] is not None:
                    return entry["result"]
                collector, report = self._loader()
                if entry is None:
                    entry = {"collector": collector.Collector(index, host=False), "seen": now, "at": 0.0, "result": None}
                    self._open[index] = entry
                    entry["collector"].sample()         # rates (engine load) need a first reading to start from
                    time.sleep(0.5)
                result = report.to_json_dict(entry["collector"].sample(), None, None)
                result["gpus"] = self.gpus()
                result["index"] = index
                entry.update(seen=now, at=now, result=result)
                return result
            except (ValueError, OSError, RuntimeError, ImportError) as e:
                return {"error": f"{type(e).__name__}: {e}", "gpus": [], "index": index}

    # ---- the record

    def start(self) -> None:
        """Begin recording; later calls do nothing."""
        with self._hlock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="vram-record", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                indexes = [g["index"] for g in self.gpus()]
            except (ValueError, OSError, RuntimeError, ImportError):
                return
            for index in indexes:
                self.record(index, self.snapshot(index))
            self._stop.wait(HISTORY_STEP_S)

    def record(self, index: int, result: dict) -> None:
        """Add a reading to the record of adapter `index`; a reading with an error or without a used figure is skipped."""
        used = result.get("vram_used")
        if result.get("error") or not isinstance(used, (int, float)):
            return
        held: dict[str, int] = {}
        for p in result.get("processes") or []:
            v = p.get("vram_bytes") if isinstance(p.get("vram_bytes"), (int, float)) else p.get("vram_inferred_bytes")
            if isinstance(v, (int, float)) and v >= MIN_PROCESS_BYTES:
                name = p.get("name") or f"pid {p.get('pid')}"
                held[name] = held.get(name, 0) + int(v)
        with self._hlock:
            self._hist.setdefault(index, collections.deque(maxlen=HISTORY_POINTS)).append(
                (self._wall(), used, result.get("vram_total"), result.get("attributed") or 0, held))

    def processes(self, index: int, range_s: int) -> dict:
        """The recorded memory of adapter `index` over the last `range_s` seconds: the mean of each bucket (so the processes add up to
        the total), the SERIES_SHOWN biggest processes by name with the rest in "other", and what no process accounts for."""
        range_s = max(HISTORY_STEP_S, min(int(range_s), HISTORY_POINTS * HISTORY_STEP_S))
        step = max(HISTORY_STEP_S, -(-range_s // SHOWN))
        n = range_s // step
        end = self._wall()
        edge = end - n * step
        with self._hlock:
            points = [p for p in self._hist.get(index, ()) if p[0] > edge]
        count = [0] * n
        used = [0.0] * n
        unattributed = [0.0] * n
        by_name: dict[str, list[float]] = {}
        total = None
        for t, u, tot, attributed, held in points:
            b = min(n - 1, int((t - edge) // step))
            count[b] += 1
            used[b] += u
            unattributed[b] += max(0, u - attributed)
            total = tot if isinstance(tot, (int, float)) else total
            for name, v in held.items():
                by_name.setdefault(name, [0.0] * n)[b] += v

        def mean(values):
            return [round(v / count[i]) if count[i] else None for i, v in enumerate(values)]
        ranked = sorted(by_name, key=lambda k: -max(x or 0 for x in mean(by_name[k])))
        series = [{"name": k, "used": mean(by_name[k])} for k in ranked[:SERIES_SHOWN]]
        other = [0.0] * n
        for k in ranked[SERIES_SHOWN:]:
            other = [a + b for a, b in zip(other, by_name[k])]
        gpus = self._gpus or []
        name = next((g["name"] for g in gpus if g["index"] == index), None)
        return {"range_s": range_s, "step_s": step, "end": end, "gpu": name, "total": total, "series": series,
                "other": mean(other), "unattributed": mean(unattributed), "used": mean(used)}

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            for entry in self._open.values():
                self._shut(entry)
            self._open.clear()

    def _close_idle(self, now: float) -> None:
        for index in [i for i, e in self._open.items() if now - e["seen"] > IDLE_CLOSE_S]:
            self._shut(self._open.pop(index))

    @staticmethod
    def _shut(entry: dict) -> None:
        try:
            entry["collector"].close()
        except Exception:  # noqa: BLE001 - a collector that cannot close must not take the server down
            pass
