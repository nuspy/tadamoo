"""Next-prompt suggestions: after an answer, a few messages the person might send next, as chips they click or edit.

One cheap structured call after the answer is shown: the conversation as it stands plus one trailing instruction that is never stored,
a JSON schema, thinking off, a short timeout. It is skipped whenever it could cost more than it gives: a long conversation (an extra
request over a long history may cost the next turn its cached prefix), a cold cache (the call would read the whole history itself), a
person who is busy (a queued message, a running task), a tiny answer, a card or an error, plan mode, unattended runs. After three sets
in a row that nobody used it runs every second turn. A suggestion is a complete message of plain text, at most 480 characters and never
cut mid-sentence; it is put in the box and never sent by itself, never given to a tool and never written to the history.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from harness import questions as qs
from harness.side import side_extra

log = logging.getLogger("harness.suggestions")

PROMPT_CHARS, MAX_ITEMS = 480, 4          # the longest a chip may be; the instruction asks for about 300
MIN_ANSWER_CHARS = 120                    # about 30 tokens: "Done." has nothing to follow up
MAX_TOKENS = 480                          # three whole sentences with their reasons, in a language that takes many tokens per word
TIMEOUT_S = 12.0
UNUSED_BEFORE_BACKOFF = 3
COLD_MIN_TOKENS = 2000                    # below this a cold cache costs nothing worth saving
INSTRUCTION = (
    "Write 3 messages the USER might send next, in the language of the user's last message, each in the user's voice (a request or a "
    "question, never a thanks and never a question for the user). Each must be complete and self-explanatory: one or two whole sentences "
    "(about 120 to 300 characters) that the user could send as they are, without this conversation in front of them. Say what to do and on "
    "what, with the real names from the answer (files, functions, numbers, terms), and add the reason or the detail that makes the request "
    "precise. Never a vague line such as \"Explain more\" or \"Check it\", never an ellipsis, never cut off. Make them different in kind: "
    "go deeper, apply it, check or compare. If nothing sensible follows, return an empty list. Output only JSON."
)
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["suggestions"], "properties": {"suggestions": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["prompt"], "properties": {"prompt": {"type": "string"}}}}}}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "next_prompts", "strict": True, "schema": SCHEMA}}
SENTENCE_END = ".!?"


def whole(text: str, limit: int) -> str:
    """`text` when it fits `limit`, else its longest run of whole sentences that fits; "" when not even the first sentence does."""
    if len(text) <= limit:
        return text
    cut = max((i for i, ch in enumerate(text[:limit]) if ch in SENTENCE_END and (i + 1 == len(text) or text[i + 1] == " ")), default=-1)
    return text[:cut + 1] if cut >= 0 else ""


def parse(content: Any) -> List[Dict[str, str]]:
    """The chips in a reply: the full text of each message, never cut in the middle (one over the limit keeps its whole sentences that fit,
    or is dropped), duplicates and near-duplicates dropped, at most four. Anything that is not the expected JSON gives none."""
    try:
        data = json.loads(content) if isinstance(content, str) else content
        raw = data["suggestions"]
    except (ValueError, KeyError, TypeError):
        return []
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        prompt = whole(qs.clean(item.get("prompt"), 10 * PROMPT_CHARS), PROMPT_CHARS)
        key = qs._same(prompt)
        if len(prompt) < 3 or key in seen:
            continue
        seen.add(key)
        out.append({"prompt": prompt})
    return out[:MAX_ITEMS]


class Suggester:
    """The settings and the memory of one conversation's suggestions."""

    def __init__(self, client: Any, enabled: bool = True, side_call_tokens: int = 6000, clock: Callable[[], float] = time.monotonic) -> None:
        self.client, self.enabled, self.side_call_tokens, self.clock = client, enabled, side_call_tokens, clock
        self.unused = 0                    # sets in a row that nobody used
        self.skipped_for_backoff = False
        self.offered: List[str] = []       # the prompts of the set on the screen

    def skip_reason(self, *, answer: str, stop_reason: str, cut: bool, error: str, attended: bool, plan_mode: bool, idle: bool,
                    prompt_tokens: int, cached_tokens: int) -> str:
        """Why no suggestions are made for this turn, or "" when they are."""
        if not self.enabled or self.client is None:
            return "off"
        if not attended:
            return "unattended"
        if stop_reason != "final" or cut or error:
            return "ending"
        if len(answer.strip()) < MIN_ANSWER_CHARS:
            return "short"
        if plan_mode:
            return "plan"
        if not idle:
            return "busy"
        if prompt_tokens > self.side_call_tokens:
            return "long"
        if prompt_tokens > COLD_MIN_TOKENS and cached_tokens * 2 < prompt_tokens:
            return "cold"
        if self.unused >= UNUSED_BEFORE_BACKOFF:
            self.skipped_for_backoff = not self.skipped_for_backoff
            if self.skipped_for_backoff:
                return "backoff"
        return ""

    def sent(self, text: str) -> None:
        """The person's next message: a set they used (the text is one of its prompts) resets the back-off; one they ignored counts against it."""
        if not self.offered:
            return
        self.unused = 0 if text.strip() in self.offered else self.unused + 1
        self.offered = []

    def generate(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]], cancelled: Callable[[], bool]) -> Dict[str, Any]:
        """The call: `messages` are the conversation as the model last saw it (already in wire form). Returns {"items", "ms", "tokens"}; the
        items are empty after any failure, a timeout or a cancel (the connection is closed, so the model stops)."""
        started = time.monotonic()

        def on_delta(kind: str, text: str) -> None:
            if cancelled():
                from harness.client import StreamCancelled
                raise StreamCancelled()
        try:
            resp = self.client.chat_completion(
                messages=[*messages, {"role": "user", "content": INSTRUCTION}], **({"tools": tools, "tool_choice": "none"} if tools else {}),
                temperature=0.4, max_tokens=MAX_TOKENS, timeout=TIMEOUT_S, response_format=RESPONSE_FORMAT,
                extra={"reasoning_effort": "none", **side_extra(self.client)}, on_delta=on_delta)
            message = resp["choices"][0]["message"]
            items = parse(message.get("content") or "")
            usage = resp.get("usage") or {}
            tokens = int(usage.get("total_tokens") or 0)
        except Exception as exc:               # an extra call that fails is no call: nothing is shown
            log.info("suggestions: none: %s", type(exc).__name__)
            return {"items": [], "ms": round((time.monotonic() - started) * 1000), "tokens": 0}
        if cancelled():
            items = []
        return {"items": items, "ms": round((time.monotonic() - started) * 1000), "tokens": tokens}
