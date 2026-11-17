"""A web address as a source: one page or file. The server's ETag and Last-Modified are sent back as conditional headers, so an unchanged page costs a 304 and no body;
a server that has neither costs one download per look. The content hash settles what the headers cannot: a changed ETag over the same bytes is not a change."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlsplit

from tadamoo.rag.errors import RagError
from tadamoo.rag.sources.local import sha_of
from tadamoo.rag.sources.model import Fetched, Provider, Remote, SourceError, doc_name, need

URL_MAX = 500
DEPTH_MAX = 10                       # how many links deep a site is followed at most
LINKS_KEPT = 300                     # links of a page that are kept in its stamp, so that a page that has not changed can still be followed from
PAGE_SUFFIXES = ("", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".xhtml")
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


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def page_links(html: bytes, base: str) -> List[str]:
    """The addresses a page links to, made whole against `base`, without fragments, in the order they appear, each once."""
    parser = _Links()
    parser.feed(html[:2_000_000].decode("utf-8", "replace"))
    out: List[str] = []
    for href in parser.hrefs:
        full = urljoin(base, href.strip()).split("#")[0]
        if full.startswith(("http://", "https://")) and full not in out:
            out.append(full)
    return out


class UrlProvider(Provider):
    """One page or file, or a site followed link by link: `depth` is how many links deep (0: the address only), `limit` how many pages at most (none: no limit), `same_site` keeps
    to the address's own host. The settings are kept on the source, so each update follows the same links."""
    kind = "url"
    keys = ["sha256"]
    robots = True

    def validate(self, config: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"url": clean_address(config.get("url"))}
        depth, limit = config.get("depth", 0), config.get("limit")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= DEPTH_MAX:
            raise RagError(f"depth: a whole number from 0 (the address only) to {DEPTH_MAX}")
        if limit not in (None, "") and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise RagError("limit: a whole number of at least 1, or none")
        if depth:
            out["depth"] = depth
            out["same_site"] = config.get("same_site", True) is not False
            if limit not in (None, ""):
                out["limit"] = limit
        return out

    def label(self, config: Dict[str, Any]) -> str:
        parts = urlsplit(config["url"])
        text = parts.netloc + (parts.path if len(parts.path) > 1 else "")
        return text + (f" (depth {config['depth']})" if config.get("depth") else "")

    def _allowed_link(self, link: str, config: Dict[str, Any], root_host: str) -> bool:
        parts = urlsplit(link)
        if config.get("same_site", True) and (parts.hostname or "").lower() != root_host:
            return False
        suffix = Path(unquote(parts.path)).suffix.lower()
        return suffix in PAGE_SUFFIXES or suffix in self.ctx.formats()

    def list(self, config: Dict[str, Any], known: Dict[str, Dict[str, Any]], cancel: Optional[Callable[[], bool]] = None) -> List[Remote]:
        depth = int(config.get("depth") or 0)
        if not depth:
            return self._one(config["url"], known)
        root_host = (urlsplit(config["url"]).hostname or "").lower()
        limit = config.get("limit")
        out: List[Remote] = []
        seen = {config["url"]}
        level = [config["url"]]
        for d in range(depth + 1):
            following: List[str] = []
            for url in level:
                if cancel is not None and cancel():
                    raise SourceError("the reading was cancelled", "cancelled")
                if limit and len(out) >= limit:
                    return out
                try:
                    got = self._one(url, known)
                except SourceError:
                    if url == config["url"]:
                        raise                                              # the address itself is what failed; a page deeper in does not stop the others
                    if url in known:
                        out.append(Remote(url, missing=True, note="the page is not there now", origin=url))
                    continue
                out += got
                if d < depth:
                    for link in (got[0].stamp.get("links") or []) if got else []:
                        if link not in seen and self._allowed_link(link, config, root_host):
                            seen.add(link)
                            following.append(link)
            level = following
        return out[:limit] if limit else out

    def _one(self, url: str, known: Dict[str, Dict[str, Any]]) -> List[Remote]:
        old = known.get(url) or {}
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
        if ext in (".html", ".htm"):                                           # the links are kept with the page, so an unchanged page can still be followed from
            stamp["links"] = page_links(payload.read_bytes(), got.url)[:LINKS_KEPT]
        return [Remote(url, stamp["name"], stamp, got.bytes, origin=got.url, payload=payload)]

    def fetch(self, config: Dict[str, Any], remote: Remote, into: Path) -> Fetched:
        into.mkdir(parents=True, exist_ok=True)
        target = into / ("u" + hashlib.sha1(remote.key.encode("utf-8")).hexdigest()[:10] + Path(remote.name).suffix.lower())
        if remote.payload is not None and remote.payload.is_file():
            shutil.move(str(remote.payload), target)
        else:
            got = self.ctx.http.get(remote.key, robots=True, to=target)
            if got.status != 200:
                raise SourceError(f"{remote.key}: the page did not come (HTTP {got.status})", "http")
        sha = sha_of(target)
        stamp = dict(remote.stamp, sha256=sha)
        return Fetched(target, remote.name, sha, target.stat().st_size, stamp, remote.origin, version=(stamp.get("last_modified") or stamp.get("etag") or "")[:60])

