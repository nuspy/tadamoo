"""The language codes Kokoro (espeak) takes, from the codes the rest of the system uses.

The language settings, the catalogue and the HTTP routes speak ISO 639-1 ("en", "pt", "zh"); Kokoro's phonemizer wants its own spelling ("en-us",
"pt-br", "cmn"). This is the one place that maps them, used by every Kokoro entry point; it imports nothing so the helper process can load it."""
from __future__ import annotations

_KOKORO = {
    "it": "it", "en": "en-us", "en-us": "en-us", "en-gb": "en-gb", "fr": "fr-fr", "fr-fr": "fr-fr", "es": "es", "pt": "pt-br", "pt-br": "pt-br",
    "pt-pt": "pt-br", "ja": "ja", "zh": "cmn", "zh-cn": "cmn", "cmn": "cmn", "hi": "hi",
}


def kokoro_lang(lang: str) -> str:
    """Kokoro's code for `lang`; a code it has no mapping for is passed on as it came (Kokoro refuses it with its own error)."""
    key = (lang or "it").strip().lower().replace("_", "-")
    return _KOKORO.get(key) or _KOKORO.get(key.split("-")[0]) or key
