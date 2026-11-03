"""Pictures and diagrams for an answer.

Pictures: `page_images` lists what an article shows (its own headline picture first) and `show_image` fetches one into the conversation's
files, through the same address checks as `browse_url`. The page shows only pictures held there (`![caption](tadamoo-file:ID)`), so a
picture address written in an answer can never make the page call out, and a hidden instruction in a page cannot send anything out in one.

Diagrams: the model writes them as ```mermaid blocks, which the page draws. `guidance` tells it when and how, by how much the person wants
of each.
"""
from __future__ import annotations

import http.client
import functools
import re
import time
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from tadamoo.tools.search import PAGE_SECONDS, MAX_REDIRECTS, PageError, _checked_connect, _validate, fetch_page

MAX_IMAGE_BYTES = 8_000_000
MAX_LISTED = 8
MIN_SIDE = 120                                           # a declared side below this is an icon or a pixel
SKIP_WORDS = ("logo", "icon", "sprite", "avatar", "pixel", "tracking", "banner", "advert", "/ads/", "badge", "emoji", "spacer", "1x1")
KINDS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
LEVELS = ("off", "very_low", "low", "normal", "high", "very_high", "highest")


def sniff(data: bytes) -> Optional[str]:
    """What the bytes are, whatever the server said: png, jpg, gif or webp; anything else (an SVG that could carry a script, a page) is None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def fetch_image(url: str, allow_private: bool = False) -> Tuple[bytes, str, str]:
    """The bytes of a picture, its kind and the address it came from, after every hop's address was checked; a picture too big is refused."""
    deadline = time.monotonic() + PAGE_SECONDS
    current = _validate(url, allow_private)
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlparse(current)
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = cls(parts.hostname, parts.port, timeout=min(10.0, max(1.0, deadline - time.monotonic())))
        conn._create_connection = functools.partial(_checked_connect, allow_private=allow_private)   # type: ignore[attr-defined]
        try:
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            try:
                conn.request("GET", target, headers={"User-Agent": "TaDaMoo/1 (+reader)", "Accept": "image/png,image/jpeg,image/gif,image/webp;q=0.9,*/*;q=0.1"})
                resp = conn.getresponse()
            except PageError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                raise PageError(f"could not read {parts.hostname}: {exc}") from None
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                current = _validate(urljoin(current, resp.getheader("Location")), allow_private)
                continue
            if resp.status >= 400:
                raise PageError(f"the address answered {resp.status} {resp.reason}")
            length = resp.getheader("Content-Length")
            if length and length.isdigit() and int(length) > MAX_IMAGE_BYTES:
                raise PageError("the picture is too big (more than 8 MB)")
            data = bytearray()
            try:
                while True:
                    if time.monotonic() > deadline:
                        raise PageError("the picture took too long to arrive")
                    block = resp.read(65536)
                    if not block:
                        break
                    data += block
                    if len(data) > MAX_IMAGE_BYTES:
                        raise PageError("the picture is too big (more than 8 MB)")
            except (OSError, http.client.HTTPException) as exc:
                raise PageError(f"the picture could not be read to the end: {exc}") from None
            kind = sniff(bytes(data))
            if kind is None:
                raise PageError("that address is not a PNG, JPEG, GIF or WebP picture")
            return bytes(data), kind, current
        finally:
            conn.close()
    raise PageError(f"more than {MAX_REDIRECTS} redirects")


