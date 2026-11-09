"""The training video of an avatar: one long video of the person (3 to 5 minutes is best, 2 to 10 are accepted), kept apart from the wizard's clips, which are capped at
120 s. A personalised engine (voice/avatar_training.py) learns from it when it is there and falls back to the longest clip otherwise.

    GET    /avatar/<name>/training-video    {video: null or {file, seconds, fps, width, height, bytes, uploaded, in_range}, limits}
    POST   /avatar/<name>/training-video    multipart field `file`: an MP4, MOV, WebM or MKV, streamed to disk, kept as `<avatar>/training/video.<ext>`
    DELETE /avatar/<name>/training-video    remove it

A file is refused before it is kept when its name does not end in a video extension, its type is not a video type, it is over the size cap, its first bytes are not the
container its extension names, or the PC's video reader (the one the training plan measures with) cannot open it. The name the client sent is never used. The length is not a
reason to refuse here: the plan refuses a video outside 2 to 10 minutes with its length, and the page shows `in_range`."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from callbridge.avatar_store import TRAINING_EXTS, TRAINING_STEM, AvatarStore, StoreError
from voice.avatar_training import VIDEO_SECONDS, TrainingError, probe_video

LIMIT = 2000 << 20                           # the size cap of the file (under the 2 GiB the service accepts for a whole request)
OVERHEAD = 1 << 16                           # the multipart framing around it
CHUNK = 1 << 20
SIDE = (128, 4096)                           # smallest and largest side of the picture, as the wizard's video
META = TRAINING_STEM + ".json"
GENERIC_TYPES = ("", "application/octet-stream")          # what a browser sends for a container it does not know
ISO_BOXES = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")
EBML = b"\x1a\x45\xdf\xa3"
FAMILY = {".mp4": "iso", ".mov": "iso", ".webm": "ebml", ".mkv": "ebml"}
_busy: set[str] = set()
_busy_lock = threading.Lock()


def _extension(filename) -> str:
    ext = os.path.splitext(os.path.basename(str(filename or "")))[1].lower()
    if ext not in TRAINING_EXTS:
        raise TrainingError(415, "bad_type", "The training video is an MP4, MOV, WebM or MKV file.")
    return ext


def _container(head: bytes) -> str:
    if head[4:8] in ISO_BOXES:
        return "iso"
    return "ebml" if head[:4] == EBML else ""


class TrainingVideos:
    def __init__(self, store: AvatarStore, probe=probe_video, limit: int = LIMIT) -> None:
        self.store, self.probe, self.limit = store, probe, limit

    def _folder(self, avatar) -> Path:
        try:
            return self.store.existing(avatar)
        except StoreError as e:
            raise TrainingError(404, "not_found", str(e)) from None

    def limits(self) -> dict:
        return {"max_bytes": self.limit, "extensions": list(TRAINING_EXTS), "seconds": list(VIDEO_SECONDS), "best": [180, 300]}

    @staticmethod
    def _in_range(seconds) -> bool:
        return isinstance(seconds, (int, float)) and VIDEO_SECONDS[0] <= seconds <= VIDEO_SECONDS[1]

    def info(self, avatar) -> dict:
        self._folder(avatar)
        path = self.store.training_video(avatar)
        out: dict = {"video": None, "limits": self.limits()}
        if path is None:
            return out
        try:
            meta = json.loads((path.parent / META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict) or meta.get("file") != path.name:
            try:
                meta = {"file": path.name, **self.probe(path)}                    # put there by hand: measured now
            except (ValueError, OSError, ImportError):
                meta = {"file": path.name}
        out["video"] = {**meta, "bytes": path.stat().st_size, "in_range": self._in_range(meta.get("seconds"))}
        return out

    def _copy(self, stream, target: Path) -> bytes:
        """Stream `stream` to `target` up to the cap; the first 12 bytes. Removes the partial file when it is refused."""
        size, head = 0, b""
        try:
            with target.open("wb") as out:
                while True:
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.limit:
                        raise TrainingError(413, "too_big", f"The file is too big: at most {self.limit >> 20} MB.")
                    if len(head) < 12:
                        head = (head + chunk)[:12]
                    out.write(chunk)
            if not size:
                raise TrainingError(400, "empty", "The file is empty.")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return head

    def save(self, avatar, storage, declared_length: int | None = None) -> dict:
        """Keep the file of `storage` (a werkzeug FileStorage) as the training video of `avatar`, replacing the one it had."""
        self._folder(avatar)
        ext = _extension(getattr(storage, "filename", ""))
        mime = (getattr(storage, "mimetype", "") or "").lower()
        if not (mime.startswith("video/") or mime in GENERIC_TYPES):
            raise TrainingError(415, "bad_type", f"That file is sent as {mime}, not as a video.")
        if declared_length is not None and declared_length > self.limit + OVERHEAD:
            raise TrainingError(413, "too_big", f"The file is too big: at most {self.limit >> 20} MB.")
        with _busy_lock:
            if avatar in _busy:
                raise TrainingError(409, "busy", "A training video is already being sent for this avatar.")
            _busy.add(avatar)
        try:
            return self._keep(avatar, ext, storage.stream)
        finally:
            with _busy_lock:
                _busy.discard(avatar)

    def _keep(self, avatar: str, ext: str, stream) -> dict:
        try:
            target = self.store.training_dir(avatar)
        except StoreError as e:
            raise TrainingError(409, "not_usable", str(e)) from None
        target.mkdir(exist_ok=True)
        for stale in target.glob("incoming.*"):
            stale.unlink(missing_ok=True)
        incoming = target / ("incoming" + ext)
        head = self._copy(stream, incoming)
        try:
            if _container(head) != FAMILY[ext]:
                raise TrainingError(422, "bad_container", f"That file is not an {ext[1:].upper()} video: its first bytes are not one.")
            try:
                info = self.probe(incoming)
            except ImportError:
                raise TrainingError(501, "needs_opencv", "needs opencv: install opencv-python-headless in the voice service's environment") from None
            except (ValueError, OSError):
                raise TrainingError(422, "unreadable", "The video cannot be read: it has no frames this PC can open.") from None
            if min(info["width"], info["height"]) < SIDE[0] or max(info["width"], info["height"]) > SIDE[1]:
                raise TrainingError(422, "bad_size", f"The video is {info['width']}x{info['height']}: its sides must be between {SIDE[0]} and {SIDE[1]} pixels.")
        except BaseException:
            incoming.unlink(missing_ok=True)
            raise
        for old in target.glob(TRAINING_STEM + ".*"):
            old.unlink()
        kept = target / (TRAINING_STEM + ext)
        os.replace(incoming, kept)
        meta = {"file": kept.name, **info, "uploaded": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        (target / META).write_text(json.dumps(meta), encoding="utf-8")
        return {"ok": True, **meta, "bytes": kept.stat().st_size, "in_range": self._in_range(info["seconds"]), "range": list(VIDEO_SECONDS)}

    def remove(self, avatar) -> dict:
        self._folder(avatar)
        path = self.store.training_video(avatar)
        if path is None:
            raise TrainingError(404, "not_found", f"{avatar} has no training video")
        target = path.parent
        for old in target.glob(TRAINING_STEM + ".*"):
            old.unlink()
        try:
            target.rmdir()
        except OSError:
            pass                                                                  # something else is in there: left alone
        return {"ok": True}
