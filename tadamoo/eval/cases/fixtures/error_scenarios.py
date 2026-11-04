"""E3 fixture: what a scripted server answers, request after request, and what a good harness does about it.

`ideal` is "answer" when waiting cures the error, so the turn must end with an answer, and "visible_error" when nothing waiting can cure
(the same request will get the same answer), so the turn should end soon with the error shown. `forever` repeats the last step.
"""
from __future__ import annotations

from typing import Any, Dict, List

STALL = 0.6                      # seconds a stalled request holds before the connection drops; the client's timeout is shorter
OVERFLOW = "This model's maximum context length is 32768 tokens: the prompt is over the context"

SCENARIOS: List[Dict[str, Any]] = [
    {"id": "transient_429_500", "ideal": "answer", "forever": False,
     "steps": [{"error": 429, "message": "rate limit, try again"}, {"error": 500, "message": "internal error"}, {"ok": "Done."}]},
    {"id": "transient_timeout", "ideal": "answer", "forever": False, "steps": [{"stall": STALL}, {"ok": "Done."}]},
    {"id": "transient_mixed", "ideal": "answer", "forever": False,
     "steps": [{"error": 429, "message": "rate limit"}, {"stall": STALL}, {"error": 500, "message": "oops"}, {"error": 429, "message": "rate limit"}, {"ok": "Done."}]},
    {"id": "loading_400", "ideal": "answer", "forever": False, "steps": [{"error": 400, "message": "the model is still loading"}, {"ok": "Done."}]},
    {"id": "deterministic_400", "ideal": "visible_error", "forever": True, "steps": [{"error": 400, "message": "malformed request: tools[0].parameters"}]},
    {"id": "deterministic_401", "ideal": "visible_error", "forever": True, "steps": [{"error": 401, "message": "invalid api key"}]},
    {"id": "overflow_forever", "ideal": "visible_error", "forever": True, "steps": [{"error": 400, "message": OVERFLOW}]},
    {"id": "overflow_twice", "ideal": "answer", "forever": False, "steps": [{"error": 400, "message": OVERFLOW}, {"error": 400, "message": OVERFLOW}, {"ok": "Done."}]},
]


def scenarios() -> List[Dict[str, Any]]:
    return SCENARIOS
