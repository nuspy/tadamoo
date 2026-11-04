"""Skill discovery (agentskills.io SKILL.md) and project instruction loading (AGENTS.md).

A skill in the search paths is the person's own: the model may load any of them.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FRONTMATTER_REGEX = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
GUIDELINE_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
MAX_GUIDELINE_BYTES = 32 * 1024


@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path
    content: str
    sha256: str = ""
    requires: List[str] = field(default_factory=list)     # programs the skill runs, from the `requires:` header
    env: List[str] = field(default_factory=list)          # environment variables it reads, from the `env:` header

    def missing(self) -> List[str]:
        """What the skill needs that this PC does not have: programs not on the path and variables not set."""
        return [*(b for b in self.requires if not shutil.which(b)), *(f"${v}" for v in self.env if not os.environ.get(v))]


def _names(value: str) -> List[str]:
    return [x.strip() for x in re.split(r"[,\s]+", value or "") if x.strip()]


def _not_ready(skill: SkillMeta) -> str:
    missing = skill.missing()
    return f" [not ready: missing {', '.join(missing)}]" if missing else ""


SKILL_DESCRIPTION_MAX = 60
SKILL_SECTIONS = ("when to use", "steps")


def check_skill_text(text: str) -> List[str]:
    """Everything wrong with the text of a skill a model wrote, all at once: name, short description, the fixed sections."""
    meta, body = parse_frontmatter(text)
    problems = []
    if not meta.get("name"):
        problems.append("the header needs a name")
    description = meta.get("description", "")
    if not description:
        problems.append("the header needs a description")
    elif len(description) > SKILL_DESCRIPTION_MAX:
        problems.append(f"the description is {len(description)} characters; the most is {SKILL_DESCRIPTION_MAX}")
    headings = {m.group(1).strip().lower() for m in re.finditer(r"^#+\s*(.+)$", body, re.M)}
    problems += [f"the section '{s}' is missing" for s in SKILL_SECTIONS if s not in headings]
    return problems


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


class SkillCatalog:
    def __init__(
        self,
        search_paths: Optional[List[str | Path]] = None,
    ) -> None:
        self.search_paths = [Path(p) for p in (search_paths or [])]
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
                    requires=_names(meta.get("requires", "")),
                    env=_names(meta.get("env", "")),
                )
        return self.skills

    def format_catalog_prompt(self) -> str:
        """Name and one line per skill, sorted so the prompt prefix never changes order."""
        return "\n".join(f"- **{self.skills[n].name}**: {self.skills[n].description}{_not_ready(self.skills[n])}" for n in sorted(self.skills))

    def body(self, name: str) -> str:
        """The full instructions of a skill."""
        skill = self.skills.get(name)
        if skill is None:
            raise KeyError(f"Unknown skill '{name}'. Available: {', '.join(sorted(self.skills)) or 'none'}")
        return skill.content if not skill.missing() else f"[this skill is not ready here: missing {', '.join(skill.missing())}]" + chr(10) + skill.content


def register_skill_tool(registry: Any, catalog: SkillCatalog) -> None:
    """Adds `load_skill`: the catalog stays in the prompt, a skill's full text arrives only when asked for."""
    from tadamoo.tools.registry import EffectClass

    registry.register(
        name="load_skill",
        description="Loads the full instructions of one of the available skills.",
        parameters={"type": "object", "properties": {"name": {"type": "string", "description": "Skill name"}},
                    "required": ["name"]},
        effect=EffectClass.PURE,
        handler=lambda name: catalog.body(name),
    )


def read_project_guidelines(workspace_dir: str | Path) -> Tuple[Optional[Path], str, int]:
    """(file, text, bytes cut off): AGENTS.md, else CLAUDE.md, else GEMINI.md, from the workspace upward to the repository root; the text
    is the first MAX_GUIDELINE_BYTES of the file."""
    here = Path(workspace_dir).resolve()
    for folder in [here, *here.parents]:
        for filename in GUIDELINE_FILES:
            target = folder / filename
            if target.is_file():
                data = target.read_bytes()
                return target, data[:MAX_GUIDELINE_BYTES].decode("utf-8", errors="replace"), max(0, len(data) - MAX_GUIDELINE_BYTES)
        if (folder / ".git").exists():
            break
    return None, "", 0


def guidelines_cut_notice(name: str, cut: int) -> str:
    """The line that tells the model a project file was read only in part."""
    return f"[{name} was cut at {MAX_GUIDELINE_BYTES // 1024} KiB; {cut} bytes ignored; read the file directly]"


def find_project_guidelines(workspace_dir: str | Path) -> Tuple[Optional[Path], str]:
    """The project's instruction file and its text, as far as it is read (see `read_project_guidelines`)."""
    path, text, _ = read_project_guidelines(workspace_dir)
    return path, text


def load_project_guidelines(workspace_dir: str | Path) -> str:
    return find_project_guidelines(workspace_dir)[1]
