"""What the page needs to show MCP Apps: where the sandbox is, the person's choices about apps, the pages kept (docs/mcp-apps.md)."""
from __future__ import annotations

from typing import Any, Dict

from serve.mcp_sandbox import SandboxOrigin
from tadamoo import mcp_apps


class AppsApiError(Exception):
    pass


class AppsApi:
    def __init__(self, choices: Any) -> None:
        """`choices()` gives the person's choices about apps (the capabilities' settings). The sandbox is listened for on the address the page's own request came to: a phone that
        reaches the server at its Tailscale address needs the sandbox there, not only on the PC's loopback."""
        self.choices = choices
        self.boxes: Dict[str, SandboxOrigin] = {}

    def config(self, bind: str) -> Dict[str, Any]:
        """The sandbox's port on `bind` (started now; 0 when it could not be), the choices, the protocol revision. The page builds the sandbox's origin from the address it reached the server at."""
        box = self.boxes.setdefault(bind, SandboxOrigin(bind))
        return {"sandbox_port": box.ensure(), "protocol": mcp_apps.PROTOCOL_VERSION, "settings": self.choices(), "defaults": mcp_apps.DEFAULTS,
                "known": mcp_apps.default_cache().known()}

    def close(self) -> None:
        for box in self.boxes.values():
            box.close()

    def template(self, digest: str) -> Dict[str, Any]:
        page = mcp_apps.default_cache().get(digest)
        if page is None:
            raise AppsApiError("That page is not kept (any more): the server it came from can give it again.")
        return {"server": page.get("server"), "uri": page.get("uri"), "html": page.get("html"), "meta": page.get("meta") or {}}
