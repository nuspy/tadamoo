"""Case studies: experiments that answer design questions of the harness by measuring them (docs/experiments.md).

    python -m harness.eval.cases list
    python -m harness.eval.cases e2 --endpoint http://127.0.0.1:7232 --k 5 --gpu-power-limit-w 400 --out e2.md --json e2.json
"""
from __future__ import annotations

import importlib
from typing import Dict

from .core import Experiment

MODULES: Dict[str, str] = {"e1": "e1_target", "e2": "e2_gate", "e3": "e3_errors", "e4": "e4_intake", "e5": "e5_compaction", "e6": "e6_longform", "e7": "e7_cache", "e8": "e8_optimize", "e9": "e9_context"}


def load(experiment_id: str) -> Experiment:
    if experiment_id not in MODULES:
        raise KeyError(f"no experiment {experiment_id!r}; the experiments are {', '.join(MODULES)}")
    return importlib.import_module(f"{__name__}.{MODULES[experiment_id]}").experiment()
