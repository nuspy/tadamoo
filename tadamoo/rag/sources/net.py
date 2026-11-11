"""A polite reader of web addresses for the refresh: it says who it is, keeps a pause between requests to the same site, follows a few redirects (http and https
only), asks the site's robots.txt where the kind of source is held to it, sends the conditional headers it was given (a 304 costs no body), caps the size of what it
downloads and the time a request takes, and waits and tries once more when a site says "slow down" (429, 503 with Retry-After)."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin, urlsplit

from tadamoo.rag.sources.model import SourceError

AGENT = "TaDaMoo-RAG-refresh/1.0"
MAX_HOPS = 5
RETRY_WAIT_MAX_S = 30.0
ROBOTS_MAX = 512 << 10
CHUNK = 64 << 10


@dataclass
class Response:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""
    sha256: str = ""
    bytes: int = 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def is_local(host: str) -> bool:
    """A host on this PC or this network by its address: loopback, private ranges, `localhost`. A name that is not an address is not looked up."""
    if host in ("localhost", ""):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


class Http:
    def __init__(self, settings: Callable[[], Dict[str, Any]], clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 opener: Optional[urllib.request.OpenerDirector] = None) -> None:
        self.settings, self.clock, self.sleep = settings, clock, sleep
        self._opener = opener or urllib.request.build_opener(_NoRedirect)
        self._last: Dict[str, float] = {}
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self.requests = 0

    def agent(self) -> str:
        contact = str(self.settings().get("contact") or "")
        return f"{AGENT} (+{contact})" if contact else AGENT

    def _pause(self, host: str) -> None:
        gap = float(self.settings().get("min_interval_s") or 0)
        if gap > 0 and not is_local(host):
            wait = self._last.get(host, -1e9) + gap - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.clock()

    def allowed(self, url: str) -> bool:
        """Whether robots.txt lets this reader fetch the address; a site with none lets it."""
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            parser: Optional[urllib.robotparser.RobotFileParser] = urllib.robotparser.RobotFileParser()
            try:
                got = self._once(base + "/robots.txt", {}, None, ROBOTS_MAX, None)
            except SourceError:
                raise SourceError(f"robots.txt of {parts.netloc} could not be read: the site is not read", "refused") from None
            if got.status == 200:
                parser.parse(got.body.decode("utf-8", "replace").splitlines())
            elif 400 <= got.status < 500:
                parser = None                                  # no robots.txt: everything is allowed
            else:
                parser.disallow_all = True                     # type: ignore[attr-defined]
            self._robots[base] = parser
        parser = self._robots[base]
        return True if parser is None else parser.can_fetch(AGENT, url)

    def get(self, url: str, headers: Optional[Dict[str, str]] = None, robots: bool = False, max_bytes: Optional[int] = None, to: Optional[Path] = None) -> Response:
        """The answer to GET `url`: 200 (the body in `body`, or in the file `to` with its `sha256` and `bytes`) or 304. Anything else is a SourceError."""
        cap = int(max_bytes if max_bytes is not None else float(self.settings().get("max_download_mb") or 50) * (1 << 20))
        sent, retried = dict(headers or {}), False
        for _ in range(MAX_HOPS + 2):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise SourceError(f"{url}: only http and https addresses are read", "refused")
            if robots and self.settings().get("respect_robots", True) and not self.allowed(url):
                raise SourceError(f"{parts.netloc} asks robots not to read {parts.path or '/'}: it is not read", "refused")
            got = self._once(url, sent, to, cap, parts.hostname)
            if got.status in (301, 302, 303, 307, 308):
                where = got.headers.get("location", "")
                if not where:
                    raise SourceError(f"{url}: a redirect with no place to go", "bad_response")
                url = urljoin(url, where)
                continue
            if got.status in (429, 503):
                wait = got.headers.get("retry-after", "")
                if wait.isdigit() and float(wait) <= RETRY_WAIT_MAX_S and not retried:
                    retried = True
                    self.sleep(float(wait))
                    continue
                raise SourceError(f"{parts.netloc} says to slow down (HTTP {got.status}): tried again later", "http")
            if got.status in (200, 304):
                got.url = url
                return got
            raise SourceError(f"{url}: HTTP {got.status}", "refused" if got.status in (401, 403) else "http")
        raise SourceError(f"{url}: too many redirects", "http")

    def _once(self, url: str, headers: Dict[str, str], to: Optional[Path], cap: int, host: Optional[str]) -> Response:
        parts = urlsplit(url)
        self._pause(host or parts.hostname or "")
        self.requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": self.agent(), "Accept-Encoding": "gzip", **headers})
        timeout = float(self.settings().get("request_timeout_s") or 30)
        try:
            resp = self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, b"", url)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise SourceError(f"{parts.netloc}: {reason}", "offline") from None
        with resp:
            head = {k.lower(): v for k, v in resp.headers.items()}
            if int(head.get("content-length") or 0) > cap:
                raise SourceError(f"{url}: {int(head['content-length']):,} bytes is more than the {cap:,} a download may be", "too_big")
            inflate = zlib.decompressobj(31) if head.get("content-encoding", "").lower() == "gzip" else None
            sha, total, parts_out = hashlib.sha256(), 0, []
            out = open(to, "wb") if to is not None else None
            try:
                while True:
                    try:
                        piece = resp.read(CHUNK)
                    except (socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                        raise SourceError(f"{parts.netloc}: the download broke ({e})", "offline") from None
                    if not piece:
                        break
                    data = inflate.decompress(piece, cap + 1 - total) if inflate else piece
                    total += len(data)
                    if total > cap:
                        raise SourceError(f"{url}: more than {cap:,} bytes: not downloaded", "too_big")
                    sha.update(data)
                    if out is not None:
                        out.write(data)
                    else:
                        parts_out.append(data)
            except zlib.error:
                raise SourceError(f"{url}: the answer is not valid gzip", "bad_response") from None
            finally:
                if out is not None:
                    out.close()
            return Response(resp.status, head, b"".join(parts_out), url, sha.hexdigest(), total)
