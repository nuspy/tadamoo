"""The providers of the Connectors tab: where connectors that are not installed yet are listed from.

A provider is a row the person can add, switch off, remove or reset: the official MCP registry (the default), a folder of skills on this PC, or a plain list of servers at an address. The
"available to install" band is made from the enabled providers at the moment it is asked, so it follows the list the person set. Nothing here installs or runs anything; it only reads
names and descriptions. What a provider says is data, never instructions."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.connectors.model import Item

KINDS = ("mcp_registry", "folder", "github")
TRUST = ("official", "known", "unknown")
DEFAULTS: List[Dict[str, Any]] = [{"id": "mcp-registry", "kind": "mcp_registry", "ref": "https://registry.modelcontextprotocol.io", "trust": "official", "enabled": True}]
MAX_PROVIDERS = 50
MAX_ITEMS = 100
Get = Callable[[str], bytes]
GITHUB_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}(?:@[A-Za-z0-9._/-]{1,100})?$")
GITHUB_SKILLS_MAX = 30


class ProviderError(ValueError):
    pass


def _id(kind: str, ref: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{kind}-{ref}".lower()).strip("-")[:60]


class Providers:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def list(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list) and all(isinstance(r, dict) and r.get("id") for r in data):
                return data
        except (OSError, ValueError):
            pass
        return [dict(d) for d in DEFAULTS]

    def _save(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        return rows

    def add(self, kind: str, ref: str, trust: str = "unknown") -> List[Dict[str, Any]]:
        ref = str(ref or "").strip()
        if kind not in KINDS:
            raise ProviderError("kind: one of " + ", ".join(KINDS))
        if trust not in TRUST:
            raise ProviderError("trust: one of " + ", ".join(TRUST))
        if not ref or len(ref) > 500 or "\x00" in ref:
            raise ProviderError("ref: an address or a folder")
        if kind == "mcp_registry" and not re.match(r"^https?://[^\s/]+", ref):
            raise ProviderError("a registry is an http or https address")
        if kind == "folder" and not Path(ref).is_dir():
            raise ProviderError("there is no such folder")
        if kind == "github" and not GITHUB_REF.match(ref):
            raise ProviderError("a GitHub repository is written owner/name, with @branch or @tag to pin a version")
        rows = self.list()
        pid = _id(kind, ref)
        if any(r["id"] == pid for r in rows):
            raise ProviderError("this provider is already in the list")
        if len(rows) >= MAX_PROVIDERS:
            raise ProviderError(f"at most {MAX_PROVIDERS} providers")
        return self._save(rows + [{"id": pid, "kind": kind, "ref": ref, "trust": trust, "enabled": True}])

    def set_enabled(self, pid: str, enabled: bool) -> List[Dict[str, Any]]:
        rows = self.list()
        row = next((r for r in rows if r["id"] == pid), None)
        if row is None:
            raise ProviderError("there is no such provider")
        row["enabled"] = bool(enabled)
        return self._save(rows)

    def remove(self, pid: str) -> List[Dict[str, Any]]:
        rows = self.list()
        if not any(r["id"] == pid for r in rows):
            raise ProviderError("there is no such provider")
        return self._save([r for r in rows if r["id"] != pid])

    def reset(self) -> List[Dict[str, Any]]:
        return self._save([dict(d) for d in DEFAULTS])


def _clean(text: Any, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def registry_items(base: str, get: Get, query: str = "") -> List[Item]:
    """The servers an MCP registry lists (its `/v0/servers` answer), as items that are not installed. An answer that is not the expected JSON gives no item."""
    url = base.rstrip("/") + "/v0/servers?limit=%d" % MAX_ITEMS + (f"&search={query}" if query else "")
    try:
        data = json.loads(get(url).decode("utf-8", "replace"))
    except (ValueError, OSError):
        return []
    rows = data.get("servers") if isinstance(data, dict) else None
    out: List[Item] = []
    seen = set()
    for row in rows if isinstance(rows, list) else []:
        s = row.get("server", row) if isinstance(row, dict) else {}
        name = _clean(s.get("name"), 120)
        if not name or name in seen:                                  # a registry lists each version of a server: the first is the one kept
            continue
        seen.add(name)
        out.append(Item(id="registry:" + name, kind="mcp_server", name=name.rsplit("/", 1)[-1], description=_clean(s.get("description"), 400), source=_clean(base, 120),
                        scope="external", installed=False, version=_clean(s.get("version"), 40), keywords=tuple(re.findall(r"[a-z0-9]+", name.lower())[:6])))
    return out[:MAX_ITEMS]


def folder_items(path: str) -> List[Item]:
    """The skills in a folder (each a subfolder with a SKILL.md), as items that are not installed."""
    from tadamoo.skills import parse_frontmatter
    out: List[Item] = []
    try:
        for d in sorted(p for p in Path(path).iterdir() if p.is_dir() and (p / "SKILL.md").is_file())[:MAX_ITEMS]:
            meta = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
            head = meta[0] if isinstance(meta, tuple) else meta
            out.append(Item(id="folder:" + d.name, kind="skill", name=_clean((head or {}).get("name") or d.name, 80), description=_clean((head or {}).get("description"), 400),
                            source=str(path), scope="external", installed=False))
    except OSError:
        return []
    return out


GITHUB_MCP_MAX = 10


def _mcp_candidate_files(tree_entries: List[Dict[str, Any]]) -> List[str]:
    """The package.json and pyproject.toml paths of a repository's tree, nearest to the root first: where a program to start might be declared."""
    names = ("package.json", "pyproject.toml")
    return sorted((str(t["path"]) for t in tree_entries if isinstance(t, dict) and str(t.get("path", "")).rsplit("/", 1)[-1] in names),
                  key=lambda p: p.count("/"))


