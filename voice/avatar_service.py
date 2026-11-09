"""The avatar as a function of the voice service: the client of the worker, its outputs, the personality it follows and the speech it makes.

One object per service (`state.avatar`). `functions.AvatarBackend` loads and unloads through it, and `avatar_routes` serves its routes.
The worker is started only by the avatar function being loaded (Functions.apply); the camera runs only while the person has turned it on in
Settings > Voice and the avatar is loaded."""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from callbridge import avatar_output, models_store
from callbridge.avatar_client import AvatarClient
from callbridge.avatar_store import AvatarStore
from callbridge.enhance import capability as enhance_capability
from callbridge.enhance import settings as enhance_settings
from callbridge.tadamoo_api import TadamooApi

from voice import persona_link
from voice.avatar_wizard import Wizard

log = logging.getLogger("voice.avatar")
SPEECH_KEEP_S = 300.0


class Unavailable(RuntimeError):
    """The avatar cannot do this now; the message says why."""

    def __init__(self, message: str, status: int = 409, code: str = "not_loaded") -> None:
        super().__init__(message)
        self.status, self.code = status, code


class AvatarService:
    def __init__(self, state, app, api_factory=None) -> None:
        self.state, self.app = state, app
        self.config = state.config
        self._api_factory = api_factory
        self.enhance_gpu = enhance_capability.gpu                 # replaceable: (index) -> GpuInfo
        self._enhance_key: tuple | None = None
        self.preview: avatar_output.Preview | None = None
        self.camera: avatar_output.VirtualCamera | None = None
        self._lock = threading.Lock()

    # ---- the pieces
    @property
    def store(self) -> AvatarStore:
        return AvatarStore(self.config.avatars_dir or "")

    def client(self) -> AvatarClient:
        """The one client of this process, shared with the functions API (it keeps it in the app's config)."""
        with self._lock:
            client = self.app.config.get("CB_AVATAR")
            if client is None:
                client = self.app.config["CB_AVATAR"] = AvatarClient(self.config)
            if self.preview is None:
                self.preview = avatar_output.Preview(client.frames)
                self.camera = avatar_output.VirtualCamera(client.frames, fps=int(self.config.avatar_fps or 25))
            return client

    def wizard(self) -> Wizard:
        drafts = Path(self.config.avatars_dir).parent / "avatar-drafts" if self.config.avatars_dir else None
        if drafts is None:
            raise Unavailable("there is no user data folder for the avatars", 409, "no_folder")
        return Wizard(drafts, self.store, self.config)

    def loaded(self) -> bool:
        proc = getattr(self.app.config.get("CB_AVATAR"), "_proc", None)
        return bool(proc is not None and proc.poll() is None)

    # ---- the personality it follows
    def _api(self) -> TadamooApi:
        if self._api_factory is not None:
            return self._api_factory()
        return TadamooApi(self.config.tadamoo_base_url, self.config.tadamoo_token, timeout=5)

    def link(self) -> persona_link.Link:
        """The personality of Settings > Voice with its avatar and voice; the config follows it (the TTS router reads the voice from there)."""
        self.state.refresh()
        link = persona_link.resolve(self.state.settings.brain.persona, self._api(), self.store)
        persona_link.apply(self.config, link)
        return link

    # ---- the acceleration of the video
    def _enhance_report(self, settings: enhance_settings.EnhanceSettings) -> dict:
        index = self.config.avatar_gpu_index or 0
        return enhance_capability.report(self.enhance_gpu(index), dll_path=settings.rtx_video_path)

    def sync_enhance(self) -> None:
        """Apply `avatar.enhance` of voice.json to the client when it (or the GPU reading) changed. Cheap when nothing did."""
        settings = enhance_settings.parse(self.config.avatar_enhance)
        client = self.client()
        key = (settings, self.config.avatar_fps, self.config.avatar_output_height, self.config.avatar_gpu_index)
        if key == self._enhance_key:
            return
        client.configure_enhance(settings, self._enhance_report(settings) if settings.active else {})      # the GPU is read only when a stage is on
        self._enhance_key = key

    def enhance_state(self) -> dict:
        """What the Settings card shows: the capabilities with their reasons, the settings, the plan and the measured cost."""
        self.sync_enhance()
        settings = enhance_settings.parse(self.config.avatar_enhance)
        return {"capabilities": self._enhance_report(settings), "settings": settings.as_dict(), "status": self.client().enhancer.status(),
                "basic": "The basic providers are plain CPU resizing and blending: they are not neural."}

    # ---- the camera
    def sync_camera(self) -> None:
        """Start or stop the virtual camera to match the setting and whether the avatar is running."""
        if self.camera is None:
            if not self.config.avatar_virtual_camera:
                return
            self.client()
        want = bool(self.config.avatar_virtual_camera) and self.loaded()
        if want and not self.camera.running:
            if not self.camera.start():
                log.warning("camera: %s", self.camera.reason)
        elif not want and self.camera.running:
            self.camera.stop()

    def outputs(self) -> dict:
        self.client()
        return {"preview": {"viewers": self.preview.viewers, "max": avatar_output.MAX_VIEWERS},
                "camera": self.camera.status(bool(self.config.avatar_virtual_camera))}

    # ---- speaking
    def voice_for(self, link: persona_link.Link, avatar: str) -> tuple[str, str]:
        """(engine, voice) the avatar speaks with: the personality's voice, else the one the wizard made for this avatar, else the TTS default."""
        if link.voice.get("engine"):
            return link.voice["engine"], link.voice.get("voice", "")
        made = self.store.voice_of(avatar) if avatar else {}
        return made.get("engine", ""), made.get("voice", "")

    def synthesize(self, text: str, engine: str, voice: str, lang: str) -> tuple[str, int]:
        """Speak `text` with the TTS router into a WAV file; (path, milliseconds). Raises models_store.ModelNotInstalled and the engines' errors."""
        from callbridge import engines, handy_compat as hc
        cfg = self.config
        samples, rate = engines._get_tts_engine(cfg).synth(text, voice or cfg.default_voice, lang or cfg.default_lang, cfg.tts_speed,
                                                          hc._engine_id_valido(engine) if engine else "")
        folder = Path(cfg.avatars_dir).parent / "avatar-state" / "speech"
        folder.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - SPEECH_KEEP_S
        for old in folder.glob("speak-*.wav"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        fd, path = tempfile.mkstemp(dir=folder, prefix="speak-", suffix=".wav")
        with os.fdopen(fd, "wb") as fh:
            fh.write(hc._pcm_f32_to_wav_bytes(samples, rate))
        return path, int(1000 * len(samples) / rate)

    def speak(self, text: str, lang: str = "") -> dict:
        """Say `text` with the active avatar and the personality's voice. Unavailable when the avatar is not loaded with an avatar in it."""
        client = self.client()
        link = self.link()
        avatar = client._active_persona or link.avatar
        if not self.loaded() or not client.is_ready():
            raise Unavailable("the avatar is not loaded with an avatar: load the avatar function and choose an avatar for the personality")
        engine, voice = self.voice_for(link, avatar)
        path, ms = self.synthesize(text, engine, voice, lang or link.language)
        client.speak_wav(path)
        return {"status": "ok", "avatar": avatar, "duration_ms": ms, "engine": engine or "auto", "voice": voice or self.config.default_voice}

    # ---- the poster
    def poster(self, name: str) -> Path | None:
        """`poster.jpg` of an avatar; made from the first frame of its first clip when there is none."""
        folder = self.store.existing(name)
        dest = folder / "poster.jpg"
        if dest.is_file():
            return dest
        clips = self.store.clips(name)
        if not clips:
            return None
        import cv2
        cap = cv2.VideoCapture(str(folder / "clips" / clips[0]))
        try:
            ok, frame = cap.read()
        finally:
            cap.release()
        if ok and frame is not None and cv2.imwrite(str(dest), frame, [cv2.IMWRITE_JPEG_QUALITY, 88]):
            return dest
        return None


def voice_installed(engine: str) -> bool:
    """Whether the TTS engine's model is in the models folder (for the page's hints)."""
    from callbridge.engines import catalog
    spec = catalog.spec(engine)
    return bool(spec is not None and not models_store.reason(catalog.model_id(None, spec) if spec.model else ""))
