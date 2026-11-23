"""Grammar and language check of a text in the Chat: a model made for one language, chosen in a list language -> model, corrects the text part by part.

The list is `language_check.json` in the user data folder:

    {"auto_offer": true, "languages": {"hu": {"provider": "<id of a provider in Providers>", "model": ""}}}

The model is a provider of the Providers page that runs on this PC or its network (an Ollama or llama.cpp server with a model for that language), never a
cloud one: the text of a conversation goes to it. Code, maths, links, file paths and the other content that must not change are put away before the call
(serve/longform.py `protect`) and put back after it, so the model never sees them; a part whose markers did not come back as they went in is kept as written.

The harness, not a model, checks each answer: not empty, not repeating itself, about as long as the part, every marker back. A part that fails is asked once more
with what was wrong, then kept as it was.

When a request is a translation into a language that has a model in the list, the Chat offers the check on the answer (`offer`)."""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from serve import longform
from tadamoo import digest

log = logging.getLogger("tadamoo.langcheck")

FILE = "language_check.json"
PART_CHARS = 1800                  # what one call corrects: a model made for a language is usually a small one
MAX_TEXT_CHARS = 400_000
LENGTH_BAND = (0.5, 1.6)           # a corrected part is about as long as the part
ATTEMPTS = 2
TIMEOUT_S = 600

