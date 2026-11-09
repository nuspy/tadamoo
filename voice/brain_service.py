"""What the voice service adds for the two brains: the push-to-talk manager made when it is first needed, the hotkeys on request, the state of
the brains and a spoken test sentence.

Routes (the control app already demands the bearer token on all of them):
    GET  /brain/state   the default mode, the personality, what waits for an approval, the last exchanges, the session
    POST /brain/say     {"text"}: speaks a test sentence with the TTS that is loaded (409 `tts_not_loaded` when it is not)

The push-to-talk manager imports `pynput`, so it is made on first use: a service on a PC without it still starts and says why a session
cannot. Nothing here opens a microphone or a speaker until a session or a test sentence is asked for."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger("voice.brain")

SAY_MAX_CHARS = 300


class Busy(Exception):
    """A test sentence is already being spoken."""


class Unavailable(Exception):
    """The push-to-talk manager cannot be made here; the message says why."""


class Session:
    """The push-to-talk manager as the session routes see it: it exists from the first session on, the hotkeys follow the setting."""

    def __init__(self, state, make_manager: Callable | None = None) -> None:
        self.state = state
        self._make = make_manager or self._default_make
        self._ptt = None
        self._lock = threading.Lock()
        self._hotkeys = False

    def _default_make(self):
        try:
            from callbridge.ptt import PushToTalkManager
        except Exception as e:                                       # noqa: BLE001 (pynput or a desktop that is missing)
            raise Unavailable(f"push-to-talk is not available on this PC ({e.__class__.__name__}: {e})") from None
        meetings = getattr(self.state, "meetings", None)
        return PushToTalkManager(self.state.config, self.state.brains, copilot=meetings.copilot if meetings is not None else None)

    def manager(self):
        with self._lock:
            if self._ptt is None:
                self._ptt = self._make()
            return self._ptt

    # ---- what api_session.py calls
    def start_session(self, mode: str, avatar: bool = False, brain: str = "") -> tuple[bool, str]:
        loaded = self.state.functions
        if loaded is not None:
            missing = [n for n in ("stt", "tts") if loaded._live(n) != "loaded"]
            if missing:
                return False, "not_loaded:" + ",".join(missing)
        try:
            return self.manager().start_session(mode, avatar=avatar, brain=brain)
        except Unavailable as e:
            return False, f"unavailable:{e}"

    def stop_session(self) -> bool:
        return self._ptt.stop_session() if self._ptt is not None else False

    def session_state(self) -> dict:
        if self._ptt is None:
            return {"active": False, "mode": None, "phase": "idle", "avatar": False, "brain": None}
        return self._ptt.session_state()

    def current_language(self) -> str:
        return self._ptt.current_language() if self._ptt is not None else ""

    def language_status(self) -> dict:
        """{language, mode, follow, enabled, engine_fallback, warnings} of the session running; {} when there is none."""
        return self._ptt.language_status() if self._ptt is not None else {}

    def set_language(self, code: str) -> bool:
        return self._ptt.set_language(code) if self._ptt is not None else False

    def sospendi(self) -> bool:
        return self._ptt.sospendi() if self._ptt is not None else False

    def riprendi(self) -> None:
        if self._ptt is not None and self._hotkeys:
            self._ptt.riprendi()

    def stop(self) -> None:
        if self._ptt is not None:
            self._ptt.stop()

    @property
    def brains(self):
        return self.state.brains

    # ---- the hotkeys follow `brain.hotkeys` of voice.json
    def sync_hotkeys(self, wanted: bool) -> None:
        if wanted == self._hotkeys:
            return
        self._hotkeys = wanted
        try:
            if wanted:
                self.manager().start()
            elif self._ptt is not None:
                self._ptt.sospendi()
        except Unavailable as e:
            log.warning("hotkeys: %s", e)
            self._hotkeys = False
        except Exception as e:                                       # noqa: BLE001 (a hook that cannot be installed)
            log.warning("hotkeys: %s", e)
            self._hotkeys = False

    @property
    def hotkeys_attive(self) -> bool:
        return bool(self._ptt is not None and self._ptt.hotkeys_attive)


class Speaker:
    """A test sentence: synthesised with the TTS router that is loaded and played on the output device. `synth` and `play` can be replaced."""

    def __init__(self, state, synth: Callable | None = None, play: Callable | None = None) -> None:
        self.state = state
        self._synth = synth
        self._play = play
        self._busy = threading.Lock()

    def _default_synth(self, text: str):
        from callbridge import engines
        cfg = self.state.config
        router = engines._get_tts_engine(cfg)
        return router.synth(text, cfg.default_voice, cfg.default_lang, cfg.tts_speed, "")

    def _default_play(self, samples, rate) -> None:
        import sounddevice as sd                                     # noqa: PLC0415 (opened only when a sentence is spoken)
        sd.play(samples, rate, device=self.state.config.ptt_output_device)
        sd.wait()

    def say(self, text: str) -> dict:
        if not self._busy.acquire(blocking=False):
            raise Busy()
        try:
            samples, rate = (self._synth or self._default_synth)(text)
            (self._play or self._default_play)(samples, rate)
            return {"status": "spoken", "duration_ms": int(1000 * len(samples) / rate)}
        finally:
            self._busy.release()


def register(app, state, speaker: Speaker | None = None) -> None:
    from flask import jsonify, request

    speaker = speaker or Speaker(state)

    @app.get("/brain/state")
    def brain_state():
        return jsonify({**state.brains.state(), "session": state.session.session_state(), "hotkeys": state.session.hotkeys_attive})

    @app.post("/brain/say")
    def brain_say():
        body = request.get_json(silent=True)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return jsonify({"error": "send {\"text\": \"a sentence\"}", "code": "bad_request"}), 400
        if len(text) > SAY_MAX_CHARS:
            return jsonify({"error": f"at most {SAY_MAX_CHARS} characters", "code": "bad_request"}), 400
        if state.functions is not None and state.functions._live("tts") != "loaded":
            return jsonify({"error": "the speech synthesis is not loaded: load it from Models > Voice services", "code": "tts_not_loaded"}), 409
        try:
            return jsonify(speaker.say(text.strip()))
        except Busy:
            return jsonify({"error": "a sentence is being spoken", "code": "busy"}), 409
        except Exception as e:                                       # noqa: BLE001 (a model that went away, no output device)
            log.warning("test sentence: %s", e)
            return jsonify({"error": f"{e.__class__.__name__}: {e}", "code": "speak_failed"}), 502
