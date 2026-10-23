"""Skill discovery and project instruction loader (agentskills.io and AGENTS.md)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


FRONTMATTER_REGEX = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path
    content: str


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    meta: Dict[str, str] = {}
    body = text
    match = FRONTMATTER_REGEX.match(text)
    if match:
        raw_meta = match.group(1)
        body = text[match.end():]
        for line in raw_meta.splitlines():
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, body


class SkillCatalog:
    def __init__(self, search_paths: Optional[List[str | Path]] = None) -> None:
        self.search_paths = [Path(p) for p in (search_paths or [])]
        self.skills: Dict[str, SkillMeta] = {}

    def discover_skills(self) -> Dict[str, SkillMeta]:
        for base in self.search_paths:
            if not base.is_dir():
                continue
            for skill_file in base.rglob("SKILL.md"):
                try:
                    text = skill_file.read_text(encoding="utf-8", errors="replace")
                    meta, body = parse_frontmatter(text)
                    name = meta.get("name") or skill_file.parent.name
                    desc = meta.get("description") or body.strip().splitlines()[0][:100]
                    self.skills[name] = SkillMeta(
                        name=name,
                        description=desc,
                        path=skill_file,
                        content=body.strip(),
                    )
                except Exception:
                    pass
        return self.skills

    def format_catalog_prompt(self) -> str:
        if not self.skills:
            return ""
        lines = []
        for name in sorted(self.skills.keys()):
            s = self.skills[name]
            lines.append(f"- **{s.name}**: {s.description}")
        return "\n".join(lines)


def load_project_guidelines(workspace_dir: str | Path) -> str:
    """Finds and loads AGENTS.md, or falls back to CLAUDE.md or GEMINI.md."""
    ws = Path(workspace_dir)
    for filename in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        target = ws / filename
        if target.is_file():
            return target.read_text(encoding="utf-8", errors="replace")
    return ""
