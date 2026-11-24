"""What the Excel, PowerPoint and PDF tools share: finding the file in the workspace, the checks every writing tool makes, and a write that
cannot leave a half-written file behind."""
from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

MAX_ERRORS = 20


class OfficeError(ValueError):
    """A request the tool cannot carry out; the text is what the model is told."""


def existing(sandbox: Any, path: str, tool: str, suffixes: Iterable[str], what: str) -> Path:
    safe = sandbox.resolve_safe(path, tool)
    if safe.suffix.lower() not in tuple(suffixes):
        raise OfficeError(f"'{path}' is not {what}.")
    if not safe.is_file():
        raise FileNotFoundError(f"File not found: {path}.{sandbox._where(path, safe)}")
    return safe


def target_of(sandbox: Any, path: str, output: Optional[str], tool: str, suffix: str, exists_ok: bool) -> Path:
    """Where the result goes: `output` when given (never over a file that exists unless `exists_ok`), else the file itself."""
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
