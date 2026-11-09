"""The guided creation of an avatar: a draft collects the materials one step at a time, `finish` turns it into a folder of the avatars store.

Steps (the names callAPIcall's app uses), in the order the pages ask for them: `foto` (the portrait, required), `voce` (a voice sample to clone),
`video` (a short clip of the face talking: real mouth shapes and what MuseTalk animates on the PC, for an emergency), `pose` (up to six more
photos, which no mode uses yet). The photo is enough: it makes the avatar for the phone and the ESP32 with no GPU. Everything that arrives is checked before it is kept: an image must decode
and is kept re-encoded as JPEG, a video must be an MP4 that opens and has frames, a voice sample must be one of the audio types of the cloning
folder and short enough. The file name the client sent is never used and a draft id or an avatar name that is not plain is refused.

`finish` writes `<avatars>/<name>/`: `poster.jpg`, `wav2lip/` (the straightened face and the mouth box, with a default mouth position the person
can correct), `visemes/` (the mouth sprites made from the photo), `clips/` with the video, `poses/`, and `avatar.json`. It answers with the render
modes the files allow. The voice sample goes to the cloning folder of the TTS, `<models folder>/tts/
voices/<name>.<ext>`; the avatar's voice is that sample on a cloning engine only when such an engine is installed, else nothing is set and the
reason is said. The MuseTalk preparation of the clip is a separate job (`AvatarClient.prepare`)."""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
import uuid
import wave
from pathlib import Path

from callbridge import models_store
from callbridge.avatar_store import AvatarStore, StoreError, slug_of, valid_name

log = logging.getLogger("voice.avatar_wizard")

STEPS = ("foto", "voce", "video", "pose")
LIMITS = {"foto": 20 << 20, "pose": 20 << 20, "video": 500 << 20, "voce": 25 << 20}
MAX_POSES = 6
MAX_DRAFTS = 8
DRAFT_AGE_S = 24 * 3600
IMAGE_SIDE = (64, 8000)                     # smallest and largest side of a photo
VIDEO_SIDE = (128, 4096)
VIDEO_MAX_S = 120.0
SAMPLE_S = (3.0, 120.0)                     # a voice sample, when its length can be read (wav)
NAME_MAX = 40
DRAFT_RE = re.compile(r"[0-9a-f]{32}")
# a cloning engine, best first for a wizard that mostly serves Italian; the engine must cover the language and be installed
CLONING_ORDER = ("cosyvoice", "xtts", "qwen3tts", "chatterbox", "dia")
AUDIO_MAGIC = {"wav": lambda h: h[:4] == b"RIFF" and h[8:12] == b"WAVE", "mp3": lambda h: h[:3] == b"ID3" or (h[0] == 0xFF and h[1] & 0xE0 == 0xE0),
               "flac": lambda h: h[:4] == b"fLaC", "ogg": lambda h: h[:4] == b"OggS"}


