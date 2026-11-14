"""Where the person is, as far as the network says: at this machine, or on another device and at what address (docs/remote-devices.md).

For each request the server knows who connected (`client_address`) and the `Host` it was asked at. A client at this machine (a loopback address, or one of this machine's own
addresses) is local. Any other client is on another device, and the base address to give it is, in this order: the Host it used (it reached the server there, so it works for it), the
Tailscale address of this machine, a LAN address of this machine. The answer is `""` for a local client, else `http://host:port`.
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

    def client_base(self, client_ip: str, host_header: str, port: int) -> str:
        """`""` for a client at this machine, else the base address that client reaches the server at (see the module's words)."""
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


def client_base(client_ip: str, host_header: str, port: int) -> str:
    return DEFAULT.client_base(client_ip, host_header, port)


def server_base(port: int) -> str:
    return DEFAULT.server_base(port)
