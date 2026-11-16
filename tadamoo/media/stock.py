"""Stock footage from Pixabay (https://pixabay.com/api/docs/): looked for before a clip is made by the video model, cut to the length the plan gives it.

The key is the person's own and lives in the credential store, under `media/pixabay` (`PIXABAY_API_KEY` in the environment counts too); it is never part of a file of the
project and never printed. A search is a GET of `/api/videos/` with the keywords of what the clip must show; the answer is cached 24 hours, as Pixabay asks.

Choosing: a clip comes with rungs, from the most specific search to the least, each made of the words of a subject (the thing the words over the clip are about), the words to ask Pixabay and whether the
place is wanted too (stockplace.py). A video passes a rung when it lasts at least `min_seconds` (shorter than a clip is allowed: the montage fits the shot to the clip it has), is not one already taken or used in another edition, carries none of the terms of another region or culture, carries one of the
subject's words in its tags or in the title Pixabay puts in its page address (the only description a hit carries) and, when the rung wants the place, one of the place's terms. Nothing generic passes: a video of the place
that is not about the subject is not taken, and with no video that passes the video model makes the clip. Of the ones that pass the one that carries most of the subject's words wins, then the better match with the
words asked, then the more downloaded. The edition's own choices and those of the editions before are kept in a registry (`Used`): a video shown in the last week is never taken again; one shown longer ago is taken only when no other video passes.
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
    source: str = "pixabay"                                                # the site it comes from: pixabay, pexels or envato
    title: str = ""                                                        # the title the site gives it, when the page address does not carry one
    width: int = 0
    height: int = 0
    partial: bool = False                                                  # the list of the site gave only part of what the page of the video says (length, tags, size)

    @property
    def key(self) -> str:
        """What the registry of shown videos and the sets of taken ones hold: the site and the id."""
        return f"{self.source}:{self.id}"

    @property
    def words(self) -> List[str]:
        """The words that describe it: its tags, its title and, for a site that puts one in the page address, the title in it (`.../videos/traffic-cars-road-12345/`)."""
        slug = "" if self.title else re.sub(r"-\d+$", "", urlsplit(self.page).path.rstrip("/").rsplit("/", 1)[-1])
        return [w for chunk in self.tags + [self.title] + slug.split("-") for w in re.findall(r"[^\W\d_]+", chunk.lower())]


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
    step: str = ""                                  # the rung it was found at, as "subject words / place" (see `Rung`)


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

    def photos(self, query: str, lang: str = "en", per_page: int = 20) -> List[Dict[str, Any]]:
        """Photographs (not videos) for the words, as Pixabay gives them: id, pageURL, tags, user, imageWidth, largeImageURL; wide ones only."""
        base = self.base.replace("/videos/", "/")
        params = {"q": query[:100], "lang": lang if lang in LANGUAGES else "en", "image_type": "photo", "orientation": "horizontal", "min_width": 1600, "safesearch": "true", "order": "popular", "per_page": per_page}
        raw = self._get(base + "?" + urlencode({"key": self.key, **params}))
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise StockError("Pixabay's answer was not JSON") from None
        return [h for h in (data.get("hits") if isinstance(data, dict) else None) or [] if isinstance(h, dict) and h.get("largeImageURL")]

    def download_file(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(self._get(url, limit=60_000_000))
        os.replace(tmp, dest)
        return dest

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


@dataclass(frozen=True)
class Rung:
    """One search for a clip: the words that say the subject (a hit must carry one), what to ask Pixabay (description, language) and whether the place is wanted too."""
    subject: Tuple[str, ...]
    queries: Tuple[Tuple[str, str], ...]
    place: bool = True

    @property
    def label(self) -> str:
        return "/".join(self.subject[:3]) + (" + place" if self.place else "")


def find(client: Any, rungs: Sequence[Rung], seconds: float, min_seconds: float, taken: Set[str], place_terms: Sequence[str] = (), exclude: Sequence[str] = (),
         refused: Optional[List[str]] = None) -> Tuple[Optional[Choice], List[str]]:
    """The best video for a clip of `seconds` (a video that lasts less than that but at least `min_seconds` gives a shorter clip), or None; and what was asked, for the record. The rungs are tried in order (see the module's text);
    what was refused is added to `refused`."""
    from tadamoo.media import stockplace
    asked: List[str] = []
    for rung in rungs:
        for text, lang in rung.queries:
            query = text[:100]
            asked.append(f"{lang}: {query} [{rung.label}]")
            keys = keywords(text)
            passing = []
            looked = 0
            for hit in client.search(query, lang):
                if hit.key in taken or (hit.duration and hit.duration < min_seconds):
                    continue
                words = stockplace.words_of([" ".join(hit.words)])
                lists_only = hit.partial and hasattr(client, "details")      # a site whose list says no length and no tags: the list is judged on the subject and the other cultures, the place after its page is read
                why = stockplace.other_culture(words, exclude) if lists_only else stockplace.refused_because(words, rung.subject, place_terms if rung.place else (), exclude)
                if lists_only and not why and not stockplace.carries(words, rung.subject):
                    why = "is not about the subject (" + ", ".join(list(rung.subject)[:4]) + ")"
                if why:
                    if refused is not None and len(refused) < 30:
                        refused.append(f"{hit.source} {hit.id} ({hit.page}) refused at '{rung.label}': {why}")
                    continue
                if lists_only:                                                     # the page of the video says its length, its tags and its size: read for the few that pass the list
                    if looked >= getattr(client, "max_details", 6):
                        continue
                    looked += 1
                    client.details(hit)
                    words = stockplace.words_of([" ".join(hit.words)])
                    why = stockplace.refused_because(words, rung.subject, place_terms if rung.place else (), exclude)
                    if not why and hit.width and hit.height and hit.width < hit.height:
                        why = "is not landscape"
                    if not why and hit.duration < min_seconds:
                        why = f"lasts {hit.duration:g} s"
                    if why:
                        if refused is not None and len(refused) < 30:
                            refused.append(f"{hit.source} {hit.id} ({hit.page}) refused at '{rung.label}': {why}")
                        continue
                carried = sum(1 for w in rung.subject if stockplace.carries(words, [w]))
                share, _ = match(keys, hit)
                passing.append((carried, round(share, 3), hit.downloads, hit))
                if getattr(client, "first_fit", False):
                    break
            if passing:
                carried, share, _, hit = max(passing, key=lambda p: p[:3])
                return Choice(hit, query, share, round(segment_start(hit.duration, min(seconds, hit.duration)), 2), rung.label), asked
    return None, asked


WEEK_S = 7 * 24 * 3600.0


class Used:
    """The registry of the stock videos already shown. A video of one edition is not taken again by another for a week (`recent`); after that it may be, when nothing else passes (`ids` lists all of them).
    One JSON file ({"videos": {id: {edition, ts, page}}}) in the folder that holds the editions."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return dict(data.get("videos") or {}) if isinstance(data, dict) else {}

    @staticmethod
    def _key(k: Any) -> str:
        return str(k) if ":" in str(k) else f"pixabay:{k}"                          # a number written before there were other sites is a Pixabay video

    def ids(self) -> Set[str]:
        return {self._key(k) for k in self._read()}

    def recent(self, now: Optional[float] = None, window: float = WEEK_S) -> Set[str]:
        """The videos shown less than `window` seconds ago."""
        now = time.time() if now is None else now
        return {self._key(k) for k, v in self._read().items() if now - float((v or {}).get("ts") or 0) < window}

    def add(self, key: Any, edition: str, page: str = "", ts: Optional[float] = None) -> None:
        videos = self._read()
        videos[self._key(key)] = {"edition": edition, "ts": round(time.time() if ts is None else ts, 1), "page": page}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".part")
        tmp.write_text(json.dumps({"videos": videos}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def seed_from(self, root: Path) -> int:
        """Adds the videos that the editions kept in `root` (a folder each, with the notes of their clips in `broll/`) show and the registry does not know yet; returns how many."""
        have, added = self.ids(), 0
        for note in sorted(Path(root).glob("*/broll/s*_c*.json")):
            try:
                info = json.loads(note.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            key = f"{info.get('source')}:{info.get('id')}"
            if info.get("source") in ("pixabay", "pexels", "envato") and info.get("id") not in (None, "") and key not in have:
                self.add(key, note.parent.parent.name, str(info.get("page", "")), note.stat().st_mtime)
                have.add(key)
                added += 1
        return added


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
