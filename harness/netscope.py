"""Whether an address is inside the person's own network: this PC, the home or office network, a Tailscale-style private overlay.

Agents that give Basalt work or answers are accepted only from there. A name is looked up and every address it has must be private."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_SHARED = ipaddress.ip_network("100.64.0.0/10")          # carrier-grade NAT space, used by private overlays such as Tailscale


class OutsideNetwork(ValueError):
    pass


def is_private_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local or ip in _SHARED)


def is_private_host(host: str) -> bool:
    """True when the host is an address of the private network, or a name whose every address is."""
    host = (host or "").strip("[]")
    if not host:
        return False
    if is_private_ip(host):
        return True
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(found) and all(is_private_ip(a) for a in found)


def require_private(url: str, what: str = "this agent") -> None:
    """Raises `OutsideNetwork` when the address of `url` is not in the person's own network."""
    host = urlsplit(url).hostname or ""
    if not is_private_host(host):
        raise OutsideNetwork(f"{what} is at {host or 'no address'}, outside the private network: instructions from agents are accepted only from inside it.")
