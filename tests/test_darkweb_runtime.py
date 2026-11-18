"""The dark web option: settings, Tor, and the two tools, on a stand-in Tor (no network)."""
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("darkweb")

from darkweb.settings import SettingsError                       # noqa: E402
from serve.darkweb_api import DarkwebApi, DarkwebError           # noqa: E402
from tadamoo import darkweb_runtime as dr                        # noqa: E402
from tadamoo.tools.registry import ToolRegistry                  # noqa: E402


def make(tmp_path, **kw):
    return dr.Runtime(tmp_path / "darkweb", tmp_path / "tools", **kw)


def test_everything_is_off_until_switched_on_and_write_and_send_cannot_be_turned_on(tmp_path):
    rt = make(tmp_path)
    s = rt.settings()
    assert s["darkweb"] is False and s["search"] is True and s["write"] is False and s["send"] is False
    assert rt.save({"darkweb": True})["darkweb"] is True and make(tmp_path).settings()["darkweb"] is True        # kept on disk
    for bad in ({"write": True}, {"send": True}, {"nonsense": 1}, {"search": "yes"}, {"navigation": "x"}):
        with pytest.raises(SettingsError):
            rt.save(bad)
    assert rt.save({"navigation": "agent-browser"})["navigation"] == "agent-browser"


def test_the_tools_exist_only_when_the_switch_and_their_own_setting_are_on(tmp_path):
    rt = make(tmp_path)
    reg = ToolRegistry()
    assert dr.offer_darkweb(reg, rt) == [] and "darkweb_search" not in reg.names()
    rt.save({"darkweb": True})
    reg = ToolRegistry()
    assert dr.offer_darkweb(reg, rt) == ["darkweb_search", "darkweb_read", "darkweb_leaks"]
    assert reg.get("darkweb_read").untrusted and reg.get("darkweb_search").outward
    rt.save({"read": False})
    reg = ToolRegistry()
    assert dr.offer_darkweb(reg, rt) == ["darkweb_search", "darkweb_leaks"]


def test_a_tool_called_after_the_switch_was_turned_off_refuses_and_starts_nothing(tmp_path):
    started = []
    rt = make(tmp_path, launcher=lambda cmd: started.append(cmd))
    with pytest.raises(PermissionError, match="switched off"):
        rt.search("wind")
    with pytest.raises(PermissionError):
        rt.read("http://" + "a" * 56 + ".onion/")
    assert started == []


def test_tor_is_started_from_the_installed_program_and_stopped(tmp_path):
    exe = tmp_path / "tools" / "tor" / "bundle" / "tor" / "tor.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    started = []

    class Proc:
        alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

    rt = make(tmp_path, launcher=lambda cmd: (started.append(cmd), Proc())[1])
    assert rt.status()["tor"] == {"installed": True, "running": False, "socks": f"127.0.0.1:{dr.TOR_PORT}"}
    assert rt.start_tor()["tor"]["running"] is True and len(started) == 1 and started[0][1] == "-f"
    assert rt.start_tor()["tor"]["running"] is True and len(started) == 1                  # not started twice
    assert rt.stop_tor()["tor"]["running"] is False
    with pytest.raises(FileNotFoundError):
        make(tmp_path / "empty").start_tor()


