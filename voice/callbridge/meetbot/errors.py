"""A request the meeting routes refuse: its HTTP status, a code, and the sentence for the person."""
from __future__ import annotations


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str, **more) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.more = status, code, message, more

    def body(self) -> dict:
        return {"error": self.message, "code": self.code, **self.more}
