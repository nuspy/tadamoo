"""The template a scheduled message is written from: plain text with `{name}` placeholders, filled from what the fire knows
(the plan it is tied to, the date, a plan's shared values such as `{bilancio}`). Not `str.format`: a name the context does not have
stays literal, so a typo is visible instead of raising, and a short note after the text says which ones were not found."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
VALUE_MAX = 200


def _text_of(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:VALUE_MAX]
    return str(value)[:VALUE_MAX]


def render_template(text: str, context: Dict[str, Any]) -> str:
    """`text` with every `{name}` found in `context` replaced; free text around the placeholders is kept as written."""
    missing: List[str] = []

    def sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name not in context:
            missing.append(name)
            return m.group(0)
        return _text_of(context[name])

    out = PLACEHOLDER.sub(sub, text)
    if missing:
        out += "\n\n[harness] placeholder(s) not resolved: " + ", ".join(sorted(set(missing)))
    return out
