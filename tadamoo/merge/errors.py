"""The one kind of problem the merge reports."""
from __future__ import annotations

from typing import Any


class MergeProblem(ValueError):
    """Why a merge cannot go on, in words for the person; `code` is for the page."""

    def __init__(self, code: str, message: str, **data: Any) -> None:
        super().__init__(message)
        self.code, self.data = code, data
