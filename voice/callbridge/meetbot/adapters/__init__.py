"""The registry of platform adapters. The address of a meeting picks the adapter; the planner and the page list only the registered names.

To add a platform: write a `MeetingAdapter` subclass (`base.py` says what to implement), then `register(MyAdapter)` here or from the module that
defines it. `platform` is the name shown to the person and stored in plans and audits; it must be unique and not `generic`. Webex, Slack and
Telegram are not built."""
from __future__ import annotations

from .base import ChatMessage, JoinResult, MeetingAdapter, SpeakerInfo  # noqa: F401

_ADAPTERS: list[type[MeetingAdapter]] = []


def register(cls: type[MeetingAdapter]) -> type[MeetingAdapter]:
    """Add a platform. Refuses an adapter without its own name, with a name that is taken, or that does not say which addresses are its."""
    if not (isinstance(cls, type) and issubclass(cls, MeetingAdapter)):
        raise TypeError("an adapter is a subclass of MeetingAdapter")
    if cls.platform in ("", "generic") or any(a.platform == cls.platform for a in _ADAPTERS):
        raise ValueError(f"the platform name {cls.platform!r} is not unique")
    if cls.matches is MeetingAdapter.matches:
        raise TypeError("an adapter must say which addresses are its: implement matches(url)")
    _ADAPTERS.append(cls)
    return cls


def adapter_for(url: str) -> MeetingAdapter | None:
    for cls in _ADAPTERS:
        if cls.matches(url):
            return cls()
    return None


def detect(url: str) -> str | None:
    """The platform name of a meeting address, or None when no registered adapter takes it."""
    for cls in _ADAPTERS:
        if cls.matches(url):
            return cls.platform
    return None


def platform_names() -> list[str]:
    return [cls.platform for cls in _ADAPTERS]


from .meet import MeetAdapter  # noqa: E402
from .teams import TeamsAdapter  # noqa: E402
from .zoom import ZoomAdapter  # noqa: E402

for _cls in (MeetAdapter, TeamsAdapter, ZoomAdapter):
    register(_cls)
