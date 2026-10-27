"""Facts about the machine the agent's commands run on: the system, the shells, which Python command works, the working folder.

The probes run once and are remembered for as long as PATH stays the same. A Python name that only reaches a store stub (it prints
nothing, or not a version) is reported as not working.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PYTHON_NAMES = ("python", "python3", "py")
_PROBE_TIMEOUT = 5.0
_VERSION = re.compile(r"^Python\s+\d+\.\d+", re.I)
_lock = threading.Lock()
_probed: Dict[str, Dict[str, Tuple[str, str]]] = {}      # PATH -> {name: (path, version)} of the names that work


def is_windows() -> bool:
    return os.name == "nt"


def _probe(path: str) -> str:
    """The version line printed by `path --version`, or '' when it does not run or prints something else (a stub)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if is_windows() else 0
    try:
        done = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=_PROBE_TIMEOUT, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (done.stdout + done.stderr).decode("utf-8", errors="replace").strip()
    return text if done.returncode == 0 and _VERSION.match(text) else ""


def working_pythons() -> Dict[str, Tuple[str, str]]:
    """{command name: (executable path, version text)} for each of python, python3 and py that really runs Python."""
    key = os.environ.get("PATH", "")
    with _lock:
        if key not in _probed:
            found: Dict[str, Tuple[str, str]] = {}
            for name in PYTHON_NAMES:
                path = shutil.which(name)
                version = _probe(path) if path else ""
                if version:
                    found[name] = (path, version)
            _probed[key] = found
        return dict(_probed[key])


def forget_probes() -> None:
    with _lock:
        _probed.clear()


def pick_python(requested: str = "") -> Optional[Tuple[str, str]]:
    """(name, executable path) of the interpreter to run: the requested name when it works, else the first that does."""
    found = working_pythons()
    name = Path(requested).stem.lower() if requested else ""
    if name in found:
        return name, found[name][0]
    for name in PYTHON_NAMES:
        if name in found:
            return name, found[name][0]
    return None


def powershell_path() -> Optional[Tuple[str, str]]:
    """(label, path) of PowerShell: pwsh when present, else Windows PowerShell."""
    for exe, label in (("pwsh", "PowerShell 7 (pwsh)"), ("powershell", "Windows PowerShell (powershell.exe)")):
        path = shutil.which(exe)
        if path:
            return label, path
    return None


def os_description() -> str:
    if is_windows():
        build = getattr(sys, "getwindowsversion", lambda: None)()
        number = build.build if build else 0
        return f"Windows {11 if number >= 22000 else 10} (version {platform.version()})"
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]}"
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return f"{line.split('=', 1)[1].strip().strip(chr(34))} (kernel {platform.release()})"
    except OSError:
        pass
    return f"{platform.system()} {platform.release()}"


def python_sentence() -> str:
    found = working_pythons()
    if not found:
        return "No Python command (python, python3, py) runs on this machine."
    works = ", ".join(f"`{n}` ({v})" for n, (_, v) in found.items())
    missing = [n for n in PYTHON_NAMES if n not in found]
    return f"Python runs with {works}" + (f"; {', '.join(f'`{n}`' for n in missing)} {'does' if len(missing) == 1 else 'do'} not work." if missing else ".")


def environment_paragraph(workspace: str | Path, output_files: bool = False) -> str:
    """One paragraph of facts for the system prompt; `output_files` when write_output_file hands files to the person."""
    parts = [f"The system is {os_description()}."]
    if is_windows():
        shell = powershell_path()
        parts.append("shell_command runs commands with cmd.exe by default" +
                     (f"; with shell \"powershell\" it uses {shell[0]}." if shell else "; PowerShell was not found on this machine.") +
                     " A command of several lines is run as a script, line by line.")
    else:
        parts.append("shell_command runs commands with sh. A command of several lines is run as is.")
    parts.append(python_sentence())
    parts.append(f"The working folder is {Path(workspace).resolve()}; relative paths, in the file tools and in commands, are relative to it.")
    if output_files:
        parts.append("A file made with write_output_file is handed to the person and is not in the working folder; a file made with write_file "
                     "in the working folder can be read, counted and edited, and publish_output_file hands it to the person.")
    return " ".join(parts)


def similar_names(folder: Path, wanted: str, limit: int = 5) -> List[str]:
    """Names in `folder` close to `wanted` (case and spelling aside), for the message about a file that is not there."""
    import difflib
    try:
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in folder.iterdir())
    except OSError:
        return []
    base = Path(wanted.replace("\\", "/")).name
    plain = {n.rstrip("/").lower(): n for n in names}
    close = difflib.get_close_matches(base.lower(), list(plain), n=limit, cutoff=0.5)
    stem = Path(base).stem.lower()
    extra = [k for k in plain if stem and stem in k and k not in close]
    return [plain[k] for k in (close + extra)[:limit]]
