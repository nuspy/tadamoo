"""The browser keeps a page on the internet from reaching this PC or its network, through redirects, subresources and frames, and does not type
a value from the vault where it would cross the network in the clear."""
import http.server
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness.browser as hb
from harness.browser import Browser, BrowserError, blocked_host, find_browser, register_browser_tools
from harness.secrets_broker import SecretsBroker
from harness.tools.registry import ToolRegistry
from harness.vault import Vault

# the frame tests of test_browser_frames, run again with the guard on (a page there is "on the internet")
from test_browser_frames import (  # noqa: F401  (collected here, with the fixtures below)
    Site, site, test_a_card_field_in_a_frame_of_another_site_is_read_typed_into_and_sent,
    test_a_frame_of_the_same_site_is_part_of_the_page, test_a_value_goes_to_the_frames_own_site_not_the_pages,
    test_a_window_the_page_opens_is_followed_and_the_page_is_back_when_it_closes)

needs_browser = pytest.mark.skipif(find_browser() is None, reason="needs Edge or Chrome on this PC")


def test_an_address_that_is_not_on_the_internet_is_blocked():
    for host in ("127.0.0.1", "10.1.2.3", "192.168.0.7", "169.254.1.1", "100.84.254.116", "0.0.0.0", "::1", "fe80::1", "::ffff:127.0.0.1"):
        assert blocked_host(host) is True, host
    assert blocked_host("8.8.8.8") is False
    assert blocked_host("no-such-host.invalid") is None


def test_the_guard_lets_the_internet_through_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "blocked_host", lambda host, port=443: host in ("127.0.0.1", "intranet"))
    b = Browser(tmp_path / "p", allow_private=False)
    assert b._request_ok("https://shop.example/pay") and b._request_ok("data:text/plain,hi") and b._request_ok("wss://shop.example/s")
    assert not b._request_ok("http://127.0.0.1:8080/admin") and not b._request_ok("http://intranet/x")
    assert not b._request_ok("file:///C:/Windows/win.ini") and not b._request_ok("ftp://shop.example/x")
    assert b.blocked == ["127.0.0.1", "intranet"]


def test_a_plain_http_page_on_the_internet_does_not_get_a_value_from_the_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "blocked_host", lambda host, port=443: host.startswith("192.168."))
    b = Browser(tmp_path / "p")
    b.page_url = "http://shop.example/login"
    assert b.insecure_public("shop.example") and not b.insecure_public("192.168.1.1")
    b.page_url = "https://shop.example/login"
    assert not b.insecure_public("shop.example")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "blocked_host", lambda host, port=443: host == "blocked.test")
    monkeypatch.setattr(Browser, "insecure_public", lambda self, host: False)        # the test pages are plain http; that rule has its own tests
    vault = Vault(tmp_path / "vault")
    secrets = SecretsBroker()
    secrets.attach_vault(vault)
    registry = ToolRegistry()
    browser = register_browser_tools(registry, secrets, profile_dir=tmp_path / "profile", headless=True, allow_private=False)
    yield registry, vault, browser
    browser.close()


class Pixel(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Pixel.hits.append(self.path)
        body = b"<!doctype html><title>P</title><h1>Page</h1><img src='http://blocked.test:1/pixel.png'><p>done</p>" if self.path == "/page" else b"x"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@needs_browser
def test_a_request_to_a_blocked_address_never_leaves_and_the_page_still_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "blocked_host", lambda host, port=443: host == "blocked.test")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pixel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    b = Browser(tmp_path / "p", headless=True, allow_private=False)
    try:
        page = b.open(f"http://127.0.0.1:{server.server_address[1]}/page")
        assert "done" in page["text"]
        assert b.blocked == ["blocked.test"]
    finally:
        b.close()
        server.shutdown()


@needs_browser
def test_a_page_that_is_blocked_says_so_to_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "blocked_host", lambda host, port=443: host == "127.0.0.1")
    b = Browser(tmp_path / "p", headless=True, allow_private=False)
    try:
        with pytest.raises(BrowserError, match="inside this PC or its network"):
            b.open("http://127.0.0.1:9/")
    finally:
        b.close()


@needs_browser
def test_a_field_that_took_a_value_from_the_vault_is_never_listed_with_it(agent, site):
    from test_browser_frames import ref_of
    registry, vault, browser = agent
    vault.put("card", kind="card", fields={"number": "4242 4242 4242 4242"}, hosts=["127.0.0.1"], guard=False)
    page = registry.execute("browser_open", {"url": site + "/card"})
    registry.execute("browser_type", {"ref": ref_of(page, "Card number"), "text": "{{vault:card.number}}"})
    browser._js("(() => { const i = document.querySelector('input[name=card]'); i.value = i.value.replace(/ /g, '-'); })()")   # the page reshapes it
    again = registry.execute("browser_read", {})
    assert "4242" not in again and "value=(set)" in again
