"""Which voice speaks a language: the order the avatar follows when its language changes.

1. An engine whose voices are speakers it renders in any of its languages (cloned or built-in speakers: XTTS, Chatterbox, CosyVoice, Qwen3-TTS, Dia) keeps
   the same voice and identity when it can speak the language.
2. The voice the person mapped to that language (`language.voices` in voice.json).
3. The standard voice of the language: a Kokoro voice, else a Piper voice installed for it.
4. Nothing speaks that language: the current voice is kept when its engine can speak it; otherwise the answer stays in the previous language
   and the caller says so once.

`choose_voice` is pure over a router that answers `motore_di_voce`, `attivo`, `parla_lingua`, `engine_available`, `is_piper` and `piper_voices` (the
TtsRouter does); a router of None means nothing is known and everything is allowed. Nothing is loaded or downloaded here."""
from __future__ import annotations

from dataclasses import dataclass

from .language_follow import iso
from .voice_commands import LANG_TABLE

# Engines whose voices are speakers rendered in any language of the engine (catalog.EngineSpec.voices_from == "cloned").
IDENTITY_ENGINES = ("xtts", "chatterbox", "cosyvoice", "qwen3tts", "dia")

_TABLE_KEY: dict[str, str] = {}
for _key in LANG_TABLE:
    _TABLE_KEY.setdefault(iso(_key), _key)


@dataclass(frozen=True)
class VoiceChoice:
    ok: bool               # a voice can speak the language (False: keep the current voice and the previous language)
    voice: str
    engine: str            # "" lets the router deduce it from the voice
    tts_lang: str          # the `lang` argument of the synthesis
    reason: str            # identity | mapped | standard | piper | kept | none


def tts_lang(code: str, engine: str) -> str:
    """The `lang` the synthesis of `engine` takes for a language: Kokoro's own spelling (fr-fr, en-us), the ISO code for the others."""
    key = _TABLE_KEY.get(code)
    if key and engine in ("", "kokoro"):
        return LANG_TABLE[key]["lang"]
    return code


def _can(router, engine: str, code: str) -> bool:
    if router is None or not engine:
        return True
    try:
        return bool(router.parla_lingua(engine, code))
    except Exception:  # noqa: BLE001 - a router that cannot say: let it try
        return True


def _standard(router, code: str, gender: str | None) -> tuple[str, str] | None:
    """(engine, voice) of the language's standard voice when it can be used now."""
    key = _TABLE_KEY.get(code)
    info = LANG_TABLE.get(key) if key else None
    if not info:
        return None
    g = "m" if gender == "m" else "f"
    if info.get(g):
        if router is None or _available(router, "kokoro"):
            return "kokoro", info[g]
        return None
    piper = info.get("p" + g)
    if piper and (router is None or _is_piper(router, piper)):
        return "piper", piper
    return None


def _available(router, engine: str) -> bool:
    try:
        return bool(router.engine_available(engine))
    except Exception:  # noqa: BLE001
        return True


def _is_piper(router, voice: str) -> bool:
    try:
        return bool(router.is_piper(voice))
    except Exception:  # noqa: BLE001
        return False


def _piper_voice_for(router, code: str) -> str:
    try:
        names = router.piper_voices()
    except Exception:  # noqa: BLE001
        return ""
    return next((n for n in sorted(names) if iso(n.split("_", 1)[0]) == code and "_" in n), "")


def choose_voice(code: str, *, voice: str, engine: str = "", gender: str | None = None, voice_map: dict | None = None, router=None) -> VoiceChoice:
    """The voice for language `code`, given the voice and engine in use."""
    code = iso(code)
    try:
        eng = (engine or (router.motore_di_voce(voice) if router is not None and voice else "") or (router.attivo if router is not None else "")).strip().lower()
    except Exception:  # noqa: BLE001
        eng = (engine or "").strip().lower()
    if eng in IDENTITY_ENGINES and voice and _can(router, eng, code):
        return VoiceChoice(True, voice, eng, tts_lang(code, eng), "identity")
    mapped = (voice_map or {}).get(code)
    if mapped and mapped.get("voice"):
        e = (mapped.get("engine") or (router.motore_di_voce(mapped["voice"]) if router is not None else "") or "").strip().lower()
        return VoiceChoice(True, mapped["voice"], e, tts_lang(code, e), "mapped")
    std = _standard(router, code, gender)
    if std:
        return VoiceChoice(True, std[1], std[0], tts_lang(code, std[0]), "standard")
    if router is not None:
        found = _piper_voice_for(router, code)
        if found:
            return VoiceChoice(True, found, "piper", code, "piper")
    if router is None or (eng and eng != "piper" and _can(router, eng, code) and router.lingue_di(eng)):
        return VoiceChoice(True, voice, eng, tts_lang(code, eng), "kept")
    return VoiceChoice(False, voice, eng, tts_lang(code, eng), "none")
