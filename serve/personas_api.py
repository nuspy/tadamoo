"""Settings > Personalities: the personalities the person made, their memory, and the text a conversation without a harness takes in.

Only the server's own page reaches it: a personality's notes are about the person and stay on this PC.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from serve.memories_api import MemoriesApi
from tadamoo import personas as pe
from tadamoo.personal_memory import INFERRED_SECTIONS, LONG_SECTIONS, SHORT_SECTIONS, PersonalMemoryError

SHOWN = 300                     # notes one answer lists; the page narrows them with a section or a search
NOTE_ACTIONS = ("item", "confirm", "delete", "clear", "forget-learned")
GOAL_CHARS = 2000
CONVERSATION = re.compile(r"^[A-Za-z0-9_-]{1,70}$")
TURNS = 200                     # lines of a conversation without a harness that one request may hand over
TURN_CHARS = 20000


class PersonasApi:
    def __init__(self, store: pe.PersonaStore, rag: Optional[Any] = None, learning: Optional[Any] = None) -> None:
        self.store = store
        self.learning = learning        # serve/persona_learning_worker.py: takes the conversations of the model-only chat to learn from
        self.rag = rag                  # the knowledge archives (tadamoo/rag): the passages of a personality's linked ones go in with its notes

    def _summary(self, persona: Dict[str, Any]) -> Dict[str, Any]:
        memory = self.store.memory(persona["slug"])
        return {"slug": persona["slug"], "name": persona["name"], "language": persona["language"], "voice": persona["voice"], "avatar": persona["avatar"],
                "learn": persona["learn"], "counts": {"long": memory.count("long"), "short": memory.count("short")}}

    def overview(self) -> Dict[str, Any]:
        return {"personas": [self._summary(p) for p in self.store.list()], "limits": pe.LIMITS, "max": pe.MAX_PERSONAS}

    def get(self, slug: str, query: str = "", kind: str = "", section: str = "") -> Dict[str, Any]:
        persona = self.store.get(slug)
        memory = self.store.memory(slug)
        try:
            rows = memory.items(kind or None, section or None, query)
        except PersonalMemoryError as e:
            raise pe.PersonaError(str(e)) from None
        state = memory.learned_state()
        return {"persona": persona, "limits": pe.LIMITS, "learned": {"conversations": state["conversations"], "last": state["last"], "notes": len(memory.learned_items())},
                "memory": {"counts": {"long": memory.count("long"), "short": memory.count("short")}, "items": rows[:SHOWN], "more": max(0, len(rows) - SHOWN),
                           "sections": {"long": list(LONG_SECTIONS), "short": list(SHORT_SECTIONS)}, "inferred": sorted(INFERRED_SECTIONS)}}

    def create(self, req: Dict[str, Any]) -> Dict[str, Any]:
        return {"persona": self.store.create(req)}

    def update(self, slug: str, req: Dict[str, Any]) -> Dict[str, Any]:
        return {"persona": self.store.update(slug, req)}

    def delete(self, slug: str, req: Dict[str, Any]) -> Dict[str, Any]:
        return self.store.delete(slug, req.get("confirm"))

    def memory(self, slug: str, action: str, req: Dict[str, Any]) -> Dict[str, Any]:
        """A note of the personality's memory added, corrected, confirmed or deleted, or all of them cleared (with the confirmation)."""
        self.store.get(slug)
        if action not in NOTE_ACTIONS:
            raise pe.PersonaError("unknown action", "not_found")
        if action == "forget-learned":
            if req.get("confirm") is not True:
                raise pe.PersonaError("forgetting what was learned from conversations needs the confirmation")
            return {"forgotten": self.store.memory(slug).forget_learned()}
        try:
            return MemoriesApi(self.store.memory(slug)).post(action, req)
        except PersonalMemoryError as e:
            raise pe.PersonaError(str(e)) from None

    def learn(self, slug: str, req: Dict[str, Any]) -> Dict[str, Any]:
        """The model-only chat ended: its lines (the person's and the model's, no more) are kept to be learned from, unless the personality does not learn."""
        persona = self.store.get(slug)
        conversation, lines = req.get("conversation"), req.get("messages")
        if not isinstance(conversation, str) or not CONVERSATION.match(conversation):
            raise pe.PersonaError("conversation: the id of the conversation")
        if (not isinstance(lines, list) or len(lines) > TURNS
                or any(not isinstance(l, dict) or l.get("role") not in ("user", "assistant") or not isinstance(l.get("text"), str) or len(l["text"]) > TURN_CHARS for l in lines)):
            raise pe.PersonaError(f"messages: up to {TURNS} lines, each with a role (user or assistant) and a text")
        if not persona["learn"] or self.learning is None:
            return {"queued": False}
        return {"queued": self.learning.submit(slug, "chat-" + conversation, lines)}

    def compose(self, slug: str, req: Dict[str, Any]) -> Dict[str, str]:
        goal = req.get("goal", "")
        if not isinstance(goal, str):
            raise pe.PersonaError("goal: the text of the request")
        return pe.compose(self.store, slug, goal[:GOAL_CHARS], self.rag)
