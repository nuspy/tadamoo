"""The guard for programs the agent's commands start: see basalt_guard.py for what it covers and what it does not."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "launch.py"


def environment(places: Iterable[str], base: str = "") -> Dict[str, str]:
    """The variables that make Python programs, and the Python programs they start, refuse the `places`. Empty when there are none."""
    found = [p for p in places if p]
    if not found:
        return {}
    path = str(HERE) + (os.pathsep + base if base else "")
    return {"BASALT_GUARD": os.pathsep.join(found), "PYTHONPATH": path}


def deno_flags(places: Iterable[str]) -> List[str]:
    found = [p for p in places if p]
    return [f"--deny-read={','.join(found)}", f"--deny-write={','.join(found)}"] if found else []
