"""A score from 0 to 100 for a capability found online, with the part each fact played and warnings in plain words.

The score measures how much a package is used and how well it is looked after: downloads, how recent and how frequent its
releases are, stars and forks, open issues against the size of the project, a declared licence, a publisher the registry
verified, age and ratings where a source has them. It does NOT measure safety: a popular, well kept package can still do
harm, and a new one can be harmless. The person reads the warnings and decides.

A fact a source does not expose is left out and the others are weighed among themselves; a fact that is absent (no repository,
no licence) counts as zero. The weights and the minimum score are the person's, in discovery.json.
"""
from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_WEIGHTS: Dict[str, float] = {
    "downloads": 20, "freshness": 20, "cadence": 10, "stars": 10, "forks": 5, "issues": 5,
    "license": 5, "publisher": 10, "age": 5, "ratings": 10,
}
DEFAULT_MIN_SCORE = 50
DOWNLOADS_FULL = 100_000          # weekly downloads that earn the whole share
STARS_FULL = 5_000
FORKS_FULL = 500
AGE_FULL_DAYS = 365
FRESH_POINTS = ((30, 1.0), (365, 0.3), (730, 0.0))          # (days since the last release, share)
CADENCE_POINTS = ((0, 0.0), (1, 0.4), (3, 0.7), (6, 1.0))   # (releases in the last year, share)
STALE_DAYS = 365
FEW_DOWNLOADS = 100

# English wording of the facts and warnings; the Chat page has the same codes in both its languages
TEXT: Dict[str, str] = {
    "downloads": "{0} downloads a week", "released": "last release {0} days ago", "stars": "{0} stars on GitHub",
    "verified": "verified publisher", "license": "licence {0}",
    "stale": "not updated for {0} days", "few_downloads": "few downloads ({0} a week)", "no_repo": "no source repository listed",
    "lookalike": "its name is almost the same as the well known {0}: it may be an imitation",
    "needs_keys": "asks for keys or settings: {0}", "install_scripts": "has install scripts, which Basalt does not run: it may not work",
    "runs_program": "it is a program that runs on this PC with your rights", "archived": "the repository is archived: nobody maintains it",
    "deprecated": "the registry marks it as deprecated", "no_license": "no licence declared",
    "github_unchecked": "GitHub could not be asked ({0}): stars and activity are not counted",
    "remote": "it is a service on someone else's server: what you send it leaves this PC",
    "skill_files": "a skill is text for the model, but its folder may hold scripts the model could run later",
}

# names people install often; one that only looks like them is a warning (typosquatting)
WELL_KNOWN = (
    "@modelcontextprotocol/server-filesystem", "@modelcontextprotocol/server-github", "@modelcontextprotocol/server-memory",
    "@modelcontextprotocol/server-puppeteer", "@modelcontextprotocol/server-brave-search", "@modelcontextprotocol/server-postgres",
    "@modelcontextprotocol/server-sequential-thinking", "@modelcontextprotocol/server-slack", "@modelcontextprotocol/server-everything",
    "@playwright/mcp", "@upstash/context7-mcp", "tavily-mcp", "exa-mcp-server", "firecrawl-mcp", "mcp-server-fetch", "mcp-server-git",
    "mcp-server-time", "mcp-server-sqlite", "@notionhq/notion-mcp-server", "@supabase/mcp-server-supabase", "@stripe/mcp",
    "chrome-devtools-mcp", "figma-developer-mcp", "@browsermcp/mcp", "@cloudflare/mcp-server-cloudflare", "mcp-remote",
)


@dataclass
class Metrics:
    """What is known about a candidate; None means the source does not say."""
    downloads_week: Optional[int] = None
    last_release: Optional[datetime] = None
    first_release: Optional[datetime] = None
    releases_12m: Optional[int] = None
    stars: Optional[int] = None
    forks: Optional[int] = None
    open_issues: Optional[int] = None
    archived: bool = False
    deprecated: bool = False
    license: str = ""
    has_repo: bool = True                       # False: the package names no repository
    repo_checked: bool = False                  # the repository was asked about and answered
    repo_error: str = ""                        # why it was not, when it was not
    publisher_verified: Optional[bool] = None
    rating: Optional[float] = None              # 0 to 5, where a source has reviews
    rating_count: Optional[int] = None
    install_scripts: bool = False
    env_required: List[str] = field(default_factory=list)
    runs_program: bool = False
    remote: bool = False
    skill: bool = False
    name: str = ""                              # the package name, for the look-alike check


@dataclass
class Note:
    code: str
    args: List[Any] = field(default_factory=list)

    def text(self) -> str:
        template = TEXT.get(self.code, self.code)
        try:
            return template.format(*self.args)
        except (IndexError, KeyError):
            return template

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "args": [str(a) for a in self.args], "text": self.text()}


@dataclass
class Score:
    total: int
    parts: List[Dict[str, Any]]
    warnings: List[Note]
    facts: List[Note]

    def reasons(self) -> str:
        return " · ".join(n.text() for n in self.facts)

    def as_dict(self) -> Dict[str, Any]:
        return {"total": self.total, "parts": self.parts, "warnings": [w.as_dict() for w in self.warnings],
                "facts": [f.as_dict() for f in self.facts], "reasons": self.reasons()}


def _log_share(n: Optional[int], full: float) -> Optional[float]:
    if n is None:
        return None
    return min(1.0, math.log10(1 + max(0, n)) / math.log10(1 + full))


def _curve(x: float, points: Iterable[Tuple[float, float]]) -> float:
    pts = list(points)
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def days_since(when: Optional[datetime], now: datetime) -> Optional[int]:
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, (now - when).days)


