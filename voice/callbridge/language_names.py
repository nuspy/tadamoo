"""The languages the avatar can be asked to speak, by name, and the spoken commands that change or release the language.

`parse_language_command(text)` is a rule matcher over a short utterance: a verb that asks for a change in one of the supported languages and the
name of a language (in any supported language, or in its own), or a phrase that gives the choice back to the automatic mode. It is pure and
decides nothing about who said the words: the caller applies a command only when it came from the person (callbridge/language_session.py)."""
from __future__ import annotations

import re

# code -> (name in English, name in Italian, spoken confirmation, every spelling that names it: Italian, English, French, German, Spanish, Portuguese, native)
_TABLE: dict[str, tuple] = {
    "it": ("Italian", "italiano", "Va bene, parlo in italiano.",
           ("italiano", "italiana", "italian", "italien", "italienne", "italienisch")),
    "en": ("English", "inglese", "Okay, I'll speak English now.",
           ("inglese", "english", "americano", "american", "british", "britannico", "anglais", "englisch", "inglés", "ingles", "inglês", "ingles")),
    "fr": ("French", "francese", "D'accord, je parle français.",
           ("francese", "french", "français", "francais", "französisch", "franzoesisch", "francés", "frances", "francês")),
    "de": ("German", "tedesco", "In Ordnung, ich spreche Deutsch.",
           ("tedesco", "german", "deutsch", "allemand", "alemán", "aleman", "alemão", "alemao")),
    "es": ("Spanish", "spagnolo", "De acuerdo, hablo en español.",
           ("spagnolo", "spagnola", "spanish", "español", "espanol", "castellano", "espagnol", "spanisch", "espanhol")),
    "pt": ("Portuguese", "portoghese", "Certo, vou falar português.",
           ("portoghese", "portuguese", "português", "portugues", "portugais", "portugiesisch", "portugués", "brasiliano")),
    "ja": ("Japanese", "giapponese", "はい、日本語で話します。",
           ("giapponese", "japanese", "日本語", "japonais", "japanisch", "japonés", "japones", "japonês")),
    "zh": ("Chinese", "cinese", "好的，我用中文说话。",
           ("cinese", "chinese", "mandarino", "mandarin", "中文", "普通话", "chinois", "chinesisch", "chino", "chinês")),
    "hi": ("Hindi", "hindi", "ठीक है, मैं हिंदी में बात करूँगा।", ("hindi", "indiano", "हिन्दी", "हिंदी")),
    "hu": ("Hungarian", "ungherese", "Rendben, most magyarul beszélek.",
           ("ungherese", "hungarian", "magyar", "magyarul", "hongrois", "ungarisch", "húngaro", "hungaro")),
    "ru": ("Russian", "russo", "Хорошо, я говорю по-русски.", ("russo", "russian", "russe", "russisch", "ruso", "русский")),
    "nl": ("Dutch", "olandese", "Goed, ik spreek Nederlands.",
           ("olandese", "dutch", "néerlandais", "neerlandais", "niederländisch", "holländisch", "neerlandés", "holandés", "nederlands")),
    "pl": ("Polish", "polacco", "Dobrze, mówię po polsku.", ("polacco", "polish", "polonais", "polnisch", "polaco", "polski")),
    "tr": ("Turkish", "turco", "Tamam, Türkçe konuşuyorum.", ("turco", "turkish", "turc", "türkisch", "tuerkisch", "türkçe", "turkce")),
    "ko": ("Korean", "coreano", "알겠습니다, 한국어로 말할게요.", ("coreano", "korean", "coréen", "coreen", "koreanisch", "한국어", "한국말")),
    "ar": ("Arabic", "arabo", "حسنًا، سأتحدث بالعربية.", ("arabo", "arabic", "arabe", "arabisch", "árabe", "العربية")),
    "cs": ("Czech", "ceco", "Dobře, mluvím česky.", ("ceco", "czech", "tchèque", "tcheque", "tschechisch", "checo", "čeština")),
}
CODES = tuple(_TABLE)


def is_known(code: str) -> bool:
    return code in _TABLE


def label(code: str, ui: str = "en") -> str:
    """The language's name in English or in Italian ("fr" -> "French" / "francese"); the code itself when unknown."""
    row = _TABLE.get(code)
    return code if row is None else (row[1] if ui == "it" else row[0])


def confirmation(code: str) -> str:
    """The sentence the avatar says, in the new language, when it changes to it."""
    row = _TABLE.get(code)
    return row[2] if row else ""


def reply_name(code: str) -> str:
    """The language's English name, as the brain is told to reply in it."""
    return label(code, "en")


def _compile(name: str):
    if re.search(r"[A-Za-zÀ-ÿčěšřž]", name):
        return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
    return re.compile(re.escape(name))              # another script: written without a reliable word boundary


_PATTERNS = {code: [_compile(n) for n in row[3]] for code, row in _TABLE.items()}


def find_languages(text: str) -> list[str]:
    """The languages named in `text`, in the order they come, without repeats."""
    found: list[tuple[int, str]] = []
    for code, patterns in _PATTERNS.items():
        spots = [m.start() for p in patterns if (m := p.search(text or ""))]
        if spots:
            found.append((min(spots), code))
    return [code for _, code in sorted(found)]


