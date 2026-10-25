"""Dual-process System 1: cheap triage of incoming events against the active plan.

Events pile up in an EventBuffer (a batch of 10, or what arrived within 5 minutes) and a gatekeeper
decides whether any of them is worth waking the local model (System 2). The gatekeeper can ask
TypeSafe's Jev service, which is a third party's cloud: it does so only with the user's consent and
an API key, sends only the card fields the user allows, with people's names replaced, and says in
every result where the verdict came from. Otherwise a local keyword heuristic answers.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from harness.integrations._http import _OPENER

from .card import PlanInsightCard, Pseudonymizer, scrub_contacts, share_card

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
HIGH_IMPACT = ("MOLTO", "CRITICAL")
GATE_MODES = ("or", "and")        # or: any strong sign wakes the model; and: a strong blocker probability must also come with a high impact or a total scope
SCOPE_ORDER = ("ZERO_EVENTS_IMPACTFUL", "MIXED_SOME_IMPACTFUL", "ALL_EVENTS_IMPACTFUL")
MAX_EVENT_CHARS = 500
CRITICAL_KEYWORDS = ["blocco", "sciopero", "precettat", "ricoverat", "chiusa", "interrotta", "valanga", "emergenza",
                     "annullat", "cancellat", "alluvione", "frana"]


@dataclass
class TriageResult:
    wake_system_2: bool
    has_blocker_prob: float
    impact_scope: str
    reasons: List[str]
    latency_ms: float
    token_usage: int = 0
    source: str = "offline"            # "cloud", "offline" (heuristic, by choice) or "error" (cloud failed)
    max_impact: Optional[str] = None   # NO / POCO / MOLTO / CRITICAL, when the cloud answered it
    sent_to_cloud: bool = False


class EventBuffer:
    """Thread-safe event accumulator: a batch is due at `max_batch_size` events or `max_interval_seconds` after the first."""

    def __init__(self, max_batch_size: int = 10, max_interval_seconds: float = 300.0, max_pending: int = 1000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_batch_size = max_batch_size
        self.max_interval = max_interval_seconds
        self.max_pending = max_pending
        self.clock = clock
        self._events: List[str] = []
        self._first_at: Optional[float] = None
        self._lock = threading.Lock()
        self.dropped = 0

    def add_event(self, event_text: str) -> bool:
        """Adds an event; True when a batch is due. Beyond `max_pending` the oldest events are dropped and counted."""
        text = event_text.strip()
        if not text:
            return False
        with self._lock:
            if self._first_at is None:
                self._first_at = self.clock()
            self._events.append(text)
            if len(self._events) > self.max_pending:
                overflow = len(self._events) - self.max_pending
                del self._events[:overflow]
                self.dropped += overflow
            return self._due()

    def _due(self) -> bool:
        if not self._events:
            return False
        return len(self._events) >= self.max_batch_size or self.clock() - (self._first_at or 0) >= self.max_interval

    def due(self) -> bool:
        with self._lock:
            return self._due()

    def flush(self, max_events: Optional[int] = None) -> List[str]:
        with self._lock:
            take = len(self._events) if max_events is None else min(max_events, len(self._events))
            batch, self._events = self._events[:take], self._events[take:]
            self._first_at = self.clock() if self._events else None
            return batch

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


class TriageTelemetry:
    def __init__(self) -> None:
        self.calls = self.cloud = self.offline = self.errors = self.wakes = 0           # errors: the cloud failed and the local filter answered
        self.tokens = 0
        self.latencies: Deque[float] = deque(maxlen=200)
        self._lock = threading.Lock()

    def record(self, result: TriageResult) -> None:
        with self._lock:
            self.calls += 1
            self.cloud += result.source == "cloud"
            self.offline += result.source == "offline"
            self.errors += result.source == "error"
            self.wakes += result.wake_system_2
            self.tokens += result.token_usage
            if result.source == "cloud":
                self.latencies.append(result.latency_ms)

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            lat = list(self.latencies)
            return {"calls": self.calls, "cloud": self.cloud, "offline": self.offline, "errors": self.errors, "fell_back": self.errors,
                    "wakes": self.wakes, "tokens": self.tokens,
                    "latency_ms_median": round(statistics.median(lat), 1) if lat else None,
                    "latency_ms_max": round(max(lat), 1) if lat else None}


def _check_endpoint(url: str) -> str:
    parts = urlparse(url)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme not in ("http", "https") or not parts.hostname or (parts.scheme == "http" and not local):
        raise ValueError("the triage endpoint must use https (plain http only on this machine)")
    return url


class JevTriageGatekeeper:
    """System 1 fast causal triage: reads an event batch against the active PlanInsightCard."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "jev-latest",
        endpoint: str = DEFAULT_ENDPOINT,
        blocker_threshold: float = 0.80,
        cloud_consent: bool = False,
        card_fields: Optional[Iterable[str]] = None,
        pseudonymize: bool = True,
        max_events: int = 20,
        timeout: float = 5.0,
        gate_mode: str = "or",
    ) -> None:
        if gate_mode not in GATE_MODES:
            raise ValueError(f"gate_mode must be one of: {', '.join(GATE_MODES)}")
        self.gate_mode = gate_mode
        self.api_key = api_key
        self.model = model
        self.endpoint = _check_endpoint(endpoint)
        self.blocker_threshold = blocker_threshold
        self.cloud_consent = cloud_consent
        self.card_fields = list(card_fields) if card_fields is not None else None
        self.pseudonymize = pseudonymize
        self.max_events = max_events
        self.timeout = timeout
        self.telemetry = TriageTelemetry()

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "JevTriageGatekeeper":
        """The key comes from TYPESAFE_API_KEY. Having a key is not consent: cloud_consent stays false until set."""
        return cls(api_key=os.environ.get("TYPESAFE_API_KEY") or None, **kwargs)

    def reconfigure(self, **settings: Any) -> None:
        """Changes how this gatekeeper behaves from now on (the key, the address, consent, the fields, the gate) and keeps its telemetry."""
        known = ("api_key", "model", "endpoint", "blocker_threshold", "cloud_consent", "card_fields", "pseudonymize", "max_events", "timeout", "gate_mode")
        unknown = [k for k in settings if k not in known]
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(unknown)}")
        if "endpoint" in settings:
            _check_endpoint(settings["endpoint"])
        if settings.get("gate_mode", "or") not in GATE_MODES:
            raise ValueError(f"gate_mode must be one of: {', '.join(GATE_MODES)}")
        if "card_fields" in settings and settings["card_fields"] is not None:
            settings["card_fields"] = list(settings["card_fields"])
        for key, value in settings.items():
            setattr(self, key, value)

    @property
    def cloud_allowed(self) -> bool:
        return bool(self.api_key and self.cloud_consent)

    # -- local answer ----------------------------------------------------------------------------

    def triage_offline(self, plan_card: Any, events: List[str], note: Optional[str] = None) -> TriageResult:
        """Deterministic keyword heuristic, used when the cloud is not allowed or fails."""
        t0 = time.time()
        impactful = [e for e in events if any(k in e.lower() for k in CRITICAL_KEYWORDS)]
        ratio = len(impactful) / max(len(events), 1)
        prob = min(1.0, ratio * 1.5) if impactful else 0.05
        scope = ("ALL_EVENTS_IMPACTFUL" if ratio >= 0.8 else "MIXED_SOME_IMPACTFUL" if impactful else "ZERO_EVENTS_IMPACTFUL")
        reasons = ([note] if note else []) + impactful
        return TriageResult(
            wake_system_2=bool(impactful), has_blocker_prob=round(prob, 3), impact_scope=scope, reasons=reasons,
            latency_ms=round((time.time() - t0) * 1000, 2), source="offline",
        )

    # -- cloud answer ----------------------------------------------------------------------------

    def build_payload(self, plan_card: Any, events: List[str]) -> Dict[str, Any]:
        card = plan_card if isinstance(plan_card, PlanInsightCard) else PlanInsightCard.from_dict(plan_card)
        shared = share_card(card, self.card_fields, self.pseudonymize)
        pseudo = Pseudonymizer(card.people()) if self.pseudonymize else None
        lines = [(pseudo.apply(scrub_contacts(e)) if pseudo else e)[:MAX_EVENT_CHARS] for e in events]
        return {
            "model": self.model,
            "state": {"active_plan": shared, "events_digest": "\n".join(f"{i + 1}. {e}" for i, e in enumerate(lines))},
            "questions": {
                "has_blocker_or_critical": {
                    "type": "noul",
                    "instructions": "Does this list of incoming events contain AT LEAST ONE event that critically disrupts, "
                                    "blocks, or threatens the active plan?",
                },
                "batch_impact_scope": {
                    "type": "choice",
                    "instructions": "Evaluate the overall proportion and nature of events in this batch affecting the plan.",
                    "criteria": {
                        "ALL_EVENTS_IMPACTFUL": "All or virtually all events disrupt, block, or severely impact the plan.",
                        "MIXED_SOME_IMPACTFUL": "A mixture of some impactful events alongside unrelated or harmless news.",
                        "ZERO_EVENTS_IMPACTFUL": "None of the events affect the plan (all are harmless or neutral).",
                    },
                },
                "max_event_impact": {
                    "type": "choice",
                    "instructions": "How strongly does the single most disruptive event in the batch affect the plan?",
                    "criteria": {
                        "NO": "No event affects the plan.",
                        "POCO": "At most a minor, easily absorbed effect.",
                        "MOLTO": "At least one event seriously disrupts part of the plan.",
                        "CRITICAL": "At least one event makes the plan, or an essential part of it, impossible.",
                    },
                },
            },
        }

    def parse_response(self, data: Dict[str, Any], latency_ms: float = 0.0) -> TriageResult:
        """Maps a System One answer onto a TriageResult; raises ValueError if it is malformed."""
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("missing answers")
        blocker = answers.get("has_blocker_or_critical", answers.get("has_blocker"))
        scope_ans = answers.get("batch_impact_scope")
        if not isinstance(blocker, dict) or not isinstance(scope_ans, dict):
            raise ValueError("missing triage answers")
        prob = float(blocker["noul"])
        scope = scope_ans["choice"]
        if not 0.0 <= prob <= 1.0 or scope not in SCOPE_ORDER:
            raise ValueError("out-of-range triage answer")
        impact_ans = answers.get("max_event_impact")
        max_impact = impact_ans.get("choice") if isinstance(impact_ans, dict) else None
        usage = data.get("usage") or {}
        tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        strong = scope == "ALL_EVENTS_IMPACTFUL" or max_impact in HIGH_IMPACT
        wake = (prob > self.blocker_threshold and strong) if self.gate_mode == "and" else (prob > self.blocker_threshold or strong)
        return TriageResult(
            wake_system_2=wake, has_blocker_prob=prob, impact_scope=scope,
            reasons=[f"Jev: blocker probability {prob:.2f}, scope {scope}" + (f", impact {max_impact}" if max_impact else "")],
            latency_ms=round(latency_ms, 2), token_usage=tokens, source="cloud", max_impact=max_impact, sent_to_cloud=True,
        )

    def _ask_cloud(self, plan_card: Any, events: List[str], timeout: float) -> TriageResult:
        payload = self.build_payload(plan_card, events)
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST")
        t0 = time.time()
        with _OPENER.open(req, timeout=timeout) as resp:         # redirects are never followed: the key stays here
            data = json.loads(resp.read().decode("utf-8"))
        return self.parse_response(data, (time.time() - t0) * 1000)

    @staticmethod
    def _merge(parts: List[TriageResult]) -> TriageResult:
        worst = max(parts, key=lambda r: SCOPE_ORDER.index(r.impact_scope))
        impacts = [r.max_impact for r in parts if r.max_impact]
        return TriageResult(
            wake_system_2=any(r.wake_system_2 for r in parts), has_blocker_prob=max(r.has_blocker_prob for r in parts),
            impact_scope=worst.impact_scope, reasons=[x for r in parts for x in r.reasons],
            latency_ms=round(sum(r.latency_ms for r in parts), 2), token_usage=sum(r.token_usage for r in parts),
            source="cloud" if all(r.source == "cloud" for r in parts) else "error",
            max_impact=next((i for i in ("CRITICAL", "MOLTO", "POCO", "NO") if i in impacts), None),
            sent_to_cloud=any(r.sent_to_cloud for r in parts),
        )

    def triage_event_batch(self, plan_card: Any, events: List[str], timeout: Optional[float] = None) -> TriageResult:
        """Judges a batch. Long batches go in chunks of `max_events`; the answer is the worst of the chunks."""
        events = [e for e in events if e and e.strip()]
        if not events:
            return TriageResult(False, 0.0, "ZERO_EVENTS_IMPACTFUL", [], 0.0)
        if not self.cloud_allowed:
            note = "cloud triage is not authorized by the user" if self.api_key else None
            result = self.triage_offline(plan_card, events, note)
            self.telemetry.record(result)
            return result
        parts: List[TriageResult] = []
        for i in range(0, len(events), self.max_events):
            chunk = events[i:i + self.max_events]
            try:
                parts.append(self._ask_cloud(plan_card, chunk, timeout or self.timeout))
            except Exception as exc:
                fallback = self.triage_offline(plan_card, chunk, f"jev unavailable: {type(exc).__name__}")
                fallback.source = "error"
                parts.append(fallback)
        result = parts[0] if len(parts) == 1 else self._merge(parts)
        self.telemetry.record(result)
        return result


class EventPump:
    """Takes due batches from an EventBuffer, triages them and hands the result on; runs on its own thread."""

    def __init__(self, buffer: EventBuffer, gatekeeper: JevTriageGatekeeper, card_provider: Callable[[], Any],
                 on_result: Callable[[TriageResult, List[str]], None], poll_seconds: float = 1.0) -> None:
        self.buffer, self.gatekeeper, self.card_provider, self.on_result = buffer, gatekeeper, card_provider, on_result
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.errors: List[str] = []

    def process_once(self) -> Optional[TriageResult]:
        if not self.buffer.due():
            return None
        events = self.buffer.flush(self.gatekeeper.max_events * 5)
        result = self.gatekeeper.triage_event_batch(self.card_provider(), events)
        try:
            self.on_result(result, events)
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
        return result

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.process_once()
            except Exception as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="event-pump")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
