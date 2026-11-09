"""The avatars of the person: one folder each under `<user data>/voice/avatars/<name>/`.

    avatar.json                 {"display_name", "created", "voice": {"engine", "voice"}}  (written by the wizard; the personality owns the voice)
    clips/*.mp4                 the footage MuseTalk animates; one is picked at random each time the avatar starts
    poster.jpg                  the portrait
    wav2lip/face.png, box.json  the straightened 96x96 face and how to paste the mouth back (the phone's Wav2Lip)
    visemes/<V>.png, frame.json the mouth sprites of the viseme mode and the frame they are drawn in (made from the photo)
    prepared/                   one folder per prepared clip (frames, coordinates, latents, masks) and `index.json` listing them

A name is one word of letters, digits, `_` and `-`: it is also what a personality's `avatar` field holds. Nothing outside the root is ever
touched: a name or a clip that is not plain is refused before any path is built."""
from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}")
CLIP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_ -]{0,79}\.mp4")
# names of the service's own routes after /avatar/: an avatar called like one would be shadowed by it
RESERVED = frozenset(("wizard", "state", "list", "output", "prepare", "start", "stop", "speak", "lipsync", "interrupt", "stream", "visemes", "preview", "models", "phone"))
META = "avatar.json"
INDEX = "prepared/index.json"
PREPARED_HEIGHT = 480


class StoreError(ValueError):
    """The request names something the store cannot use; the message says what."""


def slug_of(display: str) -> str:
    """The folder name for a display name: ASCII letters and digits, words joined by `-`; "" when nothing is left."""
    plain = unicodedata.normalize("NFKD", str(display)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]+", "-", plain).strip("-")[:60].strip("-")


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name)) and name.lower() not in RESERVED


def valid_clip(clip) -> bool:
    return isinstance(clip, str) and bool(CLIP_RE.fullmatch(clip)) and ".." not in clip


class AvatarStore:
    def __init__(self, root) -> None:
        self.root = Path(root)

    # ---- paths
    def path(self, name) -> Path:
        """The folder of an avatar (it may not exist yet); StoreError for a name that is not plain."""
        if not valid_name(name):
            raise StoreError("an avatar name is letters, digits, _ and -, up to 60 characters, and not the name of a route")
        return self.root / name

    def existing(self, name) -> Path:
        folder = self.path(name)
        if not folder.is_dir() or folder.is_symlink():
            raise StoreError(f"there is no avatar called {name}")
        return folder

    def clip_path(self, name, clip) -> Path:
        if not valid_clip(clip):
            raise StoreError("a clip is a plain .mp4 file name")
        path = self.existing(name) / "clips" / clip
        if not path.is_file():
            raise StoreError(f"{name} has no clip {clip}")
        return path

    def prepared_dir(self, name, clip, height: int = PREPARED_HEIGHT) -> Path:
        """Where the worker keeps the prepared material of one clip (the height is part of what it was prepared for)."""
        stem = Path(clip).stem
        return self.path(name) / "prepared" / (f"{stem}__h{height}" if height else stem)

    # ---- reading
    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.is_symlink() and valid_name(p.name))

    def clips(self, name) -> list[str]:
        folder = self.existing(name) / "clips"
        return sorted(p.name for p in folder.glob("*.mp4") if p.is_file() and valid_clip(p.name)) if folder.is_dir() else []

    def metadata(self, name) -> dict:
        try:
            data = json.loads((self.existing(name) / META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def voice_of(self, name) -> dict:
        """{"engine", "voice"} the wizard suggested for this avatar, {} when there is none."""
        voice = self.metadata(name).get("voice")
        if not isinstance(voice, dict):
            return {}
        engine, who = voice.get("engine"), voice.get("voice")
        return {"engine": engine, "voice": who} if isinstance(engine, str) and isinstance(who, str) else {}

    def prepared(self, name) -> dict:
        try:
            data = json.loads((self.existing(name) / INDEX).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def size_bytes(self, name) -> int:
        total = 0
        for base, _dirs, files in os.walk(self.existing(name)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(base, f))
                except OSError:
                    continue
        return total

    def info(self, name) -> dict:
        folder = self.existing(name)
        meta = self.metadata(name)
        clips = self.clips(name)
        index = self.prepared(name)
        return {"name": name, "display_name": meta.get("display_name") if isinstance(meta.get("display_name"), str) else name,
                "created": meta.get("created") if isinstance(meta.get("created"), str) else "",
                "poster": (folder / "poster.jpg").is_file(), "clips": clips,
                "prepared": sorted(c for c in clips if c in index),
                "wav2lip": (folder / "wav2lip" / "face.png").is_file() and (folder / "wav2lip" / "box.json").is_file(),
                "visemes": (folder / "visemes" / "frame.json").is_file(),
                "voice": self.voice_of(name), "size_bytes": self.size_bytes(name)}

    def listing(self) -> dict:
        """{"avatars": [info...], "size_bytes": the total}."""
        rows = [self.info(n) for n in self.names()]
        return {"avatars": rows, "size_bytes": sum(r["size_bytes"] for r in rows)}

    # ---- writing
    def create(self, name, display_name: str = "") -> Path:
        folder = self.path(name)
        if folder.exists():
            raise StoreError(f"there is already an avatar called {name}")
        (folder / "clips").mkdir(parents=True)
        self.write_metadata(name, display_name=display_name or name,
                            created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return folder

    def unique_name(self, wanted: str) -> str:
        """`wanted` as a free folder name: the slug of it, with -2, -3... when taken; StoreError when nothing usable is left."""
        base = slug_of(wanted)
        if base.lower() in RESERVED:
            base += "-avatar"
        if not base:
            raise StoreError("give the avatar a name with at least one letter or digit")
        name, n = base, 1
        while (self.root / name).exists():
            n += 1
            name = f"{base[:56]}-{n}"
        return name

    def write_metadata(self, name, **fields) -> dict:
        meta = self.metadata(name) if (self.path(name) / META).is_file() else {}
        meta.update(fields)
        target = self.existing(name) / META
        tmp = target.with_name(META + ".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return meta

    def record_prepared(self, name, clip, frames: int, height: int = PREPARED_HEIGHT) -> None:
        index = self.prepared(name)
        index[clip] = {"frames": int(frames), "height": int(height), "prepared": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        target = self.existing(name) / INDEX
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
        os.replace(tmp, target)

    def forget_prepared(self, name, clip) -> None:
        index = self.prepared(name)
        if index.pop(clip, None) is not None:
            target = self.existing(name) / INDEX
            target.write_text(json.dumps(index, indent=2), encoding="utf-8")

    def delete(self, name) -> int:
        """Remove the avatar and everything in it; returns the bytes freed. The folder must be a real folder inside the root."""
        folder = self.existing(name)
        if folder.resolve().parent != self.root.resolve():
            raise StoreError("that folder is not inside the avatars folder")
        freed = self.size_bytes(name)
        shutil.rmtree(folder)
        return freed
