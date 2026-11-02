"""What the person chose about the knowledge the Chat and the Agent can reach: how many project notes are added to a request and how strict
that is, the address of the wiki (wikibridge) and the folder of reference documents. Kept in one small file; the conversations are started
with the matching options."""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.stack import DEFAULT_MEMORY_BUDGET, DEFAULT_MEMORY_MIN_SCORE, DEFAULT_MEMORY_NOTES

STRATEGIES = ("auto", "cag", "rag", "none")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$")
LIMITS = {"memory_min_score": (0.0, 5.0), "memory_notes": (0, 20), "memory_budget_tokens": (50, 4000)}


class KnowledgeError(ValueError):
    """What is wrong with a value, in words for the person; `field` names it."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


def clean_url(value: Any) -> str:
    """An http or https address of a service, without a user name, a password, a query or a fragment; "" when none is given."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 300:
        raise KnowledgeError("The address is too long.", "wiki_url")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise KnowledgeError("The address must start with http:// or https:// and name a host, for example http://127.0.0.1:8775.", "wiki_url")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise KnowledgeError("The address has no user name, password, query or fragment: the key is set apart (BASALT_WIKIBRIDGE_KEY).", "wiki_url")
    return text.rstrip("/")


class KnowledgePrefs:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _stored(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def read(self) -> Dict[str, Any]:
        data = self._stored()

        def number(key: str, default: Any, kind: type) -> Any:
            lo, hi = LIMITS[key]
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                return default
            return kind(value)

        try:
            url = clean_url(data.get("wiki_url"))
        except KnowledgeError:
            url = ""
        project = data.get("wiki_project") if isinstance(data.get("wiki_project"), str) and SLUG.match(data["wiki_project"]) else ""
        folder = data.get("reference_folder") if isinstance(data.get("reference_folder"), str) else ""
        strategy = data.get("reference_strategy") if data.get("reference_strategy") in STRATEGIES else "auto"
        return {"memory_min_score": number("memory_min_score", DEFAULT_MEMORY_MIN_SCORE, float),
                "memory_notes": number("memory_notes", DEFAULT_MEMORY_NOTES, int),
                "memory_budget_tokens": number("memory_budget_tokens", DEFAULT_MEMORY_BUDGET, int),
                "wiki_enabled": data.get("wiki_enabled") is True, "wiki_url": url, "wiki_project": project,
                "reference_folder": folder[:500], "reference_strategy": strategy}

    def write(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Saves the values given and keeps the others; a wrong value is refused whole, with the reason."""
        now = self.read()
        for key in ("memory_min_score", "memory_notes", "memory_budget_tokens"):
            if key in changes:
                value, (lo, hi) = changes[key], LIMITS[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                    raise KnowledgeError(f"This is a number from {lo:g} to {hi:g}.", key)
                if key != "memory_min_score" and int(value) != value:
                    raise KnowledgeError("This is a whole number.", key)
                now[key] = float(value) if key == "memory_min_score" else int(value)
        if "wiki_enabled" in changes:
            if not isinstance(changes["wiki_enabled"], bool):
                raise KnowledgeError("This is on or off.", "wiki_enabled")
            now["wiki_enabled"] = changes["wiki_enabled"]
        if "wiki_url" in changes:
            now["wiki_url"] = clean_url(changes["wiki_url"])
        if "wiki_project" in changes:
            project = str(changes["wiki_project"] or "").strip()
            if project and not SLUG.match(project):
                raise KnowledgeError("A project name is letters, digits, dots, dashes and underscores, as the wiki lists it.", "wiki_project")
            now["wiki_project"] = project
        if "reference_folder" in changes:
            folder = str(changes["reference_folder"] or "").strip()
            if folder and not Path(folder).is_dir():
                raise KnowledgeError("This folder does not exist on this PC.", "reference_folder")
            now["reference_folder"] = str(Path(folder).resolve()) if folder else ""
        if "reference_strategy" in changes:
            if changes["reference_strategy"] not in STRATEGIES:
                raise KnowledgeError("Choose auto, cag, rag or none.", "reference_strategy")
            now["reference_strategy"] = changes["reference_strategy"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(now, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        return self.read()

    def view(self) -> Dict[str, Any]:
        """The values and the facts the page shows beside them."""
        values = self.read()
        folder = values["reference_folder"]
        return {**values, "reference_found": bool(folder) and Path(folder).is_dir(),
                "defaults": {"memory_min_score": DEFAULT_MEMORY_MIN_SCORE, "memory_notes": DEFAULT_MEMORY_NOTES, "memory_budget_tokens": DEFAULT_MEMORY_BUDGET},
                "limits": {k: list(v) for k, v in LIMITS.items()}, "strategies": list(STRATEGIES)}

    def flags(self) -> List[str]:
        """The options a conversation's process is started with. A reference folder that is not there is left out (the page says so)."""
        v = self.read()
        out = ["--memory-min-score", str(v["memory_min_score"]), "--memory-notes", str(v["memory_notes"]), "--memory-budget", str(v["memory_budget_tokens"])]
        if v["wiki_enabled"]:
            out += ["--wiki"] + (["--wiki-url", v["wiki_url"]] if v["wiki_url"] else []) + (["--wiki-project", v["wiki_project"]] if v["wiki_project"] else [])
        if v["reference_folder"] and Path(v["reference_folder"]).is_dir() and v["reference_strategy"] != "none":
            out += ["--reference", v["reference_folder"], "--reference-strategy", v["reference_strategy"]]
        return out


def check_wiki(url: str, client_factory: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Asks the wikibridge at this address whether it is there: the address tried, how it went and, if it did not, why."""
    from tadamoo.integrations.wikibridge_client import DEFAULT_URL, WikiBridgeClient
    client = (client_factory or WikiBridgeClient.from_environment)(url=url or None)
    status = client.status()
    out: Dict[str, Any] = {"address": client.base_url or DEFAULT_URL, "status": status, "error": client.last_error or ""}
    if status == "ok":
        out["projects"] = client.list_projects()[:50]
    return out
