"""PROPOSAL, off unless the owner turns it on (`--error-classifier`): tell a model error that waiting can cure from one it cannot.

Today every failed model call is retried without end, whatever the error, with waits that grow: a server that is not up yet, a model
that is still loading and a request the server will always refuse all look the same. This module sorts an error into one of three
kinds from the HTTP status (0: the server could not be reached) and the words of the message:

* `transient`: unreachable, busy, loading, rate limited, a server fault. Waiting and asking again can work.
* `overflow`: the conversation is longer than the model's context. Asking again unchanged cannot work; the context has to shrink first.
* `deterministic`: a malformed request, a refused key, a missing endpoint. The same request will get the same answer.

With the setting on, the session ends a turn on a `deterministic` error and shows the error; `transient` and `overflow` are retried as before.
Whether a turn may end on a model error at all is the owner's decision, against the rule that a turn is never left blocked by the model.
"""
from __future__ import annotations

import re
from typing import Optional

TRANSIENT, OVERFLOW, DETERMINISTIC = "transient", "overflow", "deterministic"
_WAIT_WORDS = ("loading", "no model", "not loaded", "busy", "try again", "overloaded", "rate limit", "temporarily", "timed out", "timeout",
               "unavailable", "connection", "queue")
_OVERFLOW = re.compile(r"context[ _](?:length|window|size)|maximum context|too many tokens|prompt is too (?:long|large)|token limit|"
                       r"(?:over|exceeds?|exceeded|beyond|too (?:long|large|big) for) the context|context.{0,30}(?:exceed|overflow|too (?:long|large|big))", re.IGNORECASE)
_DETERMINISTIC_STATUSES = (400, 401, 403, 404, 405, 410, 413, 415, 422)


def classify(status: Optional[int], message: str = "") -> str:
    """The kind of a model error. An unknown status, or a word that says the server is only busy or still loading, counts as transient."""
    text = (message or "").lower()
    if _OVERFLOW.search(text):
        return OVERFLOW
    if status is None or status == 0 or status in (408, 409, 425, 429) or status >= 500:
        return TRANSIENT
    if any(w in text for w in _WAIT_WORDS):
        return TRANSIENT
    if status in _DETERMINISTIC_STATUSES:
        return DETERMINISTIC
    return TRANSIENT
