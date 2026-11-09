"""Comandi vocali e cambio lingua per le sessioni push-to-talk.

Due funzionalita' usate da `StreamingSession` (solo nelle sessioni PTT, non
nelle telefonate):

  1. `parse_command(text)` — riconosce comandi impartiti a voce quando sono
     l'INTERO enunciato (per non scattare a meta' conversazione):
       - cambio lingua (mai di motore o modello):  "cambia lingua a inglese
         femmina", "parla in francese", "sprich Deutsch", "torna automatico", ...
       - nuova conversazione: "nuova sessione", "inizia una nuova
         conversazione", "new session", ...
       - cambio di cervello: "switch to the agent", "switch to direct",
         "passa all'agente", "passa al diretto", ...

`LANG_TABLE` mappa un codice lingua interno -> (lang Kokoro, voce F/M, nomi
riconosciuti, nome per l'istruzione all'agente, frasi di conferma). I codici
voce sono quelli di Kokoro v1.0.
"""

from __future__ import annotations

import re

from .language_follow import iso
from .language_names import parse_language_command

# codice interno -> parametri lingua/voce. `lang` e' la stringa passata a Handy
# TTS (identica a quella usata dal phone server: en-us/fr-fr/pt-br, ecc.).
LANG_TABLE: dict[str, dict] = {
    "it": {
        "lang": "it", "f": "if_sara", "m": "im_nicola",
        "names": ["italiano", "italiana", "italian"],
        "reply": "italiano",
        "switch": "Va bene, parlo in italiano.",
        "new_session": "Ho iniziato una nuova conversazione.",
    },
    "en": {
        "lang": "en-us", "f": "af_heart", "m": "am_michael",
        "names": ["inglese", "english", "americano", "american"],
        "reply": "English",
        "switch": "Okay, I'll speak English now.",
        "new_session": "I've started a new conversation.",
    },
    "en-gb": {
        "lang": "en-gb", "f": "bf_emma", "m": "bm_george",
        "names": ["britannico", "british"],
        "reply": "British English",
        "switch": "Right, I'll speak British English now.",
        "new_session": "I've started a new conversation.",
    },
    "fr": {
        "lang": "fr-fr", "f": "ff_siwis", "m": "ff_siwis",
        "names": ["francese", "french", "français", "francais"],
        "reply": "français",
        "switch": "D'accord, je parle français.",
        "new_session": "J'ai commencé une nouvelle conversation.",
    },
    "es": {
        "lang": "es", "f": "ef_dora", "m": "em_alex",
        "names": ["spagnolo", "spagnola", "spanish", "español", "espanol", "castellano"],
        "reply": "español",
        "switch": "De acuerdo, hablo en español.",
        "new_session": "He iniciado una nueva conversación.",
    },
    "pt": {
        "lang": "pt-br", "f": "pf_dora", "m": "pm_alex",
        "names": ["portoghese", "portuguese", "português", "portugues", "brasiliano"],
        "reply": "português",
        "switch": "Certo, vou falar português.",
        "new_session": "Iniciei uma nova conversa.",
    },
    "ja": {
        "lang": "ja", "f": "jf_alpha", "m": "jm_kumo",
        "names": ["giapponese", "japanese"],
        "reply": "日本語",
        "switch": "はい、日本語で話します。",
        "new_session": "新しい会話を始めました。",
    },
    "cmn": {
        "lang": "cmn", "f": "zf_xiaoxiao", "m": "zm_yunxi",
        "names": ["cinese", "chinese", "mandarino", "mandarin"],
        "reply": "中文",
        "switch": "好的，我用中文说话。",
        "new_session": "我开始了一个新的对话。",
    },
    "hi": {
        "lang": "hi", "f": "hf_alpha", "m": "hm_omega",
        "names": ["hindi", "indiano"],
        "reply": "हिन्दी",
        "switch": "ठीक है, मैं हिंदी में बात करूँगा।",
        "new_session": "मैंने एक नई बातचीत शुरू की है।",
    },
    # Lingue coperte da PIPER (assenti da Kokoro): voci `pf`/`pm` (nome file in
    # piper_voices/). Il router instrada a Piper quando la voce e' un file Piper.
    "hu": {
        "lang": "hu", "pf": "hu_HU-anna-medium", "pm": "hu_HU-imre-medium",
        "names": ["ungherese", "hungarian", "magyar", "magyarul"],
        "reply": "magyar (Hungarian)",
        "switch": "Rendben, most magyarul beszélek.",
        "new_session": "Új beszélgetést kezdtem.",
    },
}

