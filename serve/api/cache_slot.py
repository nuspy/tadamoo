"""The request key `cache_slot`: which kind of engine slot serves the request.

"side" asks for the technical session, the short slot kept for the harness's small calls; "main" (or no key) the
conversations' slots. A request for the technical session that does not fit it, or a server without one, is served
by the ordinary slots, and its answer says which kind served it."""
from __future__ import annotations

KEY = "cache_slot"
KINDS = ("main", "side")


def check_cache_slot(req: dict) -> None:
    """ValueError for a `cache_slot` that is not "main" or "side" (a 400 before the answer starts)."""
    value = req.get(KEY)
    if value is not None and value not in KINDS:
        raise ValueError(f'{KEY}: "side" (the technical session) or "main", not {value!r}')