def parse_time(text: Any) -> Optional[datetime]:
    """An ISO 8601 time as the sources write it (a trailing Z, fractions of a second); None if it is not one."""
    if not isinstance(text, str) or not text:
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def lookalike(name: str, known: Iterable[str] = WELL_KNOWN) -> Optional[str]:
    """The well known name this one nearly equals without being it: a typo, a changed scope, a swapped separator."""
    def flat(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())
    mine = name.lower()
    for other in known:
        if mine == other.lower():
            return None
    for other in known:
        a, b = flat(name), flat(other)
        if not a or not b:
            continue
        last_a, last_b = name.lower().rsplit("/", 1)[-1], other.lower().rsplit("/", 1)[-1]
        if a == b or (last_a == last_b and len(last_b) >= 6) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.88:
            return other
    return None


def _declared(license_name: str) -> bool:
    return bool(license_name) and license_name.upper() not in ("NOASSERTION", "NONE", "UNKNOWN", "OTHER")


def _components(m: Metrics, now: datetime) -> Dict[str, Tuple[Optional[float], Optional[Note]]]:
    """Each fact's share of its weight (0 to 1, or None when it does not apply) and the fact as a note."""
    age_release = days_since(m.last_release, now)
    out: Dict[str, Tuple[Optional[float], Optional[Note]]] = {}
    out["downloads"] = (_log_share(m.downloads_week, DOWNLOADS_FULL),
                        Note("downloads", [f"{m.downloads_week:,}"]) if m.downloads_week else None)
    out["freshness"] = (None if age_release is None else _curve(age_release, FRESH_POINTS),
                        None if age_release is None else Note("released", [age_release]))
    out["cadence"] = (None if m.releases_12m is None else _curve(m.releases_12m, CADENCE_POINTS), None)
    repo_known = m.has_repo and m.repo_checked
    if not m.has_repo:
        out["stars"], out["forks"], out["issues"] = (0.0, None), (0.0, None), (None, None)
    elif repo_known:
        out["stars"] = (_log_share(m.stars, STARS_FULL), Note("stars", [f"{m.stars:,}"]) if m.stars else None)
        out["forks"] = (_log_share(m.forks, FORKS_FULL), None)
        ratio = (m.open_issues or 0) / ((m.stars or 0) + (m.forks or 0) + 10)
        out["issues"] = (max(0.0, 1.0 - ratio / 0.2), None)
    else:
        out["stars"] = out["forks"] = out["issues"] = (None, None)
    declared = _declared(m.license)
    out["license"] = (1.0 if declared else (0.0 if (m.has_repo is False or repo_known or m.license) else None),
                      Note("license", [m.license]) if declared else None)
    out["publisher"] = (None if m.publisher_verified is None else (1.0 if m.publisher_verified else 0.0),
                        Note("verified") if m.publisher_verified else None)
    age = days_since(m.first_release, now)
    out["age"] = (None if age is None else min(1.0, age / AGE_FULL_DAYS), None)
    out["ratings"] = (None if m.rating is None else max(0.0, min(1.0, m.rating / 5.0)) * min(1.0, (m.rating_count or 0) / 10.0), None)
    return out


def warnings_for(m: Metrics, now: datetime) -> List[Note]:
    out: List[Note] = []
    if m.archived:
        out.append(Note("archived"))
    if m.deprecated:
        out.append(Note("deprecated"))
    age = days_since(m.last_release, now)
    if age is not None and age > STALE_DAYS:
        out.append(Note("stale", [age]))
    if m.downloads_week is not None and m.downloads_week < FEW_DOWNLOADS:
        out.append(Note("few_downloads", [m.downloads_week]))
    if not m.has_repo:
        out.append(Note("no_repo"))
    if m.has_repo and not m.repo_checked and m.repo_error:
        out.append(Note("github_unchecked", [m.repo_error]))
    if m.name and (near := lookalike(m.name)):
        out.append(Note("lookalike", [near]))
    if m.env_required:
        out.append(Note("needs_keys", [", ".join(m.env_required)]))
    if m.install_scripts:
        out.append(Note("install_scripts"))
    if m.runs_program:
        out.append(Note("runs_program"))
    if m.remote:
        out.append(Note("remote"))
    if m.skill:
        out.append(Note("skill_files"))
    if not _declared(m.license) and (not m.has_repo or m.repo_checked):
        out.append(Note("no_license"))
    return out


def score(m: Metrics, weights: Optional[Dict[str, float]] = None, now: Optional[datetime] = None) -> Score:
    """The score of one candidate; `weights` replaces the defaults for the keys it names."""
    now = now or datetime.now(timezone.utc)
    w = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in (weights or {}).items() if k in DEFAULT_WEIGHTS}}
    parts: List[Dict[str, Any]] = []
    facts: List[Note] = []
    got = total_w = 0.0
    for key, (share, note) in _components(m, now).items():
        weight = max(0.0, w[key])
        parts.append({"key": key, "weight": weight, "share": None if share is None else round(share, 3)})
        if share is not None and weight > 0:
            got += weight * share
            total_w += weight
        if note is not None:
            facts.append(note)
    value = round(100 * got / total_w) if total_w > 0 else 0
    for part in parts:                                     # points: what each fact added to the total, out of 100
        part["points"] = None if part["share"] is None or total_w <= 0 else round(100 * part["weight"] * part["share"] / total_w, 1)
    if m.archived or m.deprecated:
        value = 0
    return Score(max(0, min(100, value)), parts, warnings_for(m, now), facts)
