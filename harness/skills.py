"""Skill discovery (agentskills.io SKILL.md) and project instruction loading (AGENTS.md).

A skill written or changed by anyone but the user is code the model will follow, so a catalog can
require approval: the approval is stored with the hash of the text the user read, and any change to
the file withdraws it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FRONTMATTER_REGEX = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
GUIDELINE_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
MAX_GUIDELINE_BYTES = 32 * 1024


class SkillNotApproved(PermissionError):
    """The skill has not been approved in its current form."""


@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path
    content: str
    sha256: str = ""


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Reads the `key: value` header of a SKILL.md; indented lines continue the previous value."""
    meta: Dict[str, str] = {}
    match = FRONTMATTER_REGEX.match(text)
    if not match:
        return meta, text
    key = ""
    for line in match.group(1).splitlines():
        if line[:1] in (" ", "\t") and key:
            meta[key] = (meta[key] + " " + line.strip()).strip()
        elif ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            value = v.strip().strip("\"'")
            meta[key] = "" if value in (">", ">-", "|", "|-") else value
    return meta, text[match.end():]


class ApprovalStore:
    """Skill name -> hash of the text the user approved, kept in a JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> Dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def is_approved(self, skill: SkillMeta) -> bool:
        return self._load().get(skill.name) == skill.sha256

    def approve(self, skill: SkillMeta) -> None:
        data = self._load()
        data[skill.name] = skill.sha256
        self._save(data)

    def revoke(self, name: str) -> None:
        data = self._load()
        if data.pop(name, None) is not None:
            self._save(data)


class SkillCatalog:
    def __init__(
        self,
        search_paths: Optional[List[str | Path]] = None,
        approvals: Optional[ApprovalStore] = None,
        require_approval: bool = False,
    ) -> None:
        self.search_paths = [Path(p) for p in (search_paths or [])]
        self.approvals = approvals
        self.require_approval = require_approval
        self.skills: Dict[str, SkillMeta] = {}
        self.warnings: List[str] = []

    def discover_skills(self) -> Dict[str, SkillMeta]:
        """Rescans the search paths in order; the first skill of a name wins and a clash is reported."""
        self.skills, self.warnings = {}, []
        for base in self.search_paths:
            if not base.is_dir():
                continue
            for skill_file in sorted(base.rglob("SKILL.md")):
                try:
                    text = skill_file.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    self.warnings.append(f"{skill_file}: unreadable ({exc})")
                    continue
                meta, body = parse_frontmatter(text)
                name = meta.get("name") or skill_file.parent.name
                first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
                if name in self.skills:
                    self.warnings.append(f"{skill_file}: skill '{name}' already defined by {self.skills[name].path}")
                    continue
                self.skills[name] = SkillMeta(
                    name=name,
                    description=meta.get("description") or first_line[:100],
                    path=skill_file,
                    content=body.strip(),
                    sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
        return self.skills

    def format_catalog_prompt(self) -> str:
        """Name and one line per skill, sorted so the prompt prefix never changes order."""
        return "\n".join(f"- **{self.skills[n].name}**: {self.skills[n].description}" for n in sorted(self.skills))

    def review(self, name: str) -> Dict[str, Any]:
        """What the user must read before approving a skill."""
        skill = self.skills[name]
        return {"name": name, "path": str(skill.path), "sha256": skill.sha256, "content": skill.content,
                "approved": bool(self.approvals and self.approvals.is_approved(skill))}

    def approve(self, name: str) -> None:
        if not self.approvals:
            raise RuntimeError("this catalog has no approval store")
        self.approvals.approve(self.skills[name])

    def body(self, name: str) -> str:
        """The full instructions of a skill; refused when approval is required and missing or stale."""
        skill = self.skills.get(name)
        if skill is None:
            raise KeyError(f"Unknown skill '{name}'. Available: {', '.join(sorted(self.skills)) or 'none'}")
        if self.require_approval and not (self.approvals and self.approvals.is_approved(skill)):
            raise SkillNotApproved(f"Skill '{name}' has not been approved in its current form")
        return skill.content


def register_skill_tool(registry: Any, catalog: SkillCatalog) -> None:
    """Adds `load_skill`: the catalog stays in the prompt, a skill's full text arrives only when asked for."""
    from harness.tools.registry import EffectClass

    registry.register(
        name="load_skill",
        description="Loads the full instructions of one of the available skills.",
        parameters={"type": "object", "properties": {"name": {"type": "string", "description": "Skill name"}},
                    "required": ["name"]},
        effect=EffectClass.PURE,
        handler=lambda name: catalog.body(name),
    )


def find_project_guidelines(workspace_dir: str | Path) -> Tuple[Optional[Path], str]:
    """AGENTS.md, else CLAUDE.md, else GEMINI.md: from the workspace upward to the repository root."""
    here = Path(workspace_dir).resolve()
    for folder in [here, *here.parents]:
        for filename in GUIDELINE_FILES:
            target = folder / filename
            if target.is_file():
                data = target.read_bytes()[:MAX_GUIDELINE_BYTES]
                return target, data.decode("utf-8", errors="replace")
        if (folder / ".git").exists():
            break
    return None, ""


def load_project_guidelines(workspace_dir: str | Path) -> str:
    return find_project_guidelines(workspace_dir)[1]
