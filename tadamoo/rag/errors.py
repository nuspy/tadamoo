"""What can go wrong in the knowledge archives, in words for the person."""
from __future__ import annotations


class RagError(Exception):
    """The request cannot be done; the message says what to change. `code` is `not_found` or `bad_request`."""

    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code
