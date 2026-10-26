"""The parts of a request the person marked as variable: a phrase between two # with a space inside, as in
`find the news # of today # about # the war in Ukraine #`.

A marker is `#`, a space, the phrase, a space and `#`, on one line; the phrase can be several words, and may begin with a name and a
colon to name the parameter (`# city: Rome #`). Nothing inside a code block or between backticks is a marker, `\\#` is a plain #, and a
`# title` with no closing # is not a marker. The model is given the text without them; the history keeps what the person wrote."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_MARK = re.compile(r"(?<![\w\\#])#[ \t]+(?=\S)([^#\n]*?\S)[ \t]+#(?![\w#])")
_NAMED = re.compile(r"^([^\W\d][\w\-]*(?: [\w\-]+){0,2}):[ \t]+(\S.*)$", re.UNICODE)
MAX_PHRASE = 300


@dataclass
class Mark:
    start: int
    end: int
    value: str                    # the phrase the person marked
    name: Optional[str] = None    # the name they gave it, when they did


def find(text: str) -> List[Mark]:
    """The markers in a request, in the order they were written."""
    if "#" not in text:
        return []
    masked = _CODE.sub(lambda m: "\x00" * len(m.group()), text)
    out: List[Mark] = []
    for m in _MARK.finditer(masked):
        phrase = text[m.start(1):m.end(1)]
        if len(phrase) > MAX_PHRASE:
            continue
        named = _NAMED.match(phrase)
        out.append(Mark(m.start(), m.end(), (named.group(2) if named else phrase).strip(), named.group(1).strip() if named else None))
    return out


def clean(text: str) -> str:
    """The request without the markers (each is replaced by its phrase), which is what the model is given."""
    marks = find(text)
    for mark in reversed(marks):
        text = text[:mark.start] + mark.value + text[mark.end:]
    return text.replace("\\#", "#") if "\\#" in text else text
