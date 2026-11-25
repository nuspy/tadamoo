"""VRAM Explorer: who holds the GPU memory, read on demand with the collector of gputaskman (serve/gputaskman).

  VramExplorer.gpus()          the adapters that can be shown, [{"index", "name", "vendor"}]
  VramExplorer.snapshot(i)     one reading of adapter i as the JSON dict of gputaskman's report, plus "gpus"
  VramExplorer.close()         release every collector

A collector is made at the first request for an adapter and closed after IDLE_CLOSE_S without one, so nothing runs while the
Monitor does not ask. Requests within MIN_INTERVAL_S share one reading.
"""
from __future__ import annotations

import threading
import time

MIN_INTERVAL_S = 1.0
IDLE_CLOSE_S = 120.0


def _load():
    """The gputaskman pieces this module uses, or the reason they cannot be loaded."""
    from serve.gputaskman import collector, report
    return collector, report


class VramExplorer:
    def __init__(self, loader=_load, clock=time.monotonic):
        self._loader = loader
        self._clock = clock
        self._lock = threading.Lock()
        self._open: dict[int, dict] = {}        # adapter index -> {"collector", "seen", "at", "result"}
        self._gpus: list[dict] | None = None

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

    def close(self) -> None:
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
