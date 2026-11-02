"""What a route can do: read from the provider's type and the person's own record of it, and used by the client when it shapes a request.

A route to the model loaded in this Basalt (or to any local type) has no profile of its own: nothing about its requests changes. A
cloud route carries:

* `cache_control`: the request marks the stable start of the prompt (the tools and the system text) for the provider's cache. On for
  Anthropic routes unless the record says `"cache_control": false`.
* `context_window` and `max_output`: whole numbers of tokens the person put in the provider's record (`set_limits`), else unknown (None).
  The client keeps a request's output inside `max_output`, and the agent plans its context by `context_window`.
* `stateless`: every call carries the whole conversation. `accepts_ids` is the other kind, a provider that can refer to what it already
  holds by id; none of the types here does, and the record may say `"accepts_ids": true` for one that does.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RouteProfile:
    local: bool = True
    cache_control: bool = False
    context_window: Optional[int] = None
    max_output: Optional[int] = None
    accepts_ids: bool = False

    @property
    def stateless(self) -> bool:
        return not self.accepts_ids

    def as_dict(self) -> Dict[str, Any]:
        return {"local": self.local, "cache_control": self.cache_control, "context_window": self.context_window,
                "max_output": self.max_output, "stateless": self.stateless, "accepts_ids": self.accepts_ids}


def _tokens(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def route_profile(prov: Optional[Dict[str, Any]], spec: Optional[Dict[str, Any]] = None) -> RouteProfile:
    """The profile of a route: None for a provider means the loaded model, which has the plain one."""
    spec = spec or {}
    if not prov or spec.get("this_server"):
        return RouteProfile()
    return RouteProfile(
        local=bool(spec.get("local")),
        cache_control=spec.get("api") == "anthropic" and prov.get("cache_control") is not False,
        context_window=_tokens(prov.get("context_window")),
        max_output=_tokens(prov.get("max_output")),
        accepts_ids=prov.get("accepts_ids") is True,
    )
