"""Three-way merge of a skill folder update: the person's own edits on the installed copy, against a new version from upstream, using the original
download (kept at install time, in the originals folder) as the common base when there is one.

Without a base (the originals folder was never set, or this install predates it), there is no way to tell "only upstream changed this file" apart
from "only the person changed it": a difference between the installed copy and the new version is then always a conflict, left for the person to
choose, never guessed."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

SAME = "same"              # nobody effectively changed it (the installed copy already matches the new version)
UPSTREAM = "upstream"      # only upstream changed it (added, changed or removed it); the new version is taken
KEPT = "kept"              # only the person changed it (added, changed or removed it); their copy stands
CONFLICT = "conflict"      # both changed it, differently: the person chooses which one stands
OUTCOMES = (SAME, UPSTREAM, KEPT, CONFLICT)
IGNORE = {".tadamoo-source.json"}          # the installer's own provenance marker, never fetched from upstream and rewritten by it after every merge: not the skill's content


def _tree(folder: Optional[Path]) -> Dict[str, bytes]:
    if folder is None or not folder.is_dir():
        return {}
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file() and p.name not in IGNORE}


def plan(base: Optional[Path], ours: Path, theirs: Path) -> Dict[str, str]:
    """`{relative path: outcome}`, over every file any of the three trees has. `base` is `None` when there is none to compare against."""
    b, o, t = _tree(base), _tree(ours), _tree(theirs)
    out: Dict[str, str] = {}
    for path in sorted(set(b) | set(o) | set(t)):
        bv, ov, tv = b.get(path), o.get(path), t.get(path)
        if ov == tv:
            out[path] = SAME
        elif base is None:
            out[path] = CONFLICT
        elif ov == bv:
            out[path] = UPSTREAM
        elif tv == bv:
            out[path] = KEPT
        else:
            out[path] = CONFLICT
    return out


def changed_text(result: Dict[str, str], theirs: Path, limit: int = 8) -> Dict[str, str]:
    """The new content of the files an update would actually add or change (`upstream` and `conflict`), as text, for a risk review to read. At most
    `limit` files, each clipped; a file the new version no longer has (a removal) is left out, there being nothing new to read."""
    t = _tree(theirs)
    out: Dict[str, str] = {}
    for path, outcome in result.items():
        if outcome in (UPSTREAM, CONFLICT) and path in t:
            out[path] = t[path].decode("utf-8", "replace")[:1500]
        if len(out) >= limit:
            break
    return out


def apply(result: Dict[str, str], ours: Path, theirs: Path, resolutions: Dict[str, str]) -> None:
    """Writes `ours` in place to match the plan: `upstream`, and a conflict resolved `"upstream"`, copy the file from `theirs` (or remove it, when
    `theirs` no longer has it); `kept`, and a conflict resolved `"kept"`, are left exactly as they are; `same` needs nothing. Every conflict in
    `result` must have a resolution of `"upstream"` or `"kept"` in `resolutions`, checked by the caller before this runs."""
    t = _tree(theirs)
    for path, outcome in result.items():
        choice = resolutions.get(path, outcome) if outcome == CONFLICT else outcome
        if choice == UPSTREAM:
            dest = ours / path
            if path in t:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(t[path])
            elif dest.is_file():
                dest.unlink()
