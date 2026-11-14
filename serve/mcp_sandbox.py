"""The origin that serves the sandbox of MCP Apps (docs/mcp-apps.md).

The specification asks that a web host wrap each app in an iframe whose origin is not the host's, that the document there is served by a server the host controls so
the Content-Security-Policy comes as an HTTP header, and that the host talk to the app through that frame (the "sandbox proxy": `ui/notifications/sandbox-proxy-ready`,
`ui/notifications/sandbox-resource-ready`, every other message passed through). A second port of this server is that origin: a port is part of an origin, so the page of the
conversation and the sandbox never share one. The document is a small shim; the app's own HTML arrives by postMessage and is put in an inner iframe (`srcdoc`) that inherits
the policy of the shim. The policy is built from the domains the app declared, as the specification gives it.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

FORBIDDEN_PORT = 8080                                     # never listened on
_DOMAIN = re.compile(r"^(?:https?|wss?)://(?:\*\.)?[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*(?::\d{1,5})?(?:/[^\s;,'\"]*)?$|^(?:data|blob):$")
_ALLOW = {"camera": "camera", "microphone": "microphone", "geolocation": "geolocation", "clipboardWrite": "clipboard-write"}

# What the specification gives when the app declared no policy.
DEFAULT_POLICY = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' data:; "
                  "object-src 'none'; connect-src 'none'; frame-src 'none'; base-uri 'self'")

SHIM_HTML = '<!doctype html>\n<meta charset="utf-8">\n<title>MCP app</title>\n<style>html,body{margin:0;height:100%;overflow:hidden}iframe{border:0;width:100%;height:100%;display:block}</style>\n<script src="/sandbox.js"></script>\n'

SHIM_JS = r"""(() => {
  "use strict";
  const query = new URLSearchParams(location.search), hostOrigin = query.get("host") || "";
  let inner = null;
  const toHost = message => { try { window.parent.postMessage(message, hostOrigin); } catch (e) { /* the host is gone */ } };
  function load(p) {
    if (inner) inner.remove();
    inner = document.createElement("iframe");
    inner.setAttribute("sandbox", typeof p.sandbox === "string" && p.sandbox ? p.sandbox : "allow-scripts allow-same-origin");
    const allow = []; const perms = p.permissions || {};
    if (perms.camera) allow.push("camera"); if (perms.microphone) allow.push("microphone"); if (perms.geolocation) allow.push("geolocation"); if (perms.clipboardWrite) allow.push("clipboard-write");
    if (allow.length) inner.setAttribute("allow", allow.join("; "));
    inner.srcdoc = String(p.html || "");
    document.body.append(inner);
  }
  window.addEventListener("message", ev => {
    const m = ev.data;
    if (ev.source === window.parent) {
      if (ev.origin !== hostOrigin || !m || m.jsonrpc !== "2.0") return;
      if (m.method === "ui/notifications/sandbox-resource-ready") { load(m.params || {}); return; }
      if (typeof m.method === "string" && m.method.startsWith("ui/notifications/sandbox-")) return;
      if (inner && inner.contentWindow) inner.contentWindow.postMessage(m, "*");
    } else if (inner && ev.source === inner.contentWindow) {
      if (!m || m.jsonrpc !== "2.0" || (typeof m.method === "string" && m.method.startsWith("ui/notifications/sandbox-"))) return;
      toHost(m);
    }
  });
  toHost({ jsonrpc: "2.0", method: "ui/notifications/sandbox-proxy-ready", params: {} });
})();
"""


def clean_domains(values: Any) -> List[str]:
    """The declared domains that can be written into a policy as they are: a scheme and a host (a leading `*.` allowed), a port and a path, nothing that could end the directive."""
    return [v.strip() for v in values if isinstance(v, str) and _DOMAIN.match(v.strip())][:50] if isinstance(values, list) else []


def policy_for(csp: Optional[Dict[str, Any]]) -> str:
    """The Content-Security-Policy of the sandbox document for the `ui.csp` an app declared: the specification's default when it declared none."""
    if not isinstance(csp, dict):
        return DEFAULT_POLICY
    rd = " ".join(clean_domains(csp.get("resourceDomains")))
    cd = " ".join(clean_domains(csp.get("connectDomains")))
    fd = " ".join(clean_domains(csp.get("frameDomains"))) or "'none'"
    bd = " ".join(clean_domains(csp.get("baseUriDomains"))) or "'self'"
    parts = ["default-src 'none'", f"script-src 'self' 'unsafe-inline' {rd}", f"style-src 'self' 'unsafe-inline' {rd}", f"connect-src 'self' {cd}", f"img-src 'self' data: {rd}",
             f"font-src 'self' {rd}", f"media-src 'self' data: {rd}", f"frame-src {fd}", "object-src 'none'", f"base-uri {bd}"]
    return "; ".join(" ".join(p.split()) for p in parts)


def allow_attribute(permissions: Optional[Dict[str, Any]]) -> str:
    """The Permissions Policy features of the iframe's `allow` attribute for the permissions an app asked for."""
    return "; ".join(feature for key, feature in _ALLOW.items() if isinstance(permissions, dict) and key in permissions)


class _Handler(BaseHTTPRequestHandler):
    server_version = "TaDaMooSandbox"

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/sandbox.js":
            return self._send(200, SHIM_JS.encode("utf-8"), "text/javascript; charset=utf-8")
        if parts.path == "/sandbox":
            raw = (parse_qs(parts.query).get("csp") or [""])[0]
            try:
                csp = json.loads(raw) if raw else None
            except ValueError:
                csp = None
            return self._send(200, SHIM_HTML.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": policy_for(csp)})
        self._send(404, b"not found", "text/plain")


class SandboxOrigin:
    """The second port, started when the first app is shown and kept while the server runs. `port` is 0 when it could not be started."""

    def __init__(self, bind: str) -> None:
        self.bind, self.port = bind, 0
        self._server: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()

    def ensure(self) -> int:
        with self._lock:
            if self._server is None:
                for _ in range(5):
                    try:
                        server = ThreadingHTTPServer((self.bind, 0), _Handler)
                    except OSError:
                        return 0
                    if server.server_address[1] != FORBIDDEN_PORT:
                        break
                    server.server_close()
                else:
                    return 0
                server.daemon_threads = True
                threading.Thread(target=server.serve_forever, name="mcp-sandbox", daemon=True).start()
                self._server, self.port = server, server.server_address[1]
            return self.port

    def close(self) -> None:
        with self._lock:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
                self._server, self.port = None, 0
