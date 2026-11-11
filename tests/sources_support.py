"""Stand-ins for the tests of the refresh of archives: an embedder that counts the texts it is asked to embed, a store and a Sync over a temporary folder, a fake HTTP
server with ETag and robots.txt, a fake MediaWiki API and a fake company wiki."""
from __future__ import annotations

import gzip
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import numpy as np

from tadamoo.rag import RagStore, project_key
from tadamoo.rag.sources.engine import Sync

PROJ = project_key("sync-project")


class Counting:
    """An embedder with no model: each text becomes counts of a few words; it records every text it was asked to embed."""
    model = "count-1"
    version = "1"
    WORDS = ("alpha", "beta", "gamma", "delta", "wind", "boat")

    def __init__(self) -> None:
        self.texts: List[str] = []

    def embed(self, texts):
        self.texts += list(texts)
        out = np.zeros((len(texts), len(self.WORDS) + 1), dtype=np.float32)
        for r, t in enumerate(texts):
            low = t.lower()
            for c, w in enumerate(self.WORDS):
                out[r, c] = low.count(w)
            out[r, -1] = 0.01
        return out


def make(tmp_path: Path, embedder: Optional[Counting] = None, wiki=None, busy=None, forbidden=()) -> "tuple[RagStore, Sync, str]":
    holder = {"e": embedder}
    store = RagStore(tmp_path / "rag", embedder=lambda: holder["e"], forbidden=list(forbidden))
    store.holder = holder
    sync = Sync(store, wiki_client=wiki, busy=busy, sleep=lambda s: None)
    sync.settings.write({"min_interval_s": 0.0, "max_download_mb": 5, "request_timeout_s": 5, "contact": "test@example.org"})
    sync.ctx.http.sleep = lambda s: None
    aid = store.create("Sync test", "", PROJ)["id"]
    return store, sync, aid


def doc_of(store: RagStore, aid: str, name_part: str) -> Dict[str, Any]:
    return next(d for d in store.get(aid)["document_list"] if name_part in d["name"])


PARAS = ["Alpha paragraph number {n}. " + "The wind carries the boat along the shore, and the delta is wide. " * 3 for n in range(8)]


def body(version: int = 1, topic: str = "alpha", paragraphs: int = 6) -> str:
    """A Markdown document of `paragraphs` passages' worth of text; a higher version changes the last paragraph only."""
    parts = [f"# {topic.title()} notes", ""]
    for n in range(paragraphs):
        text = f"{topic.title()} paragraph number {n}. " + "The wind carries the boat along the shore, and the delta is wide. " * 6
        if n == paragraphs - 1 and version > 1:
            text = f"{topic.title()} revised paragraph, version {version}. Gamma and beta now matter more than the old wind notes did. " * 4
        parts += [f"## Part {n}", "", text, ""]
    return "\n".join(parts)


