"""Addresses for a person who is not at the machine the server runs on (docs/remote-devices.md).

An address that names this machine (`localhost`, `127.0.0.1`, `0.0.0.0`, a path on its disk) means nothing to a phone or another PC. For such a person every link to something
the server holds has to be the address they reach the server at. This module holds the pure part: what a loopback host is, what a usable base address looks like, how URLs in a text
that point at this machine are turned into the base, and the words the model is told. Where the base comes from (the Host the client used, else Tailscale, else the LAN) is
`serve/netaddr.py`; the web page (serve/web/netaddr.js) and the Android app (mobile/lib/src/net_address.dart) do the same rewriting on what they show.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Optional, Tuple

_HOST = r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*)"
BASE_RE = re.compile(rf"^(https?)://({_HOST})(?::(\d{{1,5}}))?$")
URL_RE = re.compile(rf"\b(https?|wss?)://({_HOST})(:\d{{1,5}})?((?:[/?#][^\s<>()\[\]\"'`]*)?)")


def is_loopback_host(host: str) -> bool:
    """A host that means this machine whoever reads it: localhost, a loopback or unspecified address (`0.0.0.0` in a message about a server that was started is the same thing)."""
    host = (host or "").strip().lower().strip("[]")
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def split_hostport(value: str) -> Tuple[str, str]:
    """(host, port) of a `Host` header or the authority of a URL; both empty when it is not one."""
    m = re.fullmatch(rf"({_HOST})(?::(\d{{1,5}}))?", (value or "").strip())
    return (m.group(1), m.group(2) or "") if m else ("", "")


def valid_base(value: str) -> str:
    """`http://host[:port]` as it can be told to a model and written in a link, or "" (anything else: a path, a query, characters that are not a host)."""
    m = BASE_RE.match((value or "").strip().rstrip("/"))
    if not m:
        return ""
    port = m.group(3)
    return f"{m.group(1)}://{m.group(2)}{':' + port if port else ''}"


def rewrite_urls(text: str, base: str) -> str:
    """The URLs in `text` whose host is this machine, with the host of `base` instead (the port and the rest as written). With no usable base, or a base that is itself this machine, the text is
    returned as it is."""
    base = valid_base(base)
    if not base or not text:
        return text
    host = BASE_RE.match(base).group(2)
    if is_loopback_host(host):
        return text

    def swap(m: "re.Match[str]") -> str:
        return f"{m.group(1)}://{host}{m.group(3) or ''}{m.group(4)}" if is_loopback_host(m.group(2)) else m.group(0)
    return URL_RE.sub(swap, text)


def note_words(base: str) -> str:
    """What the model is told when the person is on another device (the field `address` of the `[state]` note, tadamoo/capstate.py), or when they are back."""
    if not base:
        return "none (the person is at this machine: local addresses and paths work)"
    return (f"{base} (the person is on another device and reaches this server there: write every link to a file, a picture or a page this server serves as a full address "
            "of this one, never localhost, 127.0.0.1, 0.0.0.0, a relative path or a path on this machine's disk)")


def host_of(base: str) -> Optional[str]:
    m = BASE_RE.match(valid_base(base)) if base else None
    return m.group(2) if m else None