# language code -> the names a request may call it by (lower case)
NAMES: Dict[str, Tuple[str, ...]] = {
    "en": ("english", "inglese", "anglais", "englisch", "inglés", "angol"),
    "it": ("italian", "italiano", "italien", "italienisch", "olasz"),
    "hu": ("hungarian", "ungherese", "magyar", "hongrois", "ungarisch", "húngaro", "ungherese"),
    "de": ("german", "tedesco", "deutsch", "allemand", "alemán", "német"),
    "fr": ("french", "francese", "français", "francais", "französisch", "francés", "francia"),
    "es": ("spanish", "spagnolo", "español", "espagnol", "spanisch", "spanyol"),
    "pt": ("portuguese", "portoghese", "português", "portugais", "portugiesisch"),
    "pl": ("polish", "polacco", "polski", "polonais", "polnisch", "lengyel"),
    "ro": ("romanian", "rumeno", "romeno", "română", "roumain", "rumänisch", "román"),
    "cs": ("czech", "ceco", "čeština", "česky", "tchèque", "tschechisch", "cseh"),
    "sk": ("slovak", "slovacco", "slovenčina", "slovaque", "slowakisch", "szlovák"),
    "sl": ("slovenian", "sloveno", "slovenščina", "slovène", "szlovén"),
    "hr": ("croatian", "croato", "hrvatski", "croate", "horvát"),
    "sr": ("serbian", "serbo", "srpski", "serbe", "szerb"),
    "bg": ("bulgarian", "bulgaro", "български", "bulgare", "bolgár"),
    "el": ("greek", "greco", "ελληνικά", "grec", "görög"),
    "nl": ("dutch", "olandese", "nederlands", "néerlandais", "holländisch", "holland"),
    "sv": ("swedish", "svedese", "svenska", "suédois", "svéd"),
    "da": ("danish", "danese", "dansk", "danois", "dán"),
    "fi": ("finnish", "finlandese", "suomi", "finnois", "finn"),
    "ru": ("russian", "russo", "русский", "russe", "russisch", "orosz"),
    "uk": ("ukrainian", "ucraino", "українська", "ukrainien", "ukrán"),
    "tr": ("turkish", "turco", "türkçe", "turc", "türkisch", "török"),
}
DISPLAY = {"en": "English", "it": "Italian", "hu": "Hungarian", "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese", "pl": "Polish", "ro": "Romanian",
           "cs": "Czech", "sk": "Slovak", "sl": "Slovenian", "hr": "Croatian", "sr": "Serbian", "bg": "Bulgarian", "el": "Greek", "nl": "Dutch", "sv": "Swedish",
           "da": "Danish", "fi": "Finnish", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish"}
_BY_NAME = {n: code for code, names in NAMES.items() for n in names}
TARGET_RE = re.compile(r"\b(?:into|in|to|a|en|al|ins|auf|para|vers|em|nach|na|v|på|på)\s+(" + "|".join(sorted(map(re.escape, _BY_NAME), key=len, reverse=True)) + r")\b", re.I)
TRANSLATE_RE = re.compile(r"\b(?:translate|translation|traduci|traduzione|traducir|traduire|traduzca|übersetze|übersetzen|fordítsd|fordítás|przetłumacz|traduceți)\b", re.I)

_lock = threading.Lock()


class LangCheckError(ValueError):
    """A request or a setting the check cannot take; the message says what to change."""


def known(code: Any) -> bool:
    return isinstance(code, str) and code in NAMES


def language_of(name: Any) -> str:
    """A language as its code, from a code or any of its names; '' when unknown."""
    if not isinstance(name, str):
        return ""
    key = name.strip().lower()
    return key if key in NAMES else _BY_NAME.get(key, "")


def supported() -> List[Dict[str, str]]:
    return [{"code": c, "name": DISPLAY[c]} for c in NAMES]


# -- the list: language -> model ------------------------------------------------------------------------------------

def _path() -> Path:
    from serve.userdata import user_data_dir
    return user_data_dir() / FILE


def read() -> Dict[str, Any]:
    """The list as saved, with every entry that does not name a known language dropped."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    langs = data.get("languages") if isinstance(data, dict) else None
    out: Dict[str, Dict[str, str]] = {}
    for code, row in (langs.items() if isinstance(langs, dict) else []):
        if known(code) and isinstance(row, dict) and isinstance(row.get("provider"), str) and row["provider"]:
            out[code] = {"provider": row["provider"], "model": row["model"] if isinstance(row.get("model"), str) else ""}
    return {"auto_offer": not (isinstance(data, dict) and data.get("auto_offer") is False), "languages": out}


def _write(value: Dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def set_language(code: Any, provider: Any, model: Any = "", providers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Puts `code` -> `provider` (and the model name, when the provider serves several) in the list. `providers` are the ones that may be chosen."""
    if not known(code):
        raise LangCheckError("language: one of " + ", ".join(NAMES))
    if not isinstance(provider, str) or not provider:
        raise LangCheckError("provider: the id of a provider")
    if not isinstance(model, str) or len(model) > 200:
        raise LangCheckError("model: a model name")
    if providers is not None and provider not in {p["id"] for p in providers}:
        raise LangCheckError("provider: choose one that runs on this PC or its network")
    with _lock:
        value = read()
        value["languages"][code] = {"provider": provider, "model": model.strip()}
        _write(value)
    return value


def remove_language(code: Any) -> Dict[str, Any]:
    with _lock:
        value = read()
        value["languages"].pop(code if isinstance(code, str) else "", None)
        _write(value)
    return value


def set_auto_offer(on: Any) -> Dict[str, Any]:
    if not isinstance(on, bool):
        raise LangCheckError("auto_offer: true or false")
    with _lock:
        value = read()
        value["auto_offer"] = on
        _write(value)
    return value


# -- when to offer the check ------------------------------------------------------------------------------------------

def translation_target(request: str) -> str:
    """The language a request translates into, as a code, or '' when the request is not a translation or names no language. The Long answer line of the
    Chat (`[long answer: mode=translate; language=hu]`) counts as naming it."""
    text = request or ""
    hint = longform.parse_hint(text)
    if hint and hint.get("mode") == "translate":
        code = language_of((hint.get("fields") or {}).get("language", ""))
        if code:
            return code
    if not TRANSLATE_RE.search(text):
        return ""
    m = TARGET_RE.search(text[:600])
    if m:
        return _BY_NAME.get(m.group(1).lower(), "")
    return ""


def offer(request: str, config: Optional[Dict[str, Any]] = None) -> str:
    """The language to offer the check in for the answer to `request`: the translation's target when the list has a model for it and the offer is on, else ''."""
    cfg = config if config is not None else read()
    if not cfg["auto_offer"]:
        return ""
    code = translation_target(request)
    return code if code in cfg["languages"] else ""


# -- the check ------------------------------------------------------------------------------------------------------

def instruction(code: str) -> str:
    name = DISPLAY.get(code, code)
    return (f"You are a native {name} proofreader. Correct the grammar, spelling, punctuation, word choice and word order of the text you are given, so that it reads as "
            f"natural, correct {name}. Keep the meaning, the facts, the names, the numbers, the tone and the layout (paragraphs, lists, headings). Do not translate it, do not "
            "add or remove sentences, do not comment. Leave every marker of the form ⟦CODE1⟧ exactly as it is, in the same place. Answer with the corrected text only.")


def _accept(part: str, out: str) -> str:
    """What is wrong with a corrected part, or '' when it is good."""
    if not out.strip():
        return "the answer is empty"
    if digest.looping(out):
        return "the answer repeats itself"
    if Counter(longform.marks_in(part)) != Counter(longform.marks_in(longform.normalize_marks(out))):
        return "the markers ⟦…⟧ must stay exactly as they were"
    ratio = len(out) / max(1, len(part))
    if len(part) >= 80 and not LENGTH_BAND[0] <= ratio <= LENGTH_BAND[1]:
        return "the answer is much shorter or longer than the text: correct it, do not rewrite it"
    return ""


def check_text(ask: Callable[[str, str], str], text: str, code: str, job: Any = None) -> Dict[str, Any]:
    """The text corrected in the language `code`. `ask(system, part)` is one call of the model. Returns {"text", "changed", "parts", "kept", "language"}: how many
    parts were corrected and how many were kept as written because no answer was good."""
    if not known(code):
        raise LangCheckError("language: one of " + ", ".join(NAMES))
    if not isinstance(text, str) or not text.strip():
        raise LangCheckError("text: the text to check")
    if len(text) > MAX_TEXT_CHARS:
        raise LangCheckError("text: too large (at most 400,000 characters)")
    masked, prot = longform.protect(text)
    parts = digest.split(masked, PART_CHARS)
    system = instruction(code)
    out: List[str] = []
    changed = kept = 0
    for i, part in enumerate(parts):
        if job is not None:
            job.progress(i, len(parts))
            if job.stopped():
                raise digest.Stopped("stopped")
        if not part.strip():
            out.append(part)
            continue
        feedback, good = "", None
        for _ in range(ATTEMPTS):
            answer = longform.normalize_marks(ask(system + feedback, part).strip())
            why = _accept(part, answer)
            if not why:
                good = answer
                break
            feedback = f"\n\nYour earlier answer was not accepted: {why}. Answer again and put that right."
        if good is None:
            kept += 1
            out.append(part)
        else:
            changed += good.strip() != part.strip()
            out.append(good if not part.endswith("\n") or good.endswith("\n") else good + "\n")
    return {"text": prot.restore("".join(out), drop_unknown=True), "changed": changed, "parts": len(parts), "kept": kept, "language": code}


def asker(registry: Any, provider_id: str, model: str = "", session: Optional[requests.Session] = None) -> Callable[[str, str], str]:
    """`ask(system, part)` against a provider of the Providers page that runs here (OpenAI-compatible chat). Refuses a provider that is not local."""
    from tadamoo.llm.store import is_local
    prov = registry._find(registry.load(), provider_id)
    if prov is None:
        raise LangCheckError("the provider chosen for this language is no longer in Providers")
    if not is_local(prov):
        raise LangCheckError("the provider chosen for this language does not run on this PC or its network")
    resolved = registry.endpoint_of(prov, carica=False)
    if not resolved or not resolved[0]:
        raise LangCheckError(f"{prov.get('name') or provider_id} has no address: set it in Providers")
    base, key, default_model = resolved
    chosen = model or default_model
    if not chosen:
        raise LangCheckError(f"{prov.get('name') or provider_id} has no model: choose one in Providers or in the list")
    http = session or requests

    def ask(system: str, part: str) -> str:
        body = {"model": chosen, "temperature": 0.2, "stream": False, "messages": [{"role": "system", "content": system}, {"role": "user", "content": part}]}
        try:
            r = http.post(base.rstrip("/") + "/chat/completions", json=body, headers={"Authorization": f"Bearer {key or 'local'}"}, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            raise LangCheckError(f"cannot reach {prov.get('name') or provider_id}: {exc}") from None
        if r.status_code >= 400:
            raise LangCheckError(f"{prov.get('name') or provider_id} answered HTTP {r.status_code}")
        try:
            return str(r.json()["choices"][0]["message"]["content"] or "")
        except (ValueError, KeyError, IndexError, TypeError):
            raise LangCheckError(f"{prov.get('name') or provider_id} gave an answer that is not a chat completion") from None

    return ask


def run(registry: Any, text: str, code: str, session: Optional[requests.Session] = None) -> Dict[str, Any]:
    """`check_text` with the model the list names for `code`."""
    row = read()["languages"].get(code)
    if row is None:
        raise LangCheckError("this language has no model in the list: add one in Settings > Inference > Language check")
    return check_text(asker(registry, row["provider"], row["model"], session), text, code)
