"""The language of the messages (Italian or English, for now): the user's explicit choice - BASALT_LANG (the
scripts' and the app's --lang set it), else the "lang" of prefs.json in the user's data folder (set from the web
page's Settings, or below) - else English. The desktop app, the scripts, the command-line tools and the web page
follow the same choice; the operating system's language is not used.

    python -m serve.lang                 # the language in use, and where it comes from
    python -m serve.lang set it|en|auto  # the per-user choice (auto: English)
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile

LANGS = ("it", "en")


def system_lang() -> str:
    """The display language of the operating system, as one of LANGS (English for any other)."""
    if os.name == "nt":
        try:
            if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x10:     # LANG_ITALIAN
                return "it"
            return "en"
        except (AttributeError, OSError):
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var, "")
        if v:
            return "it" if v.lower().startswith("it") else "en"
    return "en"


def _prefs_path():
    from serve import userdata
    return userdata.path("prefs.json")


def prefs() -> dict:
    try:
        data = json.loads(_prefs_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def chosen() -> str | None:
    """The explicit choice: BASALT_LANG, else prefs.json; None when there is none."""
    env = (os.environ.get("BASALT_LANG") or "").strip().lower()
    if env in LANGS:
        return env
    p = prefs().get("lang")
    return p if p in LANGS else None


def lang() -> str:
    return chosen() or "en"


def set_lang(value: str | None) -> dict:
    """Save the per-user choice (None or "auto": English); the prefs as saved."""
    if value in (None, "", "auto"):
        value = None
    elif value not in LANGS:
        raise ValueError(f"lang: one of {', '.join(LANGS)} or auto, not {value!r}")
    data = {**prefs(), "lang": value}
    p = _prefs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="prefs.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)
    return data


def t(it: str, en: str) -> str:
    """The message in the language in use."""
    return it if lang() == "it" else en


def info() -> dict:
    return {"lang": lang(), "chosen": prefs().get("lang") if prefs().get("lang") in LANGS else None,
            "env": (os.environ.get("BASALT_LANG") or None), "system": system_lang(), "langs": list(LANGS)}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["set"] and len(argv) == 2:
        try:
            set_lang(argv[1].lower())
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
    elif argv:
        print("python -m serve.lang [set it|en|auto]", file=sys.stderr)
        return 2
    print(json.dumps(info()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
