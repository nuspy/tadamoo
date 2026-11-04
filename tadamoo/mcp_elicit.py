"""A request for input from an MCP server, put to the person in the conversation.

A server may stop in the middle of a call and ask for a few values (elicitation, form mode). The question is shown to the person
with the server's name and its own words, as a confirmation of kind `mcp_elicitation`: Decline and Cancel are buttons, and the
values come back as one answer after the prefix `ELICIT_PREFIX`. Only what the server declared is taken from the answer, checked
against its schema, so the server gets nothing it did not ask for.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

ELICIT_PREFIX = "elicit:"
MAX_FIELDS = 12
MAX_TEXT = 300


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return "".join(ch for ch in str(value or "") if ch == "\n" or ch >= " ")[:limit].strip()


def form_fields(schema: Any) -> List[Dict[str, Any]]:
    """The fields of a requested schema that a person can fill: a flat object of strings, numbers, integers, booleans and choices."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    required = set(schema.get("required") or []) if isinstance(schema, dict) and isinstance(schema.get("required"), list) else set()
    fields: List[Dict[str, Any]] = []
    for name, spec in (props.items() if isinstance(props, dict) else []):
        if not isinstance(spec, dict) or len(fields) >= MAX_FIELDS:
            continue
        kind = spec.get("type")
        options: Optional[List[Any]] = None
        if isinstance(spec.get("enum"), list) and spec["enum"]:
            options = [v for v in spec["enum"] if isinstance(v, (str, int, float)) and not isinstance(v, bool)][:30]
            kind = "choice"
        elif isinstance(spec.get("oneOf"), list):
            options = [o.get("const") for o in spec["oneOf"] if isinstance(o, dict) and isinstance(o.get("const"), (str, int, float))][:30]
            kind = "choice" if options else kind
        if kind not in ("string", "number", "integer", "boolean", "choice"):
            continue                                                  # nested objects and arrays are not offered
        field = {"name": _text(name, 64), "type": kind, "title": _text(spec.get("title")) or _text(name, 64),
                 "description": _text(spec.get("description")), "required": name in required}
        if options is not None:
            field["options"] = options
        if "default" in spec and isinstance(spec["default"], (str, int, float, bool)):
            field["default"] = spec["default"]
        fields.append(field)
    return fields


def checked_content(fields: List[Dict[str, Any]], content: Any) -> Dict[str, Any]:
    """The values the person gave, for the declared fields only. A value of the wrong kind, a missing required one or a choice that is
    not on offer is a ValueError."""
    if not isinstance(content, dict):
        raise ValueError("the answer is not a set of values")
    out: Dict[str, Any] = {}
    for f in fields:
        if f["name"] not in content or content[f["name"]] in (None, ""):
            if f["required"]:
                raise ValueError(f"'{f['name']}' is required")
            continue
        v = content[f["name"]]
        if f["type"] == "boolean":
            ok = isinstance(v, bool)
        elif f["type"] == "integer":
            ok = isinstance(v, int) and not isinstance(v, bool)
        elif f["type"] == "number":
            ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        elif f["type"] == "choice":
            ok = v in f["options"]
        else:
            ok = isinstance(v, str)
        if not ok:
            raise ValueError(f"'{f['name']}' is not a valid {f['type']}")
        out[f["name"]] = v
    return out


def elicit_through(ask: Callable[[Dict[str, Any]], str], server: str) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """The handler a client takes for `client.elicit`: puts the server's request to the person with `ask(payload)` (the conversation's
    confirmation) and turns the answer into the result the server expects."""
    def handle(params: Dict[str, Any]) -> Dict[str, Any]:
        fields = form_fields(params.get("requestedSchema"))
        message = _text(params.get("message"), 1000)
        payload = {"mode": "confirm", "kind": "mcp_elicitation", "free_prefix": ELICIT_PREFIX,
                   "facts": {"server": _text(server, 64), "message": message, "fields": fields},
                   "question": f"The MCP server {server} asks you for some information: {message}",
                   "options": [{"label": "Decline", "value": "decline"}, {"label": "Cancel", "value": "cancel"}]}
        answer = ask(payload)
        if answer == "decline":
            return {"action": "decline"}
        if isinstance(answer, str) and answer.startswith(ELICIT_PREFIX):
            try:
                return {"action": "accept", "content": checked_content(fields, json.loads(answer[len(ELICIT_PREFIX):]))}
            except ValueError:
                return {"action": "cancel"}
        return {"action": "cancel"}
    return handle


class ElicitAsker:
    """Where the conversation puts the question to the person (set by the session once it can ask); without it the server's request is declined."""

    def __init__(self) -> None:
        self.ask: Optional[Callable[[Dict[str, Any]], str]] = None

    def handler(self, server: str) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        def handle(params: Dict[str, Any]) -> Dict[str, Any]:
            if self.ask is None:
                return {"action": "decline"}
            return elicit_through(self.ask, server)(params)
        return handle
