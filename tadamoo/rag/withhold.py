"""Passages of archives that are not cleared for cloud models, remembered so that they can be taken out of what is sent when the conversation goes to
a provider outside this PC. The stored conversation is never changed, so going back to the local model keeps its cached prefix."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Set

PLACEHOLDER = "[passage withdrawn: its archive is not cleared for cloud models]"
SHINGLE = 8                         # words in a run that counts as a copy of a passage
MAX_SPANS = 400
_WORDS = re.compile(r"\w+", re.UNICODE)


def _shingles(text: str) -> Set[str]:
    words = [w.lower() for w in _WORDS.findall(text)]
    return {" ".join(words[i:i + SHINGLE]) for i in range(0, max(0, len(words) - SHINGLE + 1))}


class Withhold:
    def __init__(self) -> None:
        self.spans: List[str] = []
        self.shingles: Set[str] = set()

    def add(self, *texts: str) -> None:
        for text in texts:
            if text and len(text) >= 8 and text not in self.spans:
                self.spans.append(text)
                self.shingles |= _shingles(text)
        del self.spans[:-MAX_SPANS]
        self.spans.sort(key=len, reverse=True)

    def _swap(self, text: str) -> str:
        for span in self.spans:
            if span in text:
                text = text.replace(span, PLACEHOLDER)
            escaped = json.dumps(span, ensure_ascii=False)[1:-1]
            if escaped != span and escaped in text:                 # inside the JSON of a tool result
                text = text.replace(escaped, PLACEHOLDER)
        return text

    def apply(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The messages with every remembered passage (or a line that repeats one) replaced by the placeholder; messages that hold none are passed on as
        they are."""
        if not self.spans:
            return messages
        out = []
        for m in messages:
            content = m.get("content")
            now = self.redact(content) if isinstance(content, str) else content
            out.append(m if now is content or now == content else {**m, "content": now})
        return out

    def redact(self, text: str) -> str:
        """`text` with the passages (whole, or a line that repeats a run of eight words of one) replaced by the placeholder."""
        if not self.spans or not text:
            return text
        lines = []
        for line in self._swap(text).split("\n"):
            lines.append(PLACEHOLDER if self.shingles & _shingles(line) else line)
        return "\n".join(lines)
