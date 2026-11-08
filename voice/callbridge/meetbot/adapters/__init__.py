"""Adapter per piattaforma: routing dall'URL della riunione."""
from __future__ import annotations

from .base import JoinResult, MeetingAdapter, SpeakerInfo  # noqa: F401
from .meet import MeetAdapter

_ADAPTERS: list[type[MeetingAdapter]] = [MeetAdapter]

try:  # adapter opzionali (aggiunti nelle fasi successive)
    from .teams import TeamsAdapter
    _ADAPTERS.append(TeamsAdapter)
except ImportError:
    pass
try:
    from .zoom import ZoomAdapter
    _ADAPTERS.append(ZoomAdapter)
except ImportError:
    pass


def adapter_for(url: str) -> MeetingAdapter | None:
    for cls in _ADAPTERS:
        if cls.matches(url):
            return cls()
    return None
