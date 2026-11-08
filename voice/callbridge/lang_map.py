"""Mappa prefisso telefonico internazionale -> (lingua, voce) Kokoro.

Usata per le chiamate in arrivo: dal prefisso del chiamante si deduce la
lingua del Paese e si sceglie una voce coerente. Se il numero non ha prefisso
internazionale o non e' mappato, si ricade sui default dell'orchestrator.

Copre le lingue supportate da Kokoro v1.0. Per una copertura completa dei
prefissi servirebbe la libreria `phonenumbers`; qui basta una mappa curata.
"""

from __future__ import annotations

# Prefisso internazionale (senza '+') -> (lang Kokoro, voce di default).
_PREFIX_MAP: dict[str, tuple[str, str]] = {
    "39": ("it", "if_sara"),       # Italia
    "1": ("en-us", "af_heart"),    # USA / Canada
    "44": ("en-gb", "bf_emma"),    # Regno Unito
    "33": ("fr-fr", "ff_siwis"),   # Francia
    "34": ("es", "ef_dora"),       # Spagna
    "52": ("es", "ef_dora"),       # Messico
    "54": ("es", "ef_dora"),       # Argentina
    "55": ("pt-br", "pf_dora"),    # Brasile
    "351": ("pt-br", "pf_dora"),   # Portogallo (voce pt-br, Kokoro non ha pt-pt)
    "81": ("ja", "jf_alpha"),      # Giappone
    "86": ("cmn", "zf_xiaoxiao"),  # Cina
    "91": ("hi", "hf_alpha"),      # India
}

# Prefissi controllati dal piu' lungo al piu' corto (max 3 cifre).
_PREFIX_LENGTHS = (3, 2, 1)


def voice_lang_for_number(
    number: str, default_lang: str, default_voice: str
) -> tuple[str, str]:
    """Deduce (lingua, voce) dal prefisso internazionale di `number`.

    Ritorna i default se il numero non e' in forma internazionale (`+CC...`)
    o se il prefisso non e' mappato.
    """
    digits = (number or "").strip()
    if not digits.startswith("+"):
        # Senza prefisso internazionale non si puo' dedurre il Paese.
        return default_lang, default_voice
    digits = digits[1:].lstrip("0")
    for length in _PREFIX_LENGTHS:
        prefix = digits[:length]
        if prefix in _PREFIX_MAP:
            return _PREFIX_MAP[prefix]
    return default_lang, default_voice
