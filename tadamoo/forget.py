"""Taking a conversation back out of the memories: its pieces in the index of past conversations and what was learned from it.

A private conversation is kept out of both from the start (`keep_out`); "forget" also removes what was already taken (`forget`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from tadamoo.memory_export import export_markdown
from tadamoo.past import PastIndex
from tadamoo.personal_memory import PersonalMemory


class Forgetter:
    def __init__(self, past: Optional[PastIndex], memory: Optional[PersonalMemory], export_dir: Optional[str | Path] = None, personas: Any = None) -> None:
        self.past, self.memory, self.export_dir = past, memory, Path(export_dir) if export_dir else None
        self.personas = personas                          # the personalities (tadamoo/personas.py PersonaStore): what they learned from a conversation goes too

    def _personal_memories(self) -> list:
        return [self.personas.memory(p["slug"]) for p in self.personas.list()] if self.personas is not None else []

    def keep_out(self, conversation: str) -> Dict[str, int]:
        """The conversation's pieces leave the index and its messages that wait to be learned from are dropped; notes already made stay."""
        return {"chunks": self.past.drop(conversation) if self.past is not None else 0,
                "queued": (self.memory.drop_queued(conversation) if self.memory is not None else 0) + sum(m.drop_queued(conversation) for m in self._personal_memories())}

    def forget(self, conversation: str) -> Dict[str, Any]:
        """Everything taken from the conversation: its pieces in the index, the messages waiting to be learned from and the notes learned only
        from it. If the memory was exported, the export is written again so that it holds none of them."""
        chunks = self.past.drop(conversation) if self.past is not None else 0
        got = self.memory.forget_conversation(conversation) if self.memory is not None else {"queued": 0, "notes": 0, "kept": 0, "ids": []}
        if self.memory is not None and self.export_dir is not None and (self.export_dir / "index.md").exists() and (got["notes"] or got["kept"]):
            export_markdown(self.memory, self.export_dir)
        for memory in self._personal_memories():                      # and the notes a personality wrote about it
            more = memory.forget_conversation(conversation)
            got = {k: got[k] + more[k] for k in ("queued", "notes", "kept")}
        return {"chunks": chunks, "queued": got["queued"], "notes": got["notes"], "kept": got["kept"]}