# Imperative or infinitive forms that ask for a change, by language. First-person forms ("I speak French") are left out on purpose.
_VERBS = (
    "parla", "parli", "parlami", "parlare", "parlaci", "passa", "passare", "cambia", "cambiare", "metti", "imposta", "impostare", "usa", "usare",
    "rispondi", "rispondimi", "rispondere", "voglio", "vorrei",
    "speak", "talk", "switch", "change", "set", "use", "answer", "reply", "respond", "go",
    "parle", "parler", "parlez", "passe", "passer", "changer", "mets", "mettre", "utilise", "réponds", "reponds", "répondre", "repondre",
    "sprich", "sprechen", "sprecht", "wechsle", "wechseln", "wechsel", "rede", "reden", "antworte", "antworten", "stell", "stelle", "schalte",
    "schalt", "ändere", "aendere",
    "habla", "hablar", "háblame", "hablame", "cambiar", "pasa", "pasar", "responde", "contesta",
    "fala", "falar", "fale", "muda", "mudar", "troca", "trocar",
    "beszélj", "beszélni",
)
_VERB = re.compile(r"(?<!\w)(?:" + "|".join(sorted(map(re.escape, _VERBS), key=len, reverse=True)) + r")(?!\w)", re.IGNORECASE)
# "talk about German" and "parlami del tedesco" are not requests to change the language.
_ABOUT = re.compile(r"(?<!\w)(?:about|riguardo|propos|über|ueber|sobre|acerca|di|sul|sulla|sullo|del|dello|della|dell|du|des)(?!\w)|(?<!\w)de\s+l['’]|(?<!\w)de\s+la(?!\w)",
                    re.IGNORECASE)
# "I speak French at home": a subject before the verb makes it a statement.
_SUBJECT = re.compile(r"(?<!\w)(?:i|we|he|she|they|io|noi|lui|lei|loro|je|nous|il|elle|ils|ich|wir|er|yo|nosotros|eu)\s+(?:\w+\s+)?$", re.IGNORECASE)

_AUTO = re.compile(r"(?<!\w)autom[aá]t\w*", re.IGNORECASE)
_BACK = re.compile(r"(?<!\w)(?:back|return|again|torna\w*|ritorna\w*|riprendi|retour\w*|reviens|revenir|zurück|zurueck|wieder|volver|vuelve|voltar|volta)(?!\w)",
                   re.IGNORECASE)
_LANGWORD = re.compile(r"(?<!\w)(?:language|lingua|langue|sprache|idioma|língua|mode|modalità|modalita|modus|modo)(?!\w)", re.IGNORECASE)
_FOLLOW_ME = re.compile(r"(?:follow\s+my\s+language|segui\s+la\s+mia\s+lingua|suis\s+ma\s+langue|folge\s+meiner\s+sprache|sigue\s+mi\s+idioma|"
                        r"segue\s+a\s+minha\s+língua)", re.IGNORECASE)
MAX_WORDS = 9
# A language command names a language and nothing else: a phrase about an engine or a model is never one ("use the Italian engine").
_ENGINE = re.compile(r"(?<!\w)(?:engines?|motor[ie]?|moteur|modell?[oi]?|models?|mod[eè]le|piper|kokoro|xtts|chatterbox|cosyvoice|qwen\w*|orpheus|dia|whisper|"
                     r"synth\w*|sintesi|tts|stt)(?!\w)", re.IGNORECASE)
_GENDER_F = re.compile(r"\b(femmin[ae]|donna|female|woman)\b", re.IGNORECASE)
_GENDER_M = re.compile(r"\b(maschi[oale]|uomo|male|man)\b", re.IGNORECASE)


def parse_language_command(text: str) -> dict | None:
    """The language command of a short utterance, or None.

    {"kind": "lang", "lang": "fr", "gender": "f"|"m"|None}     a language is asked for (any known one: the caller checks that it is enabled)
    {"kind": "lang_auto", "bare": bool}                         back to automatic; `bare` is true when the whole utterance is only that word
    A long utterance is a conversation, not a command; two different languages in one utterance are ambiguous and give None."""
    t = (text or "").strip()
    n = len(re.findall(r"\w+", t))
    if not t or n > MAX_WORDS:
        return None
    if _FOLLOW_ME.search(t) or (_AUTO.search(t) and n <= 8 and (_BACK.search(t) or _LANGWORD.search(t))):
        return {"kind": "lang_auto", "bare": False}
    if _AUTO.fullmatch(re.sub(r"^(?:avatar|tadamoo)\W+", "", t, flags=re.IGNORECASE).strip(" .!?")):
        return {"kind": "lang_auto", "bare": True}
    verb = _VERB.search(t)
    if verb is None or _ENGINE.search(t) or _ABOUT.search(t) or _SUBJECT.search(t[:verb.start()] + " "):
        return None
    langs = find_languages(t)
    if len(langs) != 1:
        return None
    gender = "m" if _GENDER_M.search(t) else "f" if _GENDER_F.search(t) else None
    return {"kind": "lang", "lang": langs[0], "gender": gender}