# Kokoro lang string (quella passata a Handy) -> codice interno di LANG_TABLE.
_KOKORO_TO_CODE = {v["lang"]: k for k, v in LANG_TABLE.items()}


def code_for_kokoro_lang(lang: str) -> str:
    """Codice interno (chiave di LANG_TABLE) per una lang Kokoro. Default 'it'."""
    return _KOKORO_TO_CODE.get((lang or "").strip(), "it")


# Codice ISO 639-1 -> chiave di LANG_TABLE ("zh" -> "cmn"; "en" -> "en", non "en-gb").
_ISO_TO_KEY: dict[str, str] = {}
for _k in LANG_TABLE:
    _ISO_TO_KEY.setdefault(iso(_k), _k)


def table_key(code: str) -> str:
    """Chiave di LANG_TABLE per un codice lingua ("" se la lingua non e' in tabella)."""
    return _ISO_TO_KEY.get(iso(code), "")


# --- parsing comandi vocali ----------------------------------------------

# "nuova sessione / conversazione", "new session", "ricomincia", "azzera", ...
_NEW_SESSION = re.compile(
    r"\b("
    r"nuov[ao]\s+(?:sessione|conversazione|chat)|"
    r"inizia(?:re)?\s+(?:una\s+)?nuov[ao]\s+(?:sessione|conversazione)|"
    r"ricomincia(?:re)?(?:\s+da\s+capo)?|"
    r"azzera(?:re)?\s+(?:la\s+)?conversazione|"
    r"dimentica\s+tutto|"
    r"new\s+(?:session|conversation|chat)|start\s+(?:over|a\s+new\s+(?:session|conversation))|"
    r"reset(?:\s+(?:the\s+)?conversation)?"
    r")\b",
    re.IGNORECASE,
)

# Brain switch: a verb that moves, and the name of a brain. The harness is the TaDaMoo agent; direct is the plain conversation with the model.
_BRAIN_VERB = r"(?:switch|go|change|move|use|talk|speak|back|pass[ai](?:re)?|vai|usa|us[ai]|torna|parl[ai](?:re)?|cambia(?:re)?)"
_BRAIN_HARNESS = re.compile(r"\b" + _BRAIN_VERB + r"\b.*\b(?:agent|harness|agente)\b", re.IGNORECASE)
_BRAIN_DIRECT = re.compile(r"\b" + _BRAIN_VERB + r"\b.*\b(?:direct|direttamente|diretto|diretta)\b", re.IGNORECASE)


def parse_command(text: str) -> dict | None:
    """Riconosce un comando vocale se e' (in pratica) l'intero enunciato.

    Ritorna:
      {"kind": "lang", "lang": <codice ISO 639-1>, "gender": "f"|"m"|None}
      {"kind": "lang_auto", "bare": bool}   (torna alla lingua automatica)
      {"kind": "new_session"}
      {"kind": "brain", "mode": "harness"|"direct"}
      None  -> non e' un comando (passa al cervello)

    I comandi sono brevi: un enunciato lungo e' conversazione, non un comando
    (evita falsi positivi tipo "ti spiego come cambiare lingua sul telefono").
    """
    t = (text or "").strip()
    if not t:
        return None
    n_words = len(re.findall(r"\w+", t))
    if n_words > 9:
        return None

    # Cambio di cervello: comando corto, verbo di spostamento + nome del cervello.
    if _BRAIN_HARNESS.search(t):
        return {"kind": "brain", "mode": "harness"}
    if _BRAIN_DIRECT.search(t):
        return {"kind": "brain", "mode": "direct"}

    # Nuova conversazione: comando corto e specifico.
    if _NEW_SESSION.search(t) and n_words <= 7:
        return {"kind": "new_session"}

    # Cambio lingua/voce, o ritorno alla lingua automatica (callbridge/language_names.py).
    return parse_language_command(t)
