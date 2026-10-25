"""Signing in to the remote MCP servers the person approved: Settings starts it, the browser comes back here with the code.

Only a remote server the person approved can be signed in to (approving is looking at its address); signing in contacts that address and
the authorization server it names, and only when the person presses the button. The tokens go to the credential store; the harness reads
them from there when it calls the server.
"""
from __future__ import annotations

import html
import json
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from harness.mcp_oauth import Http, McpOAuth, OAuthError, OAuthStore, default_http
from serve.capabilities_api import CapabilityApi


class McpAuthError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class McpAuthApi:
    def __init__(self, capabilities: CapabilityApi, store: OAuthStore, redirect_uri: Callable[[], str], http: Optional[Http] = None) -> None:
        self.capabilities, self.store, self.redirect_uri, self.http = capabilities, store, redirect_uri, http or default_http
        self._by_state: Dict[str, McpOAuth] = {}
        self._lock = threading.Lock()

    def _oauth(self, url: str) -> McpOAuth:
        return McpOAuth(url, self.store, self.redirect_uri(), http=self.http)

    def _server(self, entry_id: Any) -> Dict[str, Any]:
        for s in self.capabilities.index.remote_servers():
            if s["id"] == entry_id:
                if s["state"] != "approved":
                    raise McpAuthError("Approve this server first (look at its address, then press Approve).", "needs_approval")
                return s
        raise McpAuthError("That server is not there any more: look again.", "gone")

    def overview(self) -> Dict[str, Any]:
        out = []
        for s in self.capabilities.index.remote_servers():
            try:
                signed = self._oauth(s["url"]).signed_in()
            except OAuthError:
                signed = False
            out.append({**s, "signed_in": signed})
        return {"servers": out}

    def start(self, entry_id: Any) -> Dict[str, Any]:
        """The address to open in the browser, or {"signed_in": true} when the server needs nothing more from the person."""
        server = self._server(entry_id)
        try:
            oauth = self._oauth(server["url"])
            status, headers, _body = self.http("POST", server["url"], {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                                               json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                                                   "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Basalt", "version": "0"}}}).encode("utf-8"), 15.0)
            if status != 401:
                return {"signed_in": oauth.signed_in(), "needs_sign_in": False}
            began = oauth.begin(headers.get("www-authenticate", ""))
        except OAuthError as e:
            raise McpAuthError(str(e), "oauth") from None
        with self._lock:
            self._by_state[began["state"]] = oauth
            while len(self._by_state) > 16:
                self._by_state.pop(next(iter(self._by_state)))
        return {"authorization_url": began["authorization_url"]}

    def callback(self, query: Dict[str, str]) -> Tuple[bool, str]:
        """What the browser brings back: (worked, a sentence for the person)."""
        state = query.get("state", "")
        if query.get("error"):
            with self._lock:
                self._by_state.pop(state, None)
            return False, "The server did not let you in: " + str(query.get("error_description") or query["error"])[:200]
        with self._lock:
            oauth = self._by_state.pop(state, None)
        if oauth is None:
            return False, "That sign-in was not started here, or it took too long. Start it again from Settings."
        try:
            oauth.complete(query.get("code", ""), state)
        except OAuthError as e:
            return False, str(e)
        return True, "You are signed in. You can close this tab and go back to Basalt."

    def sign_out(self, entry_id: Any) -> Dict[str, Any]:
        server = self._server(entry_id)
        try:
            self._oauth(server["url"]).sign_out()
        except OAuthError as e:
            raise McpAuthError(str(e), "oauth") from None
        return self.overview()


def result_page(ok: bool, message: str) -> bytes:
    title = "Signed in" if ok else "Sign-in did not work"
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>body{{font:18px/1.5 system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh;"
            f"background:#0E1114;color:#E9ECEE}}main{{max-width:34rem;padding:24px}}h1{{font-size:24px}}</style></head><body><main><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(message)}</p></main></body></html>").encode("utf-8")
