"""The company wiki (wikibridge, tadamoo/integrations/wikibridge_client.py) as a source.

The service has no change feed, but its page tree (`GET /wiki/tree`) is one cheap question that lists every page the key may see with its `updated`, the time its content
last changed (the wiki moves it only when the content changes). A check is that one call: a page whose `updated` or title differs from the one recorded is changed, a page
not listed before is new, a page no longer listed is gone (deleted, renamed, or no longer visible to this key: the wiki answers 404 for all three). Only changed or
new pages are read (`GET /wiki/page`), and the hash of their text settles the rest. An empty or failed answer is a failure of the check, never "everything is gone"."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from tadamoo.rag.errors import RagError
from tadamoo.rag.sources.model import Fetched, Provider, Remote, SourceError, doc_name

SECTIONS = ("sources", "entities", "concepts", "syntheses", "meetings")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$")


class WikiBridgeProvider(Provider):
    kind = "wikibridge"
    keys = ["updated", "title"]

    def validate(self, config: Dict[str, Any]) -> Dict[str, Any]:
        project = str(config.get("project") or "").strip()
        if not SLUG.match(project):
            raise RagError("project: the name of a wiki project, as the wiki lists it")
        address = str(config.get("address") or "").strip().rstrip("/")
        if address:
            parts = urlsplit(address)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or len(address) > 300:
                raise RagError("address: an http or https address of the wiki service, without a user name, a password or a query")
        sections = config.get("sections") or list(SECTIONS)
        if not isinstance(sections, list) or not sections or any(s not in SECTIONS for s in sections):
            raise RagError("sections: some of " + ", ".join(SECTIONS))
        return {"address": address, "project": project, "sections": [s for s in SECTIONS if s in sections]}

    def label(self, config: Dict[str, Any]) -> str:
        return f"company wiki: {config['project']}"

    def same(self, old: Dict[str, Any], new: Dict[str, Any]) -> bool:
        return bool(new.get("updated")) and super().same(old, new)             # a page with no time cannot be told unchanged without reading it

    def _client(self, config: Dict[str, Any]) -> Any:
        return self.ctx.wiki_client(config.get("address") or "")

    def list(self, config: Dict[str, Any], known: Dict[str, Dict[str, Any]], cancel: Optional[Callable[[], bool]] = None) -> List[Remote]:
        client = self._client(config)
        tree = client.tree(config["project"])
        if tree is None:
            raise SourceError(client.last_error or f"the wiki at {client.base_url} did not answer", "offline")
        out: List[Remote] = []
        for section in config["sections"]:
            for entry in tree.get(section, []):
                title = str(entry.get("title") or Path(entry["file"]).stem)
                out.append(Remote(entry["file"], doc_name(title, ".md"), {"updated": str(entry.get("updated") or ""), "title": title}, origin=f"{client.base_url}/wiki/page?path={entry['file']}"))
        if not out and known:
            raise SourceError("the wiki lists no page at all although pages were read from it before: not taken as a change (wrong project, or the key sees nothing now)", "bad_response")
        return out

    def fetch(self, config: Dict[str, Any], remote: Remote, into: Path) -> Fetched:
        client = self._client(config)
        got = client.page(config["project"], remote.key)
        if got is None:
            raise SourceError(client.last_error or f"{remote.key}: the page is not there or not visible now", "offline")
        fm, title = got["frontmatter"], remote.stamp.get("title") or remote.key
        body = got["body"].strip()
        text = (body if body.startswith("#") else f"# {title}\n\n{body}") + "\n"
        into.mkdir(parents=True, exist_ok=True)
        target = into / ("m" + hashlib.sha1(remote.key.encode("utf-8")).hexdigest()[:10] + ".md")
        raw = text.encode("utf-8")
        target.write_bytes(raw)
        stamp = dict(remote.stamp)
        for name in ("content_sha256", "content_version", "revision"):
            if fm.get(name) not in (None, ""):
                stamp[name] = fm[name]
        version = f"version {fm['content_version']}" if fm.get("content_version") else (stamp.get("updated") or "")[:40]
        return Fetched(target, remote.name, hashlib.sha256(raw).hexdigest(), len(raw), stamp, str(fm.get("url") or remote.origin), version=version)
