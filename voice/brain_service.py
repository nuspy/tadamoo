"""What the voice service adds for the two brains: the push-to-talk manager made when it is first needed, the hotkeys on request, the state of
the brains and a spoken test sentence.

Routes (the control app already demands the bearer token on all of them):
    GET  /brain/state         the default mode, the personality, what waits for an approval, the last exchanges, the session, the voice match
    POST /brain/voice/confirm {"contact_id"}: enrolls the current session's recent audio under that contact
    POST /brain/say           {"text"}: speaks a test sentence with the TTS that is loaded (409 `tts_not_loaded` when it is not). With `lang` and no `voice` the voice is the one the
                              avatar would use for that language (409 `language_unavailable` when none speaks it); with `voice` (and `engine`, `lang`) that exact voice

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


class NoVoice(Exception):
    """No voice can speak the language asked for (the selected engine cannot, and Piper has no voice for it either)."""


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
        # `self.state.desktop` is None until `serve()` calls `desktop_features.start()`, well before any session can
        # actually start (`.manager()` only runs on the first real attempt) - same late read as `_avatar` below, for
        # the same reason: without it, a session never got the state icons (listening/reasoning/speaking) at all.
        desktop = getattr(self.state, "desktop", None)
        return PushToTalkManager(self.state.config, self.state.brains, copilot=meetings.copilot if meetings is not None else None,
                                 overlay=desktop.overlay if desktop is not None else None, avatar_getter=self._avatar)

    def _avatar(self):
        """The shared avatar client while the avatar function is on in voice.json, else None; asked for when a hotkey is pressed or a session starts."""
        service = getattr(self.state, "avatar", None)
        return service.client() if service is not None and getattr(self.state.config, "avatar_enabled", False) else None

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

    def voice_status(self) -> dict:
        if self._ptt is None:
            return {"active": False}
        return self._ptt.voice_status()

    def voice_confirm(self, contact_id: str) -> str:
        if self._ptt is None:
            return "no_session"
        return self._ptt.voice_confirm(contact_id)

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

    def _default_synth(self, text: str, preview: dict | None = None):
        from callbridge import engines
        cfg = self.state.config
        router = engines._get_tts_engine(cfg)
        if preview and preview.get("voice"):
            # A catalog voice asked by name (Settings > Voice > Lingua parlata's play button): that exact
            # voice/engine/language, never the persona's - the point is to hear a choice before picking it.
            return router.synth(text, preview["voice"], preview.get("lang") or cfg.default_lang, cfg.tts_speed, preview.get("engine", ""))
        from callbridge.tadamoo_api import TadamooApi
        from voice import persona_link
        persona = self.state.settings.brain.persona
        api = TadamooApi(cfg.tadamoo_base_url, cfg.tadamoo_token, timeout=5)
        # The personality's own voice, same as the avatar speaks with: cfg.default_voice is a generic, often
        # language-locked voice (e.g. Kokoro/espeak) that garbles a sentence in any language it was not set up for.
        voice = persona_link.voice_of(persona, api) if persona else {}
        if preview and preview.get("lang"):
            # A language asked for and no voice: the voice the avatar would use when it changes to that language (language_voice.py), never an engine chosen by the caller.
            from callbridge.language_follow import LanguageSettings
            from callbridge.language_voice import choose_voice
            choice = choose_voice(preview["lang"], voice=voice.get("voice") or cfg.default_voice, engine=voice.get("engine", ""),
                                  voice_map=LanguageSettings.from_config(cfg).voices, router=router)
            if not choice.ok:
                raise NoVoice(f"no voice speaks '{preview['lang']}': the selected engine cannot and no Piper voice of that language is installed")
            return router.synth(text, choice.voice, choice.tts_lang, cfg.tts_speed, choice.engine)
        lang = (persona and persona_link.language_of(persona, api)) or cfg.default_lang
        return router.synth(text, voice.get("voice") or cfg.default_voice, lang, cfg.tts_speed, voice.get("engine", ""))

    def _default_play(self, samples, rate) -> None:
        import sounddevice as sd                                     # noqa: PLC0415 (opened only when a sentence is spoken)
        sd.play(samples, rate, device=self.state.config.ptt_output_device)
        sd.wait()

    def say(self, text: str, preview: dict | None = None) -> dict:
        if not self._busy.acquire(blocking=False):
            raise Busy()
        try:
            synth = self._synth or self._default_synth
            samples, rate = synth(text, preview) if preview else synth(text)
            (self._play or self._default_play)(samples, rate)
            return {"status": "spoken", "duration_ms": int(1000 * len(samples) / rate)}
        finally:
            self._busy.release()


def register(app, state, speaker: Speaker | None = None) -> None:
    from flask import jsonify, request

    speaker = speaker or Speaker(state)

    @app.get("/brain/state")
    def brain_state():
        return jsonify({**state.brains.state(), "session": state.session.session_state(), "hotkeys": state.session.hotkeys_attive,
                        "voice": state.session.voice_status()})

    @app.post("/brain/voice/confirm")
    def brain_voice_confirm():
        body = request.get_json(silent=True) or {}
        contact_id = str(body.get("contact_id") or "")
        if not contact_id:
            return jsonify({"error": "contact_id"}), 400
        problem = state.session.voice_confirm(contact_id)
        if problem == "no_session":
            return jsonify({"error": "no session is active"}), 409
        if problem == "unavailable":
            return jsonify({"error": "voice identification is not available"}), 503
        if problem == "not_enough_audio":
            return jsonify({"error": "not enough recent audio yet - let the person speak a little more first"}), 409
        if problem:
            return jsonify({"error": problem}), 400
        return jsonify({"status": "enrolled"})

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
        # A preview of one catalog voice (Settings > Voice > Lingua parlata's play button), never the persona's.
        voice = body.get("voice") if isinstance(body, dict) else None
        engine, lang = body.get("engine"), body.get("lang")
        preview = None
        if isinstance(voice, str) and voice.strip():
            preview = {"voice": voice.strip(), "engine": engine.strip() if isinstance(engine, str) else "",
                       "lang": lang.strip() if isinstance(lang, str) else ""}
        elif isinstance(lang, str) and lang.strip():                   # a language and no voice: the voice that language gets in the avatar's order
            preview = {"voice": "", "engine": "", "lang": lang.strip()}
        try:
            return jsonify(speaker.say(text.strip(), preview))
        except Busy:
            return jsonify({"error": "a sentence is being spoken", "code": "busy"}), 409
        except NoVoice as e:
            return jsonify({"error": str(e), "code": "language_unavailable"}), 409
        except Exception as e:                                       # noqa: BLE001 (a model that went away, no output device)
            log.warning("test sentence: %s", e)
            return jsonify({"error": f"{e.__class__.__name__}: {e}", "code": "speak_failed"}), 502
