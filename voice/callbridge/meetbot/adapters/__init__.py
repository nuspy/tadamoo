"""Platform adapters, chosen from the address of the meeting. Webex, Slack and Telegram are not built."""
from __future__ import annotations

from .base import ChatMessage, JoinResult, MeetingAdapter, SpeakerInfo  # noqa: F401
from .meet import MeetAdapter
from .teams import TeamsAdapter
from .zoom import ZoomAdapter

_ADAPTERS: list[type[MeetingAdapter]] = [MeetAdapter, TeamsAdapter, ZoomAdapter]


def adapter_for(url: str) -> MeetingAdapter | None:
    for cls in _ADAPTERS:
        if cls.matches(url):
            return cls()
    return None
