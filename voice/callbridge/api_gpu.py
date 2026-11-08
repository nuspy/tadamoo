"""Blueprint /gpu/list — enumerazione delle GPU del sistema.

Usato dalla GUI per i picker "quale scheda usare" (es. avatar_gpu_index in
Impostazioni). Fonte: nvidia-smi (CSV); se assente o fallisce la lista e'
vuota e la GUI ripiega su un campo numerico libero.

  GET /gpu/list -> {"gpus": [{"index": 0, "name": "...", "vram_mb": 32607}],
                    "source": "nvidia-smi" | "none"}
"""
from __future__ import annotations

import logging
import subprocess
import time

from flask import Blueprint, jsonify

log = logging.getLogger("callbridge.api.gpu")

bp = Blueprint("gpu", __name__)

# La lista delle GPU non cambia a runtime: cache con TTL generoso per non
# lanciare nvidia-smi a ogni render della pagina Impostazioni.
_CACHE: dict = {"at": 0.0, "data": None}
_TTL_S = 60.0


def _enumerate() -> dict:
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        log.debug("nvidia-smi non disponibile: %s", e)
        return {"gpus": [], "source": "none"}
    if proc.returncode != 0:
        return {"gpus": [], "source": "none"}
    gpus = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            gpus.append({"index": int(parts[0]), "name": parts[1],
                         "vram_mb": int(float(parts[2]))})
        except ValueError:
            continue
    return {"gpus": gpus, "source": "nvidia-smi" if gpus else "none"}


@bp.get("/gpu/list")
def gpu_list():
    now = time.monotonic()
    if _CACHE["data"] is None or now - _CACHE["at"] > _TTL_S:
        _CACHE["data"] = _enumerate()
        _CACHE["at"] = now
    return jsonify(_CACHE["data"])