class Site:
    """A web server on 127.0.0.1 that serves pages with an ETag (and 304), a robots.txt, a redirect, a page that is gone and a slow-down answer."""

    def __init__(self) -> None:
        self.pages: Dict[str, bytes] = {}
        self.types: Dict[str, str] = {}
        self.robots = "User-agent: *\nDisallow: /private/\n"
        self.hits: List[tuple] = []
        self.etags = True
        self.throttle = 0
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                site.hits.append((path, self.headers.get("If-None-Match"), self.headers.get("User-Agent")))
                if path == "/robots.txt":
                    return self._send(200, site.robots.encode(), "text/plain")
                if path == "/old":
                    self.send_response(301)
                    self.send_header("Location", "/page.html")
                    self.end_headers()
                    return
                if path == "/slow":
                    if site.throttle > 0:
                        site.throttle -= 1
                        self.send_response(429)
                        self.send_header("Retry-After", "1")
                        self.end_headers()
                        return
                    return self._send(200, b"<html><body><h1>Slow</h1><p>" + b"slow text " * 30 + b"</p></body></html>", "text/html")
                if path not in site.pages:
                    return self._send(404, b"no", "text/plain")
                data = site.pages[path]
                tag = '"' + hashlib.md5(data).hexdigest() + '"'
                if site.etags and self.headers.get("If-None-Match") == tag:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.end_headers()
                    return
                self._send(200, data, site.types.get(path, "text/html"), tag if site.etags else None)

            def _send(self, code, data, ctype, etag=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if etag:
                    self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def html_page(title: str, paragraphs: int = 4, word: str = "wind") -> bytes:
    inner = "".join(f"<p>{title} paragraph {n}. " + f"The {word} carries the boat along the shore, and the delta is wide. " * 6 + "</p>" for n in range(paragraphs))
    return f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{inner}</body></html>".encode()


class WikiSite:
    """A MediaWiki API on 127.0.0.1: pages with revision ids, redirects, a missing page, a category, and the parse action."""

    def __init__(self) -> None:
        self.pages: Dict[str, Dict[str, Any]] = {}          # title -> {revid, text} ; a redirect is {redirect: title}
        self.category: Dict[str, List[str]] = {}
        self.requests: List[Dict[str, str]] = []
        self.lag = 0
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
                site.requests.append(q)
                out: Dict[str, Any]
                if site.lag > 0:
                    site.lag -= 1
                    out = {"error": {"code": "maxlag", "info": "waiting"}}
                elif q.get("action") == "query" and q.get("list") == "categorymembers":
                    out = {"query": {"categorymembers": [{"title": t} for t in site.category.get(q["cmtitle"], [])]}}
                elif q.get("action") == "query":
                    titles = q["titles"].split("|")
                    pages, redirects = [], []
                    for t in titles:
                        p = site.pages.get(t)
                        if p and "redirect" in p:
                            redirects.append({"from": t, "to": p["redirect"]})
                            t, p = p["redirect"], site.pages.get(p["redirect"])
                        if p is None:
                            pages.append({"title": t, "missing": True})
                        else:
                            pages.append({"title": t, "pageid": 1, "lastrevid": p["revid"], "revisions": [{"revid": p["revid"], "timestamp": p.get("ts", "2026-01-01T00:00:00Z"), "sha1": "x"}]})
                    out = {"query": {"pages": pages, **({"redirects": redirects} if redirects else {})}}
                elif q.get("action") == "parse":
                    revid = int(q["oldid"])
                    hit = next((p for p in site.pages.values() if p.get("revid") == revid), None)
                    out = {"parse": {"text": "<!-- cached 123 -->" + hit["text"]}} if hit else {"error": {"code": "nosuchrevid", "info": "no"}}
                else:
                    out = {"error": {"code": "badaction", "info": "x"}}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.api = f"http://127.0.0.1:{self.server.server_address[1]}/w/api.php"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def put(self, title: str, revid: int, text: str, ts: str = "2026-01-01T00:00:00Z") -> None:
        self.pages[title] = {"revid": revid, "text": text, "ts": ts}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeWiki:
    """The client of the company wiki over a dict of pages: a tree with `updated`, a page with its front matter. `down` makes every call fail."""

    def __init__(self) -> None:
        self.pages: Dict[str, Dict[str, Any]] = {}          # path -> {title, updated, body, fm}
        self.down = False
        self.hidden: set = set()
        self.calls: List[str] = []
        self.base_url = "http://wiki.test"
        self.last_error: Optional[str] = None

    def put(self, path: str, title: str, body: str, updated: str, **fm: Any) -> None:
        self.pages[path] = {"title": title, "updated": updated, "body": body, "fm": {"title": title, "updated": updated, **fm}}

    def __call__(self, address: str) -> "FakeWiki":
        return self

    def tree(self, project: str):
        self.calls.append("tree")
        if self.down:
            self.last_error = "wikibridge offline at http://wiki.test: refused"
            return None
        out: Dict[str, List[Dict[str, Any]]] = {}
        for path, p in self.pages.items():
            if path not in self.hidden:
                out.setdefault(path.split("/")[0], []).append({"file": path, "title": p["title"], "updated": p["updated"]})
        return out

    def page(self, project: str, path: str):
        self.calls.append("page " + path)
        if self.down or path not in self.pages or path in self.hidden:
            self.last_error = "wikibridge not_found: the page is not there"
            return None
        p = self.pages[path]
        return {"path": path, "frontmatter": p["fm"], "body": p["body"]}
