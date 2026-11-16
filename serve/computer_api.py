"""GET and POST /api/computer, POST /api/computer/install: whether the agent can control this PC (screen, mouse, keyboard), which screen it works on, which backend does it
(this system's own, or agent-desktop) and the installing of agent-desktop (docs/computer-control.md).

Reading is for any client with the key; saving and installing only from this PC, because they let an agent act on the machine and download a program."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from tadamoo.computer import ComputerPrefs, pick_backend
from tadamoo.computer import install as installer
from tadamoo.computer.backend import BACKEND_CHOICES, Backend, ComputerError


def tools_dir_of(prefs: ComputerPrefs) -> Path:
    """Where agent-desktop lives: `tools/agent-desktop` beside computer.json, in the user's data folder."""
    return prefs.path.parent / "tools" / "agent-desktop"


def view(prefs: ComputerPrefs, backend: Optional[Callable[[], Backend]] = None, status: Callable[..., Dict[str, Any]] = installer.program_status) -> Dict[str, Any]:
    """The setting, whether this system can do it (and why not), the screens to choose from, and agent-desktop: whether it is installed, its version and its own status."""
    now = prefs.read()
    folder = tools_dir_of(prefs)
    found = installer.installed(folder)
    program: Dict[str, Any] = {**found, "release": installer.DOWNLOAD_PREFIX.rsplit("/releases", 1)[0], "status": None}
    if found["found"]:
        program["status"] = status([found["path"]], folder / "state")
    out: Dict[str, Any] = {**now, "supported": False, "reason": "", "monitors": [], "agent_desktop": program,
                           "in_use": "agent-desktop" if found["found"] and now["backend"] != "native" else "native"}
    try:
        b = (backend or (lambda: pick_backend(now["backend"], folder)))()
        out["reason"] = b.unavailable()
        out["supported"] = not out["reason"]
        if out["supported"]:
            out["monitors"] = [{"index": m.index, "width": m.width, "height": m.height, "primary": m.primary} for m in b.monitors()]
    except (ComputerError, OSError) as e:
        out["reason"] = str(e)
    return out


def save(prefs: ComputerPrefs, req: Dict[str, Any], backend: Optional[Callable[[], Backend]] = None, status: Callable[..., Dict[str, Any]] = installer.program_status) -> Dict[str, Any]:
    """`enabled` (a boolean), `monitor` (0 for all screens, else a screen number) and `backend` (auto, agent-desktop or native); what is left out stays."""
    now = prefs.read()
    enabled = req.get("enabled", now["enabled"])
    monitor = req.get("monitor", now["monitor"])
    choice = req.get("backend", now["backend"])
    if not isinstance(enabled, bool):
        raise ValueError("enabled is true or false")
    if isinstance(monitor, bool) or not isinstance(monitor, int) or monitor < 0:
        raise ValueError("monitor is 0 (all screens) or the number of a screen")
    if choice not in BACKEND_CHOICES:
        raise ValueError(f"backend is one of {', '.join(BACKEND_CHOICES)}")
    shown = view(prefs, backend, status)
    if monitor and shown["monitors"] and monitor > len(shown["monitors"]):
        raise ValueError(f"this PC has {len(shown['monitors'])} screen(s): choose 0 to {len(shown['monitors'])}")
    prefs.write(enabled, monitor, choice)
    return view(prefs, backend, status)


def install_program(prefs: ComputerPrefs, backend: Optional[Callable[[], Backend]] = None, **options: Any) -> Dict[str, Any]:
    """Installs agent-desktop from its GitHub release (see tadamoo/computer/install.py); the answer is the page's view with the result of the install."""
    result = installer.install(tools_dir_of(prefs), **options)
    return {**view(prefs, backend), "installed": result}
