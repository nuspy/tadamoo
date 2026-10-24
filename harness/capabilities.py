"""What the agent can use besides its own tools: skills, plugins and MCP servers found on this PC, as a tree it searches on demand.

Listing every skill in the prompt costs thousands of tokens on every call, so the prompt carries only the top of the tree
(a few lines: the roots and how many entries each has) and two tools: `find_capability` searches the index (local FTS5, BM25) and
returns at most five entries, `load_capability` reads an approved skill, `call_capability` calls an approved MCP server.

Discovery only reads. It reads names, descriptions, commands and the arguments of a command with anything that looks like a key
blanked out; it never reads a server's environment variables. Nothing found is active: a skill is approved against the hash of
the text the person read, a server against its command. A folder whose name ends in `.disabled` is off.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.skills import ApprovalStore, parse_frontmatter

ROOTS: Dict[str, Dict[str, str]] = {
    "vision": {"en": "Vision", "it": "Visione"},
    "documents": {"en": "Documents", "it": "Documenti"},
    "data": {"en": "Data", "it": "Dati"},
    "web": {"en": "Web", "it": "Web"},
    "code": {"en": "Code", "it": "Codice"},
    "audio_video": {"en": "Audio and video", "it": "Audio e video"},
    "knowledge": {"en": "Knowledge", "it": "Conoscenza"},
    "communication": {"en": "Communication", "it": "Comunicazione"},
    "other": {"en": "Other", "it": "Altro"},
}
VISION_ANALYSIS, VISION_GENERATION = "vision/analysis", "vision/generation"
RULES: List[Tuple[str, "re.Pattern[str]"]] = [(node, re.compile(rx, re.I)) for node, rx in (
    ("vision", r"\b(ocr|screenshots?|images?|vision|visual\w*|diagrams?|charts?|photos?|pictures?|figures?|png|jpe?g)\b"),
    ("documents", r"\b(pdf|docx|word|xlsx|spreadsheets?|powerpoint|pptx|markdown|documents?|slides?|presentations?)\b"),
    ("data", r"\b(csv|json|sql|database|analytics|dashboards?|pandas|datasets?|tabular)\b"),
    ("web", r"\b(web|browser|search|https?|urls?|scrap\w*|crawl\w*|websites?|chrome)\b"),
    ("code", r"\b(code|git|github|commits?|tests?|debug\w*|refactor\w*|lint\w*|compile\w*|python|javascript|typescript|repo|pull|review)\b"),
    ("audio_video", r"\b(audio|video|transcri\w*|speech|podcasts?|mp3|mp4|ffmpeg|voice)\b"),
    ("knowledge", r"\b(wiki|memory|knowledge|rag|notes|obsidian)\b"),
    ("communication", r"\b(email|mail|phone|calls?|slack|calendar|sms|whatsapp|messages?)\b"),
)]
GENERATION = re.compile(r"\b(generat\w*|draw\w*|render\w*|illustrat\w*|creat\w*|design\w*|retouch\w*|resiz\w*|text-to-image|edit\w*)\b", re.I)
ANALYSIS = re.compile(r"\b(ocr|analy\w*|read\w*|extract\w*|describe|inspect\w*|detect\w*|recogni\w*|critique|review\w*|understand\w*)\b", re.I)
SECRET_FLAG = re.compile(r"(key|token|secret|passw|auth|bearer|credential)", re.I)
LONG_TOKEN = re.compile(r"^[A-Za-z0-9_\-./+=]{28,}$")
MAX_RESULTS = 5
STOP = frozenset("a an the of to in on for from with and or is are be it this that my me i do does you how what which into as at by use using want need please can could".split())
DESC_CHARS = 300
HIT_CHARS = 160


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_args(args: List[Any]) -> List[str]:
    """The arguments of a command as shown: the value after a flag that names a key, and anything that looks like one, is blanked."""
    out: List[str] = []
    blank_next = False
    for raw in args:
        a = str(raw)
        if blank_next:
            out.append("***")
            blank_next = False
        elif "=" in a and SECRET_FLAG.search(a.split("=", 1)[0]):
            out.append(a.split("=", 1)[0] + "=***")
        elif a.startswith("-") and SECRET_FLAG.search(a):
            out.append(a)
            blank_next = True
        elif LONG_TOKEN.match(a) and "/" not in a and "\\" not in a:
            out.append("***")
        else:
            out.append(a)
    return out


@dataclass
class Found:
    """What discovery learned about one entry, before the state of approval is added."""
    id: str
    name: str
    kind: str                  # builtin, skill, mcp
    origin: str                # user, project, plugin:<name>, claude_desktop, builtin ...
    description: str
    tags: List[str]
    path: str = ""
    sha: str = ""
    command: str = ""          # for an MCP server: the command as shown, keys blanked
    raw_command: Optional[List[str]] = None      # what would be run; never written to disk


def _disabled(path: Path) -> bool:
    return any(part.lower().endswith(".disabled") for part in path.parts)


def _split_tags(value: str) -> List[str]:
    return [t.strip().lower() for t in re.split(r"[,\[\]\s]+", value or "") if t.strip()][:12]


def _read_skill(path: Path, origin: str) -> Optional[Found]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    name = (meta.get("name") or path.parent.name).strip()
    first = next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), "")
    desc = " ".join((meta.get("description") or first).split())[:DESC_CHARS]
    tags = _split_tags(meta.get("tags", "")) + _split_tags(meta.get("category", ""))
    return Found(id=f"skill:{name}", name=name, kind="skill", origin=origin, description=desc, tags=tags, path=str(path), sha=_sha(text))


def _mcp_entries(config: Any, origin: str, source: str) -> List[Found]:
    """The servers in a config file: name, command and arguments; the `env` of a server is never looked at."""
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    if servers is None and isinstance(config, dict) and config and all(isinstance(v, dict) and ("command" in v or "url" in v) for v in config.values()):
        servers = config                                       # a plugin's .mcp.json may list the servers at the top
    out: List[Found] = []
    for name, spec in (servers or {}).items():
        if not isinstance(spec, dict) or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_\-.]{1,64}", name):
            continue
        cmd = spec.get("command")
        args = [str(a) for a in spec.get("args", [])] if isinstance(spec.get("args"), list) else []
        url = str(spec.get("url") or "")
        if isinstance(cmd, str) and cmd:
            raw = [cmd, *args]
            shown = " ".join([cmd, *redact_args(args)])
        elif url:
            raw, shown = None, url.split("?", 1)[0]
        else:
            continue
        out.append(Found(id=f"mcp:{name}", name=name, kind="mcp", origin=origin, description=f"MCP server: {shown}"[:DESC_CHARS], tags=["mcp"],
                         path=source, sha=_sha(json.dumps([cmd, args, url.split("?", 1)[0]])), command=shown, raw_command=raw))
    return out


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def discover(workspace: Path, home: Optional[Path] = None, appdata: Optional[Path] = None) -> List[Found]:
    """Everything found, in a fixed order; the first of a name wins (the person's own, then the project's, then plugins')."""
    home = home or Path.home()
    appdata = appdata if appdata is not None else (Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else None)
    found: List[Found] = []

    def skills_in(base: Path, origin: str) -> None:
        if base.is_dir() and not _disabled(base):
            for f in sorted(base.rglob("SKILL.md")):
                if not _disabled(f) and (entry := _read_skill(f, origin)):
                    found.append(entry)

    skills_in(home / ".claude" / "skills", "user")
    skills_in(workspace / ".claude" / "skills", "project")
    skills_in(workspace / ".agents" / "skills", "project")
    plugins = home / ".claude" / "plugins"
    if plugins.is_dir() and not _disabled(plugins):
        for f in sorted(plugins.rglob("SKILL.md")):
            if _disabled(f):
                continue
            parts = f.relative_to(plugins).parts
            plugin = parts[parts.index("cache") + 2] if "cache" in parts and len(parts) > parts.index("cache") + 2 else parts[0]
            if entry := _read_skill(f, f"plugin:{plugin}"):
                found.append(entry)
        for f in sorted(plugins.rglob(".mcp.json")):
            if not _disabled(f):
                parts = f.relative_to(plugins).parts
                plugin = parts[parts.index("cache") + 2] if "cache" in parts and len(parts) > parts.index("cache") + 2 else parts[0]
                found += _mcp_entries(_load_json(f), f"plugin:{plugin}", str(f))
    for cfg, origin in ((home / ".claude.json", "user"), (workspace / ".mcp.json", "project"),
                        (appdata / "Claude" / "claude_desktop_config.json" if appdata else None, "claude_desktop")):
        if cfg is not None and cfg.is_file():
            data = _load_json(cfg)
            found += _mcp_entries(data, origin, str(cfg))
            if cfg.name == ".claude.json" and isinstance(data, dict):
                project = (data.get("projects") or {}).get(str(workspace)) if isinstance(data.get("projects"), dict) else None
                found += _mcp_entries(project, "project", str(cfg))
    unique: Dict[str, Found] = {}
    for e in found:
        unique.setdefault(e.id, e)
    return list(unique.values())


def classify(entry: Dict[str, Any], overrides: Optional[Dict[str, str]] = None) -> str:
    """The node of the tree an entry belongs to: the person's correction, else a declared tag, else the words of its description."""
    if overrides and overrides.get(entry["id"]) in node_names():
        return overrides[entry["id"]]
    for tag in entry.get("tags", []):
        if tag in ROOTS:
            return tag if tag != "vision" else (VISION_GENERATION if GENERATION.search(entry["description"]) and not ANALYSIS.search(entry["description"]) else VISION_ANALYSIS)
        if tag in node_names():
            return tag
    text = f"{entry['name']} {entry['description']} {' '.join(entry.get('tags', []))}".replace("_", " ").replace("-", " ")
    scores = [(len(set(m.group(0).lower() for m in rx.finditer(text))), -i, node) for i, (node, rx) in enumerate(RULES)]
    best = max(scores)
    if best[0] == 0:
        return "other"
    node = best[2]
    if node == "vision":
        return VISION_GENERATION if GENERATION.search(text) and not ANALYSIS.search(text) else VISION_ANALYSIS
    return node


def node_names() -> List[str]:
    return [*ROOTS, VISION_ANALYSIS, VISION_GENERATION]


def root_of(node: str) -> str:
    return node.split("/", 1)[0]


class CapabilityIndex:
    """The entries found, their tree, their approvals and the person's own settings, kept in one folder."""

    def __init__(self, directory: Path, workspace: Path, home: Optional[Path] = None, appdata: Optional[Path] = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.workspace, self.home, self.appdata = Path(workspace), home, appdata
        self.approvals = ApprovalStore(self.dir / "approved.json")
        self._lock = threading.RLock()
        self.entries: List[Dict[str, Any]] = []
        self._raw: Dict[str, List[str]] = {}
        self.refresh()

    # -- reading and writing the person's own choices -----------------------------------------------------

    def _file(self, name: str) -> Path:
        return self.dir / name

    def settings(self) -> Dict[str, Any]:
        data = _load_json(self._file("settings.json"))
        data = data if isinstance(data, dict) else {}
        return {"visual": data.get("visual") if isinstance(data.get("visual"), str) else "builtin",
                "off": [str(x) for x in data.get("off", []) if isinstance(x, str)]}

    def _write(self, name: str, data: Any) -> None:
        tmp = self._file(name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self._file(name))

    def overrides(self) -> Dict[str, str]:
        data = _load_json(self._file("overrides.json"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def save_settings(self, **changes: Any) -> Dict[str, Any]:
        current = self.settings()
        current.update(changes)
        self._write("settings.json", current)
        self.refresh(rescan=False)
        return self.settings()

    # -- the index ------------------------------------------------------------------------------------------

    def _builtins(self) -> List[Dict[str, Any]]:
        return [
            {"id": "builtin:vision", "name": "The model's vision", "kind": "builtin", "origin": "builtin", "path": "", "sha": "", "command": "",
             "description": "Reads an image with the vision projector of the loaded model, with a crop and zoom for fine detail.",
             "tags": ["vision"], "forced_node": VISION_ANALYSIS},
            {"id": "builtin:phone_media", "name": "Audio and video with callAPIcall", "kind": "builtin", "origin": "builtin", "path": "", "sha": "", "command": "",
             "description": "Transcribes an audio or video file with the local callAPIcall service.", "tags": ["audio_video"], "forced_node": "audio_video"},
        ]

    def refresh(self, rescan: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            if rescan or not self.entries:
                found = discover(self.workspace, self.home, self.appdata)
                self._raw = {f.id: f.raw_command for f in found if f.raw_command}
                base = [{"id": f.id, "name": f.name, "kind": f.kind, "origin": f.origin, "path": f.path, "sha": f.sha, "command": f.command,
                         "description": f.description, "tags": f.tags} for f in found]
                self._base = self._builtins() + base
            overrides, off = self.overrides(), set(self.settings()["off"])
            entries = []
            for b in self._base:
                e = {k: v for k, v in b.items() if k != "forced_node"}
                e["node"] = b.get("forced_node") or classify(e, overrides)
                if e["kind"] == "builtin":
                    e["state"] = "approved"
                elif e["id"] in off:
                    e["state"] = "off"
                else:
                    e["state"] = "approved" if self.approvals._load().get(e["id"]) == e["sha"] else "pending"
                entries.append(e)
            self.entries = entries
            self._write("index.json", {"entries": entries})
            (self.dir / "skills.md").write_text(self.skills_md(), encoding="utf-8")
            return entries

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        return next((e for e in self.entries if e["id"] == entry_id), None)

    def approve(self, entry_id: str, sha: str) -> Dict[str, Any]:
        """Approves what the person read: `sha` is the hash they were shown, so a file that changed since is not approved by mistake."""
        e = self.get(entry_id)
        if e is None or e["kind"] == "builtin":
            raise KeyError(entry_id)
        if e["sha"] != sha:
            raise ValueError("It changed since you looked at it. Look again before approving.")
        self.approvals.approve(SimpleNamespace(name=e["id"], sha256=e["sha"]))
        self.refresh(rescan=False)
        return self.get(entry_id)

    def revoke(self, entry_id: str) -> None:
        self.approvals.revoke(entry_id)
        self.refresh(rescan=False)

    def move(self, entry_id: str, node: str) -> Dict[str, Any]:
        if self.get(entry_id) is None:
            raise KeyError(entry_id)
        if node not in node_names():
            raise ValueError(f"node must be one of: {', '.join(node_names())}")
        over = self.overrides()
        over[entry_id] = node
        self._write("overrides.json", over)
        self.refresh(rescan=False)
        return self.get(entry_id)

    def review(self, entry_id: str) -> Dict[str, Any]:
        """What the person must read before approving: the text of a skill, the command of a server, and the hash that approval will bind to."""
        e = self.get(entry_id)
        if e is None or e["kind"] == "builtin":
            raise KeyError(entry_id)
        body = ""
        if e["kind"] == "skill":
            try:
                text = Path(e["path"]).read_text(encoding="utf-8", errors="replace")
                body = parse_frontmatter(text)[1].strip()[:20_000]
            except OSError:
                body = ""
        return {"entry": e, "text": body or e["command"], "sha": e["sha"]}

    # -- search -------------------------------------------------------------------------------------------------

    @staticmethod
    def _terms(query: str) -> List[str]:
        """The words of a query that carry meaning, as FTS5 prefix terms: all of them together first, then any of them."""
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1 and w not in STOP][:10]
        if not words:
            return []
        quoted = [f'"{w}"*' for w in words]
        return [" AND ".join(quoted), " OR ".join(quoted)] if len(quoted) > 1 else quoted

    def search(self, query: str, node: str = "", kind: str = "", limit: int = MAX_RESULTS) -> List[Dict[str, Any]]:
        queries = self._terms(query)
        usable = [e for e in self.entries if e["state"] != "off"]
        if not queries:
            return []
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(name, tags, node, description, id UNINDEXED, tokenize='unicode61 remove_diacritics 2')")
            conn.executemany("INSERT INTO t VALUES (?,?,?,?,?)", [(e["name"].replace("-", " ").replace("_", " "), " ".join(e["tags"]), e["node"].replace("/", " "),
                                                                     e["description"], e["id"]) for e in usable])
            rows, seen = [], set()
            for q in queries:
                for (eid,) in conn.execute("SELECT id FROM t WHERE t MATCH ? ORDER BY bm25(t, 10.0, 5.0, 3.0, 1.0), id LIMIT 60", (q,)).fetchall():
                    if eid not in seen:
                        seen.add(eid)
                        rows.append((eid,))
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()
        by_id = {e["id"]: e for e in usable}
        out = []
        for (eid,) in rows:
            e = by_id[eid]
            if node and not (e["node"] == node or e["node"].startswith(node + "/")):
                continue
            if kind and e["kind"] != kind:
                continue
            out.append({"id": e["id"], "name": e["name"], "kind": e["kind"], "node": e["node"], "state": e["state"], "description": e["description"][:HIT_CHARS]})
            if len(out) >= max(1, min(limit, MAX_RESULTS)):
                break
        return out

    # -- views ----------------------------------------------------------------------------------------------------

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {r: 0 for r in ROOTS}
        for e in self.entries:
            if e["state"] != "off":
                out[root_of(e["node"])] += 1
        return out

    def top_of_tree(self) -> str:
        """The few lines that sit in the prompt: the roots with their counts, in a fixed order."""
        c = self.counts()
        return ("Capabilities you can look up with find_capability (skills, plugins, MCP servers): " +
                ", ".join(f"{ROOTS[r]['en'].lower()} {c[r]}" for r in ROOTS if c[r]) + ".")

    def skills_md(self) -> str:
        """The index as a page to read and rearrange, one section per root."""
        lines = ["# Capabilities", "", "Generated from `index.json`; edit nothing here, move an entry in Settings.", ""]
        for root in ROOTS:
            mine = [e for e in self.entries if root_of(e["node"]) == root]
            if not mine:
                continue
            lines.append(f"## {ROOTS[root]['en']}" + (" (visual analysis)" if root == "vision" else ""))
            for e in sorted(mine, key=lambda x: (x["node"], x["name"].lower())):
                lines.append(f"- **{e['name']}** — {e['description'][:140]}  \n  `{e['id']}` · {e['kind']} · {e['origin']} · {e['node']} · {e['state']}")
            lines.append("")
        return "\n".join(lines)

    def visual_options(self) -> List[Dict[str, str]]:
        """The choices for visual analysis: the model's own, then what was found in the vision/analysis branch, then none."""
        out = [{"id": "builtin", "label": "The model's own vision", "state": "approved"}]
        out += [{"id": e["id"], "label": e["name"], "state": e["state"], "origin": e["origin"], "kind": e["kind"]}
                for e in self.entries if e["node"] == VISION_ANALYSIS and e["kind"] != "builtin" and e["state"] != "off"]
        out.append({"id": "none", "label": "None", "state": "approved"})
        return out

    # -- using an entry ---------------------------------------------------------------------------------------------

    def body(self, entry_id: str) -> str:
        """The instructions of an approved skill, read now and checked against the hash that was approved."""
        e = self.get(entry_id)
        if e is None:
            raise KeyError(f"No capability '{entry_id}'. Search with find_capability.")
        if e["kind"] != "skill":
            raise ValueError(f"'{entry_id}' is not a skill: use call_capability for a server.")
        if e["state"] != "approved":
            raise PermissionError(f"'{entry_id}' is {e['state']}: the person approves it in Settings, under Capabilities.")
        text = Path(e["path"]).read_text(encoding="utf-8", errors="replace")
        if _sha(text) != e["sha"]:
            raise PermissionError(f"'{entry_id}' changed since it was approved: it needs a new look in Settings.")
        return parse_frontmatter(text)[1].strip()

    def command(self, entry_id: str) -> List[str]:
        e = self.get(entry_id)
        if e is None or e["kind"] != "mcp":
            raise KeyError(f"No MCP server '{entry_id}'.")
        if e["state"] != "approved":
            raise PermissionError(f"'{entry_id}' is {e['state']}: the person approves it in Settings, under Capabilities.")
        raw = self._raw.get(entry_id)
        if not raw:
            raise ValueError(f"'{entry_id}' is a remote server: Basalt does not start those from here.")
        return raw


def register_capability_tools(registry: Any, index: CapabilityIndex, hub_factory: Optional[Callable[[str, List[str]], Any]] = None) -> None:
    """`hub_factory(id, command)` starts a server and returns an object with `tools` (the server's tool definitions) and `call(tool, arguments)`.

    `find_capability`, `load_capability` and `call_capability`: three fixed definitions however many entries there are."""
    from harness.tools.registry import EffectClass
    hubs: Dict[str, Any] = {}

    def find_capability(query: str, node: str = "", kind: str = "") -> str:
        hits = index.search(query, node, kind)
        if not hits:
            return "Nothing found. Try other words, or a node: " + ", ".join(node_names()) + "."
        return "\n".join(f"{h['id']} [{h['kind']}, {h['node']}, {h['state']}] {h['name']}: {h['description']}" for h in hits)

    def load_capability(id: str) -> str:
        try:
            return index.body(id)
        except (KeyError, ValueError, PermissionError) as exc:
            return str(exc.args[0] if exc.args else exc)

    def call_capability(id: str, tool: str = "", arguments: Optional[Dict[str, Any]] = None) -> str:
        try:
            cmd = index.command(id)
        except (KeyError, ValueError, PermissionError) as exc:
            return str(exc.args[0] if exc.args else exc)
        if hub_factory is None:
            return "Calling servers is not available in this conversation."
        try:
            hub = hubs.get(id) or hubs.setdefault(id, hub_factory(id, cmd))
            if not tool:
                return "\n".join(f"{t['name']}: {str(t.get('description') or '')[:200]}" for t in hub.tools) or "The server offers no tools."
            return hub.call(tool, arguments or {})
        except Exception as exc:
            return f"The server could not be used: {type(exc).__name__}: {exc}"

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}
    registry.register("find_capability", "Looks up skills, plugins and MCP servers by what you need (five results at most). " + index.top_of_tree(),
                      obj({"query": {"type": "string", "description": "What you want to do"}, "node": {"type": "string", "description": "optional: a node such as vision/analysis, documents, web"},
                           "kind": {"type": "string", "enum": ["skill", "mcp", "builtin"]}}, ["query"]), EffectClass.PURE, find_capability, untrusted=True)
    registry.register("load_capability", "Reads the instructions of an approved skill found with find_capability.",
                      obj({"id": {"type": "string"}}, ["id"]), EffectClass.PURE, load_capability, untrusted=True)
    registry.register("call_capability", "Calls a tool of an approved MCP server found with find_capability. Leave `tool` empty to list the server's tools.",
                      obj({"id": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}}, ["id"]), EffectClass.IRREVERSIBLE, call_capability, untrusted=True)
