"""GET and POST /api/computer: whether the agent can control this PC (screen, mouse, keyboard) and which screen it works on (docs/computer-control.md).

Reading is for any client with the key; saving only from this PC, because it lets an agent act on the machine."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from tadamoo.computer import ComputerPrefs, pick_backend
from tadamoo.computer.backend import Backend, ComputerError


def view(prefs: ComputerPrefs, backend: Optional[Callable[[], Backend]] = None) -> Dict[str, Any]:
    """The setting, whether this system can do it (and why not), and the screens to choose from."""
    out: Dict[str, Any] = {**prefs.read(), "supported": False, "reason": "", "monitors": []}
    try:
        b = (backend or pick_backend)()
        out["reason"] = b.unavailable()
        out["supported"] = not out["reason"]
        if out["supported"]:
            out["monitors"] = [{"index": m.index, "width": m.width, "height": m.height, "primary": m.primary} for m in b.monitors()]
    except (ComputerError, OSError) as e:
        out["reason"] = str(e)
    return out


def save(prefs: ComputerPrefs, req: Dict[str, Any], backend: Optional[Callable[[], Backend]] = None) -> Dict[str, Any]:
    """`enabled` (a boolean) and `monitor` (0 for all screens, else a screen number); what is left out stays."""
    now = prefs.read()
    enabled = req.get("enabled", now["enabled"])
    monitor = req.get("monitor", now["monitor"])
    if not isinstance(enabled, bool):
        raise ValueError("enabled is true or false")
    if isinstance(monitor, bool) or not isinstance(monitor, int) or monitor < 0:
        raise ValueError("monitor is 0 (all screens) or the number of a screen")
    shown = view(prefs, backend)
    if monitor and shown["monitors"] and monitor > len(shown["monitors"]):
        raise ValueError(f"this PC has {len(shown['monitors'])} screen(s): choose 0 to {len(shown['monitors'])}")
    prefs.write(enabled, monitor)
    return view(prefs, backend)
