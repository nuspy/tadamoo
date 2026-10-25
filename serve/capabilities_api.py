"""The Settings page's side of capabilities: what was found on this PC, what the person approved, and the choice for visual analysis.

Reading here is what the harness reads (names, descriptions, commands with keys blanked). An entry becomes usable only when the
person presses Approve after looking at its text, and the approval is tied to the hash they were shown.
"""
from __future__ import annotations

import importlib.util
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from harness import capabilities as cap
from harness.discovery.candidates import CandidateStore
from harness.discovery.installed import InstalledStore
from harness.discovery.installer import InstallError, Installer
from harness.discovery.scoring import DEFAULT_WEIGHTS
from harness.discovery.settings import DiscoverySettings, SOURCES


class CapabilityError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class CapabilityApi:
    def __init__(self, directory: Path, workspace: Path, home: Optional[Path] = None, appdata: Optional[Path] = None) -> None:
        self.index = cap.CapabilityIndex(directory, workspace, home=home, appdata=appdata)
        self._lock = threading.Lock()

    def overview(self) -> Dict[str, Any]:
        with self._lock:
            idx = self.index
            return {"entries": [{k: v for k, v in e.items() if k != "path"} for e in idx.entries],
                    "roots": {r: {"en": v["en"], "it": v["it"], "count": c} for (r, v), c in zip(cap.ROOTS.items(), (idx.counts()[r] for r in cap.ROOTS))},
                    "nodes": cap.node_names(), "visual_options": idx.visual_options(), "settings": idx.settings(),
                    "tree_top": idx.top_of_tree(), "readable": str(idx.dir / "skills.md")}

    def rescan(self) -> Dict[str, Any]:
        with self._lock:
            self.index.refresh()
        return self.overview()

    def review(self, entry_id: Any) -> Dict[str, Any]:
        try:
            with self._lock:
                r = self.index.review(str(entry_id))
        except KeyError:
            raise CapabilityError("That entry is not there any more: look again.", "gone") from None
        r["entry"] = {k: v for k, v in r["entry"].items() if k != "path"}
        return r

    def approve(self, entry_id: Any, sha: Any) -> Dict[str, Any]:
        try:
            with self._lock:
                self.index.approve(str(entry_id), str(sha))
        except KeyError:
            raise CapabilityError("That entry is not there any more: look again.", "gone") from None
        except ValueError as exc:
            raise CapabilityError(str(exc), "changed") from None
        return self.overview()

    def revoke(self, entry_id: Any) -> Dict[str, Any]:
        with self._lock:
            self.index.revoke(str(entry_id))
        return self.overview()

    def move(self, entry_id: Any, node: Any) -> Dict[str, Any]:
        try:
            with self._lock:
                self.index.move(str(entry_id), str(node))
        except KeyError:
            raise CapabilityError("That entry is not there any more: look again.", "gone") from None
        except ValueError as exc:
            raise CapabilityError(str(exc), "bad_node") from None
        return self.overview()

    def settings(self, body: Dict[str, Any]) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        if "visual" in body:
            options = {o["id"]: o for o in self.index.visual_options()}
            visual = body["visual"]
            if visual not in options:
                raise CapabilityError("Choose one of the options in the list.", "bad_choice")
            if options[visual]["state"] != "approved":
                raise CapabilityError("Approve it first: look at what it does, then press Approve.", "needs_approval")
            changes["visual"] = visual
        if "off" in body:
            ids = body["off"]
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) > 2000:
                raise CapabilityError("off is a list of entry ids.", "bad_off")
            changes["off"] = sorted({i for i in ids if self.index.get(i) is not None})
        with self._lock:
            self.index.save_settings(**changes)
        return self.overview()

    # -- the search online and what Basalt installed -------------------------------------------------------------------

    def online(self) -> Dict[str, Any]:
        """The settings of the search online, the capabilities Basalt installed, and which installers this PC has."""
        with self._lock:
            settings = DiscoverySettings(self.index.dir).load()
            settings.pop("base_urls", None)
            return {"settings": settings, "sources": list(SOURCES), "weights": list(DEFAULT_WEIGHTS), "installed": InstalledStore(self.index.dir).listing(),
                    "tools": {"npm": bool(shutil.which("npm")), "uv": bool(shutil.which("uv")), "pip": importlib.util.find_spec("pip") is not None}}

    def online_settings(self, body: Dict[str, Any]) -> Dict[str, Any]:
        changes = {k: body[k] for k in ("sources", "weights", "min_score", "auto_install", "marketplaces") if k in body}
        try:
            with self._lock:
                DiscoverySettings(self.index.dir).save(changes)
        except ValueError as exc:
            raise CapabilityError(str(exc), "bad_settings") from None
        return self.online()

    def remove_installed(self, entry_id: Any) -> Dict[str, Any]:
        """Deletes a capability Basalt installed: its folder, its record and its approval."""
        try:
            with self._lock:
                removed = Installer(self.index.dir, CandidateStore(), index=self.index).remove(str(entry_id))
        except InstallError as exc:
            raise CapabilityError(str(exc), "bad_folder") from None
        if not removed:
            raise CapabilityError("Basalt did not install that one: look again.", "gone")
        return self.online()
