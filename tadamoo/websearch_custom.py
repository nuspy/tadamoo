"""Two lists the person keeps in Settings > Web search, used automatically in every search: custom search engines and custom page directories.

Both lists share the same shape (a name, an address with {query} where the words go, on or off) and the same mechanics: a search asks the address
with the words filled in and keeps the links of the answer with the words they are written with, so it works for an engine or a directory nobody wrote
a parser for. The two lists are kept separate because they answer different questions ("what did you find" vs "where should I look"), and the person
turns each entry on or off without losing it."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

KINDS = ("engines", "directories")
MAX_PER_KIND = 40
MAX_LINKS = 10
Fetch = Callable[[str], Tuple[int, str, bytes]]


FILE = "websearch_custom.json"


class CustomSearchError(ValueError):
    pass


def shared() -> "Store":
    """The one store of this process, in the web-search folder of the user data."""
    from tadamoo.websearch import user_dir
    return Store(user_dir() / FILE)


@dataclass
class Source:
    name: str
    url: str                   # with {query} where the words go
    enabled: bool = True


def _check(name: str, url: str) -> Tuple[str, str]:
    name = (name or "").strip()[:80]
    if not name:
        raise CustomSearchError("a name")
    url = (url or "").strip()
    if len(url) > 500 or "{query}" not in url:
        raise CustomSearchError("an address with {query} where the search words go")
    p = urlparse(url.replace("{query}", "x"))
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise CustomSearchError("an address that starts with http:// or https://, with no user name or password")
    return name, url


class Store:
    """The person's own engines and directories, in one small file next to the other web-search settings."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> Dict[str, List[Dict[str, Any]]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return {k: [r for r in (raw.get(k) if isinstance(raw, dict) else None) or [] if isinstance(r, dict) and r.get("name") and r.get("url")] for k in KINDS}

    def _write(self, data: Dict[str, List[Dict[str, Any]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _kind(kind: str) -> str:
        if kind not in KINDS:
            raise CustomSearchError("kind: engines or directories")
        return kind

    def list(self, kind: str) -> List[Source]:
        rows = self._read()[self._kind(kind)]
        return [Source(str(r["name"]), str(r["url"]), bool(r.get("enabled", True))) for r in rows]

    def list_all(self) -> Dict[str, List[Source]]:
        return {k: self.list(k) for k in KINDS}

    def add(self, kind: str, name: str, url: str) -> List[Source]:
        kind = self._kind(kind)
        name, url = _check(name, url)
        data = self._read()
        rows = [r for r in data[kind] if str(r["name"]).lower() != name.lower()]
        if len(rows) >= MAX_PER_KIND:
            raise CustomSearchError("too many: remove one first")
        rows.append({"name": name, "url": url, "enabled": True})
        data[kind] = rows
        self._write(data)
        return self.list(kind)

    def set_enabled(self, kind: str, name: str, enabled: bool) -> List[Source]:
        kind = self._kind(kind)
        data = self._read()
        found = False
        for r in data[kind]:
            if str(r["name"]).lower() == str(name or "").strip().lower():
                r["enabled"] = bool(enabled)
                found = True
        if not found:
            raise CustomSearchError("no such entry")
        self._write(data)
        return self.list(kind)

    def remove(self, kind: str, name: str) -> List[Source]:
        kind = self._kind(kind)
        data = self._read()
        keep = [r for r in data[kind] if str(r["name"]).lower() != str(name or "").strip().lower()]
        if len(keep) == len(data[kind]):
            raise CustomSearchError("no such entry")
        data[kind] = keep
        self._write(data)
        return self.list(kind)


class _Anchors(HTMLParser):
    """Every http(s) link of a page with the words it is first written with (relative addresses resolved against the page's own address);
    a script or style tag is never a link's text."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.found: List[Tuple[str, str]] = []
        self._href = None
        self._words: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        elif tag == "a" and not self._skip:
            href = dict(attrs).get("href")
            self._href, self._words = (urljoin(self.base, href.strip()) if href else None), []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href:
            text = re.sub(r"\s+", " ", "".join(self._words)).strip()
            if text and self._href.startswith(("http://", "https://")):
                self.found.append((self._href, text[:200]))
            self._href = None

    def handle_data(self, data):
        if self._href and not self._skip:
            self._words.append(data)


def search_one(source: Source, query: str, fetch: Fetch, limit: int = MAX_LINKS) -> Dict[str, Any]:
    """What one engine or directory answers for `query`: its outbound links with their words, as data from outside, never instructions."""
    url = source.url.replace("{query}", quote_plus(query.strip()[:200]))
    status, _ctype, body = fetch(url)
    if not 200 <= status < 400:
        return {"name": source.name, "url": url, "hits": [], "error": f"HTTP {status}"}
    parser = _Anchors(url)
    parser.feed(body.decode("utf-8", "replace"))
    seen, hits = set(), []
    for href, text in parser.found:
        if href in seen or href == url:
            continue
        seen.add(href)
        hits.append({"title": text, "url": href})
        if len(hits) >= limit:
            break
    return {"name": source.name, "url": url, "hits": hits}


def search_many(sources: List[Source], query: str, fetch: Fetch) -> List[Dict[str, Any]]:
    """Asks every enabled source; one that fails is reported with its reason and never stops the others."""
    out = []
    for s in sources:
        if not s.enabled:
            continue
        try:
            out.append(search_one(s, query, fetch))
        except Exception as e:                           # noqa: BLE001 - one source's failure is data, not a crash
            out.append({"name": s.name, "url": s.url, "hits": [], "error": str(e)[:160] or type(e).__name__})
    return out
