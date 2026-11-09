"""Where the rendered avatar goes besides the phone: a browser preview (MJPEG) and, when the person turns it on, a virtual camera.

Both are subscribers of `AvatarClient.frames`. The preview encodes a frame to JPEG only while someone is watching, at most `PREVIEW_FPS` a
second. The virtual camera uses pyvirtualcam, on Windows and on Linux (v4l2loopback) only; it is opened on the first frame after the person
turns it on and closed when they turn it off or the avatar stops. Nothing here installs a driver, a package or a kernel module: when the
camera cannot be opened the reason is reported and the avatar goes on without it."""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable

from .avatar_client import AvatarFrames, Frame

log = logging.getLogger("callbridge.avatar.output")

PREVIEW_FPS = 15
PREVIEW_QUALITY = 75
MAX_VIEWERS = 4
BOUNDARY = "avatarframe"
CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"


def part(jpeg: bytes) -> bytes:
    """One part of the MJPEG stream."""
    return (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n").encode("ascii") + jpeg + b"\r\n"


class Preview:
    """The latest frame as JPEG, for any number of watchers (up to MAX_VIEWERS)."""

    def __init__(self, frames: AvatarFrames, fps: int = PREVIEW_FPS, quality: int = PREVIEW_QUALITY, clock: Callable[[], float] = time.monotonic) -> None:
        self.frames, self.interval, self.quality, self.clock = frames, 1.0 / fps, quality, clock
        self._cond = threading.Condition()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._last = float("-inf")
        self._viewers = 0
        self._unsubscribe: Callable[[], None] | None = None

    @property
    def viewers(self) -> int:
        return self._viewers

    def open(self) -> bool:
        """Count a watcher in; False when there are too many. The first one starts the frames."""
        with self._cond:
            if self._viewers >= MAX_VIEWERS:
                return False
            self._viewers += 1
            first = self._viewers == 1
        if first:
            self._unsubscribe = self.frames.subscribe(self._on_frame)
        return True

    def close(self) -> None:
        with self._cond:
            self._viewers = max(0, self._viewers - 1)
            last = self._viewers == 0
            if last:
                self._jpeg = None
        if last and self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _on_frame(self, frame: Frame) -> None:
        now = self.clock()
        if now - self._last < self.interval:
            return
        self._last = now
        jpeg = frame.jpeg(self.quality)
        with self._cond:
            self._jpeg, self._seq = jpeg, self._seq + 1
            self._cond.notify_all()

    def stream(self, idle_timeout: float = 30.0):
        """The parts of the MJPEG stream; ends after `idle_timeout` seconds without a frame (the avatar stopped). Call open() before, close() after."""
        seen = 0
        while True:
            with self._cond:
                if not self._cond.wait_for(lambda: self._seq != seen and self._jpeg is not None, timeout=idle_timeout):
                    return
                jpeg, seen = self._jpeg, self._seq
            yield part(jpeg)


def camera_support() -> tuple[bool, str]:
    """(whether this system can have a virtual camera, why not). pyvirtualcam is looked for, never installed."""
    if sys.platform not in ("win32", "linux"):
        return False, "the virtual camera is unsupported on this system"
    try:
        import pyvirtualcam  # noqa: F401
    except ImportError:
        return False, "pyvirtualcam is not installed in the voice environment"
    return True, ""


def _open_camera(width: int, height: int, fps: int):
    import pyvirtualcam
    return pyvirtualcam.Camera(width=width, height=height, fps=fps, fmt=pyvirtualcam.PixelFormat.BGR, print_fps=False)


class VirtualCamera:
    """Frames to a virtual camera while it is turned on."""

    def __init__(self, frames: AvatarFrames, fps: int = 25, opener: Callable | None = None, support: Callable[[], tuple[bool, str]] | None = None) -> None:
        self.frames, self.fps = frames, fps
        self._open = opener or _open_camera
        self._support = support or camera_support
        self._cam = None
        self._size: tuple[int, int] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self.reason = ""

    @property
    def running(self) -> bool:
        return self._unsubscribe is not None

    def start(self) -> bool:
        """Turn the camera on; False with `reason` when this system cannot. The device itself opens with the first frame."""
        with self._lock:
            if self._unsubscribe is not None:
                return True
            ok, why = self._support()
            if not ok:
                self.reason = why
                return False
            self.reason = ""
            self._unsubscribe = self.frames.subscribe(self._on_frame)
            return True

    def stop(self) -> None:
        with self._lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            cam, self._cam, self._size = self._cam, None, None
        if unsubscribe is not None:
            unsubscribe()
        if cam is not None:
            try:
                cam.close()
            except Exception as e:                # noqa: BLE001
                log.debug("camera: close failed: %s", e)

    def _on_frame(self, frame: Frame) -> None:
        size = (frame.width, frame.height)
        if self._cam is not None and size != self._size:
            self._release()
        if self._cam is None:
            try:
                self._cam = self._open(frame.width, frame.height, self.fps)
                self._size = size
                log.info("camera: sending to %s", getattr(self._cam, "device", "the virtual camera"))
            except Exception as e:                # noqa: BLE001 (no driver, no loopback device, the device is busy)
                self.reason = f"the virtual camera could not be opened ({e}): TaDaMoo installs no driver; OBS Virtual Camera on Windows or v4l2loopback on Linux must be there"
                log.warning("camera: %s", self.reason)
                threading.Thread(target=self.stop, daemon=True).start()
                return
        self._cam.send(frame.bgr())

    def _release(self) -> None:
        cam, self._cam, self._size = self._cam, None, None
        if cam is not None:
            try:
                cam.close()
            except Exception:                     # noqa: BLE001
                pass

    def status(self, enabled: bool) -> dict:
        ok, why = self._support()
        return {"enabled": enabled, "supported": ok, "running": self.running and self._cam is not None, "device": str(getattr(self._cam, "device", "")) if self._cam else "",
                "reason": self.reason or ("" if ok else why)}
