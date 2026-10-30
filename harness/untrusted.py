"""Text from outside the user's control, written into the conversation as a data block the text itself cannot end or imitate.

The block's tags carry a random id, so no text can close it by guessing. Before anything is placed in a block, look-alike forms of
the boundary tags (full-width letters, CJK brackets, zero-width characters) are folded to plain ones and replaced, and the control
tokens of the common chat templates are replaced, so the text cannot speak as a role.
"""
from __future__ import annotations

import hmac
import re
import secrets
import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

TAG = "untrusted_data"
ID_BYTES = 8
REMOVED_TOKEN = "[REMOVED_SPECIAL_TOKEN]"
STRIPPED_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")
_PROCESS_KEY = secrets.token_bytes(16)

_OPEN = re.compile(rf'\A<{TAG} source="[^"\n]*"(?: id="[0-9a-f]{{{2 * ID_BYTES}}}")?>\n')
_ID = re.compile(rf'(</?{TAG}\b[^>\n]*?) id="[0-9a-f]{{{2 * ID_BYTES}}}"')
_BOUNDARY = re.compile(rf"<\s*(/?)\s*{TAG}\b[^>\n]{{0,200}}>?", re.IGNORECASE)
_SOURCE_UNSAFE = re.compile(r'[^\w.:@/ +-]')

# characters that stand in for an angle bracket or a slash, folded before the tags are looked for
_LOOKALIKES: Dict[str, str] = {c: "<" for c in "〈《‹«⟨⟪❮﹤＜"} | {c: ">" for c in "〉》›»⟩⟫❯﹥＞"} | {c: "/" for c in "⁄∕／⧸"} | {c: "|" for c in "｜│∣❘"}

# the control tokens of the common chat templates, besides any <|name|> token
_TOKENS = re.compile(
    r"<\|[^|<>\n]{1,40}\|>"                                                      # ChatML, Llama 3, Harmony, Qwen, ...
    r"|\[/?INST\]|<</?SYS>>|\[/?(?:AVAILABLE_TOOLS|TOOL_RESULTS|TOOL_CALLS)\]"   # Llama 2, Mistral
    r"|</?s>|</?(?:bos|eos|pad|unk)>|<(?:start|end)_of_turn>",                   # Llama 2 and Gemma
    re.IGNORECASE)


def fold(text: str) -> Tuple[str, List[int]]:
    """`text` with look-alike characters made plain and invisible ones dropped, and for each character of the result the index of
    the character it came from."""
    out: List[str] = []
    origin: List[int] = []
    for i, ch in enumerate(text):
        if ch in _LOOKALIKES:
            piece = _LOOKALIKES[ch]
        elif unicodedata.category(ch) in STRIPPED_CATEGORIES and ch not in "\n\r\t":
            piece = ""
        else:
            piece = unicodedata.normalize("NFKC", ch)
        for p in piece:
            out.append(_LOOKALIKES.get(p, p))
            origin.append(i)
    return "".join(out), origin


def scrub(text: str, extra_closers: Sequence[str] = ()) -> str:
    """`text` with every form of a boundary tag, any tag in `extra_closers` and every chat control token replaced by a harmless mark."""
    if not text:
        return text
    plain = text.isascii()
    folded, origin = (text, []) if plain else fold(text)
    spans: List[Tuple[int, int, str]] = []
    for m in _BOUNDARY.finditer(folded):
        spans.append((m.start(), m.end(), f"[/{TAG}]" if m.group(1) else f"[{TAG}]"))
    for m in _TOKENS.finditer(folded):
        spans.append((m.start(), m.end(), REMOVED_TOKEN))
    for closer in extra_closers:
        for m in re.finditer(re.escape(closer), folded, re.IGNORECASE):
            spans.append((m.start(), m.end(), "[" + closer.strip("<>") + "]"))
    kept: List[Tuple[int, int, str]] = []
    for span in sorted(spans, key=lambda s: (s[0], -s[1])):                      # an overlapping span is left to the one before it
        if not kept or span[0] >= kept[-1][1]:
            kept.append(span)
    out = text
    for start, end, mark in reversed(kept):
        a, b = (start, end) if plain else (origin[start], origin[end - 1] + 1)
        out = out[:a] + mark + out[b:]
    return out


def clean_source(source: str) -> str:
    """A name safe to write inside the tag's quotes."""
    return _SOURCE_UNSAFE.sub("_", source)[:120]


def derived_id(seed: str) -> str:
    """An id that is the same for the same `seed` in this process and cannot be worked out from outside it: for a block that is written again
    from the same source and must come out the same."""
    return hmac.new(_PROCESS_KEY, seed.encode("utf-8", "replace"), "sha256").hexdigest()[:2 * ID_BYTES]


def fence(source: str, text: str, extra_closers: Sequence[str] = (), ident: str = "") -> str:
    """`text` as a data block with a random id on both of its tags (`ident`: this id instead, see `derived_id`)."""
    ident = ident or secrets.token_hex(ID_BYTES)
    body = scrub(text, extra_closers)
    return f'<{TAG} source="{clean_source(source)}" id="{ident}">\n{body}\n</{TAG} id="{ident}">'


def split_fence(text: str) -> Optional[Tuple[str, str, str]]:
    """(opening line, body, closing tag and what follows it) of a text that starts with a data block; None for any other text.
    Blocks written without an id are read too."""
    found = _OPEN.match(text)
    if not found:
        return None
    head = found.group(0).rstrip("\n")
    inner = text[found.end():]
    body, sep, tail = inner.rpartition(f"\n</{TAG}")
    if not sep:
        return head, inner, ""
    return head, body, sep + tail


def unfenced(text: str) -> str:
    """The text inside a data block, or the text itself when it is not one."""
    parts = split_fence(text) if text.startswith(f"<{TAG}") else None
    return parts[1] if parts else text


def normalize(text: str) -> str:
    """`text` with the ids of its data blocks removed, for comparing two texts that were fenced separately."""
    return _ID.sub(r"\1", text)


def strip_controls(text: str) -> str:
    """`text` without control, format and line or paragraph separator characters: for a string that is placed in a prompt line."""
    return "".join(ch for ch in text if unicodedata.category(ch) not in STRIPPED_CATEGORIES)
