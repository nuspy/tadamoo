"""The public services the online search asks, with the standard library only.

Every request is a GET with a declared User-Agent, a timeout and a cap on the bytes read, follows a redirect only inside the same
host, and carries no cookie and no credential: the one exception is the optional GitHub token, sent to the GitHub API address
and nowhere else. What leaves this PC is the address asked, which holds the words of the need (a search text) or the name of a
package or repository.

The addresses are arguments (`bases`), so a test points them at a local server.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

USER_AGENT = "basalt-capability-search/1.0 (+https://github.com/nuspy/basalt_win-GUI)"
DEFAULT_BASES: Dict[str, str] = {
    "registry": "https://registry.modelcontextprotocol.io",
    "npm": "https://registry.npmjs.org",
    "npm_downloads": "https://api.npmjs.org",
    "pypi": "https://pypi.org",
    "github": "https://api.github.com",
    "github_raw": "https://raw.githubusercontent.com",
}
TIMEOUT_S = 12.0
MAX_BYTES = 2_000_000
PACKUMENT_BYTES = 4_000_000


class SourceError(Exception):
    """A source could not be asked or did not answer as expected; the message says what and is safe to show."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class RateLimited(SourceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "rate_limited")


class _SameHost(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):          # type: ignore[no-untyped-def]
        a, b = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (a.scheme, a.netloc.lower()) != (b.scheme, b.netloc.lower()):
            raise SourceError(f"{a.netloc} sent the request to another host ({b.netloc}); it was not followed", "redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Http:
    def __init__(self, timeout: float = TIMEOUT_S, max_bytes: int = MAX_BYTES, user_agent: str = USER_AGENT) -> None:
        self.timeout, self.max_bytes, self.user_agent = timeout, max_bytes, user_agent
        self._opener = urllib.request.build_opener(_SameHost())          # no cookie processor: nothing is kept between requests

    def get(self, url: str, headers: Optional[Dict[str, str]] = None, limit: Optional[int] = None) -> Tuple[int, Dict[str, str], bytes]:
        cap = limit or self.max_bytes
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json", **(headers or {})}, method="GET")
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                declared = r.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > cap:
                    raise SourceError(f"{_host(url)} answered with {int(declared):,} bytes, more than the {cap:,} allowed", "too_large")
                body = r.read(cap + 1)
                if len(body) > cap:
                    raise SourceError(f"{_host(url)} answered with more than {cap:,} bytes", "too_large")
                return r.status, {k.lower(): v for k, v in r.headers.items()}, body
        except urllib.error.HTTPError as e:
            head = {k.lower(): v for k, v in (e.headers or {}).items()}
            text = ""
            try:
                text = e.read(2000).decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (403, 429) and ("rate limit" in text.lower() or head.get("x-ratelimit-remaining") == "0" or e.code == 429):
                raise RateLimited(_limit_message(_host(url), head)) from None
            raise SourceError(f"{_host(url)} answered {e.code}", "not_found" if e.code == 404 else f"http_{e.code}") from None
        except SourceError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise SourceError(f"{_host(url)} could not be reached ({type(reason).__name__})", "unreachable") from None

    def json(self, url: str, headers: Optional[Dict[str, str]] = None, limit: Optional[int] = None) -> Any:
        _, _, body = self.get(url, headers, limit)
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            raise SourceError(f"{_host(url)} did not answer with JSON", "bad_answer") from None


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url[:40]


def _limit_message(host: str, head: Dict[str, str]) -> str:
    when = ""
    reset = head.get("x-ratelimit-reset", "")
    if reset.isdigit():
        when = " It resets at " + time.strftime("%H:%M", time.localtime(int(reset))) + "."
    return f"{host} refused more requests for now (its limit for unidentified requests was reached).{when}"


def q(text: str) -> str:
    return urllib.parse.quote(text, safe="")


def parse_github_url(url: Any) -> Optional[Tuple[str, str]]:
    """(owner, repository) from the ways a package names a GitHub repository, else None."""
    if not isinstance(url, str):
        return None
    m = re.match(r"^(?:git\+)?(?:https?://|ssh://git@|git://)?(?:www\.)?github\.com[/:]([A-Za-z0-9][A-Za-z0-9_.\-]*)/([A-Za-z0-9_.\-]+?)(?:\.git)?(?:[/#?].*)?$", url.strip()) \
        or re.match(r"^git@github\.com:([A-Za-z0-9][A-Za-z0-9_.\-]*)/([A-Za-z0-9_.\-]+?)(?:\.git)?$", url.strip()) \
        or re.match(r"^github:([A-Za-z0-9][A-Za-z0-9_.\-]*)/([A-Za-z0-9_.\-]+)$", url.strip())
    return (m.group(1), m.group(2)) if m else None


class RegistryClient:
    """The official MCP registry: GET /v0.1/servers?search=<name part>&version=latest."""

    def __init__(self, http: Http, base: str) -> None:
        self.http, self.base = http, base.rstrip("/")

    def search(self, word: str, limit: int = 30) -> List[Dict[str, Any]]:
        data = self.http.json(f"{self.base}/v0.1/servers?search={q(word)}&limit={limit}&version=latest")
        items = data.get("servers") if isinstance(data, dict) else None
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("server"), dict)] if isinstance(items, list) else []


