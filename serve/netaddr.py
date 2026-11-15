"""Where the person is, as far as the network says: at this machine, or on another device and at what address (docs/remote-devices.md).

For each request the server knows who connected (`client_address`) and the `Host` it was asked at. A client at this machine (a loopback address, or one of this machine's own
addresses) is local. Any other client is on another device, and the base address to give it is, in this order: the Host it used (it reached the server there, so it works for it), the
Tailscale address of this machine, a LAN address of this machine. The answer is `""` for a local client, else `http://host:port`.

Behind a reverse proxy the server sees the proxy as the client. A request that comes from a loopback or private-network address (a proxy beside the server) and carries `X-Forwarded-Proto`
and/or `X-Forwarded-Host` says how the person reached the server: the base is then `scheme://host` of what the proxy forwarded (the port only when the forwarded host names one), and the
person is not at this machine. The same headers from any other address are ignored: anybody can send them.
"""
from __future__ import annotations

import ipaddress
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from tadamoo.netaddr import is_loopback_host, split_hostport, valid_base

OWN_TTL_S = 60.0


def _ip(value: str) -> Any:
    try:
        return ipaddress.ip_address((value or "").strip().split("%")[0])
    except ValueError:
        return None


class Network:
    """This machine's own addresses (Tailscale's and the LAN's), asked for at most once a minute: asking Tailscale starts a program."""

    def __init__(self, tailscale: Optional[Callable[[], Dict[str, Any]]] = None, lan: Optional[Callable[[], List[str]]] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._tailscale, self._lan, self._clock = tailscale, lan, clock
        self._at = -1e9
        self._own: Dict[str, Any] = {"tailscale": "", "lan": []}
        self._lock = threading.Lock()

    def _look(self) -> Dict[str, Any]:
        with self._lock:
            if self._clock() - self._at > OWN_TTL_S:
                tailscale, lan = self._tailscale, self._lan
                if tailscale is None:
                    from serve.remote import tailscale_status as tailscale
                if lan is None:
                    from serve.security import lan_addresses as lan
                try:
                    ip = str((tailscale() or {}).get("ip") or "")
                except Exception:
                    ip = ""
                try:
                    found = [str(a) for a in lan()]
                except Exception:
                    found = []
                cgnat = ipaddress.ip_network("100.64.0.0/10")                 # Tailscale's range: an interface in it is Tailscale's even when its program is not found
                ip = ip or next((a for a in found if (_ip(a) is not None and _ip(a).version == 4 and _ip(a) in cgnat)), "")
                self._own, self._at = {"tailscale": ip, "lan": [a for a in found if a != ip]}, self._clock()
            return dict(self._own)

    def own(self) -> List[str]:
        found = self._look()
        return [a for a in [found["tailscale"], *found["lan"]] if a]

    def server_base(self, port: int) -> str:
        """The address of this server for someone elsewhere: Tailscale's, else the LAN's; "" when this machine has neither."""
        found = self._look()
        host = found["tailscale"] or next(iter(found["lan"]), "")
        return f"http://{host}:{port}" if host else ""

    def client_base(self, client_ip: str, host_header: str, port: int, fwd: Optional[Dict[str, str]] = None) -> str:
        """`""` for a client at this machine, else the base address that client reaches the server at (see the module's words). `fwd` is what `forwarded` found."""
        fwd = fwd or {}
        if fwd.get("proto") or fwd.get("host"):                         # a proxy beside the server says how the person came: its name and scheme, with no port of ours
            host, given_port = split_hostport(fwd.get("host") or host_header)
            if host and not is_loopback_host(host):
                return valid_base(f"{fwd.get('proto') or 'http'}://{host}{':' + given_port if given_port else ''}")
        ip = _ip(client_ip)
        if ip is None or ip.is_loopback or ip.is_unspecified:
            return ""
        if str(ip) in {str(_ip(a)) for a in self.own()}:                # a browser at this machine that used the machine's own network address
            return ""
        host, given_port = split_hostport(host_header)
        if host and not is_loopback_host(host):
            return valid_base(f"http://{host}:{given_port or port}")
        return self.server_base(port)


DEFAULT = Network()


def client_base(client_ip: str, host_header: str, port: int, fwd: Optional[Dict[str, str]] = None) -> str:
    return DEFAULT.client_base(client_ip, host_header, port, fwd)


def server_base(port: int) -> str:
    return DEFAULT.server_base(port)


_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_SCHEMES = ("http", "https")


def trusted_proxy(client_ip: str) -> bool:
    """A proxy beside the server: a loopback address, one of a private network, or Tailscale's range. Only such a peer is believed about how the person reached the server."""
    ip = _ip(client_ip)
    return ip is not None and (ip.is_loopback or ip.is_private or (ip.version == 4 and ip in _CGNAT))


def forwarded(headers: Any, client_ip: str) -> Dict[str, str]:
    """`{"proto": ..., "host": ...}` from `X-Forwarded-Proto` and `X-Forwarded-Host` (the first value of each, the proxy's own) when the request comes from a trusted proxy; each is empty
    when it is not there or is not a scheme (http, https) or a host. Nothing is believed from any other peer."""
    out = {"proto": "", "host": ""}
    if headers is None or not trusted_proxy(client_ip):
        return out
    proto = str(headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    host = str(headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
    if proto in _SCHEMES:
        out["proto"] = proto
    if split_hostport(host)[0]:
        out["host"] = host
    return out
