"""Which requests a browser page of another site, or a DNS name pointed at this PC (DNS rebinding), may make. Pure functions; the handler
asks `refusal` once per request, before any route.

Host rule: the Host header names this PC by an IP address, `localhost` (or `*.localhost`), the PC's own name, or a name in
`server.allowed-hosts`. Origin rule: a request that carries an `Origin` comes from this server's own pages, from an allowed name or from an
origin in `server.allowed-origins`, and a state-changing request is not marked `Sec-Fetch-Site: cross-site` (unless the Origin was named in
`server.allowed-origins`; `"*"` there turns the Origin rule off). Programs send neither header and pass. `/v1/*` is left to its CORS
policy, which opens it to every page."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
OPEN_PREFIX = "/v1/"
HOST_MAX = 253
ANY_HOST = "*"                                           # in server.allowed-hosts: every Host name is answered (the Host rule is off)
ANY_ORIGIN = "*"                                         # in server.allowed-origins: every Origin is accepted (the Origin rule is off)
_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")


class Refusal:
    """Why a request is refused: the code and the sentence the answer carries."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message

    def __repr__(self) -> str:
        return f"Refusal({self.code!r})"


def check_hostname(value) -> str:
    """A name for `server.allowed-hosts`, lowercase and without a trailing dot, or ANY_HOST; ValueError for anything that is not a bare
    host name (a scheme, a port, a path, a partial wildcard or an IP address)."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r}: a host name such as mypc.example.ts.net")
    name = value.strip().lower().rstrip(".")
    if name == ANY_HOST:
        return name
    if not name or len(name) > HOST_MAX or not all(_LABEL.fullmatch(p) for p in name.split(".")):
        raise ValueError(f"{value!r}: a host name only (letters, digits, dashes and dots; no scheme, port, path or wildcard)")
    if _is_ip(name):
        raise ValueError(f"{value!r}: an IP address is always allowed; list names only")
    return name


def check_origin(value) -> str:
    """An entry for `server.allowed-origins`, normalised (`http://localhost:3000`: scheme, host and an optional port, nothing else), or
    ANY_ORIGIN; ValueError for anything else."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r}: an origin such as http://localhost:3000")
    text = value.strip().lower().rstrip("/")
    if text == ANY_ORIGIN:
        return text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError(f"{value!r}: an origin such as http://localhost:3000") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError(f"{value!r}: an origin is a scheme, a host and an optional port (http://localhost:3000), with no path, wildcard or login")
    host = parts.hostname
    host = f"[{host}]" if ":" in host else host
    if not (_is_ip(host.strip("[]")) or all(_LABEL.fullmatch(p) for p in host.split("."))):
        raise ValueError(f"{value!r}: not a host name")
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def normal_origin(origin: str) -> str:
    """An Origin header in the form `check_origin` stores, or "" when it is not an http(s) origin."""
    try:
        return check_origin(origin)
    except ValueError:
        return ""


def split_host(value: str) -> tuple[str, str]:
    """(host, port) of a Host header or an Origin's netloc: the host lowercase, without brackets or a trailing dot; port '' when absent."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return value, ""
        rest = value[end + 1:]
        return value[1:end], rest[1:] if rest.startswith(":") else ""
    host, port = value.split(":", 1) if value.count(":") == 1 else (value, "")     # more than one colon: a bare IPv6 address
    return host.rstrip("."), port


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


def _is_loopback_name(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def host_allowed(host: str, allowed=(), own_name: str = "") -> bool:
    """A Host (without the port) that no other site can point here: an IP address, a localhost name, this PC's name or an allowed name."""
    if not host:
        return True
    if _is_ip(host) or host == "localhost" or host.endswith(".localhost"):
        return True
    if own_name and host == own_name.strip().lower().rstrip("."):
        return True
    return host in allowed or ANY_HOST in allowed


def origin_allowed(origin: str, host_header: str, allowed=()) -> bool:
    """An Origin of this server's own pages: the same host and port as the request's Host, the other loopback spelling of the same port,
    or an allowed name."""
    parts = urlsplit(origin.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False                                     # "null", file:, extensions
    o_host, o_port = split_host(parts.netloc)
    h_host, h_port = split_host(host_header or "")
    if o_host in allowed:                                # never through ANY_HOST: it turns off the Host rule only
        return True
    if not host_header:
        return False
    default = "443" if parts.scheme == "https" else "80"
    same_port = (o_port or default) == (h_port or default)
    if o_host == h_host and same_port:
        return True
    return same_port and _is_loopback_name(o_host) and _is_loopback_name(h_host)


def refusal(method: str, path: str, headers, *, host_rule: bool = True, proven: bool = False, allowed=(), own_name: str = "",
            allowed_origins=()) -> Refusal | None:
    """None when the request may go on. `headers` is a mapping with .get; `host_rule` is False where every request needs a key anyway
    (the listener for phones, a server with an API key); `proven` is True when the request carries a secret in a header, which no page of
    another site can send; `allowed` holds the names of `server.allowed-hosts` and `allowed_origins` the entries of `server.allowed-origins`."""
    if proven or path.split("?", 1)[0].startswith(OPEN_PREFIX):
        return None
    allowed = frozenset(allowed)
    host_header = headers.get("Host") or ""
    if host_rule and not host_allowed(split_host(host_header)[0], allowed, own_name):
        return Refusal("host_not_allowed", "this server is not reached by that name: add it to server.allowed-hosts in the config file")
    origin = headers.get("Origin")
    if ANY_ORIGIN in allowed_origins:
        return None                                      # the person turned the Origin rule off: a page of another site is served like a program
    if origin is not None and method != "OPTIONS" and not origin_allowed(origin, host_header, allowed) and normal_origin(origin) not in allowed_origins:
        return Refusal("cross_origin", "a page of another site may not use this server")
    if method not in SAFE_METHODS and (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site" and normal_origin(origin or "") not in allowed_origins:
        return Refusal("cross_origin", "a page of another site may not use this server")
    return None
