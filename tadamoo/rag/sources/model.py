"""What a source is and what a provider gives: the shapes shared by the refresh and the providers of each kind of source.

A source of an archive is `{id, kind, label, config, added}`; kinds: `file`, `folder`, `url`, `mediawiki`, `wikibridge`. A provider lists what the source holds *now*
(`Remote`: a key that is stable for the item, the name of the document, and a `stamp`, the cheap fingerprint a change shows in: modification time and size, ETag,
revision id, the wiki's `updated`) and fetches one item into a file the ordinary ingestion reads (`Fetched`). Nothing here reads a document or touches an archive."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.rag.errors import RagError

KINDS = ("file", "folder", "url", "mediawiki", "wikibridge")
NAME_MAX = 120
_SEP = re.compile(r"[\x00-\x1f\\/]+")


class SourceError(Exception):
    """A source could not be read now. `kind`: offline, refused (HTTP 4xx/robots), http, too_big, format, bad_response, cancelled. Nothing is concluded from it
    about the documents of the source: they are neither changed nor gone."""

    def __init__(self, message: str, kind: str = "http") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Remote:
    key: str                                   # stable inside the source: a relative path, a page title, a URL
    name: str = ""                             # the document's name (with its extension)
    stamp: Dict[str, Any] = field(default_factory=dict)
    size: int = 0
    missing: bool = False                      # the source says the item is not there now (a deleted page)
    note: str = ""
    origin: str = ""                           # where to see it (a path, an address)
    payload: Optional[Path] = None             # content already downloaded while looking (a changed address)


@dataclass
class Fetched:
    path: Path
    name: str
    sha256: str
    bytes: int
    stamp: Dict[str, Any] = field(default_factory=dict)
    origin: str = ""
    version: str = ""                          # what to show for the version: a revision id, a date


def doc_name(text: str, ext: str) -> str:
    """A document name from a title or a path: no separators, at most NAME_MAX characters before `ext`."""
    stem = _SEP.sub(" - ", str(text or "")).strip(" .-") or "document"
    return stem[:NAME_MAX].rstrip(" .-") + ext


def same_stamp(old: Dict[str, Any], new: Dict[str, Any], keys: Optional[List[str]] = None) -> bool:
    """Whether two stamps say the same thing, looking at `keys` (every key of `new` when None). An empty stamp is never the same: nothing is known."""
    if not old or not new:
        return False
    return all(old.get(k) == new.get(k) for k in (keys or list(new)))


class Provider:
    """The kinds of source. A subclass sets `kind` and gives `validate`, `list` and `fetch`."""

    kind = ""
    keys: Optional[List[str]] = None           # the stamp keys that tell a change (None: all)
    robots = False                             # whether a web page of this kind is held to the site's robots.txt

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def validate(self, config: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def label(self, config: Dict[str, Any]) -> str:
        return self.kind

    def list(self, config: Dict[str, Any], known: Dict[str, Dict[str, Any]], cancel: Optional[Callable[[], bool]] = None) -> List[Remote]:
        """What the source holds now, from the cheapest question it can answer. `known` maps a key to the stamp recorded for it when it was last read."""
        raise NotImplementedError

    def same(self, old: Dict[str, Any], new: Dict[str, Any]) -> bool:
        return same_stamp(old, new, self.keys)

    def confirm(self, config: Dict[str, Any], remote: Remote, doc: Dict[str, Any]) -> Optional[bool]:
        """After the stamp says "changed": True when the content really changed, False when it is the same (a touched file), None when it cannot be told without fetching."""
        return None

    def fetch(self, config: Dict[str, Any], remote: Remote, into: Path) -> Fetched:
        """The item written to a new file inside the folder `into`."""
        raise NotImplementedError


def need(config: Dict[str, Any], key: str, kind: type = str) -> Any:
    value = config.get(key)
    if not isinstance(value, kind) or (kind is str and not value.strip()):
        raise RagError(f"{key}: it needs a value")
    return value.strip() if kind is str else value
