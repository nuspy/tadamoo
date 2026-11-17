"""The pictures of a web page or a wiki page that is kept as HTML: which `<img>` tags there are, the description and the caption each has, and the text it sits beside; then the
pictures themselves, fetched through a function the caller gives (the sources' own HTTP client, with its limits and its respect of robots), or read from beside the file for a page
added by hand. The items go to `figures_office.finish`, which cuts them down to WebP and links them like any other figure."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlsplit

from tadamoo.rag.ingest.figures_office import ALT_CHARS, ANCHOR_CHARS

SKIP = {"script", "style", "noscript", "template", "svg", "head"}
_CAPTION_CLASS = re.compile(r"caption", re.IGNORECASE)
_NOT_PIXELS = re.compile(r"\.(svg|ico)(\?|$)", re.IGNORECASE)
MIN_ATTR_PX = 48
Get = Callable[[str], Optional[bytes]]


class _Images(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: List[Dict[str, Any]] = []
        self._skip, self._last, self._cap, self._cap_depth, self._depth = 0, "", None, 0, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self._depth += 1
        if tag in SKIP:
            self._skip += 1
        if self._cap is None and (tag == "figcaption" or _CAPTION_CLASS.search(a.get("class") or "")):
            self._cap, self._cap_depth = [], self._depth
        if tag == "img" and not self._skip:
            src = a.get("src") or ""
            sets = [c.strip().split() for c in (a.get("srcset") or "").split(",") if c.strip()]
            if sets:                                                    # the largest candidate of a srcset (`url 2x`, `url 800w`)
                key = lambda c: float(re.sub(r"[^0-9.]", "", c[1]) or 1) if len(c) > 1 else 1.0      # noqa: E731
                src = max(sets, key=key)[0]
            self.images.append({"src": src, "alt": " ".join((a.get("alt") or a.get("title") or "").split())[:ALT_CHARS], "caption": "", "anchor": self._last[:ANCHOR_CHARS],
                                "w": a.get("width", ""), "h": a.get("height", "")})
        if tag in ("br", "img", "hr", "meta", "link", "input"):
            self._depth -= 1

    def handle_endtag(self, tag):
        if tag in SKIP and self._skip:
            self._skip -= 1
        if self._cap is not None and self._depth == self._cap_depth:
            text = " ".join("".join(self._cap).split())
            target = next((i for i in reversed(self.images[-3:]) if not i["caption"]), None)
            if text and target is not None:
                target["caption"] = text[:ALT_CHARS]
            self._cap = None
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._skip:
            return
        if self._cap is not None:
            self._cap.append(data)
            return
        text = " ".join(data.split())
        if len(text) >= 20:
            self._last = text


def _too_small(item: Dict[str, Any]) -> bool:
    try:
        return (item["w"] != "" and int(float(item["w"])) < MIN_ATTR_PX) or (item["h"] != "" and int(float(item["h"])) < MIN_ATTR_PX)
    except ValueError:
        return False


def local_getter(base: Path) -> Get:
    """Reads a picture from beside a page added by hand: only inside the page's own folder."""
    root = Path(base).resolve()

    def get(src: str) -> Optional[bytes]:
        try:
            target = (root / unquote(urlsplit(src).path).lstrip("/\\")).resolve()
            return target.read_bytes() if root in target.parents and target.is_file() else None
        except (OSError, ValueError):
            return None
    return get


def items(path: Path, origin: str = "", get: Optional[Get] = None, source: str = "web") -> List[Dict[str, Any]]:
    """The pictures of the HTML file `path`. With `origin` (the page's address) each address is made whole and fetched with `get`; without it the file's own folder is used. A picture
    that cannot be fetched is left out; a data address, an SVG or an icon is not a picture to keep."""
    try:
        html = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    page = _Images()
    page.feed(html)
    fetch = get if (origin and get is not None) else (local_getter(Path(path).parent) if not origin else None)
    if fetch is None:
        return []
    out, seen = [], set()
    for img in page.images:
        src = img["src"].strip()
        if not src or src.startswith("data:") or _NOT_PIXELS.search(src) or _too_small(img):
            continue
        url = urljoin(origin, src) if origin else src
        if url in seen:
            continue
        seen.add(url)
        try:
            data = fetch(url)
        except Exception:
            data = None                                                 # a picture that does not come does not stop the page
        if data:
            out.append({"data": data, "alt": img["alt"], "caption": img["caption"], "anchor": img["anchor"], "source": source, "url": url if origin else ""})
    return out