class WizardError(Exception):
    """The step cannot be done; the message says what to change."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status, self.code = status, code


NEEDS_OPENCV = "needs opencv: install opencv-python-headless in the voice service's environment"


def read_image(data: bytes):
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise WizardError(NEEDS_OPENCV, 501, "needs_opencv") from None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise WizardError("that file is not a photo that can be read (use JPEG, PNG or WebP)")
    h, w = img.shape[:2]
    if min(h, w) < IMAGE_SIDE[0] or max(h, w) > IMAGE_SIDE[1]:
        raise WizardError(f"the photo is {w}x{h}: its sides must be between {IMAGE_SIDE[0]} and {IMAGE_SIDE[1]} pixels")
    return img


def _check_video(path: Path) -> dict:
    import cv2
    with path.open("rb") as fh:
        head = fh.read(12)
    if head[4:8] != b"ftyp":
        raise WizardError("the video must be an MP4 file")
    cap = cv2.VideoCapture(str(path))
    try:
        frames, fps = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0), float(cap.get(cv2.CAP_PROP_FPS) or 0)
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        ok, _frame = cap.read()
    finally:
        cap.release()
    if not ok or frames <= 0 or fps <= 0:
        raise WizardError("the video cannot be read: it has no frames")
    if min(w, h) < VIDEO_SIDE[0] or max(w, h) > VIDEO_SIDE[1]:
        raise WizardError(f"the video is {w}x{h}: its sides must be between {VIDEO_SIDE[0]} and {VIDEO_SIDE[1]} pixels")
    if frames / fps > VIDEO_MAX_S:
        raise WizardError(f"the video is {frames / fps:.0f} s long: at most {VIDEO_MAX_S:.0f} s")
    return {"frames": frames, "fps": round(fps, 2), "width": w, "height": h, "seconds": round(frames / fps, 1)}


def _audio_kind(data: bytes) -> str:
    head = data[:16].ljust(16, b"\0")
    for kind, test in AUDIO_MAGIC.items():
        if test(head):
            return kind
    raise WizardError("the voice sample must be a WAV, MP3, FLAC or OGG file")


def _wav_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, OSError):
        return None


def cloning_engine(cfg, language: str = "it", router=None) -> tuple[str, str]:
    """(engine id, "") of the first installed cloning engine that covers `language`, else ("", why not)."""
    from callbridge.engines import catalog
    reasons = []
    for engine in CLONING_ORDER:
        spec = catalog.spec(engine)
        if spec is None:
            continue
        if spec.langs and language not in spec.langs:
            reasons.append(f"{spec.name} does not speak {language}")
        elif catalog.disponibile(cfg, spec, router):
            return engine, ""
        else:
            reasons.append(catalog.modello_mancante(cfg, spec) or f"{spec.name} has no environment installed")
    return "", "no voice-cloning engine is installed (" + "; ".join(reasons[:3]) + ")"


class Wizard:
    def __init__(self, drafts_dir, store: AvatarStore, config) -> None:
        self.drafts, self.store, self.cfg = Path(drafts_dir), store, config

    # ---- drafts
    def _draft(self, draft_id) -> Path:
        if not isinstance(draft_id, str) or not DRAFT_RE.fullmatch(draft_id):
            raise WizardError("no such draft", 404, "not_found")
        folder = self.drafts / draft_id
        if not folder.is_dir():
            raise WizardError("no such draft", 404, "not_found")
        return folder

    def start(self) -> dict:
        self.drafts.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for old in self.drafts.iterdir():
            if old.is_dir() and now - old.stat().st_mtime > DRAFT_AGE_S:
                shutil.rmtree(old, ignore_errors=True)
        if sum(1 for p in self.drafts.iterdir() if p.is_dir()) >= MAX_DRAFTS:
            raise WizardError(f"{MAX_DRAFTS} drafts are open: finish or cancel one first", 409, "too_many")
        draft_id = uuid.uuid4().hex
        (self.drafts / draft_id).mkdir()
        return {"draft_id": draft_id, "steps": list(STEPS), "limits": LIMITS}

    def status(self, draft_id) -> dict:
        folder = self._draft(draft_id)
        return {"draft_id": draft_id, "foto": (folder / "foto.jpg").is_file(), "pose": len(list(folder.glob("pose-*.jpg"))),
                "video": (folder / "video.mp4").is_file(), "voce": any(folder.glob("voce.*")), **self._info(folder)}

    @staticmethod
    def _info(folder: Path) -> dict:
        import json
        try:
            return {"video_info": json.loads((folder / "video.json").read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            return {}

    def cancel(self, draft_id) -> None:
        shutil.rmtree(self._draft(draft_id), ignore_errors=True)

    # ---- one step
    def upload(self, draft_id, step, storage, declared_length: int | None = None) -> dict:
        """Keep the file of `storage` (a werkzeug FileStorage) as the material of `step`, after checking it."""
        folder = self._draft(draft_id)
        if step not in STEPS:
            raise WizardError(f"unknown step: {step}")
        limit = LIMITS[step]
        if declared_length is not None and declared_length > limit + (1 << 16):
            raise WizardError(f"the file is too big: at most {limit >> 20} MB", 413, "too_big")
        if step == "pose" and len(list(folder.glob("pose-*.jpg"))) >= MAX_POSES:
            raise WizardError(f"at most {MAX_POSES} poses", 409, "too_many")
        if step == "video":
            return self._video(folder, storage, limit)
        data = storage.stream.read(limit + 1)
        if len(data) > limit:
            raise WizardError(f"the file is too big: at most {limit >> 20} MB", 413, "too_big")
        if not data:
            raise WizardError("the file is empty")
        if step == "voce":
            return self._voice(folder, data)
        img = read_image(data)
        import cv2
        name = "foto.jpg" if step == "foto" else f"pose-{len(list(folder.glob('pose-*.jpg'))) + 1}.jpg"
        if not cv2.imwrite(str(folder / name), img, [cv2.IMWRITE_JPEG_QUALITY, 92]):
            raise WizardError("the photo could not be kept", 500, "write_failed")
        return {"ok": True, "file": name, "width": img.shape[1], "height": img.shape[0]}

    def _video(self, folder: Path, storage, limit: int) -> dict:
        import json
        tmp = folder / "video.part"
        size = 0
        with tmp.open("wb") as out:
            while True:
                chunk = storage.stream.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    break
                out.write(chunk)
        try:
            if size > limit:
                raise WizardError(f"the file is too big: at most {limit >> 20} MB", 413, "too_big")
            info = _check_video(tmp)
        except WizardError:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, folder / "video.mp4")
        (folder / "video.json").write_text(json.dumps(info), encoding="utf-8")
        return {"ok": True, "file": "video.mp4", **info}

    def _voice(self, folder: Path, data: bytes) -> dict:
        kind = _audio_kind(data)
        for old in folder.glob("voce.*"):
            old.unlink()
        path = folder / f"voce.{kind}"
        path.write_bytes(data)
        seconds = _wav_seconds(path) if kind == "wav" else None
        if kind == "wav" and seconds is None:
            path.unlink()
            raise WizardError("that WAV file cannot be read")
        if seconds is not None and not SAMPLE_S[0] <= seconds <= SAMPLE_S[1]:
            path.unlink()
            raise WizardError(f"the voice sample is {seconds:.1f} s: between {SAMPLE_S[0]:.0f} and {SAMPLE_S[1]:.0f} s")
        return {"ok": True, "file": path.name, "format": kind, **({"seconds": round(seconds, 1)} if seconds else {})}

    # ---- the end
    def finish(self, draft_id, display_name, language: str = "it", router=None) -> dict:
        folder = self._draft(draft_id)
        display = display_name.strip() if isinstance(display_name, str) else ""
        if not display or len(display) > NAME_MAX or any(ord(c) < 32 for c in display):
            raise WizardError(f"give the avatar a name of 1 to {NAME_MAX} characters")
        slug = slug_of(display)
        if not slug:
            raise WizardError("the name needs at least one letter or digit")
        if not valid_name(slug):
            raise WizardError(f"{display} cannot be a name here (it is the name of a route of the service): choose another")
        if not (folder / "foto.jpg").is_file():
            raise WizardError("the photo is needed")
        try:
            target = self.store.create(slug, display)
        except StoreError as e:
            raise WizardError(str(e), 409, "exists") from None
        try:
            result = self._build(folder, target, slug, language, router)
        except Exception:
            self.store.delete(slug)
            raise
        shutil.rmtree(folder, ignore_errors=True)
        log.info("avatar %s created", slug)
        return {"ok": True, "avatar": slug, "display_name": display, **result}

    def _build(self, folder: Path, target: Path, slug: str, language: str, router) -> dict:
        shutil.copyfile(folder / "foto.jpg", target / "poster.jpg")
        mouth = "default"
        try:
            from callbridge import avatar_prep
            avatar_prep.prepara_wav2lip(target / "poster.jpg", target)
        except ImportError:
            mouth = f"not made: {NEEDS_OPENCV}"
        except (ValueError, OSError) as e:
            mouth = f"not made: {e}"
        poses = sorted(folder.glob("pose-*.jpg"))
        if poses:
            (target / "poses").mkdir()
            for p in poses:
                shutil.copyfile(p, target / "poses" / p.name)
        clips = []
        if (folder / "video.mp4").is_file():
            shutil.copyfile(folder / "video.mp4", target / "clips" / f"{slug}.mp4")
            clips.append(f"{slug}.mp4")
        from callbridge import avatar_assets
        out = {"clips": clips, "poses": len(poses), "mouth": mouth, "render_modes": avatar_assets.render_modes(target), "voice": {}, "voice_note": ""}
        sample = next(iter(sorted(folder.glob("voce.*"))), None)
        if sample is not None:
            out.update(self._keep_voice(sample, slug, language, router))
            if out["voice"]:
                self.store.write_metadata(slug, voice=out["voice"], voice_sample=out["voice_sample"])
            else:
                self.store.write_metadata(slug, voice_sample=out.get("voice_sample", ""), voice_note=out["voice_note"])
        return out

    def _keep_voice(self, sample: Path, slug: str, language: str, router) -> dict:
        """Copy the sample to the cloning folder of the TTS and choose the engine that will speak with it."""
        from callbridge import handy_compat as hc
        folder = Path(models_store.voices_dir() or "")
        ext = sample.suffix.lstrip(".")
        if not str(folder) or str(folder) == "." or ext not in hc._CLONE_EXTS:
            return {"voice_note": "the voice sample was not kept: there is no models folder for the voices", "voice_sample": ""}
        name = slug if hc._CLONE_NAME.match(slug) else "voice"
        n = 1
        while any((folder / f"{name}.{e}").exists() for e in hc._CLONE_EXTS):
            n += 1
            name = f"{slug}-{n}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sample, folder / f"{name}.{ext}")
        engine, why = cloning_engine(self.cfg, language, router)
        if not engine:
            return {"voice": {}, "voice_sample": name, "voice_note": f"the voice sample is saved as {name} but not used yet: {why}. The personality keeps its Kokoro voice."}
        return {"voice": {"engine": engine, "voice": name}, "voice_sample": name, "voice_note": ""}
