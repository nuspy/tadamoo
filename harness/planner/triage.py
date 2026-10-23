"""Dual-Process System 1 Triage & Gating: causal filtering at zero GPU cost."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class TriageResult:
    wake_system_2: bool
    has_blocker_prob: float
    impact_scope: str
    reasons: List[str]
    latency_ms: float
    token_usage: int = 0
    source: str = "offline"  # "cloud", "offline" or "error"


class EventBuffer:
    """Thread-safe event accumulator with size and time-window triggers."""

    def __init__(self, max_batch_size: int = 10, max_interval_seconds: float = 300.0) -> None:
        self.max_batch_size = max_batch_size
        self.max_interval = max_interval_seconds
        self._events: List[str] = []
        self._last_flush = time.time()
        self._lock = threading.Lock()

    def add_event(self, event_text: str) -> bool:
        """Adds an event. Returns True if batch trigger condition is met."""
        with self._lock:
            self._events.append(event_text.strip())
            should_flush = (
                len(self._events) >= self.max_batch_size
                or (time.time() - self._last_flush) >= self.max_interval
            )
            return should_flush

    def flush(self) -> List[str]:
        with self._lock:
            batch = list(self._events)
            self._events.clear()
            self._last_flush = time.time()
            return batch

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


class JevTriageGatekeeper:
    """System 1 fast causal triage: analyzes event batch against active PlanInsightCard."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "jev-latest",
        endpoint: str = "https://api.typesafe.ai/v1/systemone",
        blocker_threshold: float = 0.85,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.blocker_threshold = blocker_threshold

    def triage_offline(self, plan_card: Dict[str, Any], events: List[str]) -> TriageResult:
        """Deterministic heuristic triage when offline or in test environments."""
        t0 = time.time()
        critical_keywords = [
            "blocco", "sciopero", "precettat", "ricoverat", "chiusa", "interrotta",
            "valanga", "emergenza", "annullat", "cancellat", "alluvione", "frana"
        ]
        impactful = []
        for e in events:
            e_lower = e.lower()
            if any(k in e_lower for k in critical_keywords):
                impactful.append(e)

        ratio = len(impactful) / max(len(events), 1)
        prob = min(1.0, ratio * 1.5) if impactful else 0.05
        wake = prob >= self.blocker_threshold or len(impactful) > 0

        scope = (
            "ALL_EVENTS_IMPACTFUL" if ratio >= 0.8 else
            "MIXED_SOME_IMPACTFUL" if impactful else
            "ZERO_EVENTS_IMPACTFUL"
        )
        latency = (time.time() - t0) * 1000

        return TriageResult(
            wake_system_2=wake,
            has_blocker_prob=round(prob, 3),
            impact_scope=scope,
            reasons=impactful,
            latency_ms=round(latency, 2),
        )

    def triage_event_batch(
        self,
        plan_card: Dict[str, Any],
        events: List[str],
        timeout: float = 5.0,
    ) -> TriageResult:
        """Sends batch of up to 10-20 events to TypeSafe Jev API."""
        if not events:
            return TriageResult(
                wake_system_2=False,
                has_blocker_prob=0.0,
                impact_scope="ZERO_EVENTS_IMPACTFUL",
                reasons=[],
                latency_ms=0.0,
            )

        # If no API key configured, use local offline heuristic
        if not self.api_key:
            return self.triage_offline(plan_card, events)

        t0 = time.time()
        digest = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(events))
        payload = {
            "model": self.model,
            "state": {
                "active_plan": plan_card,
                "events_digest": digest,
            },
            "questions": {
                "has_blocker": {
                    "type": "noul",
                    "instructions": "Does this list of incoming events contain AT LEAST ONE event that critically disrupts, blocks, or threatens the active plan?",
                },
                "batch_impact_scope": {
                    "type": "choice",
                    "instructions": "Evaluate the overall proportion and nature of events in this batch affecting the plan.",
                    "criteria": {
                        "ALL_EVENTS_IMPACTFUL": "All or virtually all events disrupt or block the plan.",
                        "MIXED_SOME_IMPACTFUL": "Some impactful events alongside harmless news.",
                        "ZERO_EVENTS_IMPACTFUL": "None of the events affect the plan.",
                    },
                },
            },
        }

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            latency = (time.time() - t0) * 1000
            return self.parse_response(data, latency)
        except Exception as exc:
            # Network, HTTP or malformed answer: fall back to the local heuristic, flagged.
            res = self.triage_offline(plan_card, events)
            res.source = "error"
            res.reasons = [f"jev unavailable: {type(exc).__name__}"] + res.reasons
            return res

    def parse_response(self, data: Dict[str, Any], latency_ms: float = 0.0) -> TriageResult:
        """Maps a System One answer onto a TriageResult; raises ValueError if it is malformed."""
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("missing answers")
        blocker = answers.get("has_blocker")
        scope_ans = answers.get("batch_impact_scope")
        if not isinstance(blocker, dict) or not isinstance(scope_ans, dict):
            raise ValueError("missing triage answers")
        prob = float(blocker["noul"])
        scope = scope_ans["choice"]
        if not 0.0 <= prob <= 1.0 or not isinstance(scope, str):
            raise ValueError("out-of-range triage answer")

        usage = data.get("usage") or {}
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        wake = prob >= self.blocker_threshold or scope == "ALL_EVENTS_IMPACTFUL"
        return TriageResult(
            wake_system_2=wake,
            has_blocker_prob=prob,
            impact_scope=scope,
            reasons=[f"Jev confidence: {prob:.2f}"],
            latency_ms=round(latency_ms, 2),
            token_usage=tokens,
            source="cloud",
        )
