"""browse_url reads real pages (against a local fake web server) and refuses what it must; search_web exists only with a backend."""
import gzip
import ipaddress
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import harness.browser as browser
from harness.mcp_client import MCPClient, MCPHub
from harness.tools import create_standard_tool_registry
from harness.tools import search
from harness.tools.search import PageError, browse_url, clear_backends, register_backend

PAGE = """<!doctype html><html><head><title>Basalt &amp; friends</title><style>.x{}</style></head>
<body><script>alert('x')</script><h1>Heading</h1><p>Hello <a href="https://example.org/a">link</a></p>
<ul><li>one</li><li>two</li></ul></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="text/html; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path
        if path == "/page":
            self._send(200, PAGE.encode())
        elif path == "/gzip":
            self._send(200, gzip.compress(PAGE.encode()), headers={"Content-Encoding": "gzip"})
        elif path == "/text":
            self._send(200, b"plain words", "text/plain")
        elif path == "/png":
            self._send(200, b"\x89PNG....", "image/png")
        elif path == "/missing":
            self._send(404, b"nope")
        elif path == "/big":
            self._send(200, b"x" * 5_000_000, "text/plain")
        elif path == "/hop":
            self._send(302, b"", headers={"Location": "/page"})
        elif path == "/loop":
            self._send(302, b"", headers={"Location": "/loop"})
        elif path == "/to-metadata":
            self._send(302, b"", headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        elif path == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b"drip ")
                    self.wfile.flush()
                    time.sleep(0.25)
            except OSError:
                pass
        else:
            self._send(404, b"?")


@pytest.fixture()
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _clean():
    clear_backends()
    yield
    clear_backends()


def test_a_page_becomes_readable_text_marked_untrusted(site):
    page = browse_url(site + "/page", allow_private=True)
    assert page["title"] == "Basalt & friends" and page["status"] == 200
    assert "# Heading" in page["text"] and "[link](https://example.org/a)" in page["text"] and "- one" in page["text"]
    assert "alert" not in page["text"] and ".x{}" not in page["text"]
    assert page["untrusted"] is True and page["source"] == "web" and page["truncated"] is False


def test_gzip_and_plain_text_are_read(site):
    assert "# Heading" in browse_url(site + "/gzip", allow_private=True)["text"]
    assert browse_url(site + "/text", allow_private=True)["text"] == "plain words"


def test_this_pc_is_refused_unless_the_person_allowed_it(site):
    for allowed in (False, lambda: False):
        with pytest.raises(PageError, match="not allowed"):
            browse_url(site + "/page", allow_private=allowed)
    assert browse_url(site + "/page", allow_private=lambda: True)["title"]       # the setting is read at the moment of the call


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "http://user:pw@example.com/",
                                 "http://localhost/x", "http://[::1]/x", "http://169.254.169.254/", "http://100.64.0.1/", "http://10.0.0.5/"])
def test_other_schemes_credentials_and_non_global_addresses_are_refused(bad):
    with pytest.raises(PageError):
        browse_url(bad)


def test_the_address_rules():
    ok = lambda a: search._address_allowed(ipaddress.ip_address(a), False)
    assert ok("93.184.216.34") and ok("2606:4700::1111")
    for a in ("10.1.2.3", "172.16.0.1", "192.168.1.1", "127.0.0.1", "::1", "169.254.1.1", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::ffff:127.0.0.1", "fe80::1"):
        assert not ok(a), a
    assert search._address_allowed(ipaddress.ip_address("127.0.0.1"), True)


def test_a_redirect_is_followed_and_every_hop_is_checked(site, monkeypatch):
    assert browse_url(site + "/hop", allow_private=True)["final_url"].endswith("/page")
    with pytest.raises(PageError, match="redirects"):
        browse_url(site + "/loop", allow_private=True)
    # the first hop is allowed (this server), the next one points at the metadata address: refused before any connection
    real_check, real_allowed = browser.check_url, search._address_allowed
    monkeypatch.setattr(browser, "check_url", lambda url, allow_private=False: url if "127.0.0.1" in url else real_check(url, allow_private))
    monkeypatch.setattr(search, "_address_allowed", lambda ip, ap: str(ip) == "127.0.0.1" or real_allowed(ip, ap))
    assert browse_url(site + "/page")["title"]
    with pytest.raises(PageError, match="not allowed"):
        browse_url(site + "/to-metadata")


def test_a_name_is_connected_to_the_address_that_was_checked(monkeypatch):
    calls = []
    monkeypatch.setattr(search.socket, "getaddrinfo", lambda host, port, **kw: calls.append(host) or [(2, 1, 6, "", ("10.0.0.7", port))])
    monkeypatch.setattr(browser, "check_url", lambda url, allow_private=False: url)         # the name looked public to the first lookup
    with pytest.raises(PageError, match="not allowed"):
        browse_url("http://rebind.example/x")                                                # the connection itself checks again
    assert calls == ["rebind.example"]


def test_errors_binary_pages_and_size_and_time_caps(site, monkeypatch):
    with pytest.raises(PageError, match="404"):
        browse_url(site + "/missing", allow_private=True)
    with pytest.raises(PageError, match="image/png"):
        browse_url(site + "/png", allow_private=True)
    big = browse_url(site + "/big", allow_private=True)
    assert big["truncated"] is True and len(big["text"]) <= search.MAX_PAGE_CHARS
    monkeypatch.setattr(search, "PAGE_SECONDS", 1.0)
    started = time.monotonic()
    with pytest.raises(PageError, match="too long|could not be read"):
        browse_url(site + "/slow", allow_private=True)
    assert time.monotonic() - started < 6


def test_the_tool_is_always_offered_and_search_only_with_a_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    registry, _ = create_standard_tool_registry(tmp_path)
    assert registry.get("browse_url") is not None and registry.get("browse_url").untrusted
    assert registry.get("search_web") is None                              # no backend: the model is not told of a tool that cannot work
    register_backend("local_mock", search=lambda q, n: [{"title": q, "snippet": "s"}])
    registry, _ = create_standard_tool_registry(tmp_path)
    tool = registry.get("search_web")
    assert tool is not None and tool.untrusted and tool.parameters["properties"]["provider"]["enum"] == ["local_mock"]
    assert registry.execute("search_web", {"query": "sm_120"})[0]["untrusted"] is True


def test_the_browse_tool_follows_the_browser_setting(tmp_path, site):
    registry, _ = create_standard_tool_registry(tmp_path)
    with pytest.raises(PageError):
        registry.execute("browse_url", {"url": site + "/page"})
    registry, _ = create_standard_tool_registry(tmp_path, allow_private_pages=True)
    assert registry.execute("browse_url", {"url": site + "/page"})["title"] == "Basalt & friends"


# -- search backends -------------------------------------------------------------------------------------------

FAKE = str(Path(__file__).resolve().parent / "fake_search_mcp.py")


def _providers(tmp_path, monkeypatch, command=None):
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps({"endpoints": [
        {"id": "fakesearch", "capability": "search", "transport": "stdio", "auth_env": "FAKE_SEARCH_KEY", "network_egress": True,
         "command": command if command is not None else [sys.executable, FAKE]}]}), encoding="utf-8")
    monkeypatch.setattr(search, "CONFIG_FILE", cfg)


def test_a_key_in_the_environment_connects_a_provider_through_its_mcp_server(tmp_path, monkeypatch):
    _providers(tmp_path, monkeypatch)
    monkeypatch.delenv("FAKE_SEARCH_KEY", raising=False)
    registry, _ = create_standard_tool_registry(tmp_path)
    assert registry.get("search_web") is None                              # no key, no tool
    monkeypatch.setenv("FAKE_SEARCH_KEY", "k-env")
    registry, _ = create_standard_tool_registry(tmp_path)
    assert registry.get("search_web").parameters["properties"]["provider"]["enum"] == ["fakesearch"]
    hits = registry.execute("search_web", {"query": "sm_120 kernels", "limit": 3})
    assert hits[0]["snippet"] == "results for sm_120 kernels limit=3 key=k-env" and hits[0]["untrusted"] is True and hits[0]["source"] == "fakesearch"
    hits = registry.execute("search_web", {"query": "again"})
    assert "again" in hits[0]["snippet"]


def test_a_provider_whose_server_program_is_missing_is_not_offered(tmp_path, monkeypatch):
    _providers(tmp_path, monkeypatch, command=["no-such-program-anywhere"])
    monkeypatch.setenv("FAKE_SEARCH_KEY", "k")
    registry, _ = create_standard_tool_registry(tmp_path)
    assert registry.get("search_web") is None


class FakeVault:
    def __init__(self, entries):
        self.entries = entries

    def reveal(self, entry_id, field):
        if entry_id not in self.entries or field != "value":
            raise KeyError(entry_id)
        return self.entries[entry_id]


class FakeSecrets:
    def __init__(self, vault):
        self.vault = vault


def test_a_key_in_a_vault_entry_named_after_the_provider_is_enough(tmp_path, monkeypatch):
    _providers(tmp_path, monkeypatch)
    monkeypatch.delenv("FAKE_SEARCH_KEY", raising=False)
    registry, _ = create_standard_tool_registry(tmp_path, secrets=FakeSecrets(FakeVault({"other": "x"})))
    assert registry.get("search_web") is None
    registry, _ = create_standard_tool_registry(tmp_path, secrets=FakeSecrets(FakeVault({"fakesearch": "k-vault"})))
    assert "key=k-vault" in registry.execute("search_web", {"query": "q"})[0]["snippet"]


def test_a_connected_mcp_server_with_a_search_tool_becomes_a_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "CONFIG_FILE", tmp_path / "none.json")
    client = MCPClient([sys.executable, FAKE], env={"FAKE_SEARCH_KEY": "k-hub"})
    hub = MCPHub()
    hub.add("web", client)
    hub.connect_all()
    try:
        assert search.pick_search_tool(hub.catalog["web"]) == ("web_search", "query", "max_results")
        registry, _ = create_standard_tool_registry(tmp_path, mcp_hub=hub)
        assert registry.get("search_web").parameters["properties"]["provider"]["enum"] == ["mcp:web"]
        assert "key=k-hub" in registry.execute("search_web", {"query": "q"})[0]["snippet"]
        empty = MCPHub()
        assert search.hub_backends(empty) == {}
    finally:
        hub.close()
