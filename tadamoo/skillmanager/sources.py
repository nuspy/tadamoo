"""Where a skill can come from, and how an address becomes a folder to read.

An address is one of: a folder on this PC (a skill, a plugin, or a folder holding several, such as a cloned repository); a GitHub address (a repository, or a
folder or a SKILL.md inside it); a web address (a SKILL.md, an archive, or a page that links to a SKILL.md). A page that only describes a skill is the
description module's. Everything is fetched with the standard library into a staging folder and only read: nothing is run, and what the person is shown is
exactly what is then installed."""
from __future__ import annotations

import re
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from tadamoo.importer.common import SKIP_DIRS, Problem, check_name, safe_join
from tadamoo.skills import parse_frontmatter

MAX_PAGE = 2 << 20
MAX_ARCHIVE = 300 << 20
MAX_UNPACKED = 600 << 20
MAX_MEMBERS = 20000
MAX_CANDIDATES = 200
SEARCH_DEPTH = 4
GITHUB = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(/[^?#]*)?(?:[?#].*)?$", re.I)
AGENT = "TaDaMoo-skills"


@dataclass
class Reply:
    url: str
    ctype: str
    data: bytes


class Fetcher:
    """What reads the net; a test puts a fake one in its place."""

    def get(self, url: str, limit: int = MAX_PAGE) -> Reply:
        with self._open(url) as r:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise Problem("too_big", f"{url[:100]} is larger than {limit >> 20} MB.")
            return Reply(r.geturl(), r.headers.get_content_type(), data)

    def save(self, url: str, dest: Path, limit: int = MAX_ARCHIVE) -> str:
        with self._open(url) as r, open(dest, "wb") as out:
            total = 0
            for block in iter(lambda: r.read(1 << 20), b""):
                total += len(block)
                if total > limit:
                    raise Problem("too_big", f"{url[:100]} is larger than {limit >> 20} MB.")
                out.write(block)
            return r.headers.get_content_type()

    @staticmethod
    def _open(url: str) -> Any:
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": AGENT}), timeout=30)
        except urllib.error.HTTPError as exc:
            raise Problem("fetch_failed", f"The address answered {exc.code}: {url[:100]}", status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise Problem("fetch_failed", f"The address cannot be reached: {url[:100]} ({exc})") from None


@dataclass
class Fetched:
    """What an address gave: the folder to look in and where it came from."""
    type: str                      # folder | github | web
    ref: str                       # the address as given (the folder, the URL)
    root: Path
    generated: bool = False
    notes: List[str] = field(default_factory=list)


# -- looking for skills in a folder --------------------------------------------------------------------------------------

def kind_of(folder: Path) -> str:
    """'plugin', 'skill' or '' for a folder that is neither."""
    if (folder / "plugin.json").is_file() or (folder / ".claude-plugin" / "plugin.json").is_file():
        return "plugin"
    if (folder / "SKILL.md").is_file() or ((folder / "skill.json").is_file() and (folder / "skill.py").is_file()):
        return "skill"
    return ""


def discover(root: Path) -> List[Tuple[Path, str]]:
    """The skills and plugins under `root` (or `root` itself), each once, not looking inside one that was found."""
    root = Path(root)
    if not root.is_dir():
        raise Problem("no_source", f"There is no folder at {root}.")
    if kind := kind_of(root):
        return [(root, kind)]
    out: List[Tuple[Path, str]] = []

    def walk(folder: Path, depth: int) -> None:
        for d in sorted(p for p in folder.iterdir() if p.is_dir() and p.name not in SKIP_DIRS and not p.name.startswith(".")):
            if len(out) >= MAX_CANDIDATES:
                return
            if kind := kind_of(d):
                out.append((d, kind))
            elif depth < SEARCH_DEPTH:
                walk(d, depth + 1)

    walk(root, 1)
    if not out:
        raise Problem("no_skill_found", "No skill was found there: a skill is a folder with a SKILL.md, a plugin one with a plugin.json.")
    return out


# -- an archive ----------------------------------------------------------------------------------------------------------

def unpack(archive: Path, dest: Path) -> Path:
    """Unpacks a zip into `dest` (no link, nothing outside it, within the limits) and returns the folder that holds the content: the single top folder when there is one."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError):
        raise Problem("bad_archive", "That is not a readable zip archive.") from None
    with zf:
        members = zf.infolist()
        if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > MAX_UNPACKED:
            raise Problem("too_big", "The archive holds too many files or is too large to be a skill.")
        for m in members:
            parts = PurePosixPath(m.filename).parts
            if (m.external_attr >> 16) & 0o170000 == 0o120000 or not parts:
                continue                                                        # a link is left out
            if m.filename.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
                raise Problem("outside", f"'{m.filename[:80]}' points outside the archive's folder.")
            target = dest.joinpath(*parts)
            if m.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    tops = [p for p in dest.iterdir()]
    return tops[0] if len(tops) == 1 and tops[0].is_dir() else dest


# -- GitHub --------------------------------------------------------------------------------------------------------------

def parse_github(address: str) -> Optional[Dict[str, Any]]:
    """`{owner, repo, kind, rest}` for a GitHub address (`kind` tree or blob and `rest` the segments after it, else empty), None for anything else."""
    m = GITHUB.match(address.strip())
    if not m:
        return None
    segments = [s for s in (m.group(3) or "").split("/") if s]
    kind = segments[0] if segments and segments[0] in ("tree", "blob") else ""
    if segments and not kind:
        return None                                                                 # an issue, a release page... not a place that holds a skill
    return {"owner": m.group(1), "repo": m.group(2), "kind": kind, "rest": segments[1:]}


def fetch_github(address: str, work: Path, fetch: Fetcher) -> Fetched:
    g = parse_github(address)
    if g is None:
        raise Problem("bad_address", "That GitHub address is not a repository, a folder or a file of one.")
    base = f"https://github.com/{g['owner']}/{g['repo']}/archive/"
    splits = [("HEAD", g["rest"])] if not g["rest"] else [("/".join(g["rest"][:i]), g["rest"][i:]) for i in range(1, len(g["rest"]) + 1)]
    last: Optional[Problem] = None
    for ref, sub in splits:                                                         # a branch name may hold slashes: the first split that exists is the one
        archive = work / "repo.zip"
        try:
            fetch.save(base + ref + ".zip", archive)
        except Problem as exc:
            if exc.data.get("status") == 404:
                last = exc
                continue
            raise
        root = unpack(archive, work / "repo")
        archive.unlink(missing_ok=True)
        if g["kind"] == "blob" and sub:
            sub = sub[:-1] if sub[-1].lower() in ("skill.md", "plugin.json") else sub
        root = safe_join(root, "/".join(sub)) if sub else root
        if not root.is_dir():
            raise Problem("no_source", f"That folder is not in the repository: {'/'.join(sub)}")
        return Fetched("github", address.strip(), root)
    raise Problem("fetch_failed", f"The repository or its branch was not found (it may be private): {address[:100]}") from last


# -- the web -------------------------------------------------------------------------------------------------------------

class Page(HTMLParser):
    """The visible text of a page and its links."""
    SKIP = frozenset(("script", "style", "noscript", "template", "svg"))
    BREAK = frozenset(("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table", "ul", "ol"))

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: List[str] = []
        self.links: List[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BREAK:
            self.text.append("\n")
        if tag == "a" and dict(attrs).get("href"):
            self.links.append(dict(attrs)["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BREAK:
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.text.append(data)


def read_page(markup: str) -> Tuple[str, List[str]]:
    page = Page()
    try:
        page.feed(markup)
        page.close()
    except Exception:                                                               # markup so broken the parser stops: what it read is kept
        pass
    text = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", "".join(page.text))).strip()
    return text, page.links


def _is_skill_md(text: str) -> bool:
    meta, _ = parse_frontmatter(text)
    return bool(meta.get("name") and meta.get("description"))


def _skill_from_markdown(text: str, work: Path, name_hint: str) -> Path:
    meta, _ = parse_frontmatter(text)
    name = (meta.get("name") or "").strip()
    folder = work / "skills" / check_name(name if name else name_hint, "skill name")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return folder.parent


def fetch_web(address: str, work: Path, fetch: Fetcher, describe: Optional[Callable[[str, str, Path], Path]] = None) -> Fetched:
    """A web address: an archive is unpacked, a SKILL.md is the skill, a page that links to SKILL.md files (on GitHub or beside it) gives those, and any other page is
    described: `describe(url, text, work)` writes a skill folder from its text and returns the folder that holds it (None: no model, the page is not usable)."""
    url = address.strip()
    path = urlsplit(url).path
    if parse_github(url):
        return fetch_github(url, work, fetch)
    if path.lower().endswith(".zip"):                                               # an archive goes to a file, not into memory
        fetch.save(url, work / "page.zip")
        return Fetched("web", url, unpack(work / "page.zip", work / "archive"))
    reply = fetch.get(url, MAX_PAGE)
    if reply.data[:4] == b"PK\x03\x04":
        (work / "page.zip").write_bytes(reply.data)
        return Fetched("web", url, unpack(work / "page.zip", work / "archive"))
    try:
        text = reply.data.decode("utf-8")
    except UnicodeDecodeError:
        text = reply.data.decode("latin-1")
    if path.lower().endswith("skill.md") or (reply.ctype != "text/html" and _is_skill_md(text)):
        return Fetched("web", url, _skill_from_markdown(text, work, PurePosixPath(path).parent.name or "skill"))
    page_text, links = read_page(text)
    found: List[str] = []
    for href in links:
        full = urljoin(reply.url, href.strip())
        if urlsplit(full).path.lower().endswith("/skill.md") and full not in found:
            found.append(full)
    if found:
        root = work / "linked"
        for i, link in enumerate(found[:5]):
            sub = work / f"link{i}"
            sub.mkdir(parents=True, exist_ok=True)
            got = fetch_github(link, sub, fetch).root if parse_github(link) else _skill_from_markdown(fetch.get(link).data.decode("utf-8", "replace"), sub, f"skill{i}")
            shutil.copytree(got, root / f"from{i}", dirs_exist_ok=True)
        return Fetched("web", url, root)
    if describe is None or len(page_text) < 40:
        raise Problem("no_skill_found", "That page holds no SKILL.md, no archive and no description to make a skill from.")
    folder = describe(url, page_text, work)
    return Fetched("web", url, folder, generated=True, notes=["written from the page by the model"])


def fetch(address: str, work: Path, fetcher: Optional[Fetcher] = None, describe: Optional[Callable[[str, str, Path], Path]] = None) -> Fetched:
    """The folder an address gives, in `work`. A folder on this PC is read where it is."""
    address = (address or "").strip().strip('"')
    if not address:
        raise Problem("bad_address", "Give an address: a folder on this PC, a GitHub address or a web address.")
    local = Path(address)
    if local.exists():
        if not local.is_dir():
            if local.name in ("SKILL.md", "skill.json", "skill.py", "plugin.json"):
                local = local.parent
            else:
                raise Problem("no_source", f"{address[:100]} is a file: give the folder of the skill.")
        return Fetched("folder", str(local), local)
    work.mkdir(parents=True, exist_ok=True)
    fetcher = fetcher or Fetcher()
    if parse_github(address):
        return fetch_github(address, work, fetcher)
    if re.match(r"^https?://", address, re.I):
        return fetch_web(address, work, fetcher, describe)
    raise Problem("bad_address", f"'{address[:100]}' is not a folder on this PC, a GitHub address or a web address.")
