"""What the Excel, PowerPoint and PDF tools share: finding the file in the workspace, the checks every writing tool makes, and a write that
cannot leave a half-written file behind."""
from __future__ import annotations

import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

MAX_ERRORS = 20


class OfficeError(ValueError):
    """A request the tool cannot carry out; the text is what the model is told."""


ATTACHMENT = "attachment:"


def attached(sandbox: Any, path: str) -> Optional[tuple]:
    """(the stored file, its own name) for `attachment:<id>`, a file the person attached; None for any other path. The stored file is only read:
    an edit of it writes a copy in the workspace."""
    if not str(path).startswith(ATTACHMENT):
        return None
    find = getattr(sandbox, "attachment_files", None)
    if find is None:
        raise OfficeError("No file is attached to this conversation.")
    try:
        return find(str(path)[len(ATTACHMENT):].strip())
    except (KeyError, OSError):
        raise FileNotFoundError(f"There is no attachment '{str(path)[len(ATTACHMENT):].strip()}'; list_attachments gives the ids.") from None


def source_of(sandbox: Any, path: str, tool: str) -> Path:
    """The file a tool opens: an attached file by `attachment:<id>`, else a path of the workspace."""
    found = attached(sandbox, path)
    return found[0] if found else sandbox.resolve_safe(path, tool)


def existing(sandbox: Any, path: str, tool: str, suffixes: Iterable[str], what: str) -> Path:
    safe = source_of(sandbox, path, tool)
    if safe.suffix.lower() not in tuple(suffixes):
        raise OfficeError(f"'{path}' is not {what}.")
    if not safe.is_file():
        raise FileNotFoundError(f"File not found: {path}.{sandbox._where(path, safe)}")
    return safe


COPY = re.compile(r"\.edited\d*$")


def copy_for(sandbox: Any, tool: str, path: str, output: Optional[str]) -> str:
    """The file a writing tool writes. The original is never written: `output` names the copy, else it is `name.edited.ext` (`name.edited2.ext`
    and on when that exists). A file that is itself such a copy is the working copy and is written in place (the empty string)."""
    if output:
        return output
    found = attached(sandbox, path)
    p = Path(Path(found[1]).name) if found else Path(path)      # a copy of an attached file goes in the workspace under its own name
    if COPY.search(p.stem) and not found:
        return ""
    for k in range(100):
        name = str(p.with_name(f"{p.stem}.edited{'' if k == 0 else k + 1}{p.suffix}")).replace("\\", "/")
        if not sandbox.resolve_safe(name, tool).exists():
            return name
    raise OfficeError("too many edited copies; name the output")


def copy_note(output: str) -> str:
    """What the answer says about where the result is."""
    return (f"The original is as it was; the result is the copy '{output}'. Go on with that copy (path='{output}') and, when it is done, "
            "offer it to the person with publish_output_file." if output else
            "This file is a working copy made by an earlier edit; the original is as it was.")


def target_of(sandbox: Any, path: str, output: Optional[str], tool: str, suffix: str, exists_ok: bool) -> Path:
    """Where the result goes: `output` when given (never over a file that exists unless `exists_ok`), else the file itself (a working copy)."""
    if not output:
        return sandbox.resolve_safe(path, tool)
    safe = sandbox.resolve_safe(output, tool)
    if safe.suffix.lower() != suffix:
        raise OfficeError(f"'{output}' must end in {suffix}.")
    if safe.is_dir():
        raise IsADirectoryError(f"'{output}' is a folder, not a file.")
    if safe.exists() and not exists_ok:
        raise OfficeError(f"'{output}' already exists. Choose another name, or send overwrite=true.")
    return safe


def guard(sandbox: Any, tool: str, path: str, safe: Path) -> Optional[Any]:
    """The refusals every writing tool makes: a file another session owns, and a git-protected one. Returns the refusal, or None."""
    blocked = sandbox._owner_block(path, safe, append=False)
    if blocked:
        return blocked
    sandbox._guard_git(tool, path, safe)
    return None


def atomic_write(safe: Path, write: Callable[[str], None], verify: Callable[[str], None]) -> None:
    """Writes to a temporary file beside the target, checks that it opens, then replaces the target; any failure leaves the target as it was."""
    safe.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=safe.parent, prefix=".tadamoo-", suffix=safe.suffix)   # the readers go by the extension
    os.close(fd)
    try:
        write(tmp)
        verify(tmp)
        if safe.exists():
            os.chmod(tmp, safe.stat().st_mode & 0o777)
        os.replace(tmp, safe)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def zip_names(path: Path) -> List[str]:
    try:
        with zipfile.ZipFile(path) as z:
            return z.namelist()
    except (zipfile.BadZipFile, OSError):
        return []


def report_problems(problems: List[str], total: int, hint: str = "") -> str:
    shown = problems[:MAX_ERRORS] + ([f"... and {len(problems) - MAX_ERRORS} more"] if len(problems) > MAX_ERRORS else [])
    return f"Nothing was changed: {len(problems)} of {total} edits cannot be applied; the file is as it was.\n- " + "\n- ".join(shown) + (("\n" + hint) if hint else "")
