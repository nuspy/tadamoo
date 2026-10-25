"""Looks for a capability online: asks the sources the person turned on, makes candidates, scores them and returns the best.

The need is a few words the model wrote; they are the only thing that leaves the PC, as the search text of a request. A package
or repository found is untrusted. Nothing is installed here.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.discovery import sources as src
from harness.discovery.candidates import Candidate, CandidateStore, slugify
from harness.discovery.scoring import Metrics, parse_time, score
from harness.discovery.settings import DiscoverySettings
from harness.skills import parse_frontmatter

MAX_RESULTS = 5
KEYWORDS = 4
REGISTRY_POOL = 6
NPM_POOL = 6
GITHUB_REPO_CAP = 8                    # repositories asked about in one search, so one search does not use the whole unidentified limit
SKILL_REPOS = 3
SKILLS_PER_REPO = 3
SKILL_FILE_BYTES = 20_000
TOO_GENERIC = frozenset("a an the of to in on for from with and or is are be it this that my me i do does you how what which into as at by use using want need "
                        "please can could mcp server servers skill skills plugin plugins tool tools agent help able thing something".split())


def resolve_bases(configured: Dict[str, str], given: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The addresses of the services: the defaults, then the ones in the settings file, then the ones a caller names (tests)."""
    return {**src.DEFAULT_BASES, **configured, **(given or {})}


