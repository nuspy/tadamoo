"""What the server itself measured for the requests of an evaluation: time to the first token, prefill time, tokens read from the cache.

The server's request log (`GET /api/reqlog`) holds a `timing` event for every request. A trial reads the events that appeared between its
start and its end, so the figures belong to the trial only when nothing else uses the server meanwhile; the report says so. With the log
off, or an older server, a trial simply carries no server figures.
"""
from __future__ import annotations

import json
import statistics
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

TIMING_KEYS = ("first_token_s", "prefill_s", "prompt_tokens", "cached_tokens", "tokens_per_s", "completion_tokens")


def fetch_events(endpoint: str, api_key: Optional[str], after: int, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """The request log's answer after event `after`; None when the server cannot be reached or does not have the route."""
    req = urllib.request.Request(f"{endpoint.rstrip('/')}/api/reqlog?after={int(after)}", headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("enabled") else None


class ServerTimings:
    """Marks a point in the server's request log and later reads the requests made since."""

    def __init__(self, endpoint: str, api_key: Optional[str] = None, fetch: Optional[Callable[[int], Optional[Dict[str, Any]]]] = None) -> None:
        self._fetch = fetch or (lambda after: fetch_events(endpoint, api_key, after))

    def mark(self) -> Optional[int]:
        """The id of the newest event now, or None when the server has no request log to read."""
        data = self._fetch(1 << 62)
        return int(data["last"]) if data and isinstance(data.get("last"), int) else None

    def requests_since(self, mark: Optional[int]) -> List[Dict[str, Any]]:
        """One row per request that made a timing event after `mark`, with the figures of its final pass merged."""
        if mark is None:
            return []
        data = self._fetch(mark)
        merged: Dict[str, Dict[str, Any]] = {}
        for event in (data or {}).get("events", []):
            if event.get("kind") == "timing" and isinstance(event.get("data"), dict):
                row = merged.setdefault(str(event.get("req")), {})
                row.update({k: v for k, v in event["data"].items() if k in TIMING_KEYS and isinstance(v, (int, float))})
        return [row for row in merged.values() if "first_token_s" in row or "prompt_tokens" in row]


def summarize_requests(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Medians over requests; the cache share is tokens read from the cache over prompt tokens, as the server counted them."""
    firsts = [r["first_token_s"] for r in rows if "first_token_s" in r]
    prefills = [r["prefill_s"] for r in rows if "prefill_s" in r]
    speeds = [r["tokens_per_s"] for r in rows if "tokens_per_s" in r]
    prompt = sum(r.get("prompt_tokens", 0) for r in rows)
    cached = sum(r.get("cached_tokens", 0) for r in rows)
    return {"requests": len(rows), "ttft_median_s": statistics.median(firsts) if firsts else None, "prefill_median_s": statistics.median(prefills) if prefills else None,
            "tokens_per_s_median": statistics.median(speeds) if speeds else None, "prompt_tokens": prompt, "cached_tokens": cached,
            "cached_share": (cached / prompt) if prompt else None}
