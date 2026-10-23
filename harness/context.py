"""Deterministic context assembly and TokenJuice compactor.

Maintains a byte-stable, append-only prompt prefix to maximize Basalt prefix cache hits.
Implements surgical masking (keeps first 15 and last 20 lines) for long outputs.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class TokenJuiceCompactor:
    """Surgically masks large tool outputs to conserve context while keeping head and tail."""

    def __init__(
        self,
        artifacts_dir: str | Path,
        collapse_threshold_lines: int = 40,
        keep_head_lines: int = 15,
        keep_tail_lines: int = 20,
    ) -> None:
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.collapse_threshold = collapse_threshold_lines
        self.keep_head = keep_head_lines
        self.keep_tail = keep_tail_lines

    def compress_output(self, output: str, tool_name: str = "output") -> str:
        lines = output.splitlines()
        if len(lines) <= self.collapse_threshold:
            return output

        # Save full raw output to disk
        art_id = f"{tool_name}_{int(time.time() * 1000)}"
        art_path = self.artifacts_dir / f"{art_id}.txt"
        art_path.write_text(output, encoding="utf-8")

        head = lines[:self.keep_head]
        tail = lines[-self.keep_tail:]
        hidden_count = len(lines) - (self.keep_head + self.keep_tail)

        placeholder = f"\n[... {hidden_count} lines collapsed; saved to {art_path.name} ...]\n"
        return "\n".join(head) + placeholder + "\n".join(tail)


class ContextBuilder:
    """Assembles prompt messages in a byte-stable order to maximize prefix caching.

    Order:
      1. system_core (fixed agent instructions)
      2. project_rules (AGENTS.md content)
      3. skills_catalog (available tools and capabilities)
      4. memory_context (relevant retrieved memories)
      5. conversation (turns)
    """

    def __init__(self, system_core: str = "You are Basalt, an autonomous coding agent.") -> None:
        self.system_core = system_core.strip()
        self.project_rules = ""
        self.skills_catalog = ""
        self.memories: List[str] = []

    def set_project_rules(self, rules: str) -> None:
        self.project_rules = rules.strip()

    def set_skills_catalog(self, catalog: str) -> None:
        self.skills_catalog = catalog.strip()

    def set_memories(self, memories: List[str]) -> None:
        self.memories = list(memories)

    def assemble_system_message(self) -> str:
        sections = [self.system_core]

        if self.project_rules:
            sections.append(f"# Project Rules & Guidelines\n{self.project_rules}")

        if self.skills_catalog:
            sections.append(f"# Available Skills\n{self.skills_catalog}")

        if self.memories:
            mem_text = "\n".join(f"- {m}" for m in self.memories)
            sections.append(f"# Project Memory & Context\n{mem_text}")

        return "\n\n".join(sections)

    def build_messages(self, conversation_history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        system_content = self.assemble_system_message()
        return [{"role": "system", "content": system_content}] + list(conversation_history)
