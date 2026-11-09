"""The permission level of each channel that talks to the agent, kept by the server in one file.

A level is one of the Chat's permission modes, from the least to the most the agent may do without asking. A channel is where a turn comes
from: the Chat page, the voice service, the Telegram bot. The file holds only what the person set; a channel never set has its default.
A request may ask for its channel's level or a lower one, never a higher one: the level is raised only by changing the setting.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

FILE = "permission_levels.json"
LEVELS = ("plan", "read_only", "default", "auto")      # from the least to the most the agent may do without asking
CHANNELS = ("chat", "voice", "telegram")
DEFAULTS = {"chat": "default", "voice": "default", "telegram": "default"}


class PermissionLevelError(ValueError):
    """A refused channel, level or request, with a code for the answer."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def check_channel(channel: Any) -> str:
    if channel not in CHANNELS:
        raise PermissionLevelError("The channel is one of: " + ", ".join(CHANNELS) + ".", "bad_channel")
    return channel


def check_level(level: Any) -> str:
    if level not in LEVELS:
        raise PermissionLevelError("The permission level is one of: " + ", ".join(LEVELS) + ".", "bad_level")
    return level


def rank(level: str) -> int:
    return LEVELS.index(check_level(level))


def lower(a: str, b: str) -> str:
    """The lower of two levels."""
    return a if rank(a) <= rank(b) else b


class PermissionLevels:
    """`permission_levels.json`: {channel: level} for the channels the person set. Replaced whole on every change."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {c: raw[c] for c in CHANNELS if raw.get(c) in LEVELS}

    def _write(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, channel: str) -> str:
        return self._read().get(check_channel(channel), DEFAULTS[channel])

    def is_set(self, channel: str) -> bool:
        return check_channel(channel) in self._read()

    def overview(self) -> Dict[str, Any]:
        stored = self._read()
        return {"levels": {c: stored.get(c, DEFAULTS[c]) for c in CHANNELS}, "set": {c: c in stored for c in CHANNELS},
                "order": list(LEVELS), "channels": list(CHANNELS)}

    def set(self, channel: Any, level: Any) -> str:
        channel, level = check_channel(channel), check_level(level)
        with self._lock:
            data = self._read()
            data[channel] = level
            self._write(data)
        return level

    def adopt(self, channel: str, level: Any) -> bool:
        """Keeps a level found in another settings file, only when this channel was never set here. True when it was kept."""
        if level not in LEVELS:
            return False
        with self._lock:
            data = self._read()
            if check_channel(channel) in data:
                return False
            data[channel] = level
            self._write(data)
        return True

    def for_request(self, channel: str, requested: Optional[Any]) -> str:
        """The level a request of this channel gets: the channel's own when it names none, the one it names when that is not higher."""
        level = self.get(channel)
        if requested is None:
            return level
        check_level(requested)
        if rank(requested) > rank(level):
            raise PermissionLevelError(f"The {channel} level is '{level}'; a request may not ask for '{requested}'. "
                                       f"Raise the level of {channel} in its settings first.", "level_above_channel")
        return requested
