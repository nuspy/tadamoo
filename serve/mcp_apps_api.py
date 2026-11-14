"""What the page needs to show MCP Apps: where the sandbox is, the person's choices about apps, the pages kept (docs/mcp-apps.md)."""
from __future__ import annotations

from typing import Any, Dict

from serve.mcp_sandbox import SandboxOrigin
from tadamoo import mcp_apps


class AppsApiError(Exception):
    pass


class AppsApi:
    def __init__(self, sandbox: SandboxOrigin, choices: Any) -> None:
        """`choices()` gives the person's choices about apps (the capabilities' settings)."""
        self.sandbox, self.choices = sandbox, choices

    def config(self) -> Dict[str, Any]:
        """The sandbox's port (started now; 0 when it could not be), the choices, the protocol revision. The page builds the sandbox's origin from its own address."""
        return {"sandbox_port": self.sandbox.ensure(), "protocol": mcp_apps.PROTOCOL_VERSION, "settings": self.choices(), "defaults": mcp_apps.DEFAULTS,
                "known": mcp_apps.default_cache().known()}

    def template(self, digest: str) -> Dict[str, Any]:
        page = mcp_apps.default_cache().get(digest)
        if page is None:
            raise AppsApiError("That page is not kept (any more): the server it came from can give it again.")
        return {"server": page.get("server"), "uri": page.get("uri"), "html": page.get("html"), "meta": page.get("meta") or {}}
