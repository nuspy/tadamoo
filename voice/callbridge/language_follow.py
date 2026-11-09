"""The spoken language of the avatar: follow the person's speech with hysteresis, or hold a language the person asked for.

`LanguageFollower` is pure and deterministic: it is told what the speech recognizer heard for each utterance (language, its probability,
how long it lasted, the words) and says whether the avatar should change language. Nothing here loads a model or touches audio.

Rules for a switch in follow mode: the candidate differs from the current language, is one the person enabled, has a probability of at least
the threshold, is not a filler (one or two words), and, for an utterance shorter than the short-utterance duration, appears in two
consecutive utterances. A locked language ignores every detection until `unlock()`."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

FOLLOW, LOCKED = "follow", "locked"
DEFAULT_ENABLED = ("it", "en")
DEFAULT_THRESHOLD = 0.80
DEFAULT_SHORT_S = 1.5
THRESHOLD_RANGE = (0.5, 0.99)
SHORT_S_RANGE = (0.5, 5.0)
MAX_ENABLED = 16
FILLER_MAX_WORDS = 2
PENDING_TTL_S = 20.0           # a short candidate is forgotten when the next utterance comes later than this

# Codes that the recognizer, the synthesis catalogue or the old tables spell another way.
_ALIASES = {"cmn": "zh", "zho": "zh", "chi": "zh", "ita": "it", "eng": "en", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de", "spa": "es",
            "por": "pt", "jpn": "ja", "hin": "hi", "hun": "hu", "rus": "ru", "iw": "he"}
_CODE = re.compile(r"^[a-z]{2,3}$")


def iso(code) -> str:
    """The ISO 639-1 code of a language tag ("en-US", "pt_BR", "cmn", "IT"); "" when it is not a language code."""
    if not isinstance(code, str):
        return ""
    base = re.split(r"[-_]", code.strip().lower(), maxsplit=1)[0]
    base = _ALIASES.get(base, base)
    return base if _CODE.match(base) else ""


def words_of(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


@dataclass(frozen=True)
class LanguageSettings:
    """What the person chose (`language` in voice.json), as the voice service reads it."""
    follow: bool = True
    enabled: tuple = DEFAULT_ENABLED
    threshold: float = DEFAULT_THRESHOLD
    short_s: float = DEFAULT_SHORT_S
    voices: dict = field(default_factory=dict)         # language -> {"engine": id or "", "voice": name}

    @staticmethod
    def parse(stored) -> "LanguageSettings":
        """From the stored object; a missing or bad value is the default."""
        stored = stored if isinstance(stored, dict) else {}
        listed = stored.get("enabled") if isinstance(stored.get("enabled"), list) else []
        enabled = tuple(dict.fromkeys(c for c in (iso(v) for v in listed) if c))[:MAX_ENABLED]
        threshold, short_s = stored.get("threshold"), stored.get("short_s")
        voices: dict = {}
        raw = stored.get("voices")
        for key, value in (raw.items() if isinstance(raw, dict) else ()):
            code = iso(key)
            if code and isinstance(value, dict) and isinstance(value.get("voice"), str) and value["voice"].strip():
                engine = value.get("engine")
                voices[code] = {"engine": engine.strip().lower() if isinstance(engine, str) else "", "voice": value["voice"].strip()}
        return LanguageSettings(
            stored.get("follow") is not False,
            enabled or DEFAULT_ENABLED,
            float(threshold) if isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and THRESHOLD_RANGE[0] <= threshold <= THRESHOLD_RANGE[1] else DEFAULT_THRESHOLD,
            float(short_s) if isinstance(short_s, (int, float)) and not isinstance(short_s, bool) and SHORT_S_RANGE[0] <= short_s <= SHORT_S_RANGE[1] else DEFAULT_SHORT_S,
            voices)


@dataclass(frozen=True)
class Switch:
    language: str
    previous: str
    reason: str            # detected | command | auto


def merge_detections(items) -> tuple[str, float, float]:
    """One (language, probability, seconds) from the detections of the parts of an utterance: the language with the most weighted speech wins."""
    weight: dict[str, float] = {}
    prob: dict[str, float] = {}
    total = 0.0
    for lang, p, seconds in items:
        code = iso(lang)
        if not code:
            continue
        w = max(float(seconds), 0.05)
        weight[code] = weight.get(code, 0.0) + w
        prob[code] = prob.get(code, 0.0) + w * min(max(float(p), 0.0), 1.0)
        total += max(float(seconds), 0.0)
    if not weight:
        return "", 0.0, 0.0
    best = max(weight, key=weight.get)
    return best, prob[best] / weight[best], total


class LanguageFollower:
    def __init__(self, current: str = "it", enabled=DEFAULT_ENABLED, *, follow: bool = True, threshold: float = DEFAULT_THRESHOLD,
                 short_s: float = DEFAULT_SHORT_S, clock=time.monotonic, pending_ttl_s: float = PENDING_TTL_S) -> None:
        self.current = iso(current) or "it"
        self.auto = bool(follow)
        self.threshold, self.short_s = threshold, short_s
        self._clock, self._ttl = clock, pending_ttl_s
        self._enabled: tuple = ()
        self.set_enabled(enabled)
        self._locked = False
        self._pending: tuple[str, float] | None = None       # a short candidate seen once: (language, when)

    # -- state
    @property
    def mode(self) -> str:
        return FOLLOW if self.auto and not self._locked else LOCKED

    @property
    def enabled(self) -> tuple:
        return self._enabled

    def set_enabled(self, enabled) -> None:
        codes = tuple(dict.fromkeys(c for c in (iso(v) for v in enabled or ()) if c))
        self._enabled = codes or DEFAULT_ENABLED
        self._pending = None

    def is_enabled(self, lang) -> bool:
        return iso(lang) in self._enabled

    # -- explicit control
    def lock(self, lang) -> Switch | None:
        """The person asked for a language: it holds until `unlock()`. None when it is the current one (the mode is locked all the same)."""
        code = iso(lang)
        self._locked, self._pending = True, None
        if not code or code == self.current:
            return None
        previous, self.current = self.current, code
        return Switch(code, previous, "command")

    def unlock(self) -> bool:
        """Back to automatic; True when the mode changed."""
        was, self._locked, self._pending = self._locked, False, None
        return was

    def set_current(self, lang) -> None:
        """Puts the current language back (the synthesis could not speak the one that was chosen); the mode is not touched."""
        code = iso(lang)
        if code:
            self.current, self._pending = code, None

    # -- detection
    def observe(self, lang, prob: float, duration_s: float, text: str | None = None) -> Switch | None:
        """One utterance heard. A Switch when the avatar should change language, else None."""
        if self.mode != FOLLOW:
            return None
        if text is not None and words_of(text) <= FILLER_MAX_WORDS:
            return None                                       # a filler says nothing about the language and does not break a run either
        code = iso(lang)
        now = self._clock()
        if self._pending and now - self._pending[1] > self._ttl:
            self._pending = None
        if not code or code == self.current or code not in self._enabled or not isinstance(prob, (int, float)) or prob < self.threshold:
            self._pending = None
            return None
        if duration_s < self.short_s:
            if self._pending is None or self._pending[0] != code:
                self._pending = (code, now)
                return None
        previous, self.current, self._pending = self.current, code, None
        return Switch(code, previous, "detected")
