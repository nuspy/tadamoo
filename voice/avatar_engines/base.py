"""What an avatar engine is: its descriptor and its interface.

An engine is one way to draw an avatar that talks. `EngineInfo` says what it is (where it runs, what it weighs, its licence, what it needs) and
tells whether it can be used for a given avatar; `AvatarEngine` is what an implementation provides. The interface methods raise
`NotImplementedForNow` until an engine has code for them, so engines are added one at a time and the registry can list them all meanwhile.

A number appears in a descriptor only when it is a fact: a size measured on a file, a figure the project states (and the text says so), or a
choice made here (the output heights). Everything else is None or the words "not measured here"."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

TARGETS = ("pc", "web", "android", "esp32")
KINDS = ("generic", "personalized")
MODES = ("visemes", "wav2lip", "video")        # the render modes of the gateway (docs/voice/gateway.md)
LANGS = ("en", "it")
# what status_of looks for; see status.py
REQUIREMENTS = ("viseme_sprites", "wav2lip_assets", "musetalk_prepared", "trained", "engine_pack", "none")


class NotImplementedForNow(NotImplementedError):
    """The engine is described and can be chosen, but this part of it is not written yet."""


@dataclass(frozen=True)
class License:
    id: str
    noncommercial: bool            # known to forbid commercial use
    verify: bool                   # the licence or the provenance of the weights is not settled: check before distributing
    note: dict                     # {en, it}


@dataclass(frozen=True)
class Training:
    needed: bool                   # one avatar needs its own training for this engine
    video_minutes: tuple | None    # (min, max) minutes of video of the person, None when not stated
    gpu: bool                      # the training needs a GPU


@dataclass(frozen=True)
class EngineInfo:
    id: str
    name: str
    kind: str                                   # generic | personalized
    runs_on: tuple                              # subset of TARGETS
    weight_mb: int | None                       # None = not measured here
    performance: dict                           # {en, it}: plain text, numbers only when sourced
    native_resolution: int | None               # the height the model draws at, None when not stated
    resolutions: tuple                          # the output heights that can be chosen (lower is lighter; the enhance stage scales)
    default_resolution: int
    license: License
    training: Training
    needs: tuple                                # human reasons, each {en, it}
    mode: str | None                            # the gateway render mode it is served as, None = never offered on a device yet
    requires: str                               # what status_of looks for (REQUIREMENTS)
    integrated: bool                            # its code and weights are part of this build

    def validate(self) -> None:
        """Raises ValueError when the descriptor is incomplete or contradicts itself."""
        if not self.id or not self.id.replace("_", "").isalnum() or self.id != self.id.lower():
            raise ValueError(f"bad engine id {self.id!r}")
        if not self.name or self.kind not in KINDS:
            raise ValueError(f"{self.id}: a name and a kind of {KINDS} are needed")
        if not self.runs_on or not set(self.runs_on) <= set(TARGETS):
            raise ValueError(f"{self.id}: runs_on is a non-empty subset of {TARGETS}")
        if self.weight_mb is not None and (isinstance(self.weight_mb, bool) or not isinstance(self.weight_mb, int) or self.weight_mb < 0):
            raise ValueError(f"{self.id}: weight_mb is a whole number of MB or None")
        for label, text in (("performance", self.performance), ("license.note", self.license.note), *[("needs", n) for n in self.needs]):
            if set(text) != set(LANGS) or not all(isinstance(text[k], str) and text[k] for k in LANGS):
                raise ValueError(f"{self.id}: {label} needs text in {LANGS}")
        if not self.resolutions or list(self.resolutions) != sorted(set(self.resolutions)) or any(not isinstance(r, int) or r <= 0 for r in self.resolutions):
            raise ValueError(f"{self.id}: resolutions is an increasing list of heights")
        if self.default_resolution not in self.resolutions:
            raise ValueError(f"{self.id}: the default resolution is one of the resolutions")
        if self.native_resolution is not None and self.native_resolution <= 0:
            raise ValueError(f"{self.id}: native_resolution is a height or None")
        if self.training.video_minutes is not None and not (len(self.training.video_minutes) == 2 and 0 < self.training.video_minutes[0] <= self.training.video_minutes[1]):
            raise ValueError(f"{self.id}: video_minutes is (min, max) or None")
        if self.kind == "generic" and self.training.needed:
            raise ValueError(f"{self.id}: a generic engine needs no training")
        if self.kind == "personalized" and not self.training.needed:
            raise ValueError(f"{self.id}: a personalized engine is trained per person")
        if self.mode is not None and self.mode not in MODES:
            raise ValueError(f"{self.id}: mode is one of {MODES} or None")
        if self.requires not in REQUIREMENTS:
            raise ValueError(f"{self.id}: requires is one of {REQUIREMENTS}")

    def status_of(self, avatar_dir, models_base: str | None = None) -> dict:
        """{available, reason, reason_it} for the avatar in `avatar_dir`: whether what this engine draws from exists. Reads files only."""
        from . import status
        return status.of(self, avatar_dir, models_base)

    def as_dict(self) -> dict:
        lic = self.license
        return {"id": self.id, "name": self.name, "kind": self.kind, "runs_on": list(self.runs_on), "weight_mb": self.weight_mb,
                "performance": dict(self.performance), "native_resolution": self.native_resolution, "resolutions": list(self.resolutions),
                "default_resolution": self.default_resolution,
                "license": {"id": lic.id, "noncommercial": lic.noncommercial, "verify": lic.verify, "note": dict(lic.note)},
                "training": {"needed": self.training.needed, "video_minutes": list(self.training.video_minutes) if self.training.video_minutes else None,
                             "gpu": self.training.gpu},
                "needs": [dict(n) for n in self.needs], "mode": self.mode, "integrated": self.integrated}


class AvatarEngine(ABC):
    """What an engine implements. Every method but `info` raises NotImplementedForNow until the engine has code for it."""

    @property
    @abstractmethod
    def info(self) -> EngineInfo:
        """The descriptor."""

    def status_of(self, avatar_dir, models_base: str | None = None) -> dict:
        return self.info.status_of(avatar_dir, models_base)

    def prepare(self, avatar_dir: Path, progress: Callable[[str, float], None]) -> None:
        """Make what rendering needs from the avatar's files (training included, for a personalized engine); `progress(phase, percent)`."""
        raise NotImplementedForNow(f"{self.info.id}: prepare is not written yet")

    def render(self, pcm16: bytes, sample_rate: int, resolution: int, fps: int) -> Iterator:
        """Frames for this audio (PCM16 mono): objects with width, height and bgr() like `avatar_client.Frame`, `resolution` pixels high."""
        raise NotImplementedForNow(f"{self.info.id}: render is not written yet")

    def export_for_device(self, avatar_dir: Path, target: str) -> list[Path]:
        """The files a device of `target` needs to draw this avatar itself, as paths inside `avatar_dir`."""
        raise NotImplementedForNow(f"{self.info.id}: export_for_device is not written yet")
