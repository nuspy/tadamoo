"""Chat: code in an old message goes to the model as a short plain description, for the model only.

The conversation on screen and in the saved copy keeps the code. For the model, a block of code in a message that is more than a few messages
back is replaced by what it does, written in words by the technical session (a fixed outline made from the code when the session cannot), and
by the name of the file where the original is saved, so nothing is lost and the person can send it again. A call of the session reads only the
block under a prompt of its own: it never touches a conversation.

The Agent does not use this: a session with file and shell tools keeps its code whole (harness/context.py).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness import codeblocks, digest, side_uses
from serve import chatctx
from serve.longform import request_language
from serve.userdata import user_data_dir

CODE_ABSTRACT_PROMPT = (
    "You describe a piece of code to someone who cannot see it, so the conversation can go on without it.\n"
    "- Write two or three plain sentences: what the code is for, what it takes in and gives back, and what it defines, with the names of "
    "its functions, classes and files as plain words.\n"
    "- Write no code: no line of it, no fenced block, no expression, no name in backticks, no list of statements.\n"
    "- Do not explain how it works line by line. Do not answer a question that comes with the code.\n"
    "- Do not add anything the code does not show.")
SAVE_DIR = "chat_code"
ANSWER_TOKENS = 220
MAX_DESCRIPTION_CHARS = 700
COMMENT_CHARS = 100

TEXTS = {
    "en": {"pointer": "[Code removed from this message to save room. {what} The original ({lines} lines) is saved as {path} in the Basalt data folder; if you "
                      "need its exact lines, ask the person to send it again.]",
           "outline": "{lang} code of {lines} lines", "classes": "it defines the classes {names}", "functions": "the functions {names}",
           "comment": "it starts with the comment \"{text}\"", "none": "it defines no function or class"},
    "it": {"pointer": "[Codice tolto da questo messaggio per risparmiare spazio. {what} L'originale ({lines} righe) è salvato come {path} nella cartella dati di "
                      "Basalt; se ti servono le righe esatte, chiedi alla persona di rimandarlo.]",
           "outline": "Codice {lang} di {lines} righe", "classes": "definisce le classi {names}", "functions": "le funzioni {names}",
           "comment": "inizia con il commento \"{text}\"", "none": "non definisce funzioni né classi"},
}


def blocks_of(text: str, min_lines: int) -> List[Tuple[int, int, str, str]]:
    """The blocks of code to replace in a message: its fenced blocks of enough lines, else the whole message when it is source from top to bottom."""
    found = [b for b in codeblocks.fences(text) if len(b[3].strip().splitlines()) >= min_lines]
    if not found and len(text.strip().splitlines()) >= min_lines and codeblocks.looks_like_source(text):
        found = [(0, len(text), "", text)]
    return found


def outline_text(code: str, tag: str, lang: str) -> str:
    """The description made without a model: the language, the line count, the names it defines and its first comment."""
    t = TEXTS[lang]
    info = codeblocks.outline(code)
    name = codeblocks.guess_language(code, tag) or ("unknown language" if lang == "en" else "di linguaggio non riconosciuto")
    parts = [t["outline"].format(lang=name, lines=info["lines"])]
    named = [t["classes"].format(names=", ".join(info["classes"])) if info["classes"] else "", t["functions"].format(names=", ".join(info["functions"])) if info["functions"] else ""]
    named = [x for x in named if x]
    parts.append("; ".join(named) if named else t["none"])
    if info["comment"]:
        parts.append(t["comment"].format(text=info["comment"][:COMMENT_CHARS]))
    return "; ".join(parts) + "."


def plain(written: str) -> str:
    """A description from the model made safe: one paragraph without backticks; "" when it is empty, too long or holds code."""
    if "```" in written or "~~~" in written:
        return ""
    text = " ".join(written.replace("`", "").split())
    if not text or len(text) > MAX_DESCRIPTION_CHARS or codeblocks.looks_like_source(written):
        return ""
    return text


def describe(svc: Any, code: str, tag: str, lang: str, limit: int, count: Any) -> Tuple[str, str]:
    """(the description, "model" or "outline"). The technical session writes it when there is one and the use is on; else the outline."""
    info = svc.side_slot_info() if hasattr(svc, "side_slot_info") else None
    if info:
        window = int(info["context"])
        room = min(ANSWER_TOKENS, window // 4)
        body = code if digest.fits(code, window, room) else code[:digest.part_chars(window, room)]       # a block larger than the session: its start is described
        asked = "Write the description in " + ("Italian." if lang == "it" else "English.")
        prompt = [{"role": "system", "content": side_uses.system("code_abstract", CODE_ABSTRACT_PROMPT)}, {"role": "user", "content": f"The code:\n\n{body}\n\n{asked}"}]
        try:
            written = plain(chatctx._generate(svc, prompt, room, window, count, True, False, "code_abstract"))
        except Exception:                                   # a failed call is the outline, never a failed message
            written = ""
        if written:
            return written, "model"
    return outline_text(code, tag, lang), "outline"


def save_original(code: str, folder: Optional[Path] = None) -> str:
    """The code saved whole under a name made from its content; the name relative to the data folder."""
    name = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12] + ".txt"
    base = (folder or user_data_dir()) / SAVE_DIR
    try:
        base.mkdir(parents=True, exist_ok=True)
        target = base / name
        if not target.exists():
            target.write_text(code, encoding="utf-8")
    except OSError:
        return ""
    return f"{SAVE_DIR}/{name}"


def abstract_message(svc: Any, text: str, limit: int, count: Any, folder: Optional[Path] = None) -> Dict[str, Any]:
    """The message as the model reads it once it is old: each block of code replaced by a description and the place of the original.
    {"text": ""} when the use is off or the message holds no block of enough lines; the message itself stays as it is for the person."""
    if not side_uses.enabled("code_abstract"):
        return {"text": ""}
    blocks = blocks_of(text, int(side_uses.param("code_abstract", "min_lines")))
    if not blocks:
        return {"text": ""}
    lang = "it" if request_language(codeblocks.without_code(text)) == "it" else "en"
    out, last, saved, by = [], 0, [], []
    for start, end, tag, code in blocks:
        what, source = describe(svc, code, tag, lang, limit, count)
        path = save_original(code, folder)
        if not path:                                        # the original cannot be kept: the code stays in the message
            out.append(text[last:end])
            last = end
            continue
        out.append(text[last:start] + TEXTS[lang]["pointer"].format(what=what, lines=len(code.strip().splitlines()), path=path))
        last = end
        saved.append(path)
        by.append(source)
    if not saved:
        return {"text": ""}
    out.append(text[last:])
    return {"text": "".join(out), "blocks": len(saved), "saved": saved, "by": by}
