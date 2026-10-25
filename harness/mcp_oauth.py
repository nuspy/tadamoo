"""Signing in to a remote MCP server (OAuth 2.1 with PKCE), as the MCP authorization specification asks of a client.

A server that wants a sign-in answers 401 with a `WWW-Authenticate` header that points to its resource metadata (RFC 9728); that names the
authorization server, whose metadata (RFC 8414 or OpenID Connect discovery) gives the endpoints; a client id is registered on the fly
(RFC 7591) when the server allows it; the person approves in their browser (authorization code, PKCE S256, a `state` that must come back,
and the server's address as `resource`, RFC 8707); the code is exchanged for tokens, and the access token goes in an `Authorization` header
on every request to that server and to no other. Tokens live in the credential store (the Windows credential manager), never in a file in
clear; what is kept in a file is only where the endpoints are and the client id.

Discovery documents come from a server that is not trusted: every endpoint they name must be https (plain http only when the MCP server itself
is on this PC) and, for a server that is not on this PC, must not be an address on a private network.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.integrations._http import _OPENER

LOOPBACK = ("127.0.0.1", "localhost", "::1")
PENDING_S = 600
PENDING_MAX = 8
EXPIRY_MARGIN_S = 60
CHUNK = 1800                                            # a credential holds a little more than this; longer tokens go in pieces
BODY_MAX = 1 << 20

Http = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, Dict[str, str], bytes]]


class OAuthError(Exception):
    pass


class AuthRequired(OAuthError):
    """The server needs the person to sign in (again)."""

    def __init__(self, message: str, www_authenticate: str = "") -> None:
        super().__init__(message)
        self.www_authenticate = www_authenticate


def canonical(url: str) -> str:
    """The server's address as the `resource` of a token: lowercase scheme and host, no fragment, no query, no trailing slash."""
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    netloc = f"[{host}]" if ":" in host else host
    if p.port and not ((p.scheme == "https" and p.port == 443) or (p.scheme == "http" and p.port == 80)):
        netloc += f":{p.port}"
    return f"{p.scheme.lower()}://{netloc}{p.path.rstrip('/')}"


def parse_challenge(header: str) -> Dict[str, str]:
    """The parameters of the Bearer challenge in a WWW-Authenticate header: resource_metadata, scope, error..."""
    out: Dict[str, str] = {}
    m = re.search(r"(?i)\bbearer\b(.*)", header or "")
    if not m:
        return out
    for key, quoted, bare in re.findall(r'([A-Za-z_][\w\-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))', m.group(1)):
        out.setdefault(key.lower(), (quoted or bare).replace('\\"', '"'))
    return out


def _is_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal", ".localdomain"))
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


def default_http(method: str, url: str, headers: Dict[str, str], data: Optional[bytes], timeout: float) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(BODY_MAX)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(BODY_MAX)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OAuthError(f"Cannot reach {urllib.parse.urlsplit(url).netloc}: {getattr(e, 'reason', e)}") from None


class OAuthStore:
    """Where a server's sign-in is kept: the endpoints and client id in a JSON file, the tokens in the credential store."""

    def __init__(self, path: str | Path, credentials: Any) -> None:
        self.path = Path(path)
        self.credentials = credentials
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def key(resource: str) -> str:
        return hashlib.sha256(canonical(resource).encode("utf-8")).hexdigest()[:16]

    def get(self, resource: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._load().get(self.key(resource)) or {})

    def put(self, resource: str, **fields: Any) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(self.key(resource), {}).update(fields, resource=canonical(resource))
            self._save(data)

    def forget(self, resource: str, keep_client: bool = True) -> None:
        with self._lock:
            data = self._load()
            rec = data.get(self.key(resource)) or {}
            for field in ("expires_at", "scope"):
                rec.pop(field, None)
            if not keep_client:
                data.pop(self.key(resource), None)
            elif rec:
                data[self.key(resource)] = rec
            self._save(data)
        for part in ("access", "refresh"):
            self.secret(resource, part, None)

    def secret(self, resource: str, part: str, value: Optional[str] = ...) -> str:                      # type: ignore[assignment]
        """Read (no value) or write (a value; None deletes) one of the server's secrets, in pieces when it is long."""
        name = f"mcp/{self.key(resource)}/{part}"
        if value is ...:
            head = self.credentials.read(name)
            if not head.startswith("#"):
                return head
            return "".join(self.credentials.read(f"{name}.{i}") for i in range(int(head[1:] or 0)))
        old = self.credentials.read(name)
        if old.startswith("#"):
            for i in range(int(old[1:] or 0)):
                self.credentials.delete(f"{name}.{i}")
        if not value:
            self.credentials.delete(name)
            return ""
        if len(value) <= CHUNK:
            self.credentials.write(name, value)
        else:
            pieces = [value[i:i + CHUNK] for i in range(0, len(value), CHUNK)]
            for i, piece in enumerate(pieces):
                self.credentials.write(f"{name}.{i}", piece)
            self.credentials.write(name, f"#{len(pieces)}")
        return ""