class _Images(HTMLParser):
    """The pictures of a page: the ones the page names as its own (og:image), then those inside the article, then the rest."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.found: List[Dict[str, Any]] = []
        self._stack: List[str] = []
        self._caption: Optional[List[str]] = None
        self._last: Optional[Dict[str, Any]] = None

    def _add(self, src: str, alt: str, source: str, width: Any = None, height: Any = None) -> None:
        if not src or src.startswith("data:"):
            return
        url = urljoin(self.base, src.strip())
        if urlparse(url).scheme not in ("http", "https"):
            return
        low = url.lower()
        if any(w in low for w in SKIP_WORDS) and source != "page's own":
            return
        if low.split("?")[0].endswith((".svg", ".ico")):
            return
        for side in (width, height):
            if isinstance(side, str) and side.isdigit() and 0 < int(side) < MIN_SIDE:
                return
        if any(f["url"] == url for f in self.found):
            return
        self._last = {"url": url, "alt": " ".join(alt.split())[:200], "source": source, "caption": ""}
        self.found.append(self._last)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            if (a.get("property") in ("og:image", "og:image:url") or a.get("name") in ("twitter:image", "twitter:image:src")) and a.get("content"):
                self._add(a["content"], "", "page's own")
            return
        if tag == "link" and a.get("rel") == "image_src":
            self._add(a.get("href", ""), "", "page's own")
            return
        self._stack.append(tag)
        if tag == "figcaption":
            self._caption = []
        if tag == "img":
            src = a.get("src") or a.get("data-src") or a.get("data-lazy-src") or ""
            if a.get("srcset"):
                best = a["srcset"].split(",")[-1].strip().split(" ")[0]
                src = best or src
            inside = any(t in self._stack for t in ("article", "figure", "main"))
            self._add(src, a.get("alt", ""), "in the article" if inside else "elsewhere on the page", a.get("width"), a.get("height"))
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag == "figcaption" and self._caption is not None:
            if self._last is not None:
                self._last["caption"] = " ".join("".join(self._caption).split())[:200]
            self._caption = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self._caption is not None:
            self._caption.append(data)


def page_images(url: str, allow_private: bool = False) -> List[Dict[str, Any]]:
    """What the page at this address shows: its own picture first, then the article's, then any other, at most eight."""
    page = fetch_page(url, allow_private)
    parser = _Images(page["final_url"])
    try:
        parser.feed(page["text"])
    except Exception:                                      # a page the parser cannot follow gives what it found so far
        pass
    order = {"page's own": 0, "in the article": 1, "elsewhere on the page": 2}
    return sorted(parser.found, key=lambda f: order[f["source"]])[:MAX_LISTED]


def format_images(url: str, found: List[Dict[str, Any]]) -> str:
    if not found:
        return f"No usable picture was found on {url}. Search for the subject and try another page, or go on without a picture."
    lines = [f"Pictures on {url}. Pick one that shows what the answer is about, and show it with show_image."]
    for i, f in enumerate(found, 1):
        extra = " - ".join(x for x in (f"alt: {f['alt']}" if f["alt"] else "", f"caption: {f['caption']}" if f["caption"] else "") if x)
        lines.append(f"{i}. [{f['source']}] {f['url']}" + (f"  ({extra})" if extra else ""))
    return "\n".join(lines)


def register_visual_tools(registry: Any, outputs: Any, session: str, allow_private: Callable[[], bool] | bool = False) -> None:
    """`page_images` and `show_image`: offered when the person turned pictures on."""
    from tadamoo.attachments import clean_name
    from tadamoo.tools.registry import EffectClass

    def private() -> bool:
        return bool(allow_private() if callable(allow_private) else allow_private)

    def list_images(url: str) -> str:
        try:
            return format_images(url, page_images(url, private()))
        except PageError as exc:
            return f"Could not read {url}: {exc}"

    def show_image(url: str, caption: str = "") -> str:
        try:
            data, kind, final = fetch_image(url, private())
        except PageError as exc:
            return f"Could not get that picture: {exc}. Try another one from the page, or go on without a picture."
        base = re.sub(r"[^A-Za-z0-9_-]+", "_", str(caption or urlparse(final).path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "picture")).strip("_")[:40] or "picture"
        meta = outputs.save(session, clean_name(f"{base}.{kind}"), data)
        return (f"[file:{meta['id']}:{meta['name']}] Picture kept from {final}. Show it in your answer with ![short caption](tadamoo-file:{meta['id']}) "
                f"and name the page it came from ({urlparse(final).hostname}) as the source.")

    def obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
        return {"type": "object", "properties": props, "required": required}

    registry.register("page_images", "Lists the pictures a web page shows, its own headline picture first, with their alt text and captions. Use it on the article you cite.",
                      obj({"url": {"type": "string", "description": "http or https address of the page"}}, ["url"]), EffectClass.PURE, list_images, untrusted=True, outward=True)
    registry.register("show_image", "Fetches a picture (PNG, JPEG, GIF or WebP, up to 8 MB) from a web address into this conversation, so an answer can show it. Returns how to show it.",
                      obj({"url": {"type": "string", "description": "address of the picture itself"}, "caption": {"type": "string", "description": "a few words naming it"}}, ["url"]),
                      EffectClass.REVERSIBLE, show_image, untrusted=True, outward=True)


