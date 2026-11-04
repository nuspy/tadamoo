"""The one way a conversation is cut down to the text a side task reads.

A long item keeps its beginning and its end; a long list keeps its first and its newest items. Wherever something is left out the text
says so ("[... N characters omitted]", "[... N messages omitted]"), and a `Cut` counts what was left out so that the consumer can keep it
next to its result: an empty result is then either "nothing found" in text that was whole, or a result over text that was cut.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

CHARS_PER_TOKEN = 3.5
HEAD_SHARE = 0.6                # of a cut item, the share kept from its start
EDGE_SHARE = 0.25               # of a cut list, the share of the budget kept from its first items; the rest is for the newest
ARGUMENT_CHARS = 300
LABELS = {"user": "USER", "assistant": "ASSISTANT", "system": "SYSTEM", "tool": "TOOL"}


@dataclass
class Cut:
    """What was left out of the text a consumer was given."""
    items: int = 0               # items offered
    clipped: int = 0             # items cut inside
    chars_omitted: int = 0       # characters left out inside items
    items_omitted: int = 0       # whole items left out
    chars_in: int = 0
    chars_out: int = 0

    @property
    def whole(self) -> bool:
        return not (self.clipped or self.items_omitted or self.chars_omitted)

    def add(self, other: "Cut") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Rendered:
    text: str
    cut: Cut


def marker(count: int, unit: str = "characters") -> str:
    return f"[... {count} {unit} omitted]"


def clip(text: str, limit: int, head_share: float = HEAD_SHARE) -> Tuple[str, int]:
    """`text` within `limit` characters, marker included: its start and its end with the marker between. Returns the text and the characters left out."""
    if limit <= 0 or len(text) <= limit:
        return text, 0
    keep = limit
    for _ in range(6):                                          # the marker's length depends on the count it states
        omitted = len(text) - keep
        width = len(marker(omitted)) + 2
        if keep == max(0, limit - width):
            break
        keep = max(0, limit - width)
    omitted = len(text) - keep
    head = int(keep * min(1.0, max(0.0, head_share)))
    tail = keep - head
    return text[:head] + "\n" + marker(omitted) + "\n" + (text[len(text) - tail:] if tail else ""), omitted


def fit(items: Sequence[str], *, item_chars: Any = None, budget: Optional[int] = None, head_share: float = HEAD_SHARE,
        unit: str = "messages", joiner: int = 1) -> Tuple[List[str], Cut]:
    """The items cut to fit: each within `item_chars` (a number, or one number per item), then, if they still pass `budget` characters together
    (`joiner` characters between two), the first ones and the newest ones with one marker standing for the middle."""
    cut = Cut(items=len(items), chars_in=sum(len(t) for t in items))
    caps = item_chars if isinstance(item_chars, (list, tuple)) else [item_chars] * len(items)
    texts: List[str] = []
    for text, cap in zip(items, caps):
        shorter, omitted = clip(text, int(cap), head_share) if cap else (text, 0)
        texts.append(shorter)
        cut.clipped += 1 if omitted else 0
        cut.chars_omitted += omitted
    if budget and sum(len(t) + joiner for t in texts) > budget:
        texts = _edges(texts, int(budget), joiner, head_share, unit, cut)
    cut.chars_out = sum(len(t) for t in texts)
    return texts, cut


def _edges(texts: List[str], budget: int, joiner: int, head_share: float, unit: str, cut: Cut) -> List[str]:
    reserve = len(marker(len(texts), unit)) + joiner
    room = max(0, budget - reserve)
    first, used = 0, 0
    while first < len(texts) and used + len(texts[first]) + joiner <= room * EDGE_SHARE:
        used += len(texts[first]) + joiner
        first += 1
    head = texts[:first]
    if not head and len(texts) > 1:                             # the first item alone is longer than its share: its start and end
        shorter, omitted = clip(texts[0], max(1, int(room * EDGE_SHARE) - joiner), head_share)
        head, first = [shorter], 1
        used += len(shorter) + joiner
        cut.clipped += 1 if omitted else 0
        cut.chars_omitted += omitted
    last, tail = len(texts), []
    while last > first and used + len(texts[last - 1]) + joiner <= room:
        last -= 1
        used += len(texts[last]) + joiner
        tail.append(texts[last])
    if not tail and last > first:                               # the newest item alone is longer than what is left: its start and end
        last -= 1
        shorter, omitted = clip(texts[last], max(1, room - used - joiner), head_share)
        tail.append(shorter)
        cut.clipped += 1 if omitted else 0
        cut.chars_omitted += omitted
    gone = last - first
    cut.items_omitted += gone
    return head + ([marker(gone, unit)] if gone else []) + tail[::-1]


def _words(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) if p.get("type") in ("text", "input_text") else "[image]" for p in content if isinstance(p, dict))
    return ""


def message_text(message: Dict[str, Any], argument_chars: int = ARGUMENT_CHARS, cut: Optional[Cut] = None) -> str:
    """One message as a labelled line of text: its words, and for an assistant the tools it called with their arguments cut (counted in `cut`)."""
    role = str(message.get("role") or "")
    label = LABELS.get(role, role.upper() or "?")
    if role == "tool" and message.get("name"):
        label = f"TOOL {message['name']}"
    body = _words(message.get("content")).strip()
    calls = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments")
        shown, omitted = clip(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False), argument_chars)
        if cut is not None and omitted:
            cut.clipped += 1
            cut.chars_omitted += omitted
        calls.append(f"[call {fn.get('name') or ''}] {shown}")
    return f"{label}: " + "\n".join(([body] if body else []) + calls)


def render(messages: Sequence[Dict[str, Any]], *, budget: Optional[int] = None, budget_tokens: Optional[int] = None, item_chars: int = 1500,
           result_chars: int = 600, argument_chars: int = ARGUMENT_CHARS, chars_per_token: float = CHARS_PER_TOKEN) -> Rendered:
    """The messages as the text a side task reads, within a budget in characters (or tokens). A message is cut to `item_chars`, a tool result to
    `result_chars`, each keeping its beginning and end; if the whole still passes the budget the first and the newest messages stay, with a marker
    for the ones between."""
    if budget is None and budget_tokens:
        budget = int(budget_tokens * chars_per_token)
    inside = Cut()
    texts = [message_text(m, argument_chars, inside) for m in messages]
    caps = [result_chars if m.get("role") == "tool" else item_chars for m in messages]
    parts, cut = fit(texts, item_chars=caps, budget=budget, joiner=2)
    cut.clipped += inside.clipped
    cut.chars_omitted += inside.chars_omitted
    return Rendered("\n\n".join(parts), cut)
