"""Optimize prompt: one short rewrite of the person's message before the model reads it.

When the person turned the option on and the local server has a technical session (harness/side.py), the message is sent alone to that session
with a fixed instruction: rewrite it as a clear, complete, self-contained instruction in the same language. The reply replaces the message the
model sees; the person's own words stay as they wrote them, shown and stored beside it. The reply is checked in plain code and thrown away when
it lost anything the person wrote that must survive (code, links, paths, numbers, quotations, mentions), when its length is out of proportion,
or when it reads as an answer or leaks the instruction. Every failure sends the original: nothing here ever blocks a message.
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from harness import questions as qs
from harness.side import side_extra
from serve import longform as lf

log = logging.getLogger("harness.optimize")

TIMEOUT_S = 8.0
MIN_WORDS = 4
MAX_INPUT_CHARS = 4000               # a longer message cannot come back within the output cap
PREVIOUS_CHARS = 800
MAX_TOKENS, MIN_TOKENS, OUT_PER_IN, CHARS_PER_TOKEN = 2000, 128, 1.5, 3.0
RATIO = (0.5, 4.0)
DETAIL_CHARS = 60
NOT_THE_PERSONS = ("/", "Target Loop ·", "[harness]", "[planner]")

SYSTEM_PROMPT = (
    "You rewrite one message a user wrote to an AI assistant. Rewrite the request as a clear, complete, self-contained instruction, in the same "
    "language as the user. Keep every fact, name, number, path, link, code block, quotation and constraint exactly as given. Do not add "
    "requirements the user did not state. Do not answer the request and do not do the task. A previous message may be shown: use it only to "
    "understand what the user refers to. Output only the rewritten text, with no preface, no explanation and no quotation marks around it."
)
LEAKS = ("rewrite the request", "rewritten text", "self-contained instruction", "output only", "same language as the user", "do not answer the request",
         "<message>", "</message>", "<previous>", "</previous>", "system prompt")

_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INLINE = re.compile(r"`([^`\n]{1,200})`")
_URL = re.compile(r"(?:https?|ftp)://[^\s<>\"'`]+|\bwww\.[^\s<>\"'`]+")
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_MENTION = re.compile(r"(?<![\w@.])@[A-Za-z0-9_][\w.\-]*")
_PATH = re.compile(r"(?<![\w/\\:.])(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|~[\\/]|[\\/])[\w.\-/\\]{2,}")
_RELPATH = re.compile(r"(?<![\w/\\:.@])[\w.\-]+(?:[\\/][\w.\-]+)+")
_FILE = re.compile(r"(?<![\w/\\])[\w\-]+(?:\.[\w\-]+)*\.(?:py|js|ts|tsx|jsx|cpp|cu|h|hpp|c|rs|go|java|cs|sh|ps1|sql|md|json|txt|csv|yml|yaml|toml|ini|cfg|log|html|css|xml|pdf|docx|xlsx|png|jpg|jpeg|gif|svg|zip)\b")
_QUOTED = re.compile(r"\"([^\"\n]{1,300})\"|“([^”\n]{1,300})”|«([^»\n]{1,300})»")
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*(?![\w])")
_OPENER = re.compile(
    r"^\W*(?:sure|certainly|of course|absolutely|okay|ok|here(?:'s| is| are)|i(?:'ll| will| can| have|'ve)|i'm sorry|sorry|unfortunately|as an ai|yes|"
    r"certo|certamente|ecco|volentieri|mi dispiace|purtroppo|posso|va bene|ottimo)\b", re.I)
_TRAIL = ".,;:!?)]}\"'”»"


@dataclass
class Result:
    """`sent`: `text` replaces the message. `rejected`: the reply was thrown away for `reason` (and `detail`). `skipped`: nothing was tried or
    nothing came of it, and the person is told nothing."""
    state: str = "skipped"
    text: str = ""
    reason: str = ""
    detail: str = ""
    ms: int = 0


def skip_reason(text: str, *, origin: str = "user", attended: bool = True) -> str:
    """Why this message is left alone, in a word; empty when it may be rewritten. Left alone: not the person's own message, a run nobody
    watches, a command, a note of the harness, the answer to a card, a Target Loop message, the Long answer control line, fewer than four
    words, and a message too long for its rewrite to fit the output cap."""
    stripped = text.strip()
    if origin != "user":
        return "origin"
    if not attended:
        return "unattended"
    if not stripped:
        return "empty"
    if stripped.startswith(NOT_THE_PERSONS):
        return "command"
    if stripped.startswith(qs._ASK_BACK):
        return "answer"
    if lf.parse_hint(text) is not None:
        return "long"
    if len(stripped.split()) < MIN_WORDS:
        return "short"
    if len(stripped) > MAX_INPUT_CHARS:
        return "big"
    return ""


def _squash(text: str) -> str:
    return " ".join(text.split())


def _trim(token: str) -> str:
    return token.rstrip(_TRAIL)


def _is_path(token: str) -> bool:
    """A relative path is told from words joined by a slash ("and/or", "he/she"): it names a file by its extension or goes three levels down."""
    return bool(re.search(r"\.[A-Za-z0-9]{1,6}$", token)) or len(re.findall(r"[\\/]", token)) >= 3


def protected(text: str) -> List[Tuple[str, str]]:
    """What the rewrite must still contain, as (kind, value): every code block, link, path, file name, address, mention, quoted string and
    number the person wrote."""
    found: List[Tuple[str, str]] = [("code", m) for m in _FENCE.findall(text)]
    rest = _FENCE.sub(" ", text)
    found += [("code", m) for m in _INLINE.findall(rest)]
    rest = _INLINE.sub(" ", rest)
    for kind, pattern in (("link", _URL), ("address", _EMAIL), ("path", _PATH), ("path", _RELPATH), ("file", _FILE)):
        def take(m: "re.Match[str]", kind: str = kind, pattern: "re.Pattern[str]" = pattern) -> str:
            token = _trim(m.group(0))
            if not token or (pattern is _RELPATH and not _is_path(token)):
                return m.group(0)                        # words joined by a slash stay in the text
            found.append((kind, token))
            return " "
        rest = pattern.sub(take, rest)
    found += [("mention", _trim(m)) for m in _MENTION.findall(rest)]
    quotes = [next(g for g in groups if g) for groups in _QUOTED.findall(rest)]
    found += [("quote", _squash(q)) for q in quotes if _squash(q)]
    rest = _QUOTED.sub(lambda m: " " + next(g for g in m.groups() if g) + " ", rest)       # a number inside a quotation is still a number
    found += [("number", m) for m in _NUMBER.findall(rest)]
    seen, out = set(), []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def lost(original: str, rewrite: str) -> Optional[Tuple[str, str]]:
    """The first protected item of `original` that `rewrite` does not hold, as (kind, value), or None."""
    squashed = _squash(rewrite)
    numbers = set(_NUMBER.findall(rewrite))
    for kind, value in protected(original):
        if kind == "number":
            ok = value in numbers
        elif kind == "quote":
            ok = value in squashed
        else:
            ok = value in rewrite
        if not ok:
            return kind, value
    return None


def tidy(reply: str) -> str:
    """The reply without a wrapper the instruction forbade: one pair of quotation marks or one code fence around all of it."""
    out = (reply or "").strip()
    fenced = re.fullmatch(r"```[\w+-]*\n(.*)\n```", out, re.S)
    if fenced and "```" not in fenced.group(1):
        out = fenced.group(1).strip()
    for left, right in (('"', '"'), ("“", "”"), ("«", "»")):
        if len(out) > 2 and out.startswith(left) and out.endswith(right) and left not in out[1:-1] and right not in out[1:-1]:
            return out[1:-1].strip()
    return out


def judge(original: str, rewrite: str) -> Tuple[str, str]:
    """(reason, detail) when the rewrite cannot be used, else ("", ""): `empty`, `leak` (it repeats the instruction), `answer` (it starts as a
    reply), `ratio` (its length is out of proportion), `lost` (a protected item of the original is missing; the detail names it)."""
    if not rewrite.strip():
        return "empty", ""
    low, base = rewrite.lower(), original.lower()
    for phrase in LEAKS:
        if phrase in low and phrase not in base:
            return "leak", phrase
    if _OPENER.match(rewrite) and not _OPENER.match(original):
        return "answer", ""
    ratio = len(rewrite.strip()) / max(1, len(original.strip()))
    if not RATIO[0] <= ratio <= RATIO[1]:
        return "ratio", f"{ratio:.1f}"
    missing = lost(original, rewrite)
    if missing:
        return "lost", f"{missing[0]}: {_squash(missing[1])[:DETAIL_CHARS]}"
    return "", ""


def output_cap(text: str) -> int:
    """The tokens the reply may have: about one and a half times the message, within a floor and the fixed ceiling."""
    return min(MAX_TOKENS, max(MIN_TOKENS, int(OUT_PER_IN * math.ceil(len(text) / CHARS_PER_TOKEN))))


def prompt_for(text: str, previous: str = "") -> List[Dict[str, str]]:
    """The whole request: the fixed instruction, and the message (with the person's one earlier message, for context only)."""
    before = f"<previous>\n{previous.strip()[-PREVIOUS_CHARS:]}\n</previous>\n" if previous.strip() else ""
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"{before}<message>\n{text.strip()}\n</message>"}]


