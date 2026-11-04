"""The adapters, one module per benchmark. Each module exposes `ADAPTER`, a class that implements the runner's Adapter protocol (runner.py).

The table below maps a benchmark id to its module. A module is imported only when its adapter is asked for, so a missing module (or a missing
optional package inside it) makes that one adapter unavailable and nothing else."""
from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, Optional

MODULES: Dict[str, str] = {
    "terminal-bench-2": "tadamoo.bench.adapters.terminal_bench",
    "swe-bench-verified": "tadamoo.bench.adapters.swe_bench",
    "clawbench": "tadamoo.bench.adapters.clawbench",
    "bfcl-v4": "tadamoo.bench.adapters.bfcl",
    "tau3-bench": "tadamoo.bench.adapters.tau3",
}


class AdapterModuleMissing(LookupError):
    """No module for this benchmark, or the module does not exist yet."""


def load(spec_id: str, modules: Optional[Dict[str, str]] = None) -> Callable[[], Any]:
    """The factory (the class `ADAPTER`) of a benchmark's module. Raises AdapterModuleMissing when the module is not written; an import error inside an
    existing module is raised as it is, because that is a fault in the adapter and not a missing one."""
    table = MODULES if modules is None else modules
    name = table.get(spec_id)
    if not name:
        raise AdapterModuleMissing(f"no adapter module is listed for {spec_id}")
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name or (exc.name and name.startswith(exc.name + ".")):
            raise AdapterModuleMissing(f"the adapter module {name} does not exist yet") from None
        raise
    adapter = getattr(module, "ADAPTER", None)
    if adapter is None:
        raise AdapterModuleMissing(f"{name} does not define ADAPTER")
    return adapter