def github_items(ref: str, get: Get) -> List[Item]:
    """The skills (each a folder with a SKILL.md) and the MCP servers (a package.json with "bin", or a pyproject.toml with [project.scripts]) in a GitHub repository, read from
    the repository's file list at the pinned branch or tag (the default branch when none is pinned). Only names and descriptions are read; nothing is cloned or run. An answer
    that is not the expected JSON gives no item."""
    repo, _, pin = ref.partition("@")
    try:
        if not pin:
            pin = json.loads(get(f"https://api.github.com/repos/{repo}").decode("utf-8", "replace")).get("default_branch") or "HEAD"
        tree = json.loads(get(f"https://api.github.com/repos/{repo}/git/trees/{pin}?recursive=1").decode("utf-8", "replace"))
    except (ValueError, OSError, AttributeError):
        return []
    entries = (tree.get("tree") if isinstance(tree, dict) else None) or []
    paths = sorted(t["path"] for t in entries if isinstance(t, dict) and str(t.get("path", "")).endswith("/SKILL.md"))
    out: List[Item] = []
    for p in paths[:GITHUB_SKILLS_MAX]:
        folder = p[: -len("/SKILL.md")]
        name, desc = folder.rsplit("/", 1)[-1], ""
        try:
            from tadamoo.skills import parse_frontmatter
            meta = parse_frontmatter(get(f"https://raw.githubusercontent.com/{repo}/{pin}/{p}").decode("utf-8", "replace")[:4000])
            head = meta[0] if isinstance(meta, tuple) else meta
            name, desc = _clean((head or {}).get("name") or name, 80), _clean((head or {}).get("description"), 400)
        except Exception:                                                  # a file that does not come keeps the folder's name
            pass
        out.append(Item(id=f"github:{repo}:{folder}", kind="skill", name=name, description=desc, source=f"github.com/{repo}@{pin}", scope="external", installed=False,
                        keywords=tuple(re.findall(r"[a-z0-9]+", folder.lower())[:6])))
    for p in _mcp_candidate_files(entries)[:GITHUB_MCP_MAX]:
        folder = p.rsplit("/", 1)[0] if "/" in p else ""
        try:
            text = get(f"https://raw.githubusercontent.com/{repo}/{pin}/{p}").decode("utf-8", "replace")[:40_000]
        except Exception:
            continue
        if p.endswith("package.json"):
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not (isinstance(data, dict) and data.get("bin")):
                continue
            name, desc = _clean(data.get("name") or folder.rsplit("/", 1)[-1] or repo.rsplit("/", 1)[-1], 80), _clean(data.get("description"), 400)
        else:
            if not re.search(r"\[project\.scripts\]", text):
                continue
            name = _clean((re.search(r'(?m)^name\s*=\s*"([^"]+)"', text) or [None, folder.rsplit("/", 1)[-1] or repo.rsplit("/", 1)[-1]])[1], 80)
            desc = _clean((re.search(r'(?m)^description\s*=\s*"([^"]+)"', text) or [None, ""])[1], 400)
        out.append(Item(id=f"github_mcp:{repo}:{folder}", kind="mcp_server", name=name, description=desc, source=f"github.com/{repo}@{pin}", scope="external", installed=False,
                        keywords=tuple(re.findall(r"[a-z0-9]+", (folder or name).lower())[:6])))
    return out


def available(providers: List[Dict[str, Any]], get: Get, query: str = "") -> List[Item]:
    """The items of every enabled provider. One that fails gives nothing and does not stop the others."""
    out: List[Item] = []
    seen = set()
    for row in providers:
        if not row.get("enabled"):
            continue
        try:
            items = (registry_items(row["ref"], get, query) if row["kind"] == "mcp_registry" else folder_items(row["ref"]) if row["kind"] == "folder"
                     else github_items(row["ref"], get) if row["kind"] == "github" else [])
        except Exception:
            items = []
        for it in items:
            if it.id not in seen:
                seen.add(it.id)
                out.append(it)
    return out
