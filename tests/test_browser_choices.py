"""What the browser allows follows the person's level: at Auto nothing is asked and nothing is refused for safety; below it the person is asked,
for the call, the conversation or for good, or an allow rule names the site. And nothing the model reads is cut off without a way to read on."""
import base64
import http.server
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness.browser as hb
from harness.browser import Browser, BrowserError, PrivateAddress, check_url, describe, find_browser, register_browser_tools
from harness.tools.registry import ToolRegistry

needs_browser = pytest.mark.skipif(find_browser() is None, reason="needs Edge or Chrome on this PC")


# -- pages inside this PC or its network ---------------------------------------------------------------------

def test_a_private_address_raises_one_that_names_the_host():
    with pytest.raises(PrivateAddress) as e:
        check_url("http://127.0.0.1:8080/admin", False)
    assert e.value.host == "127.0.0.1" and isinstance(e.value, BrowserError)
    assert check_url("http://127.0.0.1:8080/admin", True).startswith("http://127.0.0.1")


def test_a_private_page_is_free_at_auto_and_with_the_setting_and_asked_about_otherwise(tmp_path):
    assert Browser(tmp_path / "a", auto=lambda: True)._private_ok("192.168.1.1")
    assert Browser(tmp_path / "b", allow_private=True)._private_ok("192.168.1.1")
    asked = []
    b = Browser(tmp_path / "c", confirm=lambda tool, args: asked.append((tool, args)) or True)
    assert b._private_ok("192.168.1.1") and b._private_ok("192.168.1.1")                 # asked once for the host
    assert asked == [("browser_private_page", {"site": "192.168.1.1"})]
    assert b._request_ok("http://192.168.1.1/x")                                         # and the requests of its page go through
    assert not Browser(tmp_path / "d", confirm=lambda *a: False)._private_ok("192.168.1.1")
    assert not Browser(tmp_path / "e")._private_ok("192.168.1.1")                        # nobody to ask


def test_an_allow_rule_for_a_host_lets_a_private_page_open(tmp_path):
    b = Browser(tmp_path / "p", rules=lambda: ["browser_private_page:192.168.*"])
    assert b._private_ok("192.168.1.1") and not b._private_ok("10.0.0.1")


# -- the screenshot after a vault value was typed ---------------------------------------------------------------

class FakeCdp:
    def call(self, method, params=None, timeout=0):
        return {"data": base64.b64encode(b"png").decode()}


