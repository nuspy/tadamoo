"""Comandi vocali e cambio lingua per le sessioni push-to-talk.

Due funzionalita' usate da `StreamingSession` (solo nelle sessioni PTT, non
nelle telefonate):

  1. `parse_command(text)` — riconosce comandi impartiti a voce quando sono
     l'INTERO enunciato (per non scattare a meta' conversazione):
       - cambio lingua/voce:  "cambia lingua a inglese femmina", "parla in
         francese", "switch to english", ...
       - nuova conversazione: "nuova sessione", "inizia una nuova
         conversazione", "new session", ...
       - cambio di cervello: "switch to the agent", "switch to direct",
         "passa all'agente", "passa al diretto", ...
  2. `detect_language(text)` — euristica leggera per l'auto-rilevamento della
     lingua parlata dall'utente (per cambiare voce/lingua TTS in automatico).

`LANG_TABLE` mappa un codice lingua interno -> (lang Kokoro, voce F/M, nomi
riconosciuti, nome per l'istruzione all'agente, frasi di conferma). I codici
voce sono quelli di Kokoro v1.0.
"""

from __future__ import annotations

import re

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


def voice_for(code: str, gender: str | None) -> str:
    """Voce per (codice lingua, genere 'f'/'m'). Default femminile.

    Ogni lingua puo' avere voci Kokoro (`f`/`m`) e/o Piper (`pf`/`pm`): si usa
    la Kokoro se c'e', altrimenti la Piper (l'Ungherese, per esempio, esiste
    solo li'). Non c'e' piu' una preferenza configurabile fra i due: se un
    motore non ha una lingua, semplicemente quella lingua non gli si chiede
    (vedi `StreamingSession._motore_parla`)."""
    info = LANG_TABLE.get(code)
    if not info:
        return ""
    g = "m" if gender == "m" else "f"
    return info.get(g) or info.get("p" + g) or ""


# --- parsing comandi vocali ----------------------------------------------

# Verbi imperativi che introducono un cambio lingua o voce. Deliberatamente
# solo verbi (non "lingua"/"voce" da soli): serve comunque un nome di lingua
# noto (`_find_lang`), quindi i falsi positivi restano rari.
_LANG_TRIGGER = re.compile(
    r"\b("
    r"cambia(?:re)?|parl[ai](?:re)?|pass[ai](?:re)?|"
    r"mett[ii]|impost[ai]|us[ai]|vogli[o']|"
    r"switch|speak|talk|change|set"
    r")\b",
    re.IGNORECASE,
)

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

_GENDER_F = re.compile(r"\b(femmin[ae]|donna|female|woman)\b", re.IGNORECASE)
_GENDER_M = re.compile(r"\b(maschi[oale]|uomo|male|man)\b", re.IGNORECASE)


def _find_lang(text: str) -> str | None:
    """Primo codice lingua il cui nome compare nel testo."""
    low = text.lower()
    for code, info in LANG_TABLE.items():
        for name in info["names"]:
            if re.search(r"\b" + re.escape(name) + r"\b", low):
                return code
    return None


def _find_gender(text: str) -> str | None:
    if _GENDER_M.search(text):
        return "m"
    if _GENDER_F.search(text):
        return "f"
    return None


def parse_command(text: str) -> dict | None:
    """Riconosce un comando vocale se e' (in pratica) l'intero enunciato.

    Ritorna:
      {"kind": "lang", "lang": <codice>, "gender": "f"|"m"|None}
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

    # Cambio lingua/voce: serve un verbo-trigger E un nome di lingua noto.
    if _LANG_TRIGGER.search(t):
        code = _find_lang(t)
        if code:
            return {"kind": "lang", "lang": code, "gender": _find_gender(t)}
    return None


# --- rilevamento automatico della lingua ---------------------------------

# parole-spia per le lingue latine (soglia: >=2 parole per accettare).
_LATIN_HINTS = {
    "it": {"ciao", "come", "stai", "grazie", "bene", "sono", "una", "che", "non",
           "perché", "anche", "quando", "dove", "cosa", "questo", "buongiorno",
           "buonasera", "allora", "però", "ancora", "molto", "ho", "hai",
           "abbiamo", "voglio", "puoi", "devo", "siamo", "adesso", "vorrei"},
    "en": {"hello", "hi", "hey", "how", "are", "you", "the", "is", "what",
           "thank", "thanks", "please", "good", "morning", "yes", "no", "can",
           "want", "need", "know", "think", "that", "this", "with", "have",
           "don't", "do", "does", "will", "would", "should", "could"},
    "fr": {"bonjour", "salut", "merci", "oui", "non", "comment", "ça", "bien",
           "avec", "pour", "dans", "une", "que", "qui", "est", "pas", "vous",
           "je", "suis", "avoir", "faire", "veux", "peux"},
    "es": {"hola", "gracias", "sí", "cómo", "estás", "bien", "por", "qué",
           "donde", "cuando", "puedo", "quiero", "necesito", "buenos", "días",
           "noche", "tengo", "eres", "esto", "ahora"},
    "pt": {"olá", "obrigado", "obrigada", "como", "está", "bem", "sim", "não",
           "por", "que", "onde", "quando", "posso", "quero", "bom", "dia",
           "noite", "tenho", "você", "isso", "agora"},
    "hu": {"szia", "köszönöm", "igen", "nem", "napot", "kérem", "hogy", "van",
           "és", "egy", "nagyon", "vagyok", "beszélek", "magyarul", "reggelt",
           "estét", "viszlát", "kérlek", "szeretnék", "van", "vagy", "mit"},
}


def detect_language(text: str) -> str | None:
    """Codice lingua (chiave di LANG_TABLE) rilevato dal testo, o None.

    Euristica prudente: caratteri CJK/Devanagari per ja/cmn/hi, altrimenti
    conteggio di parole-spia (serve >=2 per accettare) per le lingue latine.
    """
    t = (text or "").lower().strip()
    if not t:
        return None

    cjk = len(re.findall(r"[一-鿿　-〿＀-￯]", t))
    kana = len(re.findall(r"[぀-ゟ゠-ヿ]", t))
    deva = len(re.findall(r"[ऀ-ॿ]", t))
    if deva >= 2:
        return "hi"
    if kana >= 2:
        return "ja"
    if cjk >= 2 and kana == 0:
        return "cmn"

    # Parole di sole lettere (unicode: include á é ő ű ecc. dell'ungherese).
    words = set(re.findall(r"\b[^\W\d_]+\b", t, re.UNICODE))
    if not words:
        return None
    scores = {code: len(words & hints) for code, hints in _LATIN_HINTS.items()}
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best if scores[best] >= 2 else None
