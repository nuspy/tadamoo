import pytest

from tadamoo import netscope
from tadamoo.a2a import A2AClient, A2AError


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "10.1.2.3", "172.20.0.5", "192.168.1.58", "100.84.254.116", "169.254.1.1", "fd00::1", "::ffff:192.168.1.5"])
def test_addresses_of_the_private_network(address):
    assert netscope.is_private_ip(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "100.128.0.1", "172.32.0.1", "2001:4860:4860::8888", "not-an-address"])
def test_addresses_of_the_open_internet_are_not(address):
    assert not netscope.is_private_ip(address)


def test_a_name_counts_only_when_every_address_it_has_is_private(monkeypatch):
    answers = {"nas.lan": ["192.168.1.58"], "mixed.example": ["192.168.1.9", "93.184.216.34"], "ghost.example": []}
    monkeypatch.setattr(netscope.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", (a, 0)) for a in answers[host]])
    assert netscope.is_private_host("nas.lan")
    assert not netscope.is_private_host("mixed.example") and not netscope.is_private_host("ghost.example")


def test_an_agent_outside_the_network_is_not_reached(monkeypatch):
    called = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: called.append(a))
    with pytest.raises(A2AError, match="outside the private network"):
        A2AClient("https://8.8.8.8/agent")._http("GET", "https://8.8.8.8/agent/.well-known/agent-card.json")
    assert not called