class NpmClient:
    def __init__(self, http: Http, base: str, downloads_base: str) -> None:
        self.http, self.base, self.downloads = http, base.rstrip("/"), downloads_base.rstrip("/")

    def search(self, text: str, size: int = 10) -> List[Dict[str, Any]]:
        data = self.http.json(f"{self.base}/-/v1/search?text={q(text)}&size={size}")
        out = []
        for o in (data.get("objects") if isinstance(data, dict) else None) or []:
            p = o.get("package") if isinstance(o, dict) else None
            if not isinstance(p, dict) or not isinstance(p.get("name"), str):
                continue
            dl = o.get("downloads") if isinstance(o.get("downloads"), dict) else {}
            publisher = p.get("publisher") if isinstance(p.get("publisher"), dict) else {}
            links = p.get("links") if isinstance(p.get("links"), dict) else {}
            out.append({"name": p["name"], "version": str(p.get("version") or ""), "description": str(p.get("description") or ""),
                        "date": p.get("date"), "license": p.get("license") if isinstance(p.get("license"), str) else "",
                        "weekly": dl.get("weekly") if isinstance(dl.get("weekly"), int) else None,
                        "repository": links.get("repository") or "", "trusted_publisher": bool(publisher.get("trustedPublisher"))})
        return out

    def release(self, name: str, version: str) -> Dict[str, Any]:
        doc = self.http.json(f"{self.base}/{urllib.parse.quote(name, safe='@')}/{q(version)}")
        if not isinstance(doc, dict):
            raise SourceError("npm answered in an unexpected shape", "bad_answer")
        return doc

    def history(self, name: str) -> Dict[str, Any]:
        """Release dates from the full record of the package: {"first", "last", "count", "last_year"}; empty if the record is too large."""
        try:
            doc = self.http.json(f"{self.base}/{urllib.parse.quote(name, safe='@')}", limit=PACKUMENT_BYTES)
        except SourceError as e:
            if e.code == "too_large":
                return {}
            raise
        times = doc.get("time") if isinstance(doc, dict) and isinstance(doc.get("time"), dict) else {}
        dates = sorted(v for k, v in times.items() if k not in ("created", "modified") and isinstance(v, str))
        if not dates:
            return {}
        return {"first": dates[0], "last": dates[-1], "count": len(dates), "dates": dates}

    def weekly_downloads(self, name: str) -> Optional[int]:
        try:
            data = self.http.json(f"{self.downloads}/downloads/point/last-week/{urllib.parse.quote(name, safe='@')}")
        except SourceError:
            return None
        n = data.get("downloads") if isinstance(data, dict) else None
        return n if isinstance(n, int) else None


