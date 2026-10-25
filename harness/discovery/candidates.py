"""What the online search finds, and the memory that keeps it between the search and the person's choice.

The model never names a package, a command or an address. It can only point at a candidate by the id this process gave it, and
the installer builds every command from the fields of a candidate it holds here, after checking each one against a strict
pattern. Text that came from a source (names, descriptions) is untrusted and is only shown, never run.
"""
from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from harness.discovery.scoring import Metrics, Score

NPM_NAME = re.compile(r"^(@[a-z0-9~][a-z0-9._~-]{0,213}/)?[a-z0-9~][a-z0-9._~-]{0,213}$")
NPM_VERSION = re.compile(r"^\d{1,9}\.\d{1,9}\.\d{1,9}(-[0-9A-Za-z.\-]{1,64})?(\+[0-9A-Za-z.\-]{1,64})?$")
PYPI_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
PYPI_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-+!_]{0,63}$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,62}$")
GITHUB_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_ARG = 200


def slugify(name: str) -> str:
    """A folder and capability name from a package or server name: its last part, in letters, digits, dots, dashes and underscores."""
    last = re.split(r"[/\\]", name.strip())[-1] if name.strip() else ""
    slug = re.sub(r"[^A-Za-z0-9_.\-]+", "-", last).strip("-.")[:60]
    return slug or "capability"


def safe_relative(path: str) -> bool:
    """A path inside a repository: no empty part, no `..`, no drive, no backslash, no absolute start."""
    if not path or path.startswith("/") or "\\" in path or ":" in path or "\x00" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


@dataclass
class Candidate:
    kind: str                                   # mcp or skill
    name: str                                   # the short name the capability will have
    source: str                                 # registry, npm, pypi, github or marketplace
    route: str                                  # npm, pypi, remote or skill: how it would be installed
    title: str = ""
    description: str = ""                       # from the source: untrusted
    version: str = ""
    package: str = ""                           # npm or PyPI name
    url: str = ""                               # a remote server's address
    transport: str = ""
    repo: str = ""                              # owner/repo on GitHub, when the source names one
    path: str = ""                              # a skill's folder inside the repository
    commit: str = ""                            # a skill's pinned commit
    integrity: str = ""                         # npm: the hash the registry records for the release
    digests: List[str] = field(default_factory=list)    # PyPI: sha256 of the release's files
    bins: Dict[str, str] = field(default_factory=dict)  # npm: the programs the package installs
    args: List[str] = field(default_factory=list)       # fixed arguments the registry gives for a local server
    env: List[Dict[str, Any]] = field(default_factory=list)   # {"name", "required", "secret", "value"} the server reads
    metrics: Metrics = field(default_factory=Metrics)
    score: Optional[Score] = None
    id: str = ""
    relevance: float = 1.0                      # the share of the need's words that appear in its name and description
    installable: bool = True
    problem: str = ""                           # why it cannot be installed, when it cannot

    def check(self) -> Optional[str]:
        """Why the fields cannot be trusted to build a command from, or None when they can."""
        if not SLUG.match(self.name):
            return f"the name '{self.name[:40]}' is not usable"
        if self.route == "npm":
            if not NPM_NAME.match(self.package) or not NPM_VERSION.match(self.version):
                return "the package name or version is not an exact npm release"
        elif self.route == "pypi":
            if not PYPI_NAME.match(self.package) or not PYPI_VERSION.match(self.version):
                return "the package name or version is not an exact PyPI release"
        elif self.route == "remote":
            if not re.match(r"^https?://[^\s/]+", self.url) or any(c.isspace() for c in self.url):
                return "the address is not a web address"
        elif self.route == "skill":
            owner, _, repo = self.repo.partition("/")
            if not (GITHUB_PART.match(owner) and GITHUB_PART.match(repo) and SHA.match(self.commit) and safe_relative(self.path)):
                return "the skill's repository, folder or commit is not valid"
        else:
            return "unknown way to install"
        if any(not isinstance(a, str) or len(a) > MAX_ARG or "\x00" in a for a in self.args):
            return "an argument is not usable"
        if any(not ENV_NAME.match(str(e.get("name", ""))) for e in self.env):
            return "a variable name is not usable"
        return None

    def preview(self) -> str:
        """What would be started, in a line the person can read before choosing."""
        args = " ".join(self.args)
        if self.route == "npm":
            program = next(iter(self.bins), self.package)
            return f"{program} {args}".strip() + f"   [a copy of npm package {self.package}@{self.version}]"
        if self.route == "pypi":
            return f"{self.package} {args}".strip() + f"   [python package {self.package}=={self.version}]"
        if self.route == "remote":
            return self.url
        return f"no program: the folder {self.path} of github.com/{self.repo} at commit {self.commit[:10]}"

    def keys(self) -> List[str]:
        return [str(e["name"]) for e in self.env if e.get("required") and not e.get("value")]

    def card(self, min_score: int = 0) -> Dict[str, Any]:
        """The candidate as the person is shown it; the description is the source's own words."""
        s = self.score
        return {"id": self.id, "kind": self.kind, "name": self.name, "package": self.package, "source": self.source, "route": self.route,
                "version": self.version, "score": s.total if s else 0, "reasons": s.reasons() if s else "",
                "facts": [f.as_dict() for f in s.facts] if s else [], "warnings": [w.as_dict() for w in s.warnings] if s else [],
                "command": self.preview(), "keys": self.keys(), "description": self.description[:300], "below_min": bool(s and s.total < min_score),
                "repo": self.repo}


class CandidateStore:
    """The candidates of the last searches, by the id given to the model. Nothing else can be installed."""

    LIMIT = 60

    def __init__(self) -> None:
        self._items: Dict[str, Candidate] = {}
        self._lock = threading.Lock()

    def add(self, candidate: Candidate) -> Candidate:
        with self._lock:
            candidate.id = "c" + secrets.token_hex(3)
            self._items[candidate.id] = candidate
            while len(self._items) > self.LIMIT:
                self._items.pop(next(iter(self._items)))
        return candidate

    def get(self, candidate_id: str) -> Optional[Candidate]:
        with self._lock:
            return self._items.get(str(candidate_id))
