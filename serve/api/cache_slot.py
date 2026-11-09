"""The request key `cache_slot`: which kind of engine slot serves the request.

"side" asks for the technical session, the short slot kept for the harness's small calls; "main" (or no key) the
conversations' slots. A request for the technical session that does not fit it, or a server without one, is served
by the ordinary slots, and its answer says which kind served it. "side_only" is the same request that is refused
(400, "technical session") instead of being served by an ordinary slot, for work that would otherwise take a
conversation's slot and its cache."""
from __future__ import annotations

KEY = "cache_slot"
KINDS = ("main", "side", "side_only")
USE_KEY = "side_use"                # the harness's name for what the call is for, used only to count the session's work by use


def check_cache_slot(req: dict) -> None:
    """ValueError for a `cache_slot` that is not "main" or "side", or that names a use switched off in the settings (a 400 before the answer starts)."""
    value = req.get(KEY)
    if value is not None and value not in KINDS:
        raise ValueError(f'{KEY}: "side" (the technical session), "side_only" or "main", not {value!r}')
    use = req.get(USE_KEY)
    if use is not None and not (isinstance(use, str) and 0 < len(use) <= 40 and use.replace("_", "").isalnum()):
        raise ValueError(f"{USE_KEY}: a short name of letters, digits and underscores")
    if value in ("side", "side_only") and isinstance(use, str):
        from tadamoo import side_uses
        if use in side_uses.BY_ID and not side_uses.enabled(use):               # a caller outside this process (the voice service) cannot read the switch
            raise ValueError(f"{USE_KEY}: {use} is switched off in the settings of the technical session")