IMAGE_RULES = {
    "very_low": "Show a picture only when the person asks for one, or when the answer cannot be understood without seeing it.",
    "low": "Show a picture rarely: only when it clearly helps and the subject is something people recognise by sight.",
    "normal": "When an answer is about a person, place, product, event or thing that a picture would make clearer, look for one and show it.",
    "high": "Often show a picture: whenever an answer names a concrete subject that has a good, relevant photo, look for it.",
    "very_high": "Show pictures freely: for each concrete subject you explain, look for a relevant picture, and for each news item its own picture.",
    "highest": "Show a picture in every answer that can have one: for every subject, event or item you mention that has a relevant picture.",
}
CHART_RULES = {
    "very_low": "Draw a diagram only when the person asks for one, or when a process or structure cannot be followed in words.",
    "low": "Draw a diagram rarely: only for a process or a structure that is hard to follow in words.",
    "normal": "Draw a diagram when it makes an explanation easier: a workflow, a sequence of steps, how parts connect, what talks to what.",
    "high": "Often draw a diagram: for each process, architecture, deployment, interaction or decision you explain, alongside the words.",
    "very_high": "Draw diagrams freely: most explanations should come with one, and a long one with several.",
    "highest": "Draw a diagram in every answer that explains anything with parts, steps, flow or relations, and use the kind that fits (flow, sequence, state, class, entity, timeline, gantt, pie, mindmap).",
}


FILES_GUIDANCE = (
    "The person reads you in a page that turns a link to a file into a button that saves it. To offer a file you made with write_output_file or publish_output_file, "
    "write [name](tadamoo-file:ID) with the ID from its [file:ID:name] line. To point to a file in the folder, write [name](tadamoo-path:relative/path), with the path "
    "relative to the folder and only for a file that exists. Offer a file when the person asks for a file or would want to keep one; when the person asks to see "
    "a text on screen, the text goes in the answer.")


def guidance(images: str, charts: str) -> str:
    """The paragraph added to the instructions for what the person wants of pictures and diagrams; empty when both are off."""
    parts: List[str] = []
    if images in IMAGE_RULES:
        parts.append(
            f"Pictures from the web: {IMAGE_RULES[images]} Take them from the page of the article you cite when it has a fitting one: read the article, call page_images "
            "on it, choose the picture that shows what it reports, fetch it with show_image and write ![short caption](tadamoo-file:ID) where it belongs, "
            "then name the source page. If that page has none, look for one on another reliable page about the same fact. Never write a picture address "
            "yourself, never show a picture you did not fetch, and never show one that has nothing to do with the point.")
    if charts in CHART_RULES:
        parts.append(
            f"Diagrams: {CHART_RULES[charts]} Write a diagram as a fenced block with the language mermaid (flowchart TD, sequenceDiagram, stateDiagram-v2, classDiagram, "
            "erDiagram, gantt, pie, mindmap, timeline), put quotes around any label with punctuation, keep labels short, use no HTML, and add one line before it "
            "saying what it shows. The page draws it; the person can open its source.")
    return "\n\n".join(parts)