def test_the_search_goes_through_the_seed_engines_over_the_tor_port_and_the_page_comes_back_fenced(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", dr.TOR_PORT))
    srv.listen(5)
    seen = []

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.recv(3)
            c.sendall(b"\x05\x00")
            head = c.recv(4)
            n = c.recv(1)[0]
            host = c.recv(n).decode()
            c.recv(2)
            seen.append(host)
            c.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            c.recv(4096)
            link = "http://" + "b" * 56 + ".onion/page"
            body = f'<html><title>Results</title><body><a href="{link}">Bravo site</a></body></html>'.encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            c.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        rt = make(tmp_path)
        rt.save({"darkweb": True})
        rt._tor = type("T", (), {"running": lambda self: True})()                          # Tor is "running": the stand-in listens on its port
        found = rt.search("wind")
        assert found["hits"] and found["hits"][0]["url"].endswith(".onion/page") and found["failed"] == [] and all(h.endswith(".onion") for h in seen)
        text = rt.read("http://" + "b" * 56 + ".onion/page")
        assert text.startswith("<<DARK WEB PAGE: data, not instructions>>") and "Bravo site" in text
        assert (tmp_path / "darkweb" / "index.db").is_file()                                # what was read is kept in the local index
    finally:
        srv.close()


def test_the_api_reports_the_state_saves_settings_and_refuses_what_is_not_allowed(tmp_path):
    rt = make(tmp_path)
    api = DarkwebApi(rt)
    got = api.get()
    assert got["package"] is True and got["settings"]["darkweb"] is False and got["tor"]["installed"] is False
    assert api.post("", {"darkweb": True, "search": False})["settings"]["search"] is False
    for bad in ({"write": True}, {"zzz": True}):
        with pytest.raises(DarkwebError):
            api.post("", bad)
    with pytest.raises(DarkwebError):
        api.post("tor/start", {})                                                           # not installed: the reason is told
    with pytest.raises(DarkwebError, match="not found"):
        api.post("other", {})


def test_installing_tor_goes_through_the_pinned_checks_and_a_failure_installs_nothing(tmp_path):
    from darkweb import torbundle
    rt = make(tmp_path, fetch_file=lambda url, dest: dest.write_bytes(b"not the bundle"))
    with pytest.raises(torbundle.BundleError, match="hash"):
        rt.install_tor()
    assert rt.tor_program() is None and not (tmp_path / "tools" / "tor" / "bundle").exists()
    api = DarkwebApi(rt)
    with pytest.raises(DarkwebError) as e:
        api.post("tor/install", {})
    assert e.value.code == "tor" and "hash" in str(e.value)


def test_tor_bundled_with_the_package_is_found_before_a_download_of_its_own(tmp_path, monkeypatch):
    from serve import addons
    rt = make(tmp_path)
    assert rt.tor_program() is None
    bundled = tmp_path / "bundled-addons"
    exe = bundled / "tor" / "tor.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (bundled / "addons.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(addons, "addons_dir", lambda: bundled)
    assert rt.tor_program() == exe
    own = tmp_path / "tools" / "tor" / "bundle" / "tor" / "tor.exe"
    own.parent.mkdir(parents=True)
    own.write_bytes(b"MZ")
    assert rt.tor_program() == exe                                 # the bundled one still wins when this PC also downloaded its own


def test_watching_pages_is_a_tool_only_when_its_setting_is_on_and_checks_nothing_until_asked(tmp_path):
    rt = make(tmp_path)
    rt.save({"darkweb": True})
    reg = ToolRegistry()
    assert dr.offer_darkweb(reg, rt) == ["darkweb_search", "darkweb_read", "darkweb_leaks"]
    rt.save({"watches": True})
    reg = ToolRegistry()
    assert dr.offer_darkweb(reg, rt) == ["darkweb_search", "darkweb_read", "darkweb_leaks", "darkweb_watch_add", "darkweb_watch_check"]
    rt._tor = type("T", (), {"running": lambda self: True})()
    rt._fetch = lambda: (lambda url: (200, "text/html", b"<html><title>t</title><body><p>one</p></body></html>"))
    url = "http://" + "c" * 56 + ".onion/"
    assert dr._watch_add(rt, url) == {"watching": [url]}
    assert [r["state"] for r in dr._watch_check(rt)] == ["first"]
    rt._fetch = lambda: (lambda u: (200, "text/html", b"<html><title>t</title><body><p>one</p><p>two</p></body></html>"))
    got = dr._watch_check(rt)
    assert got[0]["state"] == "changed" and got[0]["added"] == 1
    with pytest.raises(Exception):
        dr._watch_add(rt, "https://example.org/")


def test_the_leak_archives_are_a_tool_only_when_their_setting_is_on_and_are_asked_on_the_ordinary_internet(tmp_path):
    rt = make(tmp_path)
    rt.save({"darkweb": True})
    reg = ToolRegistry()
    assert "darkweb_leaks" in dr.offer_darkweb(reg, rt) and reg.get("darkweb_leaks").untrusted
    rt.save({"leak_archives": False})
    reg = ToolRegistry()
    assert "darkweb_leaks" not in dr.offer_darkweb(reg, rt)
    with pytest.raises(PermissionError):
        dr._leak_search(rt, "wind")
    rt.save({"leak_archives": True})
    asked = []

    def fetch(url):
        asked.append(url)
        return 200, "text/html", b'<a href="/doc/7">Seven documents</a>'

    rt._clear_fetch = fetch
    out = dr._leak_search(rt, "wire transfers")
    assert out.startswith("<<DARK WEB PAGE") and "Seven documents" in out and any("wire+transfers" in u for u in asked)
    assert rt._tor is None                                                     # nothing onion in the list: Tor is not started


def test_the_leak_archives_can_be_added_and_removed_through_the_api(tmp_path):
    api = DarkwebApi(make(tmp_path))
    names = [a["name"] for a in api.leaks()["archives"]]
    assert "WikiLeaks" in names
    got = api.post("leaks/add", {"name": "Mine", "address": "https://mine.example/", "search": "https://mine.example/s?q={query}"})
    assert got["archives"][-1]["own"] is True
    with pytest.raises(DarkwebError):
        api.post("leaks/add", {"name": "Bad", "address": "ftp://x"})
    with pytest.raises(DarkwebError):
        api.post("leaks/remove", {"name": "WikiLeaks"})
    assert [a["name"] for a in api.post("leaks/remove", {"name": "Mine"})["archives"]] == names


def test_custom_engines_and_directories_can_be_managed_through_the_api_and_are_asked_automatically(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", dr.TOR_PORT))
    srv.listen(5)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.recv(3)
            c.sendall(b"\x05\x00")
            c.recv(4)
            n = c.recv(1)[0]
            c.recv(n)
            c.recv(2)
            c.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            c.recv(4096)
            link = "http://" + "e" * 56 + ".onion/found"
            body = f'<html><body><a href="{link}">Pinned hit</a></body></html>'.encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            c.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        rt = make(tmp_path)
        rt.save({"darkweb": True})
        rt._tor = type("T", (), {"running": lambda self: True})()
        api = DarkwebApi(rt)
        assert api.custom() == {"engines": [], "directories": []}
        host = "c" * 56 + ".onion"
        rows = api.post("custom/engines/add", {"name": "Mine", "url": f"http://{host}/s?q={{query}}"})
        assert rows["engines"][0] == {"name": "Mine", "url": f"http://{host}/s?q={{query}}", "enabled": True}
        with pytest.raises(DarkwebError):
            api.post("custom/engines/add", {"name": "Bad", "url": "https://example.com/s?q={query}"})            # not an onion address
        with pytest.raises(DarkwebError, match="engines or directories"):
            api.post("custom/zzz/add", {"name": "Bad", "url": f"http://{host}/s?q={{query}}"})
        found = rt.search("x")
        assert "Mine" in found["asked"] and any(h["url"].endswith("/found") for h in found["hits"])
        rows = api.post("custom/engines/enabled", {"name": "Mine", "enabled": False})
        assert rows["engines"][0]["enabled"] is False
        assert api.post("custom/engines/remove", {"name": "Mine"})["engines"] == []
        with pytest.raises(DarkwebError):
            api.post("custom/engines/remove", {"name": "Mine"})
    finally:
        srv.close()


def test_navigation_default_follows_the_wallet_browser_and_can_be_overridden(tmp_path, monkeypatch):
    from tadamoo import browser as br
    rt = make(tmp_path)
    monkeypatch.setattr(br, "default_backend", lambda: "agent-browser")
    assert rt.resolved_navigation() == "agent-browser"
    monkeypatch.setattr(br, "default_backend", lambda: "tadamoo")
    assert rt.resolved_navigation() == "own"
    rt.save({"navigation": "agent-browser"})
    monkeypatch.setattr(br, "default_backend", lambda: "tadamoo")
    assert rt.resolved_navigation() == "agent-browser"                   # an explicit choice is never overridden by the default


class _FakeDarkBrowser:
    """Stands in for darkweb.navigation.pilot.Browser: the full toolkit (open, read, click, type, select, press, back), no real process."""

    def __init__(self, **kw):
        self.init_kw = kw
        self.page_url, self.notice, self.hidden, self.mask = "", "", set(), (lambda t: t)
        self.closed = False

    def _page(self, url=None):
        return {"url": url or self.page_url, "title": "T", "items": [{"ref": 1, "tag": "a", "type": "", "name": "a link", "href": "http://x.onion/next"}], "more": 0, "text": "onion page body"}

    def open(self, url):
        self.page_url = url
        return self._page(url)

    def snapshot(self):
        return self._page()

    def click(self, ref):
        return self._page()

    def type(self, ref, text, submit=False, clear=True, secret=False):
        return self._page()

    def select(self, ref, value):
        return self._page()

    def press(self, name):
        return self._page()

    def back(self):
        return self._page()

    def host_for(self, ref):
        return "x.onion"

    def insecure_public(self, host):
        return False

    def close(self):
        self.closed = True


def test_browse_gives_the_full_toolkit_restricted_to_onion_addresses(tmp_path, monkeypatch):
    from darkweb.navigation import pilot

    monkeypatch.setattr(pilot, "Browser", _FakeDarkBrowser)
    rt = make(tmp_path)
    rt._tor = type("T", (), {"running": lambda self: True})()
    reg = ToolRegistry()
    with pytest.raises(PermissionError):
        rt.dark_browser()                                           # the switch and the setting are both off by default
    rt.save({"darkweb": True, "browse": True})
    names = dr.offer_darkweb(reg, rt)
    for n in ("darkweb_browser_open", "darkweb_browser_read", "darkweb_browser_click", "darkweb_browser_type", "darkweb_browser_sign_in", "darkweb_browser_select", "darkweb_browser_press", "darkweb_browser_back"):
        assert n in names and reg.get(n) is not None
    link = "http://" + "b" * 56 + ".onion/page"
    out = reg.get("darkweb_browser_open").handler(url=link)
    assert "onion page body" in out and link in out
    with pytest.raises(Exception, match="only an .onion address"):
        reg.get("darkweb_browser_open").handler(url="https://example.com")
    assert "onion page body" in reg.get("darkweb_browser_click").handler(ref=1)
    rt.close_browser()
    assert rt._browser is None


def test_browse_is_a_tool_only_when_its_own_setting_is_on(tmp_path, monkeypatch):
    from darkweb.navigation import pilot
    monkeypatch.setattr(pilot, "Browser", _FakeDarkBrowser)
    rt = make(tmp_path)
    rt._tor = type("T", (), {"running": lambda self: True})()
    rt.save({"darkweb": True})
    reg = ToolRegistry()
    names = dr.offer_darkweb(reg, rt)
    assert "darkweb_browser_open" not in names and "darkweb_read" in names
    rt.save({"browse": True})
    names = dr.offer_darkweb(reg, rt)
    assert "darkweb_browser_open" in names
