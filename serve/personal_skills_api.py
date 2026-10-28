"""The page's side of the personal skills: the list, one skill, saving, deleting, moving as a `.pskill` file, and the
live reading of a form (what a date written in words will be). The skills themselves are in harness/personal_skills/."""
from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from harness.personal_skills import check as script_check
from harness.personal_skills.store import PersonalSkills, SkillError
from harness.personal_skills.values import TYPES, ITEM_TYPES, ValueError_, parse_value


class PersonalSkillsApi:
    def __init__(self, directory: str | Path) -> None:
        self.store = PersonalSkills(directory)

    def overview(self) -> Dict[str, Any]:
        return {"skills": self.store.list(), "types": list(TYPES), "item_types": list(ITEM_TYPES)}

    def get(self, slug: str) -> Dict[str, Any]:
        found = self.store.get(slug)
        found["versions"] = self.store.versions(slug)
        return {"skill": found}

    def save(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """{"skill": {...definition}, "script", "slug"?}"""
        spec, script = req.get("skill"), req.get("script")
        if not isinstance(spec, dict) or not isinstance(script, str):
            raise SkillError("Send the skill's definition and its script.")
        slug = req.get("slug")
        if slug is not None and not isinstance(slug, str):
            raise SkillError("There is no skill with that name.")
        return {"skill": self.store.save(spec, script, slug=slug)}

    def delete(self, slug: str) -> Dict[str, Any]:
        self.store.delete(slug)
        return {"deleted": slug}

    def restore(self, slug: str, version: Any) -> Dict[str, Any]:
        if not isinstance(version, int) or isinstance(version, bool):
            raise SkillError("Say which version, as a number.")
        return {"skill": self.store.restore(slug, version)}

    def export(self, slug: str) -> bytes:
        return self.store.export_bytes(slug)

    def import_file(self, data: Any) -> Dict[str, Any]:
        """The content of a `.pskill` file, in base64."""
        try:
            raw = base64.b64decode(data, validate=True) if isinstance(data, str) else None
        except ValueError:
            raw = None
        if raw is None:
            raise SkillError("Send the file's content in base64.")
        return {"skill": self.store.import_bytes(raw)}

    def check(self, script: Any) -> Dict[str, Any]:
        if not isinstance(script, str):
            raise SkillError("Send the script.")
        return {"problems": script_check.check(script), "tools": script_check.tools_used(script)}

    def preview(self, params: Any, given: Any) -> Dict[str, Any]:
        """What each value of a form will be, as the person types it: a date in words becomes the day it means now."""
        if not isinstance(params, list) or not isinstance(given, dict):
            raise SkillError("Send the parameters and the values.")
        now = datetime.now()
        shown: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        for spec in params:
            if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
                continue
            name = spec["name"]
            raw = given.get(name)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            try:
                value = parse_value(spec, raw, now)
                shown[name] = value
            except ValueError_ as exc:
                errors[name] = str(exc)
        return {"values": shown, "errors": errors}


def describe(skill: Dict[str, Any], values: Dict[str, Any]) -> str:
    """The person's request as the conversation shows it: the skill and the values it was started with."""
    parts: List[str] = []
    for spec in skill.get("params") or []:
        item = values.get(spec["name"])
        if item is None or item.get("value") in (None, ""):
            continue
        v = item["value"]
        parts.append(f"{spec.get('label') or spec['name']} = " + (", ".join(str(x) for x in v) if isinstance(v, list) else str(v)))
    text = f"Skill “{skill['name']}”"
    return text + (": " + "; ".join(parts) if parts else "")
