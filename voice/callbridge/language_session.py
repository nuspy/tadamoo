"""The language state of one voice session: what the avatar speaks, why, and what has to change when it changes.

A session holds a `LanguageFollower` (callbridge/language_follow.py) and the voice in use. Two things reach it:

- a detection (`on_detection`): what the recognizer heard in an utterance of the person, applied only in follow mode;
- a command (`on_command`, `request`): the person asked for a language, or for the automatic mode back.

Both are honoured only for the person's own input (`owner=True`: the person's microphone, their typed text or their own panel). Speech of a
meeting participant, the other end of a call, and text from a document, a page or a tool never change the language; the caller passes
`owner=False` for those and nothing happens.

A change returns an `Outcome`: the voice, engine and `lang` to configure the synthesis with, the one-line state note for the brain, and a
sentence to say (the confirmation, or why nothing changed). Nothing here loads a model, downloads anything or speaks."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import language_names as names
from .language_follow import FOLLOW, LOCKED, LanguageFollower, LanguageSettings, iso
from .language_voice import choose_voice

log = logging.getLogger("callbridge.language")

_SAY = {
    "no_voice": {"it": "Non c'è una voce installata per {lang}: resto in {current}. Puoi installarla con il pulsante Installa in Impostazioni > Voce.",
                 "en": "No voice is installed for {lang}, so I stay in {current}. You can install one with the Install button in Settings > Voice."},
    "not_enabled": {"it": "{lang} non è tra le lingue abilitate. Aggiungila in Impostazioni > Voce.",
                    "en": "{lang} is not among the enabled languages. Add it in Settings > Voice."},
    "auto": {"it": "Va bene, seguo la tua lingua.", "en": "Okay, I'll follow your language."},
    "auto_off": {"it": "La lingua automatica è spenta in Impostazioni > Voce.", "en": "Automatic language is switched off in Settings > Voice."},
}
WARNING_STT_ENGLISH_ONLY = "stt_english_only"
WARNING_STT_FIXED = "stt_language_fixed"


def state_note(code: str) -> str:
    """The line the brain gets when the language changes."""
    return f"[state] reply_language={code}"


@dataclass(frozen=True)
class Outcome:
    language: str
    mode: str
    changed: bool          # the language the avatar speaks changed: configure the synthesis, tell the brain
    voice: str = ""
    engine: str = ""
    tts_lang: str = ""
    say: str = ""          # a sentence for the person, to be spoken
    reason: str = ""       # detected | command | auto | refused
    warning: str = ""      # no_voice | not_enabled | auto_off

    @property
    def note(self) -> str:
        return state_note(self.language) if self.changed else ""

    @property
    def reply_name(self) -> str:
        return names.reply_name(self.language)


class LanguageSession:
    def __init__(self, settings: LanguageSettings, *, language: str, voice: str, engine: str = "", gender: str | None = None, router_fn=None,
                 stt_fixed: bool = False, clock=time.monotonic) -> None:
        self.settings = settings
        self.voice, self.engine, self.gender = voice, (engine or "").strip().lower(), gender
        self._router_fn = router_fn
        self.stt_fixed = bool(stt_fixed)
        enabled = tuple(dict.fromkeys((*settings.enabled, iso(language)))) if iso(language) else settings.enabled
        self.follower = LanguageFollower(language, enabled, follow=settings.follow and not stt_fixed, threshold=settings.threshold,
                                         short_s=settings.short_s, clock=clock)
        self._no_voice: set[str] = set()
        self._told: set[str] = set()
        self.tts_lang = choose_voice(self.language, voice=voice, engine=engine, gender=gender, voice_map=settings.voices, router=self._router()).tts_lang

    # -- state
    @property
    def language(self) -> str:
        return self.follower.current

    @property
    def mode(self) -> str:
        return self.follower.mode

    @property
    def detecting(self) -> bool:
        """Whether the recognizer should report the language of each utterance."""
        return self.mode == FOLLOW

    def stt_hint(self) -> str:
        """The language to hint the recognizer with: none (detect) in follow mode, the held language when locked; the person's own fixed
        recognizer language is not touched here."""
        return "" if self.mode == FOLLOW or self.stt_fixed else self.language

    def status(self, stt_model: str = "") -> dict:
        return {"language": self.language, "mode": self.mode, "follow": self.settings.follow, "enabled": list(self.follower.enabled),
                "warnings": self.warnings(stt_model)}

    def warnings(self, stt_model: str = "") -> list[str]:
        out = []
        if (stt_model or "").lower().endswith(".en") and any(c != "en" for c in self.follower.enabled):
            out.append(WARNING_STT_ENGLISH_ONLY)
        if self.stt_fixed and self.settings.follow:
            out.append(WARNING_STT_FIXED)
        return out

    def apply_settings(self, settings: LanguageSettings) -> None:
        """New settings from the person: the enabled languages, the thresholds and the voices; the language and the mode stay."""
        self.settings = settings
        enabled = tuple(dict.fromkeys((*settings.enabled, self.language)))
        self.follower.set_enabled(enabled)
        self.follower.auto = settings.follow and not self.stt_fixed
        self.follower.threshold, self.follower.short_s = settings.threshold, settings.short_s
        self._no_voice.clear()
        self._told.clear()

    # -- what reaches it
    def on_detection(self, text: str, info: dict | None, owner: bool) -> Outcome | None:
        """An utterance of the person was heard with `info` ({language, prob, duration_s}); an Outcome when the language changes."""
        if not owner or not info or not info.get("language"):
            return None
        previous = self.language
        sw = self.follower.observe(info["language"], float(info.get("prob") or 0.0), float(info.get("duration_s") or 0.0), text)
        if sw is None:
            return None
        if sw.language in self._no_voice:
            self.follower.set_current(previous)
            return None
        return self._apply(sw.language, previous, "detected")

    def on_command(self, cmd: dict | None, owner: bool) -> Outcome | None:
        """A parsed command (voice_commands.parse_command); None when it is not a language command, came from someone else, or is a bare
        automatic word said in a normal conversation."""
        if not owner or not isinstance(cmd, dict):
            return None
        kind = cmd.get("kind")
        if kind == "lang_auto":
            if cmd.get("bare") and self.mode == FOLLOW:
                return None
            return self._auto()
        if kind != "lang":
            return None
        return self._ask(iso(cmd.get("lang")), cmd.get("gender"))

    def request(self, code: str) -> Outcome:
        """The person's own panel or app asks for a language, or for "auto"."""
        code = (code or "").strip().lower()
        return self._auto() if code == "auto" else self._ask(iso(code), None)

    # -- internals
    def _router(self):
        try:
            return self._router_fn() if self._router_fn is not None else None
        except Exception as e:  # noqa: BLE001 - the synthesis backend is not ready: nothing is known
            log.debug("language: no router (%s)", e)
            return None

    def _ui(self) -> str:
        return "it" if self.language == "it" else "en"

    def _said(self, key: str, code: str = "") -> str:
        ui = self._ui()
        return _SAY[key][ui].format(lang=names.label(code, ui), current=names.label(self.language, ui))

    def _auto(self) -> Outcome:
        if not self.settings.follow or self.stt_fixed:
            return Outcome(self.language, self.mode, False, say=self._said("auto_off"), reason="refused", warning="auto_off")
        self.follower.unlock()
        return Outcome(self.language, self.mode, False, say=self._said("auto"), reason="auto")

    def _ask(self, code: str, gender: str | None) -> Outcome:
        if not names.is_known(code) or not self.follower.is_enabled(code):
            return Outcome(self.language, self.mode, False, say=self._said("not_enabled", code) if names.is_known(code) else "", reason="refused",
                           warning="not_enabled")
        was_locked, previous = self.mode == LOCKED, self.language
        if gender in ("f", "m"):
            self.gender = gender
        self.follower.lock(code)
        self._no_voice.discard(code)
        out = self._apply(code, previous, "command")
        if out.warning == "no_voice" and not was_locked:
            self.follower.unlock()
        return Outcome(out.language, self.mode, out.changed, out.voice, out.engine, out.tts_lang, out.say, out.reason, out.warning)

    def _apply(self, code: str, previous: str, reason: str) -> Outcome:
        choice = choose_voice(code, voice=self.voice, engine=self.engine, gender=self.gender, voice_map=self.settings.voices, router=self._router())
        if not choice.ok:
            self.follower.set_current(previous)
            self._no_voice.add(code)
            once = reason == "command" or code not in self._told
            self._told.add(code)
            log.info("language: no voice for %s, staying on %s", code, previous)
            return Outcome(previous, self.mode, False, self.voice, self.engine, self.tts_lang, self._said("no_voice", code) if once else "", reason, "no_voice")
        moved = (code != previous, (choice.voice, choice.engine, choice.tts_lang) != (self.voice, self.engine, self.tts_lang))
        self.voice, self.engine, self.tts_lang = choice.voice, choice.engine, choice.tts_lang
        if any(moved):
            log.info("language: %s -> %s (%s, voice %s)", previous, code, reason, choice.voice)
        say = names.confirmation(code) if reason == "command" else ""
        return Outcome(code, self.mode, any(moved), choice.voice, choice.engine, choice.tts_lang, say, reason)
