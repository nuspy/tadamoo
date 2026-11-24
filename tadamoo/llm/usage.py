"""The tokens every model call used, one line for each call, for the Usage tab of Models > Providers.

A call is recorded where it ends: a provider's answer in `RoutedClient` (and the relay of a heavy swap), the local engine's in `Service.run`. Nothing of the
messages is kept: only the time, who answered (a provider id, or `local`), the model, the function that asked, and the counts the answer reported. Kept in
`llm_usage_log.jsonl` in the data folder, newest last, with no limit on time: the "all" filter reads the whole file.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from tadamoo.llm.store import data_dir

LOCAL = "local"
STEPS = (60, 300, 900, 3600, 3 * 3600, 6 * 3600, 86400, 7 * 86400)       # the width of a bar of the chart: the smallest that leaves at most MAX_BARS
MAX_BARS = 60
_lock = threading.Lock()


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def counts(result: Any) -> Dict[str, int]:
    """prompt, completion and cached tokens of an answer in the OpenAI shape (a count the answer did not report is 0)."""
    usage = result.get("usage") if isinstance(result, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    detail = usage.get("prompt_tokens_details")
    return {"prompt": _int(usage.get("prompt_tokens")), "completion": _int(usage.get("completion_tokens")),
            "cached": _int(detail.get("cached_tokens")) if isinstance(detail, dict) else 0}


class UsageLog:
    def __init__(self, path: str | Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path else data_dir() / "llm_usage_log.jsonl"
        self.clock = clock

    def record(self, provider: str, name: str, model: str, function: str, prompt: int = 0, completion: int = 0, cached: int = 0, api: str = "") -> None:
        """Adds one call; a call with no tokens at all is not a line. A failure to write never reaches the caller: the count is a courtesy."""
        prompt, completion, cached = _int(prompt), _int(completion), min(_int(cached), _int(prompt))
        if not prompt and not completion:
            return
        row = {"t": round(self.clock(), 3), "provider": provider or LOCAL, "name": name or provider or LOCAL, "model": model or "", "function": function or "",
               "prompt": prompt, "completion": completion, "cached": cached, **({"api": api} if api else {})}
        try:
            with _lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def rows(self, start: float = 0.0, end: Optional[float] = None) -> Iterator[Dict[str, Any]]:
        end = float("inf") if end is None else end
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("t"), (int, float)) and start <= row["t"] <= end:
                        yield row
        except OSError:
            return

    def report(self, start: float = 0.0, end: Optional[float] = None) -> Dict[str, Any]:
        """What the page shows for a period: totals, each provider, each model, each function, and the bars over time."""
        now = self.clock()
        end = now if end is None else end
        picked = list(self.rows(start, end))
        first = min((r["t"] for r in picked), default=None)
        lo = start if start > 0 else (first if first is not None else end)
        span = max(end - lo, 1.0)
        step = next((s for s in STEPS if span / s <= MAX_BARS), STEPS[-1])

        def add(into: Dict[Any, Dict[str, Any]], key: Any, row: Dict[str, Any], **names: Any) -> None:
            slot = into.setdefault(key, {**names, "calls": 0, "prompt": 0, "completion": 0, "cached": 0, "total": 0})
            slot["calls"] += 1
            slot["prompt"] += _int(row.get("prompt"))
            slot["completion"] += _int(row.get("completion"))
            slot["cached"] += _int(row.get("cached"))
            slot["total"] += _int(row.get("prompt")) + _int(row.get("completion"))

        total: Dict[Any, Dict[str, Any]] = {}
        providers: Dict[Any, Dict[str, Any]] = {}
        models: Dict[Any, Dict[str, Any]] = {}
        functions: Dict[Any, Dict[str, Any]] = {}
        bars: Dict[int, Dict[str, int]] = {}
        for r in picked:
            add(total, "all", r)
            add(providers, r.get("provider") or LOCAL, r, id=r.get("provider") or LOCAL, name=r.get("name") or r.get("provider") or LOCAL)
            add(models, (r.get("provider") or LOCAL, r.get("model") or ""), r, provider=r.get("provider") or LOCAL, name=r.get("name") or r.get("provider") or LOCAL, model=r.get("model") or "")
            add(functions, r.get("function") or "", r, function=r.get("function") or "")
            at = int((r["t"] - lo) // step)
            bar = bars.setdefault(at, {})
            bar[r.get("provider") or LOCAL] = bar.get(r.get("provider") or LOCAL, 0) + _int(r.get("prompt")) + _int(r.get("completion"))
        by_total = lambda items: sorted(items.values(), key=lambda x: -x["total"])      # noqa: E731
        empty = {"calls": 0, "prompt": 0, "completion": 0, "cached": 0, "total": 0}
        return {"from": lo, "to": end, "first": first, "bar_s": step, "totals": total.get("all", empty), "providers": by_total(providers), "models": by_total(models),
                "functions": by_total(functions), "bars": [{"t": lo + i * step, "by_provider": bars[i]} for i in sorted(bars)]}
