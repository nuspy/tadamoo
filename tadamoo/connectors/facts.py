"""The facts sheet of a connector: what a program can say about it by reading it, without running anything. For an MCP server of a registry: the packages it would run and with what command, the
addresses it talks to, the variables and headers it asks for (which are secret), where its code lives. For a skill folder: its files, the addresses written in them, the places that run commands,
install packages, read variables or open the network, and the tools the skill says it may use. The sheet sits next to the advisory review of a model: the model can be wrong, these facts are counted."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlsplit

TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".mjs", ".ts", ".sh", ".bat", ".ps1", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".html"}
FILES_MAX = 400
FILE_BYTES = 400_000
URL = re.compile(r"https?://[^\s'\"<>)\]]+", re.I)
SIGNS = {                                            # what a line of code can do that the person should know about, found by its words
    "runs commands": re.compile(r"\b(subprocess|os\.system|os\.popen|child_process|exec\(|execSync|spawn\(|Popen|Invoke-Expression|iex\b|powershell|cmd\.exe)", re.I),
    "installs packages": re.compile(r"\b(pip3? install|npm (?:i|install)|npx -y|yarn add|uvx?|cargo install|go install|apt(?:-get)? install|choco install|winget install|curl [^|\n]*\|\s*(?:ba)?sh)", re.I),
    "downloads files": re.compile(r"\b(curl |wget |Invoke-WebRequest|urllib\.request|requests\.(?:get|post)|fetch\(|urlretrieve)", re.I),
    "reads environment variables": re.compile(r"(os\.environ|os\.getenv|process\.env|\$env:|getenv\()", re.I),
    "reads or writes outside its folder": re.compile(r"(\.\./|~/|%USERPROFILE%|\$HOME|os\.path\.expanduser|C:\\Users)", re.I),
    "evaluates code": re.compile(r"\b(eval\(|exec\(|new Function|compile\(|__import__)", re.I),
    "encoded content": re.compile(r"(base64\.b64decode|atob\(|fromCharCode|[A-Za-z0-9+/]{120,}={0,2})"),
}
RUN_WITH = {"npm": "npx -y {id}@{v}", "pypi": "uvx {id}=={v}", "oci": "docker run {id}:{v}", "nuget": "dnx {id}@{v}", "mcpb": "an MCP bundle: {id}"}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _names(rows: Any) -> List[Dict[str, Any]]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("name"):
            out.append({"name": str(r["name"])[:80], "secret": bool(r.get("isSecret")), "required": bool(r.get("isRequired")), "about": " ".join(str(r.get("description") or "").split())[:160]})
    return out


def from_server(server: Dict[str, Any]) -> Dict[str, Any]:
    """The sheet of an MCP registry entry (the `server` object of its listing)."""
    packages, remotes, hosts, commands, secrets = [], [], [], [], []
    for p in server.get("packages") or []:
        if not isinstance(p, dict):
            continue
        kind, ident, ver = str(p.get("registryType") or p.get("registry_name") or ""), str(p.get("identifier") or p.get("name") or ""), str(p.get("version") or "latest")
        hint = str(p.get("runtimeHint") or "")
        runs = (RUN_WITH.get(kind) or "{id}").format(id=ident, v=ver) if ident else ""
        env = _names(p.get("environmentVariables"))
        packages.append({"type": kind, "identifier": ident, "version": ver, "runs_with": hint or runs, "env": env, "transport": str((p.get("transport") or {}).get("type") or "stdio")})
        if runs:
            commands.append(runs)
        secrets += [e["name"] for e in env if e["secret"]]
    for r in server.get("remotes") or []:
        if isinstance(r, dict) and r.get("url"):
            headers = _names(r.get("headers"))
            remotes.append({"type": str(r.get("type") or ""), "url": str(r["url"])[:300], "host": _host(str(r["url"])), "headers": headers})
            hosts.append(_host(str(r["url"])))
            secrets += [h["name"] for h in headers if h["secret"]]
    repo = server.get("repository") if isinstance(server.get("repository"), dict) else {}
    notes = []
    if commands:
        notes.append("It downloads code from a package registry and runs it on this PC when it first starts.")
    if remotes:
        notes.append("It is a service on the internet: what you send to it leaves this PC.")
    if not commands and not remotes:
        notes.append("The listing does not say what it runs or where it connects.")
    return {"kind": "mcp_server", "name": str(server.get("name") or ""), "version": str(server.get("version") or ""), "repository": str(repo.get("url") or ""),
            "website": str(server.get("websiteUrl") or ""), "packages": packages, "remotes": remotes, "hosts": sorted(set(h for h in hosts if h)), "commands": commands,
            "secrets": sorted(set(secrets)), "notes": notes}


def from_skill(folder: Path) -> Dict[str, Any]:
    """The sheet of a skill folder, from its files."""
    files, hosts, signs, tools, big = [], set(), {}, "", 0
    try:
        listing = sorted(p for p in Path(folder).rglob("*") if p.is_file())[:FILES_MAX]
    except OSError:
        listing = []
    for p in listing:
        rel = p.relative_to(folder).as_posix()
        try:
            size = p.stat().st_size
        except OSError:
            continue
        files.append({"path": rel, "bytes": size})
        if p.suffix.lower() not in TEXT_SUFFIXES or size > FILE_BYTES:
            big += 1 if size > FILE_BYTES else 0
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for u in URL.findall(text):
            if _host(u):
                hosts.add(_host(u))
        for sign, pattern in SIGNS.items():
            if pattern.search(text):
                signs.setdefault(sign, []).append(rel)
        if p.name.lower() == "skill.md":
            m = re.search(r"^allowed-tools:\s*(.+)$", text, re.M | re.I)
            tools = m.group(1).strip()[:200] if m else ""
    executable = [f["path"] for f in files if Path(f["path"]).suffix.lower() in (".py", ".js", ".mjs", ".ts", ".sh", ".bat", ".ps1")]
    notes = []
    if executable:
        notes.append(f"It carries {len(executable)} script file(s) that can run on this PC.")
    if not executable and not signs:
        notes.append("It is only text for the model: no script, no command found.")
    return {"kind": "skill", "name": Path(folder).name, "files": files[:60], "file_count": len(files), "unread_large_files": big, "scripts": executable[:30], "hosts": sorted(hosts)[:40],
            "signs": {k: v[:8] for k, v in signs.items()}, "allowed_tools": tools, "notes": notes}
