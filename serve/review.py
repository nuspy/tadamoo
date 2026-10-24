"""Review of the agent's file edits, one change block (hunk) at a time.

The agent applies an edit when it makes it; this lets the user keep a block or put the old lines back. A block
can only be undone while the file on disk is exactly what the agent wrote, so nothing the user did since is
overwritten. The diff shown and the blocks acted on come from the same grouping of lines, so block N in the
panel is block N here.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONTEXT = 3
MAX_TEXT = 200_000


class ReviewError(Exception):
    """A review that cannot be done, with a message fit for the person who asked."""


def _lines(text: str) -> List[str]:
    return text.splitlines(keepends=True)


def groups(old: str, new: str) -> List[List[Tuple[str, int, int, int, int]]]:
    return list(difflib.SequenceMatcher(None, _lines(old), _lines(new), autojunk=False).get_grouped_opcodes(CONTEXT))


def _range(start: int, stop: int) -> str:
    beginning, length = start + 1, stop - start
    if length == 1:
        return str(beginning)
    if not length:
        beginning -= 1
    return f"{beginning},{length}"


def unified(old: str, new: str, file: str) -> str:
    """A unified diff whose blocks are exactly `groups(old, new)`."""
    a, b = _lines(old), _lines(new)
    out: List[str] = []
    for group in groups(old, new):
        if not out:
            out += [f"--- a/{file}\n", f"+++ b/{file}\n"]
        first, last = group[0], group[-1]
        out.append(f"@@ -{_range(first[1], last[2])} +{_range(first[3], last[4])} @@\n")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out += [" " + line for line in a[i1:i2]]
                continue
            if tag in ("replace", "delete"):
                out += ["-" + line for line in a[i1:i2]]
            if tag in ("replace", "insert"):
                out += ["+" + line for line in b[j1:j2]]
    return "".join(line if line.endswith("\n") else line + "\n" for line in out)


def counts(diff: str) -> Tuple[int, int]:
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return added, removed


def fingerprint(old: str, new: str, group: List[Tuple[str, int, int, int, int]]) -> str:
    a, b = _lines(old), _lines(new)
    first, last = group[0], group[-1]
    h = hashlib.sha1()
    h.update("".join(a[first[1]:last[2]]).encode("utf-8"))
    h.update(b"\0")
    h.update("".join(b[first[3]:last[4]]).encode("utf-8"))
    return h.hexdigest()[:16]


def revert(old: str, new: str, index: int) -> str:
    """`new` with block `index` put back as in `old`."""
    gs = groups(old, new)
    if not 0 <= index < len(gs):
        raise ReviewError(f"There is no block {index + 1} in this change.")
    a, b = _lines(old), _lines(new)
    for tag, i1, i2, j1, j2 in reversed(gs[index]):
        if tag != "equal":
            b[j1:j2] = a[i1:i2]
    return "".join(b)


def usable(review: Any, kind: str) -> Optional[Dict[str, str]]:
    """The review data of an edit if it can be reviewed, else None (text too large, not UTF-8, not a plain edit)."""
    if not isinstance(review, dict) or kind not in ("added", "modified"):
        return None
    root, old, new = review.get("root"), review.get("old_text"), review.get("new_text")
    if not (isinstance(root, str) and root and isinstance(old, str) and isinstance(new, str)):
        return None
    if len(old) > MAX_TEXT or len(new) > MAX_TEXT or "�" in old or "�" in new:
        return None
    return {"root": root[:1000], "old": old, "current": new}


def safe_path(root: str, file: str) -> Path:
    base = Path(root)
    if not base.is_dir():
        raise ReviewError("The folder the agent worked in is not there any more.")
    target = (base / file).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        raise ReviewError("That file is outside the folder the agent worked in.") from None
    return target


def read(path: Path) -> Optional[str]:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReviewError(f"The file cannot be read: {exc.strerror or exc}") from None


def write(path: Path, text: str) -> None:
    """Replaces the file through a temporary one; the text is written as it is, newlines included."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".basalt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ReviewError(f"The file cannot be written: {exc.strerror or exc}") from None
