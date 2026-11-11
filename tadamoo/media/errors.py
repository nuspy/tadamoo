"""What a picture or video job can end with besides a file."""
from __future__ import annotations


class MediaError(Exception):
    """The job did not make a file; the message says why in the words of whoever knew."""

    def __init__(self, message: str, code: str = "failed") -> None:
        super().__init__(message)
        self.code = code


class Refused(MediaError):
    """The server could not lend the VRAM (`reason` is the broker's: not_enough, busy, unavailable, no_engine, disabled, engine_died)."""

    def __init__(self, reason: str, message: str, message_it: str = "") -> None:
        super().__init__(message or f"the VRAM was not lent ({reason})", "refused")
        self.reason, self.message_it = reason, message_it


class Cancelled(MediaError):
    def __init__(self) -> None:
        super().__init__("the job was cancelled", "cancelled")
