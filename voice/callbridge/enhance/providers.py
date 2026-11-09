"""The providers of the two stages, and the registry a new one plugs into.

A stage has a provider chosen by name. The interface is two methods on numpy BGR frames (uint8, height x width x 3):

    Upscaler.upscale(img, out_w, out_h) -> img of exactly out_w x out_h
    Interpolator.interpolate(a, b, alphas) -> one frame per alpha: the picture a fraction `alpha` of the way from `a` to `b`

A provider that cannot work raises `ProviderError` (`NeedsSdk` when something the person must install is missing); the pipeline then skips
the stage for that frame and reports it. To add a provider (a neural interpolator, another SDK), implement the class, `register()` it and add
its name to `settings.UPSCALE` or `settings.INTERPOLATE` and to the capability report.

The `basic` providers are plain CPU arithmetic: a Lanczos resize and a linear blend of two frames. They are not neural. A blend of two
frames of a moving face ghosts a little; it smooths the pace of the picture and does not invent detail."""
from __future__ import annotations

from typing import Callable


class ProviderError(RuntimeError):
    """The provider could not process a frame."""


class NeedsSdk(ProviderError):
    """The provider needs software the person has to install."""


class BasicUpscaler:
    name, neural = "basic", False

    def upscale(self, img, out_w: int, out_h: int):
        import cv2
        h, w = img.shape[:2]
        if (w, h) == (out_w, out_h):
            return img
        return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4 if out_w > w else cv2.INTER_AREA)


class BasicInterpolator:
    name, neural = "basic", False

    def interpolate(self, a, b, alphas):
        import cv2
        if a.shape != b.shape:
            raise ProviderError("the two frames differ in size")
        return [cv2.addWeighted(a, 1.0 - float(t), b, float(t), 0.0) for t in alphas]


_UPSCALERS: dict[str, Callable[[object], object]] = {"basic": lambda settings: BasicUpscaler()}
_INTERPOLATORS: dict[str, Callable[[object], object]] = {"basic": lambda settings: BasicInterpolator()}


def register(stage: str, name: str, factory: Callable[[object], object]) -> None:
    """Add a provider: `factory(settings)` returns the object with the stage's method."""
    {"upscale": _UPSCALERS, "interpolate": _INTERPOLATORS}[stage][name] = factory


def make(stage: str, name: str, settings):
    """The provider `name` of a stage, built for these settings (an `EnhanceSettings`)."""
    table = {"upscale": _UPSCALERS, "interpolate": _INTERPOLATORS}[stage]
    if name not in table:
        raise ProviderError(f"there is no {stage} provider called {name}")
    return table[name](settings)
