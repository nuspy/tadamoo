"""Chooses the desktop implementation for this machine."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from .base import Desktop
from .unsupported import MACOS, WAYLAND, UnsupportedDesktop

_cached: Desktop | None = None


def session_type(env: Mapping[str, str]) -> str:
    """"x11", "wayland" or "none" for a Linux session."""
    kind = (env.get("XDG_SESSION_TYPE") or "").lower()
    if kind == "wayland":
        return "wayland"
    if env.get("WAYLAND_DISPLAY") and kind != "x11":
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return "none"


def _load_windows() -> Desktop:
    from .windows import WindowsDesktop
    return WindowsDesktop()


def _load_x11() -> Desktop:
    from .x11 import X11Desktop
    return X11Desktop()


def choose(sys_platform: str, env: Mapping[str, str]) -> Desktop:
    if sys_platform == "win32":
        try:
            return _load_windows()
        except ImportError as e:
            return UnsupportedDesktop(f"Windows clipboard support is missing: {e}")
    if sys_platform.startswith("linux"):
        kind = session_type(env)
        if kind == "wayland":
            return UnsupportedDesktop(WAYLAND)
        if kind == "none":
            return UnsupportedDesktop("no X11 display (DISPLAY is not set)")
        return _load_x11()
    if sys_platform == "darwin":
        return UnsupportedDesktop(MACOS)
    return UnsupportedDesktop(f"no desktop support for {sys_platform}")


def platform() -> Desktop:
    """The desktop of this process, created once."""
    global _cached
    if _cached is None:
        _cached = choose(sys.platform, os.environ)
    return _cached
