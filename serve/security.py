"""The API key check and the exposure test for a server without an API key."""
from __future__ import annotations

import hmac


LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


def exposed_without_key(host: str, api_key: str) -> bool:
    """True when the server listens beyond this PC without an API key."""
    return host not in LOOPBACK_NAMES and not api_key


def api_key_of(value) -> str:
    """The key as a client can send it. An HTTP header loses the spaces and line ends around its value, so a key kept
    with them would match no request. A value that is only such characters raises ValueError: it never means "no key"."""
    key = "" if value is None else str(value)
    if key and not key.strip():
        raise ValueError("an API key was given but it is empty")
    return key.strip()


def key_matches(given: str, key: str) -> bool:
    """`given` is a header's value as http.server read it, each byte one character. A key with characters outside
    ASCII arrives as UTF-8 from most clients and as Latin-1 from some; the right key passes in both forms.
    Constant-time."""
    raw = given.encode("latin-1", "replace")
    ok = hmac.compare_digest(raw, key.encode())
    try:
        ok |= hmac.compare_digest(raw, key.encode("latin-1"))
    except UnicodeEncodeError:
        pass
    return ok


def lan_addresses() -> list[str]:
    """This PC's non-loopback, non-link-local IPv4 addresses."""
    import socket
    first, ips = None, set()
    try:                                                # default-route address; UDP connect sends nothing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            first = s.getsockname()[0]
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ok = lambda ip: ip and not ip.startswith(("127.", "169.254.", "0."))
    return ([first] if ok(first) else []) + sorted(ip for ip in ips if ok(ip) and ip != first)