class PypiClient:
    def __init__(self, http: Http, base: str) -> None:
        self.http, self.base = http, base.rstrip("/")

    def release(self, name: str, version: str = "") -> Dict[str, Any]:
        path = f"/pypi/{q(name)}/{q(version)}/json" if version else f"/pypi/{q(name)}/json"
        doc = self.http.json(self.base + path, limit=3_000_000)
        if not isinstance(doc, dict) or not isinstance(doc.get("info"), dict):
            raise SourceError("PyPI answered in an unexpected shape", "bad_answer")
        return doc


class GithubClient:
    def __init__(self, http: Http, base: str, raw_base: str, token: Callable[[], str] = lambda: "") -> None:
        self.http, self.base, self.raw = http, base.rstrip("/"), raw_base.rstrip("/")
        self._token = token
        self.requests = 0

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        token = self._token()
        if token:
            h["Authorization"] = "Bearer " + token
        return h

    def _limited(self, e: RateLimited) -> RateLimited:
        hint = (" Even with the token in the vault the limit is reached." if self._token()
                else " Put a GitHub token in the vault (a secret entry named github, field value) to raise the limit.")
        return RateLimited(str(e) + hint)

    def _api(self, path: str, accept: str = "application/vnd.github+json") -> Any:
        self.requests += 1
        try:
            return self.http.json(self.base + path, self._headers(accept))
        except RateLimited as e:
            raise self._limited(e) from None

    @staticmethod
    def facts(repo: Dict[str, Any]) -> Dict[str, Any]:
        lic = repo.get("license") if isinstance(repo.get("license"), dict) else {}
        return {"full_name": str(repo.get("full_name") or ""), "stars": repo.get("stargazers_count"), "forks": repo.get("forks_count"),
                "open_issues": repo.get("open_issues_count"), "pushed_at": repo.get("pushed_at"), "created_at": repo.get("created_at"),
                "archived": bool(repo.get("archived")), "disabled": bool(repo.get("disabled")), "license": str(lic.get("spdx_id") or lic.get("name") or ""),
                "default_branch": str(repo.get("default_branch") or "main"), "description": str(repo.get("description") or ""),
                "topics": [t for t in repo.get("topics", []) if isinstance(t, str)] if isinstance(repo.get("topics"), list) else []}

    def repo(self, owner: str, name: str) -> Dict[str, Any]:
        data = self._api(f"/repos/{q(owner)}/{q(name)}")
        if not isinstance(data, dict):
            raise SourceError("GitHub answered in an unexpected shape", "bad_answer")
        return self.facts(data)

    def search_repos(self, query: str, per_page: int = 5) -> List[Dict[str, Any]]:
        data = self._api(f"/search/repositories?q={q(query)}&sort=stars&order=desc&per_page={per_page}")
        items = data.get("items") if isinstance(data, dict) else None
        return [self.facts(i) for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def head_commit(self, owner: str, name: str, ref: str) -> str:
        self.requests += 1
        try:
            _, _, body = self.http.get(f"{self.base}/repos/{q(owner)}/{q(name)}/commits/{q(ref)}", self._headers("application/vnd.github.sha"))
        except RateLimited as e:
            raise self._limited(e) from None
        sha = body.decode("ascii", "replace").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise SourceError("GitHub did not give a commit hash", "bad_answer")
        return sha

    def tree(self, owner: str, name: str, sha: str) -> Dict[str, Any]:
        data = self._api(f"/repos/{q(owner)}/{q(name)}/git/trees/{q(sha)}?recursive=1")
        if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
            raise SourceError("GitHub answered in an unexpected shape", "bad_answer")
        return data

    def raw_file(self, owner: str, name: str, ref: str, path: str, limit: int = 200_000) -> bytes:
        quoted = "/".join(q(part) for part in path.split("/"))
        _, _, body = self.http.get(f"{self.raw}/{q(owner)}/{q(name)}/{q(ref)}/{quoted}", {"Accept": "*/*"}, limit)
        return body