class McpOAuth:
    """The sign-in of one remote MCP server."""

    def __init__(self, resource: str, store: OAuthStore, redirect_uri: str = "", http: Optional[Http] = None,
                 clock: Callable[[], float] = time.time, client_name: str = "Basalt", timeout: float = 15.0) -> None:
        self.resource = canonical(resource)
        self.store, self.redirect_uri, self.http, self.clock = store, redirect_uri, http or default_http, clock
        self.client_name, self.timeout = client_name, timeout
        parts = urllib.parse.urlsplit(self.resource)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise OAuthError("An MCP server's address is an http(s) address.")
        self.local = parts.hostname in LOOPBACK
        if parts.scheme == "http" and not self.local:
            raise OAuthError("An MCP server must be reached over https (plain http only on this PC).")
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # -- small things -------------------------------------------------------------------------------------------

    def _ok_endpoint(self, url: Any, what: str) -> str:
        if not isinstance(url, str) or not url:
            raise OAuthError(f"The server did not name its {what}.")
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        if p.scheme != "https" and not (p.scheme == "http" and self.local and host in LOOPBACK):
            raise OAuthError(f"The {what} is not an https address; it was not used.")
        if not self.local and _is_private(host):
            raise OAuthError(f"The {what} is an address on a private network; it was not used.")
        return url

    def _get_json(self, url: str, what: str) -> Dict[str, Any]:
        status, _h, body = self.http("GET", self._ok_endpoint(url, what), {"Accept": "application/json"}, None, self.timeout)
        if status != 200:
            raise OAuthError(f"The {what} answered HTTP {status}.")
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            raise OAuthError(f"The {what} did not answer with JSON.") from None
        if not isinstance(data, dict):
            raise OAuthError(f"The {what} did not answer with an object.")
        return data

    def _post_form(self, url: str, fields: Dict[str, str], what: str, as_json: bool = False) -> Dict[str, Any]:
        if as_json:
            data, ctype = json.dumps(fields).encode("utf-8"), "application/json"
        else:
            data, ctype = urllib.parse.urlencode(fields).encode("utf-8"), "application/x-www-form-urlencoded"
        status, _h, body = self.http("POST", self._ok_endpoint(url, what), {"Content-Type": ctype, "Accept": "application/json"}, data, self.timeout)
        try:
            out = json.loads(body.decode("utf-8")) if body else {}
        except ValueError:
            out = {}
        if not isinstance(out, dict):
            out = {}
        if status >= 400 or out.get("error"):
            reason = str(out.get("error") or f"HTTP {status}")[:80]
            detail = str(out.get("error_description") or "")[:160]
            err = OAuthError(f"The {what} refused: {reason}" + (f" ({detail})" if detail else ""))
            err.code = reason                                              # type: ignore[attr-defined]
            raise err
        return out

    # -- finding out where to sign in ---------------------------------------------------------------------------

    def discover(self, www_authenticate: str = "") -> Dict[str, Any]:
        """The authorization server's endpoints for this MCP server, as {"issuer", "authorization_endpoint", "token_endpoint",
        "registration_endpoint"?, "scope"?}."""
        hint = parse_challenge(www_authenticate)
        parts = urllib.parse.urlsplit(self.resource)
        base = f"{parts.scheme}://{parts.netloc}"
        candidates = [hint["resource_metadata"]] if hint.get("resource_metadata") else []
        candidates += [f"{base}/.well-known/oauth-protected-resource{parts.path}", f"{base}/.well-known/oauth-protected-resource"]
        prm: Optional[Dict[str, Any]] = None
        last: Optional[OAuthError] = None
        for url in candidates:
            try:
                prm = self._get_json(url, "resource metadata")
                break
            except OAuthError as e:
                last = e
        if prm is None:
            raise OAuthError(f"The server does not say where to sign in ({last}).")
        if prm.get("resource") and canonical(str(prm["resource"])) != self.resource:
            raise OAuthError("The server's resource metadata is for another address; it was not used.")
        servers = [s for s in prm.get("authorization_servers") or [] if isinstance(s, str)]
        if not servers:
            raise OAuthError("The server names no authorization server.")
        issuer = servers[0]
        ip = urllib.parse.urlsplit(self._ok_endpoint(issuer, "authorization server"))
        path = ip.path.rstrip("/")
        root = f"{ip.scheme}://{ip.netloc}"
        urls = ([f"{root}/.well-known/oauth-authorization-server{path}", f"{root}/.well-known/openid-configuration{path}",
                 f"{root}{path}/.well-known/openid-configuration"])
        meta: Optional[Dict[str, Any]] = None
        for url in urls:
            try:
                meta = self._get_json(url, "authorization server metadata")
                break
            except OAuthError as e:
                last = e
        if meta is None:
            raise OAuthError(f"The authorization server's metadata could not be read ({last}).")
        if meta.get("issuer") and canonical(str(meta["issuer"])) != canonical(issuer):
            raise OAuthError("The authorization server's metadata names another issuer; it was not used.")
        methods = meta.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" not in methods:
            raise OAuthError("The authorization server does not do PKCE with S256, which signing in requires.")
        found = {"issuer": canonical(issuer), "authorization_endpoint": self._ok_endpoint(meta.get("authorization_endpoint"), "authorization endpoint"),
                 "token_endpoint": self._ok_endpoint(meta.get("token_endpoint"), "token endpoint")}
        if meta.get("registration_endpoint"):
            found["registration_endpoint"] = self._ok_endpoint(meta["registration_endpoint"], "registration endpoint")
        scope = hint.get("scope") or ""
        if scope:
            found["scope"] = scope
        return found

    def _client_id(self, found: Dict[str, Any]) -> str:
        saved = self.store.get(self.resource)
        if saved.get("client_id") and saved.get("issuer") == found["issuer"] and saved.get("redirect_uri") == self.redirect_uri:
            return str(saved["client_id"])
        if not found.get("registration_endpoint"):
            raise OAuthError("This server does not register clients by itself, and no client id was given for it.")
        reply = self._post_form(found["registration_endpoint"], {
            "client_name": self.client_name, "redirect_uris": [self.redirect_uri], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}, "registration endpoint", as_json=True)       # type: ignore[dict-item]
        if not reply.get("client_id"):
            raise OAuthError("The registration did not give a client id.")
        self.store.put(self.resource, client_id=str(reply["client_id"]), issuer=found["issuer"], redirect_uri=self.redirect_uri)
        return str(reply["client_id"])

    # -- signing in ---------------------------------------------------------------------------------------------

    def begin(self, www_authenticate: str = "") -> Dict[str, str]:
        """The address to open in the browser, and the `state` that has to come back with the code."""
        if not self.redirect_uri:
            raise OAuthError("Signing in needs an address for the answer to come back to.")
        found = self.discover(www_authenticate)
        client_id = self._client_id(found)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(24)
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": self.redirect_uri, "state": state,
                 "code_challenge": challenge, "code_challenge_method": "S256", "resource": self.resource}
        if found.get("scope"):
            query["scope"] = found["scope"]
        sep = "&" if "?" in found["authorization_endpoint"] else "?"
        url = found["authorization_endpoint"] + sep + urllib.parse.urlencode(query)
        now = self.clock()
        with self._lock:
            for s in [s for s, p in self._pending.items() if now - p["at"] > PENDING_S]:
                self._pending.pop(s, None)
            while len(self._pending) >= PENDING_MAX:
                self._pending.pop(min(self._pending, key=lambda s: self._pending[s]["at"]))
            self._pending[state] = {"verifier": verifier, "found": found, "client_id": client_id, "at": now}
        return {"authorization_url": url, "state": state}

    def complete(self, code: str, state: str) -> None:
        """The browser came back with a code: exchange it for tokens. An unknown or old `state` is refused."""
        with self._lock:
            pending = self._pending.pop(state, None) if isinstance(state, str) else None
        if pending is None or self.clock() - pending["at"] > PENDING_S:
            raise OAuthError("That sign-in was not started here, or it took too long. Start it again.")
        if not isinstance(code, str) or not code or len(code) > 4096:
            raise OAuthError("The sign-in came back without a code.")
        reply = self._post_form(pending["found"]["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri, "client_id": pending["client_id"],
            "code_verifier": pending["verifier"], "resource": self.resource}, "token endpoint")
        self._keep(reply, pending["found"])

    def _keep(self, reply: Dict[str, Any], found: Dict[str, Any]) -> None:
        token = reply.get("access_token")
        if not isinstance(token, str) or not token or str(reply.get("token_type", "Bearer")).lower() != "bearer":
            raise OAuthError("The token endpoint did not give a bearer token.")
        try:
            lifetime = float(reply.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600.0
        self.store.secret(self.resource, "access", token)
        if isinstance(reply.get("refresh_token"), str) and reply["refresh_token"]:
            self.store.secret(self.resource, "refresh", reply["refresh_token"])
        self.store.put(self.resource, token_endpoint=found["token_endpoint"], issuer=found["issuer"], redirect_uri=self.redirect_uri,
                       expires_at=self.clock() + max(0.0, lifetime), scope=str(reply.get("scope") or found.get("scope") or ""))

    # -- using it -----------------------------------------------------------------------------------------------

    def signed_in(self) -> bool:
        return bool(self.store.secret(self.resource, "access") or self.store.secret(self.resource, "refresh"))

    def access_token(self) -> Optional[str]:
        """The token to send, refreshed first when it is about to run out; None when the person never signed in to this server."""
        token = self.store.secret(self.resource, "access")
        saved = self.store.get(self.resource)
        if token and self.clock() < float(saved.get("expires_at") or 0) - EXPIRY_MARGIN_S:
            return token
        if self.store.secret(self.resource, "refresh"):
            if self.refresh():
                return self.store.secret(self.resource, "access")
            raise AuthRequired("The sign-in to this server ran out. Sign in again.")
        return token or None

    def refresh(self) -> bool:
        """A new access token from the refresh token; False when there is none or the server will not give one (then the person signs in again)."""
        refresh = self.store.secret(self.resource, "refresh")
        saved = self.store.get(self.resource)
        if not refresh or not saved.get("token_endpoint") or not saved.get("client_id"):
            return False
        try:
            reply = self._post_form(saved["token_endpoint"], {"grant_type": "refresh_token", "refresh_token": refresh,
                                                              "client_id": saved["client_id"], "resource": self.resource}, "token endpoint")
            self._keep(reply, {"token_endpoint": saved["token_endpoint"], "issuer": saved.get("issuer", ""), "scope": saved.get("scope", "")})
            return True
        except OAuthError as e:
            if getattr(e, "code", "") in ("invalid_grant", "invalid_token", "unauthorized_client"):
                self.store.forget(self.resource)
            return False

    def sign_out(self) -> None:
        self.store.forget(self.resource)
