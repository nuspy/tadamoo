"""The person's skills: a folder each in the user data folder, `personal-skills/<name>/skill.json` and `skill.py`.

A skill the person makes, edits or imports in the app is theirs and usable at once; nothing is approved by hash and an edit never
withdraws anything. A skill moves to another PC as one `.pskill` file (a zip with the two files and a manifest), which `import_file`
checks and registers."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import check as script_check
from .values import check_spec

FORMAT = "basalt-personal-skill"
SPEC, SCRIPT = "skill.json", "skill.py"
VERSIONS_KEPT = 10
MAX_FILE = 300_000
MAX_NAME = 60
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class SkillError(ValueError):
    """What is wrong with a skill, or why it cannot be done; the message is for the person."""


def slugify(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", name.lower().strip().encode("ascii", "ignore").decode() or "skill").strip("-")
    return (text or "skill")[:40].strip("-") or "skill"


def digest(spec: Dict[str, Any], script: str) -> str:
    """A hash of the definition and the script: the manifest of an exported skill carries it."""
    body = {k: v for k, v in spec.items() if k not in ("updated", "version", "uses", "last_run", "created", "source")}
    return hashlib.sha256((json.dumps(body, sort_keys=True, ensure_ascii=False) + "\n--\n" + script).encode("utf-8")).hexdigest()


class PersonalSkills:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- reading -----------------------------------------------------------------------------------------

    def _folder(self, slug: str) -> Path:
        if not SLUG.match(str(slug)):
            raise SkillError("There is no skill with that name.")
        return self.dir / slug

    def _read(self, slug: str) -> Optional[Dict[str, Any]]:
        folder = self._folder(slug)
        try:
            spec = json.loads((folder / SPEC).read_text(encoding="utf-8"))
            script = (folder / SCRIPT).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        if not isinstance(spec, dict):
            return None
        spec["slug"] = slug
        spec["script"] = script
        spec["status"] = "approved"
        spec["problems"] = script_check.check(script)
        return spec

    def get(self, slug: str) -> Dict[str, Any]:
        found = self._read(slug)
        if found is None:
            raise SkillError("There is no skill with that name.")
        return found

    def list(self) -> List[Dict[str, Any]]:
        out = []
        for folder in sorted(p for p in self.dir.iterdir() if p.is_dir() and SLUG.match(p.name)):
            found = self._read(folder.name)
            if found:
                out.append({k: v for k, v in found.items() if k != "script"})
        return sorted(out, key=lambda s: str(s.get("name", "")).lower())

    # -- writing -----------------------------------------------------------------------------------------

    def _validate(self, spec: Dict[str, Any], script: str) -> Dict[str, Any]:
        name = " ".join(str(spec.get("name", "")).split())
        if not name or len(name) > MAX_NAME:
            raise SkillError(f"Give the skill a name of up to {MAX_NAME} characters.")
        params = spec.get("params") or []
        if not isinstance(params, list) or len(params) > 40:
            raise SkillError("A skill has up to 40 parameters.")
        names = [p.get("name") if isinstance(p, dict) else None for p in params]
        if len(set(names)) != len(names):
            raise SkillError("Two parameters have the same name.")
        for p in params:
            problems = check_spec(p)
            if problems:
                raise SkillError("; ".join(problems))
        problems = script_check.check(script)
        if problems:
            raise SkillError("The script cannot be saved: " + "; ".join(f"line {p['line']}: {p['message']}" for p in problems[:5]))
        clean_params = [{k: v for k, v in p.items() if k in ("name", "label", "explanation", "type", "default", "required", "choices", "item_type", "format")} for p in params]
        return {"name": name, "description": " ".join(str(spec.get("description", "")).split())[:500], "params": clean_params,
                "tools": script_check.tools_used(script), "timeout": _timeout(spec.get("timeout"))}

    def save(self, spec: Dict[str, Any], script: str, slug: Optional[str] = None) -> Dict[str, Any]:
        """Saves a new skill, or a new version of `slug` (the old one is kept)."""
        body = self._validate(spec, script)
        with self._lock:
            if slug is None:
                slug = self._free_slug(slugify(body["name"]))
            folder = self._folder(slug)
            old = self._read(slug) if folder.exists() else None
            now = time.time()
            meta = {**body, "created": (old or {}).get("created", now), "updated": now, "version": int((old or {}).get("version", 0)) + 1,
                    "uses": int((old or {}).get("uses", 0)), "last_run": (old or {}).get("last_run"), "source": spec.get("source") or (old or {}).get("source")}
            if old is not None:
                self._keep_version(folder, old)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / SCRIPT).write_text(script, encoding="utf-8")
            (folder / SPEC).write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
        return self.get(slug)

    def _free_slug(self, base: str) -> str:
        slug, n = base, 2
        while (self.dir / slug).exists():
            slug, n = f"{base[:36]}-{n}", n + 1
        return slug

    def _keep_version(self, folder: Path, old: Dict[str, Any]) -> None:
        target = folder / ".versions" / str(old.get("version", 0))
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(folder / SCRIPT, target / SCRIPT)
        shutil.copy2(folder / SPEC, target / SPEC)
        kept = sorted((p for p in (folder / ".versions").iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name))
        for extra in kept[:-VERSIONS_KEPT]:
            shutil.rmtree(extra, ignore_errors=True)

    def versions(self, slug: str) -> List[int]:
        folder = self._folder(slug) / ".versions"
        return sorted(int(p.name) for p in folder.iterdir() if p.is_dir() and p.name.isdigit()) if folder.is_dir() else []

    def restore(self, slug: str, version: int) -> Dict[str, Any]:
        """Goes back to an earlier version; it is saved as a new one."""
        folder = self._folder(slug) / ".versions" / str(int(version))
        try:
            spec = json.loads((folder / SPEC).read_text(encoding="utf-8"))
            script = (folder / SCRIPT).read_text(encoding="utf-8")
        except (OSError, ValueError):
            raise SkillError("That version is not kept.") from None
        return self.save(spec, script, slug=slug)

    def delete(self, slug: str) -> None:
        folder = self._folder(slug)
        if not folder.is_dir():
            raise SkillError("There is no skill with that name.")
        with self._lock:
            shutil.rmtree(folder, ignore_errors=True)

    def note_run(self, slug: str) -> None:
        """Counts a run; it is not a change of the skill."""
        with self._lock:
            found = self._read(slug)
            if found is None:
                return
            folder = self._folder(slug)
            spec = json.loads((folder / SPEC).read_text(encoding="utf-8"))
            spec["uses"] = int(spec.get("uses", 0)) + 1
            spec["last_run"] = time.time()
            (folder / SPEC).write_text(json.dumps(spec, indent=1, ensure_ascii=False), encoding="utf-8")

    # -- moving a skill to another PC ---------------------------------------------------------------------

    def export_bytes(self, slug: str) -> bytes:
        """One `.pskill` file: the definition (without where it came from or how often it ran), the script and a manifest with their hash."""
        found = self.get(slug)
        spec = {k: v for k, v in spec_without(found).items() if k not in ("source", "uses", "last_run", "created", "updated", "version")}
        spec_text = json.dumps(spec, indent=1, ensure_ascii=False)
        manifest = {"format": FORMAT, "version": 1, "name": spec["name"], "sha256": digest(spec, found["script"]), "exported": time.time()}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
            z.writestr(SPEC, spec_text)
            z.writestr(SCRIPT, found["script"])
        return buffer.getvalue()

    def import_bytes(self, data: bytes) -> Dict[str, Any]:
        """Registers the skill in a `.pskill` file. A file that is damaged, was changed after it was made, or holds
        anything but the two files is refused."""
        if len(data) > 2 * MAX_FILE:
            raise SkillError("That file is too large to be a skill.")
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise SkillError("That is not a personal skill file.") from None
        with z:
            names = z.namelist()
            if sorted(names) != sorted(["manifest.json", SPEC, SCRIPT]) or any(i.file_size > MAX_FILE for i in z.infolist()):
                raise SkillError("That is not a personal skill file (it holds something else, or too much).")
            try:
                manifest = json.loads(z.read("manifest.json"))
                spec = json.loads(z.read(SPEC))
                script = z.read(SCRIPT).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise SkillError("That file is damaged.") from None
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or not isinstance(spec, dict):
            raise SkillError("That is not a personal skill file.")
        if manifest.get("sha256") != digest(spec_without(spec), script):
            raise SkillError("That file was changed after it was made, or is damaged: its content does not match its hash.")
        return self.save(spec, script)

    def import_file(self, path: str | Path) -> Dict[str, Any]:
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise SkillError(f"The file could not be read: {exc}") from None
        return self.import_bytes(data)


def _timeout(value: Any) -> int:
    """The seconds a run may take; 0 is no limit."""
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        raise SkillError("The time limit is a number of seconds.") from None


def spec_without(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The definition alone: the parts of what `get` returns that the hash covers."""
    return {k: v for k, v in spec.items() if k not in ("script", "slug", "status", "problems")}
