"""Which voice speaks a language: the order the avatar follows when its language changes.

The selected engine and voice stay whenever they can speak the new language. For a language L with the selected engine E:

1. E speaks L (the catalogue's languages of E):
   - an engine whose voices are speakers rendered in any of its languages (XTTS, Chatterbox, CosyVoice, Qwen3-TTS, Dia) keeps the voice: the same identity;
   - Kokoro: the voice the person mapped to L when it is a Kokoro voice, else the standard Kokoro voice of L;
   - Piper: the voice the person mapped to L when it is a Piper voice, else an installed Piper voice of L;
   - any other engine keeps its voice.
2. E cannot speak L, or has no voice for it: the reply is synthesised with Piper, as a routing decision for the turn: the voice the person mapped to
   L when it is a Piper voice, else an installed Piper voice of L. The selection is not rewritten, and the next language E speaks goes back to E.
3. Piper is not installed or has no voice for L: nothing changes; the caller keeps the previous language and tells the person once.

No voice command chooses an engine: only a language reaches this module. `choose_voice` is pure over a router that answers `motore_di_voce`,
`attivo`, `parla_lingua`, `engine_available`, `is_piper` and `piper_voices` (the TtsRouter does); a router of None means nothing is known and the
voice is kept. It only asks questions: it never activates, loads or downloads; the engine swap, when there is one, happens in the synthesis through
the router's own one-engine-in-memory path."""
from __future__ import annotations

from dataclasses import dataclass

from .language_follow import iso
from .voice_commands import LANG_TABLE, table_key

# Engines whose voices are speakers rendered in any language of the engine (catalog.EngineSpec.voices_from == "cloned").
IDENTITY_ENGINES = ("xtts", "chatterbox", "cosyvoice", "qwen3tts", "dia")

FALLBACK = "fallback"            # the selected engine cannot speak the language: Piper does
ENGINE_CANNOT = "engine_cannot"  # it cannot, and Piper has no voice for it either
VOICE_MISSING = "voice_missing"  # it can, but has no voice for the language installed, and Piper has none either


@dataclass(frozen=True)
class VoiceChoice:
    ok: bool               # a voice speaks the language (False: keep the previous language)
    voice: str
    engine: str            # the selected engine, or "piper" for the fallback
    tts_lang: str          # the `lang` argument of the synthesis
    reason: str            # identity | mapped | standard | piper | kept | fallback | engine_cannot | voice_missing


def tts_lang(code: str, engine: str) -> str:
    """The `lang` the synthesis of `engine` takes for a language: Kokoro's own spelling (fr-fr, en-us), the ISO code for the others."""
    key = table_key(code)
    if key and engine in ("", "kokoro"):
        return LANG_TABLE[key]["lang"]
    return code


def selected_engine(router, voice: str, engine: str = "") -> str:
    """The engine in use: the one named, else the one that owns `voice`, else the router's active one."""
    if (engine or "").strip():
        return engine.strip().lower()
    if router is None:
        return ""
    try:
        owner = router.motore_di_voce(voice) if voice else ""
        return (owner or router.attivo or "").strip().lower()
    except Exception:  # noqa: BLE001 - a router that cannot say
        return ""


def _can(router, engine: str, code: str) -> bool:
    if router is None or not engine:
        return True
    try:
        return bool(router.parla_lingua(engine, code))
    except Exception:  # noqa: BLE001 - a router that cannot say: let it try
        return True


def _owns(router, voice: str, engine: str) -> bool:
    try:
        return router.motore_di_voce(voice) == engine
    except Exception:  # noqa: BLE001
        return False


def _piper_ok(router) -> bool:
    try:
        return bool(router.engine_available("piper"))
    except Exception:  # noqa: BLE001
        return False


def _piper_voice_for(router, code: str, voice_map: dict | None) -> str:
    """The Piper voice for `code`: the one the person mapped when it is an installed Piper voice, else the first installed one of the language."""
    mapped = ((voice_map or {}).get(code) or "").strip()
    try:
        if mapped and router.is_piper(mapped):
            return mapped
        names = router.piper_voices()
    except Exception:  # noqa: BLE001
        return ""
    return next((n for n in sorted(names) if "_" in n and iso(n.split("_", 1)[0]) == code), "")


def choose_voice(code: str, *, voice: str, engine: str = "", gender: str | None = None, voice_map: dict | None = None, router=None) -> VoiceChoice:
    """The voice for language `code`, given the SELECTED voice and engine (not the one a previous fallback is using); see the module text."""
    code = iso(code)
    eng = selected_engine(router, voice, engine)
    lang = tts_lang(code, eng)
    if router is None:
        return VoiceChoice(True, voice, eng, lang, "kept")
    can = _can(router, eng, code)
    if can:
        if eng in IDENTITY_ENGINES:
            return VoiceChoice(True, voice, eng, lang, "identity")
        mapped = ((voice_map or {}).get(code) or "").strip()
        if mapped and _owns(router, mapped, eng):
            return VoiceChoice(True, mapped, eng, lang, "mapped")
        if eng == "kokoro":
            info = LANG_TABLE.get(table_key(code))
            standard = (info or {}).get("m" if gender == "m" else "f")
            if standard:
                return VoiceChoice(True, standard, eng, lang, "standard")
        elif eng == "piper":
            found = _piper_voice_for(router, code, voice_map)
            if found:
                return VoiceChoice(True, found, eng, lang, "piper")
        else:
            return VoiceChoice(True, voice, eng, lang, "kept")
    if eng != "piper" and _piper_ok(router):
        found = _piper_voice_for(router, code, voice_map)
        if found:
            return VoiceChoice(True, found, "piper", code, FALLBACK)
    return VoiceChoice(False, voice, eng, lang, VOICE_MISSING if can else ENGINE_CANNOT)
