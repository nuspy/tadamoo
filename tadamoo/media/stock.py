"""Stock footage from Pixabay (https://pixabay.com/api/docs/): looked for before a clip is made by the video model, cut to the length the plan gives it.

The key is the person's own and lives in the credential store, under `media/pixabay` (`PIXABAY_API_KEY` in the environment counts too); it is never part of a file of the
project and never printed. A search is a GET of `/api/videos/` with the keywords of what the clip must show; the answer is cached 24 hours, as Pixabay asks.

Choosing, in order:
  1. the keywords are the content words of the clip's description, in the order written, with the words that only say how footage looks (realistic, documentary, footage...) left out;
  2. searches go from the first four keywords down to the first one, each stopping the descent as soon as a video passes;
  3. a video passes when it lasts at least as long as a clip, and the keywords found in its tags and in the title Pixabay puts in its page address (the only description a hit
     carries) weigh at least `min_match` of all, the first three keywords counting more than the rest, and at least two of them when the clip has two or more;
  4. of the ones that pass the best match wins, then the more downloaded; a video already taken for another clip is not taken again.
The video is downloaded in the size asked (`small`: 1280x720 for most), and a clip is cut from the middle of it (the start and the end of stock footage are often fades),
scaled and cropped to the size of the clips the video model makes, with no sound."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode, urlsplit

from tadamoo.media.errors import MediaError

API_URL = "https://pixabay.com/api/videos/"
KEY_NAME = "media/pixabay"
KEY_ENV = "PIXABAY_API_KEY"
CACHE_S = 24 * 3600.0
LANGUAGES = frozenset("cs da de en es fr id it hu nl no pl pt ro sk fi sv tr vi th bg ru el ja ko zh".split())
QUALITIES = ("large", "medium", "small", "tiny")
USER_AGENT = "TaDaMoo/1"
STOP = frozenset("""a an the of in on at to for from with without and or but is are was were be by as it its this that these those into over under near onto up down out off
no not very more most some any each other such than then so while during about around across between through behind above below inside outside""".split())
LOOK = frozenset("""realistic documentary news footage shot video clip scene view camera steady natural light lighting cinematic style high quality hd 4k slow motion
b-roll broll background beautiful professional real""".split())
HEAD_WEIGHT, TAIL_WEIGHT, HEAD = 1.0, 0.6, 3


class StockError(MediaError):
    """Pixabay could not be asked or did not answer well; the message says why."""


class Keys:
    """The Pixabay key in the credential store (the one the other keys of the application use); a key is never returned to a page or logged."""

    def __init__(self, store: Any = None) -> None:
        if store is None:
            from serve.userdata import user_data_dir
            from tadamoo.llm.credentials import default_store
            store = default_store(Path(user_data_dir()))
        self.store = store

    def get(self, environ: Optional[Dict[str, str]] = None) -> str:
        env = os.environ if environ is None else environ
        if env.get(KEY_ENV):
            return str(env[KEY_ENV]).strip()
        try:
            return self.store.read(KEY_NAME) or ""
        except Exception:                                   # a store that cannot be read has no key
            return ""

    def set(self, key: str) -> None:
        key = str(key or "").strip()
        if key and (len(key) > 200 or not re.fullmatch(r"[\x21-\x7e]+", key)):
            raise ValueError("a key is one line of letters, digits and signs, up to 200 characters")
        if key:
            self.store.write(KEY_NAME, key)
        else:
            self.store.delete(KEY_NAME)


@dataclass
class Hit:
    id: int
    page: str
    tags: List[str]
    user: str
    duration: float
    downloads: int
    files: Dict[str, Dict[str, Any]] = field(default_factory=dict)       # quality -> {url, width, height, size}

    @property
    def words(self) -> List[str]:
        """The words that describe it: its tags and the title in its page address (`.../videos/traffic-cars-road-12345/`)."""
        slug = urlsplit(self.page).path.rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"-\d+$", "", slug)
        return [w for chunk in self.tags + slug.split("-") for w in re.findall(r"[^\W\d_]+", chunk.lower())]


def stem(word: str) -> str:
    word = word.lower()
    for tail in ("ies", "es", "s"):
        if len(word) > len(tail) + 2 and word.endswith(tail) and not word.endswith("ss"):
            return word[:-len(tail)] + ("y" if tail == "ies" else "")
    return word


def keywords(text: str) -> List[str]:
    """The content words of a description, in order, once each."""
    out: List[str] = []
    for word in re.findall(r"[^\W\d_]+", text.lower()):
        if len(word) < 3 or word in STOP or word in LOOK:
            continue
        if stem(word) not in [stem(k) for k in out]:
            out.append(word)
    return out


def _same(a: str, b: str) -> bool:
    a, b = stem(a), stem(b)
    return a == b or (min(len(a), len(b)) >= 5 and a[:5] == b[:5])


def match(keys: Sequence[str], hit: Hit) -> Tuple[float, int]:
    """(the share of the keywords' weight found in the hit's words, how many keywords were found)."""
    if not keys:
        return 0.0, 0
    words = hit.words
    total = found = 0.0
    count = 0
    for i, k in enumerate(keys):
        weight = HEAD_WEIGHT if i < HEAD else TAIL_WEIGHT
        total += weight
        if any(_same(k, w) for w in words):
            found += weight
            count += 1
    return found / total, count


def segment_start(duration: float, seconds: float) -> float:
    """Where in a video of `duration` seconds a clip of `seconds` begins: the middle."""
    return max(0.0, (duration - seconds) / 2)


@dataclass
class Choice:
    hit: Hit
    query: str
    match: float
    start: float


class Pixabay:
    def __init__(self, key: str, base: str = API_URL, cache_dir: Optional[Path] = None, timeout: float = 30.0, now: Callable[[], float] = time.time) -> None:
        if not key:
            raise StockError("there is no Pixabay key")
        self.key, self.base, self.cache_dir, self.timeout, self.now = key, base, Path(cache_dir) if cache_dir else None, timeout, now

    def _get(self, url: str, limit: int = 8_000_000) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.read(limit)
        except urllib.error.HTTPError as exc:
            why = {400: "the request was refused (bad parameters or key)", 401: "the key was refused", 403: "the key was refused", 429: "too many requests: Pixabay's limit is reached, try later"}
            raise StockError(f"Pixabay answered {exc.code}: {why.get(exc.code, 'an error')}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise StockError(f"Pixabay could not be reached ({getattr(exc, 'reason', exc)})") from None

    def search(self, query: str, lang: str = "en", per_page: int = 20) -> List[Hit]:
        params = {"q": query[:100], "lang": lang if lang in LANGUAGES else "en", "video_type": "film", "safesearch": "true", "order": "popular", "per_page": per_page}
        cache = None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache = self.cache_dir / (hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16] + ".json")
            if cache.is_file() and self.now() - cache.stat().st_mtime < CACHE_S:
                return _hits(json.loads(cache.read_text(encoding="utf-8")))
        raw = self._get(self.base + "?" + urlencode({"key": self.key, **params}))
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise StockError("Pixabay's answer was not JSON") from None
        if cache is not None:
            cache.write_text(json.dumps(data), encoding="utf-8")          # the answer never holds the key
        return _hits(data)

    def download(self, hit: Hit, quality: str, dest: Path) -> Path:
        at = QUALITIES.index(quality) if quality in QUALITIES else 2
        order = QUALITIES[at:] + tuple(reversed(QUALITIES[:at]))
        url = next((hit.files[q]["url"] for q in order if hit.files.get(q, {}).get("url")), "")
        if not url:
            raise StockError(f"video {hit.id} has no file to download")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(self._get(url, limit=400_000_000))
        os.replace(tmp, dest)
        return dest


def _hits(data: Any) -> List[Hit]:
    out: List[Hit] = []
    for h in (data.get("hits") if isinstance(data, dict) else None) or []:
        try:
            out.append(Hit(int(h["id"]), str(h.get("pageURL", "")), [t.strip() for t in str(h.get("tags", "")).split(",") if t.strip()], str(h.get("user", "")),
                           float(h.get("duration", 0)), int(h.get("downloads", 0)),
                           {q: dict(v) for q, v in (h.get("videos") or {}).items() if isinstance(v, dict)}))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def find(client: Pixabay, queries: Sequence[Tuple[str, str]], seconds: float, min_match: float, taken: Set[int]) -> Tuple[Optional[Choice], List[str]]:
    """The best video for a clip of `seconds`, given as (description, language) to try in turn, or None; and what was asked, for the record."""
    asked: List[str] = []
    for text, lang in queries:
        keys = keywords(text)
        for size in range(min(4, len(keys)), 0, -1):
            query = " ".join(keys[:size])
            asked.append(f"{lang}: {query}")
            passing = []
            for hit in client.search(query, lang):
                share, count = match(keys[:size], hit)
                if hit.id in taken or hit.duration < seconds or share < min_match or count < min(2, size):
                    continue
                passing.append((share, hit.downloads, hit))
            if passing:
                share, _, hit = max(passing, key=lambda p: (round(p[0], 3), p[1]))
                return Choice(hit, query, round(share, 3), round(segment_start(hit.duration, seconds), 2)), asked
    return None, asked


def cut(ffmpeg: str, source: Path, dest: Path, start: float, seconds: float, size: Tuple[int, int], fps: int) -> Path:
    """The clip: `seconds` of the video from `start`, filling `size` (scaled, the rest cropped), at `fps`, without sound."""
    w, h = size
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".part" + dest.suffix)
    done = subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{seconds:.3f}", "-an",
                           "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},fps={fps}",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(tmp)], capture_output=True)
    if done.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        raise StockError(f"{source.name} could not be cut: {done.stderr.decode('utf-8', 'replace')[-200:]}")
    os.replace(tmp, dest)
    return dest
