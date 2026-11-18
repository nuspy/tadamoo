"""Guided help filling in a connector's settings: an ephemeral question to the technical session. It is told only the field NAMES (required, secret, already
set or not), never a value — a secret is still only ever typed into its own field on the page, never into this chat. Nothing of the conversation is kept once
answered; the page sends its own short history back on the next question and the server keeps none of it."""
from __future__ import annotations

from typing import Any, Dict, List

from tadamoo import side_uses
from tadamoo.side import side_extra_for

USE = "connector_configure_ask"
SYSTEM = (
    "You help a person fill in the settings of a connector on their own computer. You are told the NAMES of its fields and whether each is required, a "
    "secret or already set, never a value, and you must never see or ask for one. If the person starts typing or pasting what looks like an actual key or "
    "password here, tell them to put it directly into that field on the page instead, not in this chat. Say where a value is usually found (the service's "
    "own dashboard, an OAuth app registration, a local config file) and what a field means. Be concise and practical."
)


def field_lines(variables: List[Dict[str, Any]]) -> str:
    out = []
    for v in variables:
        bits = ["required" if v.get("required") else "optional", "secret" if v.get("secret") else "plain", "already set" if v.get("set") else "not set"]
        out.append(f"- {v.get('name')}: {', '.join(bits)}")
    return "\n".join(out) or "(this connector reads no settings)"


def ask(client: Any, name: str, variables: List[Dict[str, Any]], question: str, history: List[Dict[str, str]]) -> str:
    """One answer; the model never receives a secret's value, only the field names. A model that cannot be asked, or that fails, gets a plain, honest reply."""
    if client is None:
        return "There is no model to ask right now."
    system = f"{SYSTEM}\n\nConnector: {name}\nFields:\n{field_lines(variables)}"
    convo = [{"role": str(h.get("role")), "content": str(h.get("content"))[:2000]} for h in history if isinstance(h, dict) and h.get("role") in ("user", "assistant")
             and isinstance(h.get("content"), str)][-10:]
    messages = [{"role": "system", "content": system}] + convo + [{"role": "user", "content": str(question)[:2000]}]
    tokens = int(side_uses.param(USE, "answer_tokens"))
    extra = side_extra_for(client, USE, len(system) + sum(len(m["content"]) for m in convo), tokens) if side_uses.enabled(USE) else {}
    try:
        reply = client.chat_completion(messages=messages, temperature=0.2, max_tokens=tokens, timeout=side_uses.param(USE, "timeout_s"), extra=extra)
        text = str(reply["choices"][0]["message"].get("content") or "").strip()[:4000]
        return text or "I don't have an answer for that."
    except Exception:
        return "The model did not answer this time."