def previous_words(messages: List[Dict[str, Any]], before: int) -> str:
    """The person's own words in their message before position `before` (what they typed, not what the model saw), or empty."""
    for m in reversed(messages[:before]):
        if m.get("role") != "user" or m.get("note"):
            continue
        content = m.get("raw") if isinstance(m.get("raw"), str) else m.get("content")
        if not isinstance(content, str) or content.startswith(("[harness]", "[planner]", "<untrusted_data", qs.ANSWER_HEADER)):
            continue
        if content.startswith("<request>\n") and "\n</request>" in content:
            content = content[len("<request>\n"):content.index("\n</request>")]
        if content.startswith("<documents>\n"):
            from harness.attachments import split_documents
            content = split_documents(content)[0]
        if content.strip() and not content.startswith(qs._ASK_BACK):
            return content.strip()
    return ""


class Optimizer:
    """One conversation's option: whether it is on and the model that makes the call."""

    def __init__(self, client: Any = None, enabled: bool = False) -> None:
        self.client, self.enabled = client, enabled

    def ready(self, text: str, *, origin: str = "user", attended: bool = True) -> str:
        """Why no call will be made for `text`, in a word; empty when one will."""
        why = skip_reason(text, origin=origin, attended=attended)
        if why:
            return why
        if not self.enabled or self.client is None:
            return "off"
        stays = getattr(self.client, "stays_local", None)
        if callable(stays) and not stays():
            return "route"                                  # a route to another provider is not the technical session
        return "" if side_extra(self.client) else "no_session"

    def run(self, text: str, *, previous: str = "", origin: str = "user", attended: bool = True) -> Result:
        """The rewrite of `text`, or why there is none. Never raises."""
        why = self.ready(text, origin=origin, attended=attended)
        if why:
            return Result(reason=why)
        extra = side_extra(self.client)
        t0 = time.monotonic()
        try:
            resp = self.client.chat_completion(messages=prompt_for(text, previous), temperature=0.0, max_tokens=output_cap(text), timeout=TIMEOUT_S,
                                               extra={"reasoning_effort": "none", **extra})
            choice = resp["choices"][0]
            reply = choice["message"].get("content") or ""
        except Exception as exc:                            # any failure of an extra call is a skipped call, never an error for the person
            log.info("optimize: skipped: %s", type(exc).__name__)
            return Result(reason="failed", ms=int((time.monotonic() - t0) * 1000))
        ms = int((time.monotonic() - t0) * 1000)
        if choice.get("finish_reason") == "length":
            return Result("rejected", reason="cut", ms=ms)
        out = tidy(reply)
        if not out:
            return Result(reason="failed", ms=ms)
        if _squash(out) == _squash(text):
            return Result(reason="same", ms=ms)
        reason, detail = judge(text, out)
        if reason:
            return Result("rejected", reason=reason, detail=detail, ms=ms)
        return Result("sent", text=out, ms=ms)