def shot_browser(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(Browser, "start", lambda self: None)
    b = Browser(tmp_path / "p", **kw)
    b.pages, b.page_url = [FakeCdp()], "https://shop.example/pay"
    b.hidden.add("4111")
    return b


def test_the_screenshot_after_a_vault_value_is_taken_at_auto_asked_otherwise_and_refused_without_an_answer(tmp_path, monkeypatch):
    assert shot_browser(tmp_path, monkeypatch, auto=lambda: True, confirm=lambda *a: pytest.fail("asked")).screenshot() == b"png"
    asked = []
    b = shot_browser(tmp_path, monkeypatch, confirm=lambda tool, args: asked.append((tool, args["site"])) or True)
    assert b.screenshot() == b"png" and asked == [("browser_screenshot", "https://shop.example")]
    with pytest.raises(BrowserError, match="did not allow"):
        shot_browser(tmp_path, monkeypatch, confirm=lambda *a: False).screenshot()
    with pytest.raises(BrowserError, match="did not allow"):
        shot_browser(tmp_path, monkeypatch).screenshot()
    plain = Browser(tmp_path / "q")
    plain.start = lambda: None
    plain.pages = [FakeCdp()]
    assert plain.screenshot() == b"png"                                                  # nothing typed: no question at all


def test_a_vault_value_on_a_plain_http_page_is_typed_when_allowed(tmp_path):
    assert Browser(tmp_path / "a", auto=lambda: True).insecure_ok("shop.example")
    assert Browser(tmp_path / "b", confirm=lambda tool, args: tool == "browser_insecure_typing").insecure_ok("shop.example")
    assert not Browser(tmp_path / "c").insecure_ok("shop.example")


# -- reading a page: no element and no text is unreachable --------------------------------------------------------

def page_of(n, words=0):
    items = [{"ref": i, "tag": "a" if i % 2 else "button", "type": "", "name": f"item {i}", "href": f"https://x.example/{i}" if i % 2 else ""} for i in range(n)]
    return {"url": "https://x.example/", "title": "T", "items": items, "text": " ".join(f"w{i}" for i in range(words))}


def test_the_caps_on_elements_and_text_are_gone():
    assert not hasattr(hb, "MAX_ELEMENTS") and not hasattr(hb, "PAGE_TEXT_CHARS")
    assert "MAX" not in hb.SNAPSHOT_JS.replace("MAXBYTES", "")


def test_the_elements_are_read_in_pages_and_the_end_says_how_to_go_on():
    page = page_of(400)
    first = describe(page)
    assert f"[{hb.ELEMENTS_PER_READ - 1}] " in first and f"[{hb.ELEMENTS_PER_READ}] " not in first
    assert f"250 more elements not listed: read them with browser_read(offset={hb.ELEMENTS_PER_READ}" in first
    last = describe(page, offset=300, limit=500)
    assert "[399] " in last and "[300] " in last and "[299] " not in last and "more elements" not in last and "(301 to 400 of 400)" in last
    seen = set()
    offset = 0
    while offset < 400:
        out = describe(page, offset=offset, limit=70)
        seen |= {int(line.split("]")[0][1:]) for line in out.splitlines() if line.startswith("[")}
        offset += 70
    assert seen == set(range(400))                                                       # every element is reachable


def test_the_elements_can_be_filtered_by_text_and_by_role_and_keep_the_numbers_of_the_page():
    page = page_of(400)
    out = describe(page, contains="item 37")
    assert [l.split()[0] for l in out.splitlines() if l.startswith("[")] == ["[37]", "[370]", "[371]", "[372]", "[373]", "[374]", "[375]", "[376]", "[377]", "[378]", "[379]"]
    assert "of 400 on the page" in out and "Text:" not in out
    buttons = describe(page, role="button", limit=1000)
    assert "link" not in buttons.split("Elements")[1] and buttons.count("button") >= 200
    assert "[2] button" in buttons and "[3] " not in buttons
    assert "[7] link" in describe(page, role="link", contains="item 7", limit=5)


def test_the_text_is_read_in_words_and_the_cut_says_how_to_read_on():
    page = page_of(3, words=2500)
    out = describe(page)
    assert "w999" in out and "w1000" not in out and "1500 more words" in out and "browser_read(words_from=1000)" in out
    rest = describe(page, words_from=1000, words=2000)
    assert "w1000" in rest and "w2499" in rest and "more words" not in rest and "Elements" in rest
    assert "w2499" in describe(page, words_from=2400)
    short = describe(page_of(3, words=50))
    assert "w49" in short and "more words" not in short


def test_browser_read_takes_the_paging_and_filter_arguments():
    class Fake:
        page_url, notice, hidden, mask = "https://x.example/", "", set(), None

        def snapshot(self):
            return page_of(400, words=30)
    registry = ToolRegistry()
    register_browser_tools(registry, None, browser=Fake())
    props = registry.get("browser_read").parameters["properties"]
    assert {"offset", "limit", "contains", "role", "words_from", "words"} <= set(props)
    out = registry.execute("browser_read", {"offset": 10, "limit": 2})
    assert "[10] " in out and "[11] " in out and "[12] " not in out
    assert "[37] " in registry.execute("browser_read", {"contains": "item 37", "limit": 1})


# -- downloads and dialogs, through the real browser ---------------------------------------------------------------

class Files(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/file.txt":
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="report.txt"')
        else:
            body = b'<!doctype html><title>D</title><h1>Files</h1><a id="f" href="/file.txt" download>get</a>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def files():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Files)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**kw):
        b = Browser(tmp_path / f"profile{len(made)}", headless=True, **kw)
        made.append(b)
        return b
    yield build
    for b in made:
        b.close()


def download(b, base):
    b.open(base + "/")
    b.cdp.call("Runtime.evaluate", {"expression": "document.getElementById('f').click()", "userGesture": True})
    b.cdp.pump(1.0)
    b.snapshot()


@needs_browser
def test_a_download_is_kept_at_auto_and_cancelled_when_the_person_declines(make, files, tmp_path):
    b = make(auto=lambda: True, confirm=lambda *a: pytest.fail("asked"))
    download(b, files)
    assert 'download of "report.txt"' in b.notice and "kept" in b.notice
    assert any(p.name == "report.txt" or p.suffix in (".txt", ".crdownload") for p in (b.profile_dir / "downloads").iterdir())
    asked = []
    b2 = make(allow_private=True, confirm=lambda tool, args: asked.append((tool, args["file"])) or False)
    download(b2, files)
    assert asked == [("browser_download", "report.txt")] and "not allowed" in b2.notice


@needs_browser
def test_a_page_on_this_pc_opens_at_auto_and_asks_the_person_below_it(make, files):
    b = make(auto=lambda: True, confirm=lambda *a: pytest.fail("asked"))                 # allow_private is off: the level decides
    assert b.open(files + "/")["title"] == "D"
    asked = []
    b2 = make(confirm=lambda tool, args: asked.append((tool, args)) or True)
    assert b2.open(files + "/")["title"] == "D" and asked == [("browser_private_page", {"site": "127.0.0.1"})]
    b3 = make(confirm=lambda *a: False)
    with pytest.raises(BrowserError, match="did not allow"):
        b3.open(files + "/")
