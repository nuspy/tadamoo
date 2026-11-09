"""Only the selected engine is loaded: the one engine in use for the use at hand.

`ActiveEngine.use(engine_id)` makes an engine the one in use; the engine that was in use is unloaded first (its processes stopped, its models freed), so no
process or model of an engine that is not selected stays in memory. An engine is started only by `render` or `prepare` on the one in use; asking for the
registry's engines, their status or their descriptor starts nothing."""
from __future__ import annotations

import threading

from . import registry


class ActiveEngine:
    def __init__(self, engines=None) -> None:
        self._engines = engines if engines is not None else registry.ENGINES
        self._lock = threading.Lock()
        self._id: str | None = None

    @property
    def current_id(self) -> str | None:
        return self._id

    def use(self, engine_id: str | None):
        """Make `engine_id` the engine in use (None: none), unloading the one that was; returns the engine, or None."""
        with self._lock:
            if engine_id == self._id:
                return self._engines.get(engine_id) if engine_id else None
            if engine_id is not None and engine_id not in self._engines:
                raise KeyError(engine_id)
            previous = self._engines.get(self._id) if self._id else None
            self._id = engine_id
        if previous is not None:
            previous.unload()
        return self._engines.get(engine_id) if engine_id else None

    def release(self) -> None:
        """Unload the engine in use and use none (the avatar is closed, or the service stops)."""
        self.use(None)

    def render(self, pcm16: bytes, sample_rate: int, resolution: int, fps: int, **kw):
        engine = self._engines.get(self._id) if self._id else None
        if engine is None:
            raise RuntimeError("no avatar engine is selected")
        return engine.render(pcm16, sample_rate, resolution, fps, **kw)

    def prepare(self, avatar_dir, progress, **kw):
        engine = self._engines.get(self._id) if self._id else None
        if engine is None:
            raise RuntimeError("no avatar engine is selected")
        return engine.prepare(avatar_dir, progress, **kw)
