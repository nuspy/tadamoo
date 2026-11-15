"""The absolute links the server writes behind a reverse proxy (serve/netaddr.py): the scheme and host the proxy forwarded are used, only when the request comes from a proxy beside the
server (loopback, a private network, Tailscale's range); the same headers from any other address are ignored."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import netaddr as net
from test_remote_devices import TS, network


class Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "10.0.0.7", "172.16.4.4", "192.168.1.9", "100.64.0.3", "fd00::5"])
def test_a_proxy_beside_the_server_is_believed(peer):
    got = net.forwarded(Headers({"X-Forwarded-Proto": "HTTPS", "X-Forwarded-Host": "tadamoo.example.org"}), peer)
    assert got == {"proto": "https", "host": "tadamoo.example.org"}


@pytest.mark.parametrize("peer", ["8.8.8.8", "1.1.1.1", "2606:4700::1111", "", "not an address"])
def test_the_same_headers_from_anywhere_else_are_ignored(peer):
    assert net.forwarded(Headers({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"}), peer) == {"proto": "", "host": ""}


def test_only_the_first_value_a_scheme_and_a_host_count():
    h = Headers({"X-Forwarded-Proto": "https, http", "X-Forwarded-Host": "a.example.org, b.example.org"})
    assert net.forwarded(h, "127.0.0.1") == {"proto": "https", "host": "a.example.org"}
    assert net.forwarded(Headers({"X-Forwarded-Proto": "javascript", "X-Forwarded-Host": "bad host\r\nX: y"}), "127.0.0.1") == {"proto": "", "host": ""}
    assert net.forwarded(Headers({}), "127.0.0.1") == {"proto": "", "host": ""} and net.forwarded(None, "127.0.0.1") == {"proto": "", "host": ""}


def test_behind_https_the_base_is_the_public_name_with_its_scheme_and_without_our_port():
    n = network()
    https = {"proto": "https", "host": "tadamoo.example.org"}
    assert n.client_base("127.0.0.1", "127.0.0.1:7232", 7232, https) == "https://tadamoo.example.org"          # the proxy is at this machine: the person is not
    assert n.client_base("192.168.1.2", "tadamoo.example.org", 7232, https) == "https://tadamoo.example.org"
    assert n.client_base("127.0.0.1", "tadamoo.example.org", 7232, {"proto": "https", "host": ""}) == "https://tadamoo.example.org"      # only the scheme was forwarded: the Host is the name
    assert n.client_base("127.0.0.1", "tadamoo.example.org:8443", 7232, {"proto": "https", "host": ""}) == "https://tadamoo.example.org:8443"
    assert n.client_base("127.0.0.1", "x", 7232, {"proto": "http", "host": "pluto.tail1.ts.net:7232"}) == "http://pluto.tail1.ts.net:7232"
    assert n.client_base("127.0.0.1", "x", 7232, {"proto": "", "host": "tadamoo.example.org"}) == "http://tadamoo.example.org"


def test_without_forwarded_headers_nothing_changes_and_a_loopback_name_is_not_used():
    n = network()
    assert n.client_base("127.0.0.1", "localhost:7232", 7232) == "" and n.client_base("127.0.0.1", "localhost:7232", 7232, {"proto": "", "host": ""}) == ""
    assert n.client_base("100.64.0.9", "100.84.1.2:7232", 7232) == "http://100.84.1.2:7232"
    assert n.client_base("127.0.0.1", "localhost:7232", 7232, {"proto": "https", "host": "localhost"}) == ""            # a proxy that forwards a loopback name tells nothing
    assert n.client_base("100.64.0.9", "", 7232, {"proto": "https", "host": ""}) == f"http://{TS}:7232"                 # the scheme alone, with no name: the machine's own address
