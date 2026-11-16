"""Stock footage from sites that have no API to ask: Pexels and Envato, read through a browser (the one the browser tools use: agent-browser when it is installed, else TaDaMoo's own driver on Chrome or Edge).

Each site is a source like `stock.Pixabay`: `search(query, lang)` gives results (`stock.Hit`), `details(hit)` reads the page of a video for what the list does not say (length, tags, size) and `download(hit, dest)`
gets the file. `stock.find` asks them in the order the production gives; the rules for choosing are the same for every site (the subject, the place, no other culture, not shown in the last week).

- Pexels: a search page lists videos with a title only in the address (`/video/students-studying-outdoors-7969393/`) and a download link that works without an account; the free licence of the site applies.
  A page the site's own check (Cloudflare) holds back is not worked around: the source is left out of that clip.
- Envato: the search page lists length, title and author; the page of a video gives the tags, the size, a description and the link "Download Preview", a watermarked preview that needs no account. The
  files are for tests: the previews are not for publication.

The window of the browser is visible: the sites do not answer a browser that has none.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, quote_plus, urljoin

from tadamoo.media.stock import Hit, StockError

SCROLLS = 3
SCROLL_WAIT_S = 1.8
CHECK_WAIT_S = 24.0


class Pages:
    """One browser for the sites of a production: started when the first page is asked for, closed with `close`."""

    def __init__(self, make: Callable[[], Any]) -> None:
        self._make, self._browser = make, None
        self.cache: Dict[str, List[Hit]] = {}

    def browser(self) -> Any:
        if self._browser is None:
            try:
                self._browser = self._make()
                start = getattr(self._browser, "start", None)
                if start:
                    start()
            except Exception as exc:
                self._browser = None
                raise StockError(f"the browser could not be started ({str(exc)[:120]})") from None
        return self._browser

    def js(self, script: str) -> Any:
        b = self.browser()
        run = getattr(b, "_js", None) or getattr(b, "_eval")
        try:
            return run(script)
        except Exception as exc:
            raise StockError(f"the page could not be read ({str(exc)[:120]})") from None

    def open(self, url: str) -> None:
        b = self.browser()
        try:
            b.open(url)
        except Exception as exc:
            raise StockError(f"the page could not be opened ({str(exc)[:120]})") from None
        began = time.monotonic()
        while "just a moment" in str(self.js("document.title")).lower():          # the site's own check: left to finish by itself, never answered
            if time.monotonic() - began > CHECK_WAIT_S:
                raise StockError("the site holds the page back with a check of the browser")
            time.sleep(1.5)
        self.privacy()

    def privacy(self) -> None:
        """A cookie banner is answered with the most private choice the page offers (reject or only the essential ones); it covers the page and takes the clicks otherwise."""
        for _ in range(3):
            if self.js(REJECT_COOKIES):
                time.sleep(1.0)
            else:
                return

    def goto(self, url: str, until: str = "") -> None:
        """Another page of the same site by a script, which is much lighter than opening it again (a page of a video is read in seconds instead of a minute); waits for it to load."""
        self.js(f"window.location.assign({json.dumps(url)})")
        began = time.monotonic()
        time.sleep(1.0)
        while time.monotonic() - began < 25.0:
            ready = str(self.js("document.readyState"))
            title = str(self.js("document.title")).lower()
            if ready == "complete" and "just a moment" not in title and (not until or until in title):
                self.privacy()
                return
            time.sleep(0.5)
        raise StockError("the page did not finish loading")

    def download(self, name: str, dest: Path, wait_s: float = 60.0) -> Path:
        """The video a link or button called `name` gives: it is clicked with the driver's mouse, and the video the link opens in the tab is read and written to `dest` (the person's rule for the site allows it)."""
        b = self.browser()
        time.sleep(2.5)
        self.privacy()
        time.sleep(1.5)
        items = (b.snapshot() or {}).get("items") or []
        ref = next((x["ref"] for x in items if re.search(name, str(x.get("name") or ""), re.I)), None)
        if ref is None:
            raise StockError(f"the page has no \"{name}\" to click")
        b.click(ref)
        video = re.compile(r"\.(mp4|mov|webm)(\?|$)", re.I)
        began, assigned = time.monotonic(), False
        while time.monotonic() - began < wait_s:
            if str(self.js("document.contentType")).startswith("video/") or video.search(str(self.js("location.href"))):                  # the link opened the video itself: what the browser shows is read and kept
                return self._keep_open_video(dest)
            if not assigned and time.monotonic() - began > 8.0:               # the click did not move the page: its address is opened, which is what the link does
                href = next((str(x.get("href") or "") for x in items if x["ref"] == ref), "")
                if href:
                    assigned = True
                    self.js(f"window.location.assign({json.dumps(href if href.startswith('http') else 'https://' + href)})")
            time.sleep(0.5)
        raise StockError("the link did not give a video")

    def save_from(self, url: str, dest: Path, wait_s: float = 45.0) -> Path:
        """The video at the address a site's download link leads to, opened in the browser's own tab and written to `dest` from there."""
        self.js(f"window.location.assign({json.dumps(url)})")
        began = time.monotonic()
        while time.monotonic() - began < wait_s:
            time.sleep(1.0)
            if str(self.js("document.contentType")).startswith("video/"):
                return self._keep_open_video(dest)
        raise StockError("the address did not open a video")

    def _keep_open_video(self, dest: Path) -> Path:
        """The video the browser shows in its tab, read in pieces by the page and written to `dest`."""
        import base64
        size = int(self.js(READ_OPEN_VIDEO) or 0)
        if size < 50_000:
            raise StockError(f"the video is too small ({size} bytes)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with tmp.open("wb") as f:
            for i in range(0, size, CHUNK):
                f.write(base64.b64decode(str(self.js(READ_CHUNK % (i, CHUNK)))))
        tmp.replace(dest)
        return dest

    def scroll(self, times: int = SCROLLS) -> None:
        for _ in range(times):
            time.sleep(SCROLL_WAIT_S)
            self.js("window.scrollBy(0, 900)")
        time.sleep(SCROLL_WAIT_S)

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None


READ_OPEN_VIDEO = """(async () => { const r = await fetch(location.href); window.__clip = new Uint8Array(await r.arrayBuffer()); return window.__clip.length; })()"""
CHUNK = 1_500_000
READ_CHUNK = """(() => { const u = window.__clip, i = %d; let s = ''; const end = Math.min(u.length, i + %d); for (let k = i; k < end; k += 32768) s += String.fromCharCode.apply(null, u.subarray(k, Math.min(end, k + 32768))); return btoa(s); })()"""
REJECT_COOKIES = """(() => { const b = [...document.querySelectorAll('button, a[role=button]')].find(x => /^(reject all|decline all|decline|only essential|essential only|necessary only|reject non-essential)$/i.test((x.innerText || '').trim()));
  if (!b) return false; b.click(); return true; })()"""


def _json(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return None


PEXELS_TILES = """(() => { const out = []; const seen = new Set();
  for (const a of document.querySelectorAll('a[href^="/video/"]')) { const h = a.getAttribute('href') || ''; if (seen.has(h)) continue; seen.add(h); out.push(h.slice(0, 200)); }
  return JSON.stringify(out.slice(0, 40)); })()"""
PEXELS_PAGE = """(() => JSON.stringify({text: (document.body.innerText || '').slice(0, 6000), keywords: ((document.querySelector('meta[name=keywords]') || {}).content || ''),
  description: ((document.querySelector('meta[name=description]') || {}).content || '')}))()"""


class Pexels:
    name = "pexels"
    max_details = 4
    first_fit = True                                                          # slow to read: the first video of the list that passes is taken, not the best of all

    def __init__(self, pages: Pages) -> None:
        self.pages = pages

    def search(self, query: str, lang: str = "en", per_page: int = 20) -> List[Hit]:
        if lang != "en":
            return []
        key = "pexels|" + query
        if key not in self.pages.cache:
            self.pages.open(f"https://www.pexels.com/search/videos/{quote(query.strip())}/?orientation=landscape")
            self.pages.scroll()
            hits: List[Hit] = []
            for href in _json(self.pages.js(PEXELS_TILES)) or []:
                m = re.search(r"/video/([a-z0-9-]*?)-?(\d{5,})/?$", href)
                if m:
                    hits.append(Hit(int(m.group(2)), urljoin("https://www.pexels.com", href), [], "", 0.0, 0, {}, "pexels", "", 0, 0, True))
            self.pages.cache[key] = hits[:per_page * 2]
        return list(self.pages.cache[key])

    def details(self, hit: Hit) -> None:
        """The length, the size, the tags and the description, from the page of the video."""
        self.pages.goto(hit.page)
        self.read(hit, _json(self.pages.js(PEXELS_PAGE)) or {})

    @staticmethod
    def read(hit: Hit, page: Dict[str, Any]) -> None:
        """What the text of the page of a video says (`text`, `keywords`, `description`) put in the result."""
        text = str(page.get("text") or "")
        m = re.search(r"Duration\D{0,12}(\d+):(\d{2})", text)
        if m:
            hit.duration = int(m.group(1)) * 60 + int(m.group(2))
        m = re.search(r"Dimensions\D{0,12}(\d{3,5})\s*[x×]\s*(\d{3,5})", text)
        if m:
            hit.width, hit.height = int(m.group(1)), int(m.group(2))
        m = re.search(r"\n\s*([^\n]{2,40})\n\s*Follow", text)
        hit.user = m.group(1).strip() if m else hit.user
        hit.tags = [t.strip() for t in str(page.get("keywords") or "").split(",") if t.strip()] + [str(page.get("description") or "")]
        hit.files["download"] = {"url": f"https://www.pexels.com/download/video/{hit.id}/"}
        hit.partial = False

    def download(self, hit: Hit, quality: str, dest: Path) -> Path:
        """The free file of the site: the address its "Free download" link leads to, opened in the browser."""
        return self.pages.save_from(hit.files.get("download", {}).get("url") or f"https://www.pexels.com/download/video/{hit.id}/", dest)


ENVATO_TILES = """(() => { const out = []; const seen = new Set();
  for (const b of document.querySelectorAll('button')) { if ((b.innerText || '').trim() === 'Dismiss') { try { b.click(); } catch (e) {} } }
  for (const a of document.querySelectorAll('a[href*="/stock-video/"]')) { const h = a.getAttribute('href') || ''; if (!/\\/stock-video\\/[0-9a-f-]{36}/.test(h) || seen.has(h)) continue; seen.add(h);
    out.push({href: h.slice(0, 200), text: (a.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200)}); }
  return JSON.stringify(out.slice(0, 40)); })()"""
ENVATO_TILES_BOX = """(() => { const out = []; const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/stock-video/"]')) { const h = a.getAttribute('href') || ''; if (!/\\/stock-video\\/[0-9a-f-]{36}/.test(h) || seen.has(h)) continue; seen.add(h);
    let box = a; for (let k = 0; k < 4 && box.parentElement; k++) box = box.parentElement;
    out.push({href: h.slice(0, 200), text: (box.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200)}); }
  return JSON.stringify(out.slice(0, 40)); })()"""
ENVATO_PAGE = """(() => JSON.stringify({text: (document.body.innerText || '').slice(0, 8000), preview: ([...document.querySelectorAll('a[href]')].find(a => /download preview/i.test(a.innerText || '')) || {}).href || ''}))()"""


class Envato:
    name = "envato"
    max_details = 4
    first_fit = True

    def __init__(self, pages: Pages) -> None:
        self.pages = pages

    def search(self, query: str, lang: str = "en", per_page: int = 20) -> List[Hit]:
        if lang != "en":
            return []
        key = "envato|" + query
        if key not in self.pages.cache:
            self.pages.open(f"https://app.envato.com/search?itemType=stock-video&term={quote_plus(query.strip())}")
            self.pages.js(ENVATO_TILES)                                      # closes the offer that covers the list
            self.pages.scroll(4)
            hits: List[Hit] = []
            for row in _json(self.pages.js(ENVATO_TILES_BOX)) or []:
                m = re.search(r"/stock-video/([0-9a-f-]{36})", row.get("href", ""))
                t = re.match(r"(\d+):(\d{2})\s*•\s*(.+)$", row.get("text", ""))
                if m and t:
                    title = t.group(3).strip()
                    hits.append(Hit(m.group(1), urljoin("https://app.envato.com", row["href"]), [], "", int(t.group(1)) * 60 + int(t.group(2)), 0, {}, "envato", title, 0, 0, True))  # type: ignore[arg-type]
            self.pages.cache[key] = hits[:per_page * 2]
        return list(self.pages.cache[key])

    def details(self, hit: Hit) -> None:
        """The tags, the size and the description, and the address of the preview, from the page of the video."""
        self.pages.goto(hit.page)
        time.sleep(1.5)
        self.read(hit, _json(self.pages.js(ENVATO_PAGE)) or {})

    @staticmethod
    def read(hit: Hit, page: Dict[str, Any]) -> None:
        """What the text of the page of a video says (`text`, `preview`) put in the result."""
        lines = [ln.strip() for ln in str(page.get("text") or "").splitlines() if ln.strip()]
        spec = next((i for i, ln in enumerate(lines) if re.fullmatch(r"\d+ seconds?", ln)), None)
        ref = next((i for i, ln in enumerate(lines) if ln == "Add as video reference"), None)
        if spec is not None and ref is not None and ref < spec:
            hit.tags = lines[ref + 1:spec]
            hit.duration = float(lines[spec].split()[0])
            size = next((re.fullmatch(r"(\d{3,5}) x (\d{3,5}) px", ln) for ln in lines[spec:spec + 4] if re.fullmatch(r"(\d{3,5}) x (\d{3,5}) px", ln)), None)
            if size:
                hit.width, hit.height = int(size.group(1)), int(size.group(2))
            orient = next((i for i in range(spec, min(spec + 8, len(lines))) if lines[i] in ("Horizontal", "Vertical", "Square")), None)
            if orient is not None:
                if lines[orient] == "Vertical":
                    hit.width, hit.height = min(hit.width or 1, hit.height or 2), max(hit.width or 1, hit.height or 2)
                if orient + 1 < len(lines):
                    hit.tags = hit.tags + [lines[orient + 1]]            # the description
        hit.files["preview"] = {"url": str(page.get("preview") or "")}
        hit.partial = False

    def download(self, hit: Hit, quality: str, dest: Path) -> Path:
        """The preview: the address its "Download Preview" link leads to, opened in the browser."""
        url = hit.files.get("preview", {}).get("url", "")
        if not url:
            raise StockError("the page of the video gives no preview to download")
        return self.pages.save_from(url, dest)
