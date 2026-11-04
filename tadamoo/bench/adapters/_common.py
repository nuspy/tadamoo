"""What the ClawBench, BFCL and tau-bench adapters share: the recorded model of a run, an environment without any cloud key, the `uv` program that runs
an imported benchmark in its own environment, and small reading helpers."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from tadamoo.bench.runner import SETTINGS, TrialContext, read_json

CLOUD_KEYS = re.compile(r"(?:OPENAI|ANTHROPIC|GEMINI|GOOGLE_API|OPENROUTER|AZURE_OPENAI|XAI|MISTRAL|COHERE|GROQ|TOGETHER|DEEPSEEK|PERPLEXITY|HF_TOKEN|SERPAPI)", re.I)
LOCAL_KEY = "local"                      # what the local engine is given where a client insists on a key; the engine does not check it
MODEL_NAME_BAD = re.compile(r'[\s\\*?"<>|/:]+')


def source_of(bench_dir: Path) -> Path:
    s = Path(bench_dir) / "source"
    return s if s.is_dir() else Path(bench_dir)


def clean_env(base: Optional[Mapping[str, str]], extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment of a benchmark process: the given one without any variable of a cloud provider, plus `extra`, with UTF-8 output."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if not CLOUD_KEYS.search(k)}
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(extra or {})
    return env


def uv_argv(which: Callable[[str], Optional[str]] = shutil.which) -> List[str]:
    return [which("uv") or "uv"]


def run_record(ctx: TrialContext) -> Dict[str, Any]:
    """The record the run wrote when it started (its settings, the model that was served)."""
    return read_json(ctx.run_dir / SETTINGS) or {}


def served_model(ctx: TrialContext) -> Dict[str, Any]:
    return dict(run_record(ctx).get("model") or {})


def model_key(model: Mapping[str, Any]) -> str:
    """The name the served model is called in a benchmark's own configuration: its name without characters those configurations refuse."""
    raw = str(model.get("name") or Path(str(model.get("file") or "")).stem or "local-model")
    return MODEL_NAME_BAD.sub("-", raw).strip("-") or "local-model"


def clip(text: Any, n: int = 400) -> str:
    return " ".join(str(text).split())[:n]


def read_lines(path: Path) -> List[Dict[str, Any]]:
    """The JSON objects of a file of JSON lines (or of one JSON list); a line that is not JSON is skipped."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return []
    if raw.startswith("["):
        try:
            return [e for e in json.loads(raw) if isinstance(e, dict)]
        except ValueError:
            return []
    out: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def write_events(path: Path, events: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in events), encoding="utf-8")


def pass_hat_k(trials: int, successes: int, k: int) -> float:
    """pass^k of one task: the chance that k trials drawn from its `trials` all succeed, with `successes` of them having succeeded (tau-bench's formula)."""
    if k < 1 or trials < k:
        raise ValueError(f"pass^{k} needs at least {k} trials, got {trials}")
    return math.comb(successes, k) / math.comb(trials, k)


def rate(won: int, of: int) -> float:
    return won / of if of else 0.0


def first_present(paths: Sequence[Path]) -> Optional[Path]:
    return next((p for p in paths if p.is_file()), None)
