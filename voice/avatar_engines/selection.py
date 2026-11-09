"""The person's choice of engine and output height per target, as `voice.json` `avatar` holds it.

    {"engines": {"pc": id, "web": id, "android": id, "esp32": id}, "resolution": {"pc": height, ...}, "fps_hint": 5..60}

`parse` reads what is stored and replaces every damaged value by its default, so a bad file never leaves a target without an engine.
`check` is for a request: it refuses an engine that does not run on the target and a height the engine does not offer. serve/voice_settings.py
keeps a copy of the table of engines it needs to check a request (the server cannot import this package); a test keeps the two equal.

The height is the size of the picture the target gets. A model draws at its own size; the rest is the enhance stage (callbridge/enhance), which
this module does not repeat."""
from __future__ import annotations

from dataclasses import dataclass

from . import registry
from .base import TARGETS

FPS_HINT = (5, 60)
DEFAULT_FPS = 25


class SelectionError(ValueError):
    """A refused choice: `code` names the field and `message` and `message_it` say why."""

    def __init__(self, code: str, en: str, it: str) -> None:
        super().__init__(en)
        self.code, self.en, self.it = code, en, it


@dataclass(frozen=True)
class Selection:
    engines: dict
    resolution: dict
    fps_hint: int = DEFAULT_FPS

    def as_dict(self) -> dict:
        return {"engines": dict(self.engines), "resolution": dict(self.resolution), "fps_hint": self.fps_hint}


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def default_resolution(target: str, engine_id: str, current=None) -> int:
    """The height to use for `engine_id` on `target`: `current` when the engine offers it, else the target's default when it does, else the engine's own."""
    info = registry.info_of(engine_id)
    for height in (current, registry.DEFAULT_RESOLUTION[target]):
        if _whole(height) and height in info.resolutions:
            return height
    return info.default_resolution


def parse(stored) -> Selection:
    """The selection in a stored `avatar` object; each value that is missing or invalid takes its default."""
    stored = stored if isinstance(stored, dict) else {}
    engines_in = stored.get("engines") if isinstance(stored.get("engines"), dict) else {}
    heights_in = stored.get("resolution") if isinstance(stored.get("resolution"), dict) else {}
    engines, heights = {}, {}
    for target in TARGETS:
        info = registry.info_of(engines_in.get(target))
        engines[target] = info.id if info and target in info.runs_on else registry.DEFAULT_ENGINES[target]
        heights[target] = default_resolution(target, engines[target], heights_in.get(target))
    fps = stored.get("fps_hint")
    return Selection(engines, heights, fps if _whole(fps) and FPS_HINT[0] <= fps <= FPS_HINT[1] else DEFAULT_FPS)


def check_engine(target, engine_id):
    """The descriptor of `engine_id` for `target`, or SelectionError."""
    if target not in TARGETS:
        raise SelectionError("target", f"The target is one of {', '.join(TARGETS)}.", f"La destinazione è una tra {', '.join(TARGETS)}.")
    info = registry.info_of(engine_id)
    if info is None:
        raise SelectionError("engine", "This engine is not known.", "Questo motore non è conosciuto.")
    if target not in info.runs_on:
        raise SelectionError("engine", f"{info.name} does not run on {target}: it runs on {', '.join(info.runs_on)}.",
                             f"{info.name} non gira su {target}: gira su {', '.join(info.runs_on)}.")
    return info


def check_resolution(info, height) -> int:
    if not _whole(height) or height not in info.resolutions:
        raise SelectionError("resolution", f"{info.name} draws at {', '.join(map(str, info.resolutions))} pixels high.",
                             f"{info.name} disegna a {', '.join(map(str, info.resolutions))} pixel di altezza.")
    return height


def select(stored, target, engine_id, resolution=None) -> Selection:
    """The stored selection with `target` set to `engine_id`; the height is `resolution`, or the one kept when the new engine offers it, else a default."""
    info = check_engine(target, engine_id)
    now = parse(stored)
    height = check_resolution(info, resolution) if resolution is not None else default_resolution(target, info.id, now.resolution[target])
    return Selection({**now.engines, target: info.id}, {**now.resolution, target: height}, now.fps_hint)


# ---- what one device gets

def target_of(device) -> str | None:
    """The target a device kind belongs to (the kinds are named like the targets), None for a kind that has no setting."""
    return device if device in TARGETS else None


def wanted(selection: Selection, device, engine=None, height=None):
    """(descriptor or None, height or None): the engine a device is configured for and the height to draw it at.

    The device's own declaration wins when the engine runs on its target and offers the height; otherwise the person's choice for the
    target counts, and a device kind with no target has none."""
    target = target_of(device)
    info = registry.info_of(engine)
    if info is not None and target is not None and target not in info.runs_on:
        info = None
    if info is None and target is not None:
        info = registry.info_of(selection.engines[target])
    if info is None:
        return None, None
    if _whole(height) and height in info.resolutions:
        return info, height
    if target is not None and info.id == selection.engines[target]:
        return info, selection.resolution[target]
    return info, None


def for_mode(mode: str, info, height, device) -> tuple[str, int]:
    """(engine id, height) that draws `mode` for a device: the configured engine when it draws that mode and is integrated in this build,
    else the integrated engine of the mode."""
    use = info if info is not None and info.mode == mode and info.integrated else registry.info_of(registry.MODE_ENGINE[mode])
    if use is info and height in use.resolutions:
        return use.id, height
    target = target_of(device)
    return use.id, default_resolution(target, use.id) if target else use.default_resolution