def keywords(need: str) -> List[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", need.lower()) if len(w) >= 3 and w not in TOO_GENERIC]
    return list(dict.fromkeys(words))[:KEYWORDS]


def _relevance(words: List[str], *texts: str) -> float:
    if not words:
        return 1.0
    blob = " ".join(texts).lower().replace("-", " ").replace("_", " ")
    return sum(1 for w in words if w in blob) / len(words)


@dataclass
class SearchResult:
    candidates: List[Candidate] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    asked: List[str] = field(default_factory=list)
    skipped: int = 0                      # found but not offered: they need something Basalt cannot give (arguments, a key in a header, another kind of package)


class Finder:
    def __init__(self, settings: DiscoverySettings, store: CandidateStore, *, http: Optional[src.Http] = None,
                 token: Callable[[], str] = lambda: "", bases: Optional[Dict[str, str]] = None,
                 now: Optional[Callable[[], datetime]] = None) -> None:
        self.settings, self.store = settings, store
        self.http = http or src.Http()
        self.token = token
        self.bases = bases or {}
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._github_left = 0
        self._github_stopped = ""

    # -- the whole search -----------------------------------------------------------------------------------------

    def search(self, need: str) -> SearchResult:
        cfg = self.settings.load()
        on = {s for s, enabled in cfg["sources"].items() if enabled}
        bases = resolve_bases(cfg["base_urls"], self.bases)
        words = keywords(need)
        result = SearchResult(asked=sorted(on))
        if not words:
            result.errors["need"] = "The need has no usable words: describe it in a few plain words."
            return result
        self.registry = src.RegistryClient(self.http, bases["registry"])
        self.npm = src.NpmClient(self.http, bases["npm"], bases["npm_downloads"])
        self.pypi = src.PypiClient(self.http, bases["pypi"])
        self.github = src.GithubClient(self.http, bases["github"], bases["github_raw"], self.token)
        self._github_left, self._github_stopped = GITHUB_REPO_CAP, ""
        pool: List[Candidate] = []
        jobs: List[Tuple[str, Callable[[], List[Candidate]]]] = []
        if "registry" in on:
            jobs.append(("registry", lambda: self._registry(words, result)))
        if "npm" in on:
            jobs.append(("npm", lambda: self._npm_search(words)))
        if "skills" in on:
            jobs.append(("skills", lambda: self._skills(words, cfg)))
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
            futures = [(name, ex.submit(fn)) for name, fn in jobs]
            for name, fut in futures:
                try:
                    pool += fut.result()
                except src.SourceError as e:
                    result.errors[name] = str(e)
                except Exception as e:                                        # a source's own surprise must not stop the others
                    result.errors[name] = f"{type(e).__name__}: {e}"
        unique: Dict[Tuple[str, str], Candidate] = {}
        for c in pool:
            unique.setdefault((c.route, c.package or c.url or f"{c.repo}/{c.path}"), c)
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda c: self._enrich(c, on, cfg, result), unique.values()))
        for c in unique.values():
            c.score = score(c.metrics, cfg["weights"], self.now())
        ready = [c for c in unique.values() if c.installable and not c.check() and c.relevance > 0]
        result.skipped += sum(1 for c in unique.values() if not c.installable)
        ready.sort(key=lambda c: (c.score.total * (0.5 + 0.5 * c.relevance), c.score.total), reverse=True)
        result.candidates = [self.store.add(c) for c in ready[:MAX_RESULTS]]
        return result

    # -- the MCP registry and npm ---------------------------------------------------------------------------------

    def _registry(self, words: List[str], result: SearchResult) -> List[Candidate]:
        found: Dict[str, Dict[str, Any]] = {}
        errors: List[src.SourceError] = []
        for word in sorted(words, key=len, reverse=True)[:3]:
            try:
                for item in self.registry.search(word):
                    found.setdefault(str(item["server"].get("name")), item)
            except src.SourceError as e:
                errors.append(e)
        if errors and not found:
            raise errors[0]
        ranked = sorted(found.values(), key=lambda i: -_relevance(words, str(i["server"].get("name")), str(i["server"].get("description"))))
        out: List[Candidate] = []
        for item in ranked[:REGISTRY_POOL]:
            c = self._from_registry(item, words)
            if c is None:
                result.skipped += 1
            else:
                out.append(c)
        return out

    def _from_registry(self, item: Dict[str, Any], words: List[str]) -> Optional[Candidate]:
        server = item["server"]
        name, desc = str(server.get("name") or ""), str(server.get("description") or "")
        meta = (item.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
        status = meta.get("status", "active")
        if status == "deleted" or not name:
            return None
        repo = server.get("repository") if isinstance(server.get("repository"), dict) else {}
        parsed = src.parse_github_url(repo.get("url"))
        m = Metrics(last_release=parse_time(meta.get("updatedAt") or meta.get("publishedAt")), first_release=parse_time(meta.get("publishedAt")),
                    deprecated=status == "deprecated", has_repo=bool(repo.get("url")), name=name.split("/", 1)[-1])
        m.publisher_verified = self._namespace_verified(name, parsed)
        base = dict(title=str(server.get("title") or ""), description=desc, repo=f"{parsed[0]}/{parsed[1]}" if parsed else "", metrics=m,
                    relevance=_relevance(words, name, desc, str(server.get("title") or "")))
        for pkg in server.get("packages") or []:
            if not isinstance(pkg, dict) or pkg.get("registryType") not in ("npm", "pypi"):
                continue
            if (pkg.get("transport") or {}).get("type", "stdio") != "stdio":
                continue
            args = self._fixed_args(pkg.get("packageArguments"))
            if args is None:
                continue
            env = [{"name": str(e.get("name")), "required": bool(e.get("isRequired")), "secret": bool(e.get("isSecret")),
                    "value": str(e["value"]) if e.get("value") is not None and not e.get("isSecret") else ""}
                   for e in pkg.get("environmentVariables") or [] if isinstance(e, dict) and e.get("name")]
            m.env_required = [e["name"] for e in env if e["required"] and not e["value"]]
            m.runs_program = True
            route = str(pkg["registryType"])
            ident = str(pkg.get("identifier") or "")
            version = str(pkg.get("version") or server.get("version") or "")
            return Candidate(kind="mcp", name=slugify(ident if route == "npm" else name), source="registry", route=route, package=ident, version=version,
                             transport="stdio", args=args, env=env, **base)
        for remote in server.get("remotes") or []:
            if not isinstance(remote, dict) or remote.get("type") not in ("streamable-http", "sse") or not remote.get("url"):
                continue
            if any(isinstance(h, dict) and h.get("isRequired") for h in remote.get("headers") or []):
                continue                                                      # a key in a header: Basalt signs in to remote servers with OAuth only
            m.remote = True
            return Candidate(kind="mcp", name=slugify(name), source="registry", route="remote", url=str(remote["url"]), version=str(server.get("version") or ""),
                             transport=str(remote["type"]), **base)
        return None

    @staticmethod
    def _namespace_verified(name: str, parsed: Optional[Tuple[str, str]]) -> Optional[bool]:
        """The registry lets a name be published only by the owner of the GitHub account or the domain it starts with."""
        ns = name.split("/", 1)[0].lower()
        if ns.startswith("io.github."):
            return bool(parsed) and parsed[0].lower() == ns[len("io.github."):]
        return True if "." in ns else None

    @staticmethod
    def _fixed_args(spec: Any) -> Optional[List[str]]:
        """The arguments the registry fixes for a server; None when one is required and has no value Basalt could give."""
        out: List[str] = []
        for a in spec or []:
            if not isinstance(a, dict):
                continue
            value = a.get("value") if a.get("value") is not None else (a.get("default") if a.get("isRequired") else None)
            if value is None or "{" in str(value):
                if a.get("isRequired"):
                    return None
                continue
            if a.get("type") == "named":
                out += [str(a.get("name") or ""), str(value)]
            else:
                out.append(str(value))
        return out

    def _npm_search(self, words: List[str]) -> List[Candidate]:
        out: List[Candidate] = []
        for r in self.npm.search(" ".join(words) + " mcp", size=NPM_POOL * 2):
            if not re.search(r"mcp|model.?context", f"{r['name']} {r['description']}", re.I):
                continue
            m = Metrics(downloads_week=r["weekly"], last_release=parse_time(r["date"]), license=r["license"], has_repo=bool(r["repository"]),
                        publisher_verified=True if r["trusted_publisher"] else None, runs_program=True, name=r["name"])
            parsed = src.parse_github_url(r["repository"])
            out.append(Candidate(kind="mcp", name=slugify(r["name"]), source="npm", route="npm", package=r["name"], version=r["version"], transport="stdio",
                                 description=r["description"], repo=f"{parsed[0]}/{parsed[1]}" if parsed else "", metrics=m,
                                 relevance=_relevance(words, r["name"], r["description"])))
            if len(out) >= NPM_POOL:
                break
        return out

    # -- more facts about each candidate --------------------------------------------------------------------------

    def _enrich(self, c: Candidate, on: set, cfg: Dict[str, Any], result: SearchResult) -> None:
        try:
            if c.route == "npm":
                if "npm" not in on:
                    c.installable = False
                    return
                self._enrich_npm(c)
            elif c.route == "pypi":
                if "pypi" not in on:
                    c.installable = False
                    return
                self._enrich_pypi(c)
        except src.SourceError as e:
            c.installable, c.problem = False, str(e)
            return
        if c.route != "skill" and "github" in on and c.repo:
            self._enrich_github(c)

    def _enrich_npm(self, c: Candidate) -> None:
        doc = self.npm.release(c.package, c.version)
        dist = doc.get("dist") if isinstance(doc.get("dist"), dict) else {}
        c.integrity = str(dist.get("integrity") or "")
        bins = doc.get("bin")
        c.bins = {c.package.rsplit("/", 1)[-1]: str(bins)} if isinstance(bins, str) else {str(k): str(v) for k, v in bins.items()} if isinstance(bins, dict) else {}
        if not c.integrity or not c.bins:
            c.installable, c.problem = False, "the release has no integrity hash or no program to start"
            return
        scripts = doc.get("scripts") if isinstance(doc.get("scripts"), dict) else {}
        c.metrics.install_scripts = any(k in scripts for k in ("preinstall", "install", "postinstall"))
        if not c.metrics.license and isinstance(doc.get("license"), str):
            c.metrics.license = doc["license"]
        if not c.repo:
            repo = doc.get("repository")
            parsed = src.parse_github_url(repo.get("url") if isinstance(repo, dict) else repo)
            if parsed:
                c.repo, c.metrics.has_repo = f"{parsed[0]}/{parsed[1]}", True
        if c.metrics.downloads_week is None:
            c.metrics.downloads_week = self.npm.weekly_downloads(c.package)
        hist = self.npm.history(c.package)
        if hist:
            c.metrics.first_release, c.metrics.last_release = parse_time(hist["first"]), parse_time(hist["last"])
            cut = self.now() - timedelta(days=365)
            c.metrics.releases_12m = sum(1 for d in hist["dates"] if (t := parse_time(d)) and t >= cut)

    def _enrich_pypi(self, c: Candidate) -> None:
        latest = self.pypi.release(c.package)
        exact = latest if str(latest["info"].get("version")) == c.version else self.pypi.release(c.package, c.version)
        files = [u for u in exact.get("urls") or [] if isinstance(u, dict)]
        c.digests = [str((u.get("digests") or {}).get("sha256")) for u in files if (u.get("digests") or {}).get("sha256")]
        if not c.digests or any(u.get("yanked") for u in files):
            c.installable, c.problem = False, "the release has no files or was withdrawn"
            return
        info = latest["info"]
        releases = latest.get("releases") if isinstance(latest.get("releases"), dict) else {}
        dates = sorted(t for files_ in releases.values() if isinstance(files_, list) and files_
                       for t in [min((str(f.get("upload_time_iso_8601") or "~") for f in files_ if isinstance(f, dict)), default="~")] if t != "~")
        if dates:
            c.metrics.first_release, c.metrics.last_release = parse_time(dates[0]), parse_time(dates[-1])
            cut = self.now() - timedelta(days=365)
            c.metrics.releases_12m = sum(1 for d in dates if (t := parse_time(d)) and t >= cut)
        lic = str(info.get("license") or "")
        if not lic or len(lic) > 40:
            lic = next((str(x).split("::")[-1].strip() for x in info.get("classifiers") or [] if str(x).startswith("License ::")), lic[:40])
        c.metrics.license = lic
        if not c.repo:
            urls = [*(info.get("project_urls") or {}).values(), info.get("home_page")]
            parsed = next((p for u in urls if (p := src.parse_github_url(u))), None)
            if parsed:
                c.repo, c.metrics.has_repo = f"{parsed[0]}/{parsed[1]}", True
        c.metrics.has_repo = bool(c.repo)

    def _enrich_github(self, c: Candidate) -> None:
        owner, _, name = c.repo.partition("/")
        with self._lock:
            if self._github_stopped:
                c.metrics.repo_error = self._github_stopped
                return
            if self._github_left <= 0:
                c.metrics.repo_error = "this search already asked about enough repositories"
                return
            self._github_left -= 1
        try:
            facts = self.github.repo(owner, name)
        except src.RateLimited as e:
            with self._lock:
                self._github_stopped = str(e)
            c.metrics.repo_error = str(e)
            return
        except src.SourceError as e:
            c.metrics.repo_error = str(e) if e.code != "not_found" else ""
            if e.code == "not_found":
                c.metrics.has_repo = False
            return
        self._apply_repo(c.metrics, facts)

    @staticmethod
    def _apply_repo(m: Metrics, facts: Dict[str, Any]) -> None:
        m.repo_checked = True
        m.stars, m.forks, m.open_issues = facts["stars"], facts["forks"], facts["open_issues"]
        m.archived = m.archived or facts["archived"] or facts["disabled"]
        if not m.license:
            m.license = facts["license"]
        if m.last_release is None:
            m.last_release = parse_time(facts["pushed_at"])
        if m.first_release is None:
            m.first_release = parse_time(facts["created_at"])

    # -- skills ---------------------------------------------------------------------------------------------------

    def _skills(self, words: List[str], cfg: Dict[str, Any]) -> List[Candidate]:
        repos: List[Tuple[str, str, str, str, Optional[Dict[str, Any]]]] = []      # owner, name, folder prefix, source, repository facts
        errors: List[src.SourceError] = []
        for market in cfg["marketplaces"][:5]:
            owner, _, name = market.partition("/")
            try:
                repos += self._marketplace(owner, name, words)
            except src.SourceError as e:
                errors.append(e)
        for topic in ("agent-skills", "claude-skills"):
            try:
                for facts in self.github.search_repos(f"{' '.join(words[:2])} topic:{topic}", per_page=SKILL_REPOS):
                    owner, _, name = facts["full_name"].partition("/")
                    repos.append((owner, name, "", "github", facts))
            except src.RateLimited as e:
                raise e
            except src.SourceError as e:
                errors.append(e)
        seen, out = set(), []
        for owner, name, prefix, source, facts in repos:
            key = (owner.lower(), name.lower(), prefix)
            if key in seen or len(seen) >= SKILL_REPOS + 2:
                continue
            seen.add(key)
            try:
                out += self._skills_in(owner, name, prefix, source, facts, words)
            except src.RateLimited:
                raise
            except src.SourceError as e:
                errors.append(e)
        if errors and not out and not repos:
            raise errors[0]
        return out

    def _marketplace(self, owner: str, name: str, words: List[str]) -> List[Tuple[str, str, str, str, Optional[Dict[str, Any]]]]:
        raw = self.github.raw_file(owner, name, "HEAD", ".claude-plugin/marketplace.json", 400_000)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise src.SourceError(f"the marketplace {owner}/{name} has no readable marketplace.json", "bad_answer") from None
        out = []
        for p in (data.get("plugins") if isinstance(data, dict) else None) or []:
            if not isinstance(p, dict) or _relevance(words, str(p.get("name")), str(p.get("description")), " ".join(map(str, p.get("tags") or []))) <= 0:
                continue
            source = p.get("source")
            if isinstance(source, str) and source.startswith("./"):
                out.append((owner, name, source[2:].strip("/"), "marketplace", None))
            elif isinstance(source, dict) and (parsed := src.parse_github_url(source.get("url") or (f"github:{source['repo']}" if source.get("repo") else ""))):
                out.append((parsed[0], parsed[1], str(source.get("path") or "").strip("/"), "marketplace", None))
        return out

    def _skills_in(self, owner: str, name: str, prefix: str, source: str, facts: Optional[Dict[str, Any]], words: List[str]) -> List[Candidate]:
        facts = facts or self.github.repo(owner, name)
        sha = self.github.head_commit(owner, name, facts["default_branch"])
        tree = self.github.tree(owner, name, sha)
        blobs = [e for e in tree["tree"] if isinstance(e, dict) and e.get("type") == "blob" and isinstance(e.get("path"), str)]
        folders = sorted({e["path"].rsplit("/", 1)[0] for e in blobs if e["path"].rsplit("/", 1)[-1] == "SKILL.md" and "/" in e["path"]
                          and (not prefix or e["path"].startswith(prefix + "/"))}, key=len)
        by_name = sorted(folders, key=lambda f: -_relevance(words, f))
        look = by_name[:5] if any(_relevance(words, f) > 0 for f in by_name) else folders[:4]
        out: List[Candidate] = []
        for folder in look:
            try:
                text = self.github.raw_file(owner, name, sha, folder + "/SKILL.md", SKILL_FILE_BYTES).decode("utf-8", "replace")
            except src.SourceError:
                continue
            meta, body = parse_frontmatter(text)
            title = meta.get("name") or folder.rsplit("/", 1)[-1]
            desc = " ".join((meta.get("description") or "").split())
            rel = _relevance(words, title, desc, folder)
            if rel <= 0:
                continue
            m = Metrics(has_repo=True, name=title)
            self._apply_repo(m, facts)
            m.skill = any(not e["path"].lower().endswith((".md", ".txt")) for e in blobs if e["path"].startswith(folder + "/"))
            out.append(Candidate(kind="skill", name=slugify(title), source=source, route="skill", title=title, description=desc, version=sha[:7],
                                 repo=f"{owner}/{name}", path=folder, commit=sha, metrics=m, relevance=rel))
        out.sort(key=lambda c: -c.relevance)
        return out[:SKILLS_PER_REPO]
