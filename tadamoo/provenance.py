"""Where a message to a conversation comes from, and how far its words are trusted.

The person's own messages are the person's. A message a schedule or a plan the person set up delivers is an order (`cron`, `plan`).
What another conversation sends (`agent:<name>`) is data, however it is worded, unless that sender is one the conversation was told
may give orders: it reaches the model fenced as data, and the permission manager treats any instruction found in it like one found in
a web page. Anything that is not a known form is taken for another conversation's text.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterable, List

ORDERS = ("cron", "plan")
VALID = re.compile(r"^(?:cron(?::[A-Za-z0-9_.-]{1,40})?|plan:[A-Za-z0-9_-]{1,64}|agent:[A-Za-z0-9_-]{1,40})$")
SEEN_FILE = "deliveries_seen.json"
SEEN_KEEP = 200


def clean(value: Any) -> str:
    """The provenance of a delivered message: itself when it is a known form, else the text of another conversation."""
    text = value if isinstance(value, str) else ""
    return text if VALID.match(text) else "agent:unknown"


def family(origin: str) -> str:
    return origin.partition(":")[0]


def may_command(origin: str, senders: Iterable[str]) -> bool:
    """A message from another conversation that was told it may give orders: the sender's name matches one of the patterns."""
    name = origin.partition(":")[2]
    return family(origin) == "agent" and any(fnmatchcase(name, pattern) for pattern in senders)


class SeenDeliveries:
    """The ids of the deliveries this conversation took, kept in a file so that a message written twice is one turn, restart or not."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / SEEN_FILE
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.ids: List[str] = [i for i in data.get("ids", []) if isinstance(i, str)][-SEEN_KEEP:]
        except (OSError, ValueError, AttributeError):
            self.ids = []

    def __contains__(self, delivery_id: str) -> bool:
        return delivery_id in self.ids

    def add(self, delivery_id: str) -> None:
        if delivery_id in self.ids:
            return
        self.ids = (self.ids + [delivery_id])[-SEEN_KEEP:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".seen.")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"ids": self.ids}, f)
            os.replace(tmp, str(self.path))
        except OSError:
            pass                                               # the list in memory still guards this process
