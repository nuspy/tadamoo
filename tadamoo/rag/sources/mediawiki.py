"""Pages of a MediaWiki site (Wikipedia and every wiki that runs the same software) as a source: a list of page titles and/or a category.

Change detection is one batched question for up to 50 pages (`action=query&prop=info|revisions`): the id of the latest revision of each page. A page that was
redirected is followed (the redirect's target is the page; a target that changes is a changed revision), a page that is gone says `missing`, and a revision that is
new but has the same text (a revert, a null edit) is settled by the content hash after the page is read. `recentchanges` is not used: it keeps only a few days and
needs a time to start from, where asking the revision of the pages that are tracked is exact whenever the check runs.

The page is read at its revision (`action=parse&oldid=`), so what was asked about is what is read. The API's etiquette applies: a User-Agent with a contact, one request
at a time with a pause, `maxlag`, and a retry after the wait it asks."""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, unquote, urlencode, urlsplit

from tadamoo.rag.errors import RagError
from tadamoo.rag.sources.model import Fetched, Provider, Remote, SourceError, doc_name, need
from tadamoo.rag.sources.web import DEPTH_MAX

BATCH = 50
PAGES_MAX = 500
MAXLAG = 5
LINKS_PER_PAGE = 500                 # the wiki's own cap per request (pllimit); good enough to find the next level without paging through every one
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def from_page_address(url: str) -> Dict[str, Any]:
    """`{api, titles}` from the address of a page of a wiki (`https://en.wikipedia.org/wiki/Foo`, or `.../index.php?title=Foo`)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RagError("url: the address of a wiki page")
    base = f"{parts.scheme}://{parts.netloc}"
    if "/wiki/" in parts.path:
        prefix, _, title = parts.path.partition("/wiki/")
        return {"api": f"{base}{prefix}/w/api.php", "titles": [unquote(title).replace("_", " ")]}
    if parts.path.endswith("/index.php") and "title=" in parts.query:
        title = dict(q.split("=", 1) for q in parts.query.split("&") if "=" in q).get("title", "")
        return {"api": base + parts.path[: -len("index.php")] + "api.php", "titles": [unquote(title).replace("_", " ")]}
    raise RagError("url: not the address of a wiki page (it looks like https://en.wikipedia.org/wiki/Title)")


class MediaWikiProvider(Provider):
    kind = "mediawiki"
    keys = ["revid", "title"]

    def validate(self, config: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if config.get("url"):
            out.update(from_page_address(need(config, "url")))
        if config.get("api"):
            api = need(config, "api")
            parts = urlsplit(api)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or not parts.path.endswith("api.php"):
                raise RagError("api: the address of the wiki's api.php")
            out["api"] = api.split("#")[0]
        if "api" not in out:
            raise RagError("api: the address of the wiki's api.php, or the address of one of its pages")
        titles = list(out.get("titles") or [])
        for t in config.get("titles") or []:
            if not isinstance(t, str) or not t.strip() or len(t) > 250:
                raise RagError("titles: page titles, each up to 250 characters")
            titles.append(t.strip())
        category = str(config.get("category") or "").strip()
        if len(category) > 250:
            raise RagError("category: at most 250 characters")
        if category and not category.lower().startswith("category:"):
            category = "Category:" + category
        titles = list(dict.fromkeys(titles))
        if not titles and not category:
            raise RagError("titles: name at least one page, or a category")
        if len(titles) > PAGES_MAX:
            raise RagError(f"titles: at most {PAGES_MAX} pages")
        depth, limit = config.get("depth", 0), config.get("limit")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= DEPTH_MAX:
            raise RagError(f"depth: a whole number from 0 (the named pages only) to {DEPTH_MAX}")
        if limit not in (None, "") and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise RagError("limit: a whole number of at least 1, or none")
        result = {"api": out["api"], "titles": titles, "category": category}
        if depth:
            result["depth"] = depth
            if limit not in (None, ""):
                result["limit"] = limit
        return result

    def label(self, config: Dict[str, Any]) -> str:
        host = urlsplit(config["api"]).netloc
        text = f"{host}: {config['category']}" if config.get("category") else \
            f"{host}: {config['titles'][0]}" + (f" and {len(config['titles']) - 1} more" if len(config["titles"]) > 1 else "")
        return text + (f" (depth {config['depth']})" if config.get("depth") else "")

    # -- the API ---------------------------------------------------------------------------------

    def _api(self, api: str, params: Dict[str, Any]) -> Dict[str, Any]:
        query = urlencode({**params, "format": "json", "formatversion": 2, "maxlag": MAXLAG})
        for attempt in (0, 1):
            got = self.ctx.http.get(f"{api}?{query}", {"Accept": "application/json"}, robots=False, max_bytes=16 << 20)
            try:
                data = json.loads(got.body.decode("utf-8"))
            except ValueError:
                raise SourceError(f"{urlsplit(api).netloc}: the answer is not JSON (is this a wiki's api.php?)", "bad_response") from None
            err = data.get("error") if isinstance(data, dict) else None
            if err and err.get("code") == "maxlag" and attempt == 0:
                self.ctx.sleep(min(30.0, float(got.headers.get("retry-after") or 5)))
                continue
            if err:
                raise SourceError(f"{urlsplit(api).netloc}: {err.get('code', 'error')}: {str(err.get('info', ''))[:120]}", "http")
            if not isinstance(data, dict):
                raise SourceError(f"{urlsplit(api).netloc}: an answer of the wrong shape", "bad_response")
            return data
        raise SourceError(f"{urlsplit(api).netloc}: the wiki is lagging; try later", "http")

    def _members(self, api: str, category: str) -> List[str]:
        out: List[str] = []
        more: Dict[str, Any] = {}
        while True:
            data = self._api(api, {"action": "query", "list": "categorymembers", "cmtitle": category, "cmnamespace": 0, "cmtype": "page", "cmlimit": 500, **more})
            out += [m["title"] for m in (data.get("query") or {}).get("categorymembers", []) if m.get("title")]
            if len(out) > PAGES_MAX:
                raise SourceError(f"{category} holds more than {PAGES_MAX} pages: name the pages or a smaller category", "too_big")
            if "continue" not in data:
                return out
            more = {k: v for k, v in data["continue"].items()}

    def _page_url(self, api: str, title: str) -> str:
        parts = urlsplit(api)
        base, name = f"{parts.scheme}://{parts.netloc}", quote(title.replace(" ", "_"))
        return f"{base}/wiki/{name}" if parts.path.endswith("/w/api.php") else f"{base}{parts.path[: -len('api.php')]}index.php?title={name}"

    def _resolve(self, api: str, titles: List[str], cancel: Optional[Callable[[], bool]] = None) -> List[Remote]:
        out: List[Remote] = []
        for start in range(0, len(titles), BATCH):
            if cancel is not None and cancel():
                raise SourceError("cancelled", "cancelled")
            batch = titles[start:start + BATCH]
            data = (self._api(api, {"action": "query", "redirects": 1, "prop": "info|revisions", "rvprop": "ids|timestamp|sha1", "rvslots": "main", "titles": "|".join(batch)})
                    .get("query") or {})
            normalized = {n["from"]: n["to"] for n in data.get("normalized", [])}
            redirects = {r["from"]: r["to"] for r in data.get("redirects", [])}
            pages = {p.get("title"): p for p in data.get("pages", [])}
            for asked in batch:
                title = normalized.get(asked, asked)
                title = redirects.get(title, title)
                page = pages.get(title)
                if page is None or page.get("missing") or page.get("invalid") or not page.get("revisions"):
                    out.append(Remote(asked, missing=True, note="the page does not exist now (deleted, or the title is wrong)", origin=self._page_url(api, asked)))
                    continue
                rev = page["revisions"][0]
                stamp = {"revid": int(rev.get("revid") or page.get("lastrevid") or 0), "title": title, "timestamp": rev.get("timestamp", ""), "sha1": rev.get("sha1", "")}
                out.append(Remote(asked, doc_name(title, ".html"), stamp, origin=self._page_url(api, title), note=f"redirected to {title}" if title != asked else ""))
        return out

    def _links(self, api: str, title: str) -> List[str]:
        """The article-namespace (`ns=0`) pages `title` links to, from the wiki's own API: at most LINKS_PER_PAGE of them, which is enough to find the
        next level without paging through a page's entire link list."""
        data = self._api(api, {"action": "query", "prop": "links", "titles": title, "plnamespace": 0, "pllimit": LINKS_PER_PAGE, "redirects": 1}).get("query") or {}
        return [link["title"] for page in (data.get("pages") or []) for link in (page.get("links") or [])]

    def list(self, config: Dict[str, Any], known: Dict[str, Dict[str, Any]], cancel: Optional[Callable[[], bool]] = None) -> List[Remote]:
        """The named pages and category members (depth 0), then, up to `depth` levels, the pages they link to on the same wiki (article namespace only),
        the same shape as `web.UrlProvider`: a page's links are asked fresh every time, since this provider has no conditional-fetch shortcut to carry them
        on. `limit` caps how many pages are kept; following stops past PAGES_MAX distinct titles regardless."""
        api = config["api"]
        wanted = list(config["titles"])
        if config.get("category"):
            wanted += [t for t in self._members(api, config["category"]) if t not in wanted]
        depth, limit = int(config.get("depth") or 0), config.get("limit")
        out: List[Remote] = []
        seen = set(wanted)
        level = wanted
        for d in range(depth + 1):
            if not level or (limit and len(out) >= limit):
                break
            out += self._resolve(api, level, cancel)
            if d < depth:
                following: List[str] = []
                for r in out[-len(level):]:
                    if r.missing:
                        continue
                    if cancel is not None and cancel():
                        raise SourceError("cancelled", "cancelled")
                    for link in self._links(api, r.stamp["title"]):
                        if link in seen:
                            continue
                        seen.add(link)
                        following.append(link)
                        if len(seen) > PAGES_MAX:
                            raise SourceError(f"following links went past {PAGES_MAX} pages: use a smaller depth or name fewer pages", "too_big")
                level = following
        return out[:limit] if limit else out

    def fetch(self, config: Dict[str, Any], remote: Remote, into: Path) -> Fetched:
        data = self._api(config["api"], {"action": "parse", "oldid": remote.stamp["revid"], "prop": "text|displaytitle", "disableeditsection": 1, "disablelimitreport": 1,
                                         "redirects": 0, "disabletoc": 1})
        body = (data.get("parse") or {}).get("text")
        if not isinstance(body, str) or not body.strip():
            raise SourceError(f"{remote.stamp.get('title')}: the wiki gave no text for revision {remote.stamp['revid']}", "bad_response")
        title = remote.stamp.get("title") or remote.key
        page = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + html.escape(title) + "</title></head><body><h1>" + html.escape(title) + "</h1>\n"
                + _COMMENT.sub("", body) + "\n</body></html>")                      # the parser cache's own comments carry times: not part of the content
        into.mkdir(parents=True, exist_ok=True)
        target = into / ("w" + hashlib.sha1(remote.key.encode("utf-8")).hexdigest()[:10] + ".html")
        raw = page.encode("utf-8")
        target.write_bytes(raw)
        return Fetched(target, remote.name, hashlib.sha256(raw).hexdigest(), len(raw), dict(remote.stamp), remote.origin, version=f"revision {remote.stamp['revid']}")
