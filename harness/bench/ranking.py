"""Where a result would stand among published entries. Nothing here reaches the network: the caller passes the text of a downloaded results file."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from harness.eval.report import wilson

NAME_COLUMNS = ("name", "model", "entry", "agent", "submission", "harness")


@dataclass
class Entry:
    name: str
    metrics: Dict[str, float] = field(default_factory=dict)
    raw: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "metrics": self.metrics}


def _column(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _number(text: str) -> Optional[float]:
    t = (text or "").strip().rstrip("%").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def load_csv(source: Union[str, Path]) -> List[Entry]:
    """Entries from a results CSV (a path or the text itself). The name is the first of the usual name columns (else the first column); every column
    that holds a number becomes a metric, with its header lower-cased and joined by underscores. A percent sign is dropped and the number kept."""
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source and "," not in source and Path(source).is_file()):
        text = Path(source).read_text(encoding="utf-8-sig")
    else:
        text = str(source).lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return []
    columns = {_column(h): h for h in rows[0] if h}
    name_col = next((columns[c] for c in NAME_COLUMNS if c in columns), next(iter(rows[0])))
    out = []
    for row in rows:
        metrics = {}
        for h, v in row.items():
            if h and h != name_col and (n := _number(v if isinstance(v, str) else "")) is not None:
                metrics[_column(h)] = n
        out.append(Entry((row.get(name_col) or "").strip(), metrics, {str(k): str(v) for k, v in row.items() if k}))
    return out


def interval(successes: int, n: int) -> Dict[str, float]:
    """The 95% Wilson interval of a resolution rate, as fractions."""
    low, high = wilson(successes, n)
    return {"low": low, "high": high, "n": n}


def _scale(values: List[float], ours: float, scale: str) -> float:
    """Our value in the units of the entries ('fraction' 0..1 or 'percent' 0..100)."""
    if scale == "auto":
        scale = "percent" if values and max(values) > 1.0 else "fraction"
    if scale == "percent" and ours <= 1.0:
        return ours * 100.0
    if scale == "fraction" and ours > 1.0:
        return ours / 100.0
    return ours


def rank(our_metrics: Mapping[str, float], entries: List[Entry], metric: str, scale: str = "auto") -> Dict[str, Any]:
    """Our place among the entries on one metric: `position` (1 + entries that score strictly higher), `of` (entries plus us), `percentile` (share of
    entries we beat, ties counting half) and the nearest entry above and below. `scale` says whether the entries hold fractions or percents ('auto' guesses)."""
    scored = [e for e in entries if metric in e.metrics]
    if metric not in our_metrics:
        raise KeyError(f"our results have no metric {metric!r}; they hold {', '.join(sorted(our_metrics)) or 'none'}")
    values = [e.metrics[metric] for e in scored]
    ours = _scale(values, float(our_metrics[metric]), scale)
    above = sorted((e for e in scored if e.metrics[metric] > ours), key=lambda e: e.metrics[metric])
    below = sorted((e for e in scored if e.metrics[metric] < ours), key=lambda e: -e.metrics[metric])
    equal = sum(1 for v in values if v == ours)
    n = len(scored)
    pick = lambda e: None if e is None else {"name": e.name, "value": e.metrics[metric]}
    return {"metric": metric, "our_value": ours, "position": 1 + len(above), "of": n + 1,
            "percentile": None if not n else round(100.0 * (len(below) + 0.5 * equal) / n, 1),
            "nearest_above": pick(above[0] if above else None), "nearest_below": pick(below[0] if below else None),
            "entries_compared": n, "entries_without_metric": len(entries) - n}


def simulate(our_metrics: Mapping[str, float], entries: List[Entry], metric: str, *, official_judge: bool, successes: Optional[int] = None,
             trials: Optional[int] = None, scale: str = "auto") -> Dict[str, Any]:
    """The preview shown before publishing. `basis` says how much it can be trusted: 'entries' (compared with published entries), 'local_only' (no entries
    could be downloaded: only our own score and its interval). A score made without the official judge is marked provisional."""
    out: Dict[str, Any] = {"provisional": not official_judge, "metric": metric, "our_value": our_metrics.get(metric)}
    if successes is not None and trials:
        out["interval"] = interval(successes, trials)
    if entries:
        out.update(rank(our_metrics, entries, metric, scale))
        out["basis"] = "entries"
        if "interval" in out:
            lo = rank({metric: out["interval"]["low"]}, entries, metric, scale)
            hi = rank({metric: out["interval"]["high"]}, entries, metric, scale)
            out["position_range"] = {"best": hi["position"], "worst": lo["position"]}
        out["note"] = "Compared with the entries in the downloaded results file."
    else:
        out.update({"position": None, "of": None, "percentile": None, "nearest_above": None, "nearest_below": None, "basis": "local_only"})
        out["note"] = "No published entries could be downloaded, so the place in the ranking cannot be shown. This is the benchmark's own score computed locally."
    if out["provisional"]:
        out["note"] += " Unofficial judge: not comparable with the board; the place is provisional."
    return out
