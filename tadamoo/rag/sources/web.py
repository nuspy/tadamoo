"""A web address as a source: one page or file. The server's ETag and Last-Modified are sent back as conditional headers, so an unchanged page costs a 304 and no body;
a server that has neither costs one download per look. The content hash settles what the headers cannot: a changed ETag over the same bytes is not a change."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote, urlsplit

from tadamoo.rag.errors import RagError
from tadamoo.rag.sources.local import sha_of
from tadamoo.rag.sources.model import Fetched, Provider, Remote, SourceError, doc_name, need

URL_MAX = 500
TYPES = {"text/html": ".html", "application/xhtml+xml": ".html", "text/plain": ".txt", "text/markdown": ".md", "application/pdf": ".pdf",
         "application/epub+zip": ".epub", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}
_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)


def clean_address(value: Any) -> str:
    """An http or https address without a user name or a password (a secret does not belong in a source)."""
    text = need({"url": value}, "url")
    parts = urlsplit(text)
    if len(text) > URL_MAX or parts.scheme not in ("http", "https") or not parts.hostname:
        raise RagError(f"url: an http or https address of at most {URL_MAX} characters")
    if parts.username or parts.password:
        raise RagError("url: no user name or password in the address")
    return text.split("#")[0]


def _extension(content_type: str, url: str, allowed: List[str]) -> str:
    kind = content_type.split(";")[0].strip().lower()
    ext = TYPES.get(kind) or (mimetypes.guess_extension(kind) or "")
    if ext not in allowed:
        ext = Path(unquote(urlsplit(url).path)).suffix.lower()
    if ext not in allowed:
        raise SourceError(f"the address gives {kind or 'an unknown kind of file'}, which is not a format that can be read: use " + ", ".join(allowed), "format")
    return ext


def _title(path: Path, ext: str) -> str:
    if ext not in (".html", ".htm"):
        return ""
    with path.open("rb") as f:
        found = _TITLE.search(f.read(16384))
    return re.sub(r"\s+", " ", found.group(1).decode("utf-8", "replace")).strip() if found else ""


class UrlProvider(Provider):
    kind = "url"
    keys = ["sha256"]
    robots = True

    def validate(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return {"url": clean_address(config.get("url"))}

    def label(self, config: Dict[str, Any]) -> str:
        parts = urlsplit(config["url"])
        return parts.netloc + (parts.path if len(parts.path) > 1 else "")

    def list(self, config: Dict[str, Any], known: Dict[str, Dict[str, Any]], cancel: Optional[Callable[[], bool]] = None) -> List[Remote]:
        url, old = config["url"], known.get(config["url"]) or {}
        headers = {}
        if old.get("etag"):
            headers["If-None-Match"] = old["etag"]
        if old.get("last_modified"):
            headers["If-Modified-Since"] = old["last_modified"]
        self.ctx.stage.mkdir(parents=True, exist_ok=True)
        payload = self.ctx.stage / ("u" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12])
        got = self.ctx.http.get(url, headers, robots=True, to=payload)
        if got.status == 304:
            payload.unlink(missing_ok=True)
            return [Remote(url, old.get("name", ""), dict(old), origin=url, note="not modified")]
        try:
            ext = _extension(got.headers.get("content-type", ""), got.url, self.ctx.formats())
        except SourceError:
            payload.unlink(missing_ok=True)
            raise
        title = _title(payload, ext)
        last = unquote(Path(urlsplit(got.url).path).stem) or urlsplit(got.url).hostname or "page"
        stamp = {"sha256": got.sha256, "etag": got.headers.get("etag", ""), "last_modified": got.headers.get("last-modified", ""), "url": got.url, "name": doc_name(title or last, ext)}
        return [Remote(url, stamp["name"], stamp, got.bytes, origin=got.url, payload=payload)]

    def fetch(self, config: Dict[str, Any], remote: Remote, into: Path) -> Fetched:
        into.mkdir(parents=True, exist_ok=True)
        target = into / ("u" + hashlib.sha1(remote.key.encode("utf-8")).hexdigest()[:10] + Path(remote.name).suffix.lower())
        if remote.payload is not None and remote.payload.is_file():
            shutil.move(str(remote.payload), target)
        else:
            got = self.ctx.http.get(config["url"], robots=True, to=target)
            if got.status != 200:
                raise SourceError(f"{config['url']}: the page did not come (HTTP {got.status})", "http")
        sha = sha_of(target)
        stamp = dict(remote.stamp, sha256=sha)
        return Fetched(target, remote.name, sha, target.stat().st_size, stamp, remote.origin, version=(stamp.get("last_modified") or stamp.get("etag") or "")[:60])

