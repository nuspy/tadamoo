"""The inference settings of a conversation: what the settings panel keeps (serve/web/chatset.js), read as the fields a conversation's process runs with.

A conversation keeps its own settings, saved with it. A new conversation starts from the machine default, the inference preset "Machine Default"
(serve/profiles.py), which the person edits in Settings; a conversation a plan opens starts from the plan's own settings, which are the
machine default until the person changes them. Nothing here is global at run time: each conversation's process is started from its own record.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from serve import profiles

EFFORTS = ("", "none", "low", "medium", "high")                   # how long the model thinks; empty is the model's own
POLICIES = ("stop", "rolling", "compact")                         # what is done when the context is full
THRESHOLD = 0.8                                                   # the share of the window at which old results are masked and older steps folded
THRESHOLD_LIMITS = (0.3, 1.0)
FOLD_LIMITS = (0.5, 0.98)                                         # tadamoo/compaction.py: MIN_THRESHOLD, MAX_THRESHOLD
SAMPLING_NUMBERS = ("top_p", "top_k", "min_p", "repetition_penalty", "presence_penalty", "frequency_penalty", "penalty_last_n", "seed",
                    "reasoning_budget_tokens")                    # the request fields a conversation's model calls carry besides the temperature
INTEGER_KEYS = ("top_k", "penalty_last_n", "seed", "reasoning_budget_tokens")
STOPS_MAX = 4


def _number(value: Any) -> Optional[float]:
    """A setting as typed in the panel (a number or its text), or None when it is empty or not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    return float(text) if re.fullmatch(r"-?\d+(\.\d+)?", text) else None


def machine_default() -> Dict[str, Any]:
    """The settings every new conversation starts from: the inference preset "Machine Default", or none (the built-in values below)."""
    preset = profiles._read()["inference"].get(profiles.MACHINE_NAME)
    settings = preset.get("settings") if isinstance(preset, dict) else None
    try:
        return profiles.check_inference(settings) if isinstance(settings, dict) else {}
    except ValueError:
        return {}


def normalize(settings: Any) -> Dict[str, Any]:
    """The settings checked, as the panel keeps them; ValueError says what is wrong."""
    out = profiles.check_inference(settings)
    if out.get("effort") not in (None, *EFFORTS):
        raise ValueError("effort: none, low, medium or high; empty is the model's own")
    if out.get("policy") not in (None, "", *POLICIES):
        raise ValueError("policy: stop, rolling or compact")
    if out.get("threshold") not in (None, ""):
        share = _number(out["threshold"])
        if share is None or not THRESHOLD_LIMITS[0] * 100 <= share <= THRESHOLD_LIMITS[1] * 100:
            raise ValueError("threshold: a share of the context from 30 to 100")
    if out.get("temperature") not in (None, ""):
        temp = _number(out["temperature"])
        if temp is None or not 0.0 <= temp <= 5.0:
            raise ValueError("temperature: a number from 0 to 5")
    for key in SAMPLING_NUMBERS:
        if out.get(key) not in (None, "") and _number(out[key]) is None:
            raise ValueError(f"{key}: a number")
    return out


def resolve(settings: Any = None) -> Dict[str, Any]:
    """The settings a conversation is given: its own when it has them, else the machine default."""
    return normalize(settings) if isinstance(settings, dict) else machine_default()


def stops_of(settings: Dict[str, Any]) -> List[str]:
    raw = settings.get("stop")
    lines = raw if isinstance(raw, list) else str(raw or "").splitlines()
    return [s.rstrip("\r") for s in lines if isinstance(s, str) and s.strip("\r\n")][:STOPS_MAX]


def fields(settings: Dict[str, Any]) -> Dict[str, Any]:
    """What a conversation's record keeps and its process runs with, from settings: the thinking level, how the full context is dealt with and where, the steps one
    answer may take, the end-of-turn bias, the temperature, the most a reply may write and the other sampling fields."""
    policy = settings.get("policy") or "compact"
    share = _number(settings.get("threshold"))
    threshold = share / 100 if share is not None and THRESHOLD_LIMITS[0] <= share / 100 <= THRESHOLD_LIMITS[1] else THRESHOLD
    steps = _number(settings.get("max_turns"))
    room = _number(settings.get("max_tokens"))
    bias = _number(settings.get("end_turn_bias"))
    temp = _number(settings.get("temperature"))
    sampling: Dict[str, Any] = {}
    for key in SAMPLING_NUMBERS:
        value = _number(settings.get(key))
        if value is None or (key == "reasoning_budget_tokens" and value <= 0):
            continue
        sampling[key] = int(value) if key in INTEGER_KEYS else value
    stops = stops_of(settings)
    if stops:
        sampling["stop"] = stops
    return {"effort": settings.get("effort") or "", "context_policy": policy, "context_threshold": threshold,
            "compaction": policy == "compact", "compaction_threshold": min(FOLD_LIMITS[1], max(FOLD_LIMITS[0], threshold)),
            "max_turns": int(steps) if steps is not None and (steps == -1 or steps >= 1) else -1,
            "end_turn_bias": min(0.0, bias) if bias is not None else 0.0,
            "temperature": temp, "max_tokens": int(room) if room is not None and room >= 1 else None, "sampling": sampling}


def from_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The settings a conversation record made before they were kept with it runs with, in the panel's own form."""
    out: Dict[str, Any] = {"policy": "compact"}
    if rec.get("effort"):
        out["effort"] = rec["effort"]
    if rec.get("context_threshold"):
        out["threshold"] = str(round(float(rec["context_threshold"]) * 100))
    if rec.get("max_turns") not in (None, -1):
        out["max_turns"] = str(rec["max_turns"])
    for key in ("end_turn_bias", "temperature", "max_tokens"):
        if rec.get(key):
            out[key] = str(rec[key])
    if isinstance(rec.get("context_patterns"), dict) and rec["context_patterns"]:
        out["context_patterns"] = rec["context_patterns"]
    return out
