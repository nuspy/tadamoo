"""The browser reaches what a payment page is made of: frames of other sites, windows a page opens, and a profile that keeps sign-ins."""
import http.server
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.browser import Browser, BrowserError, ProfileLock, find_browser, forget_signins, open_signin_window, register_browser_tools
from harness.secrets_broker import SecretsBroker
from harness.tools.registry import ToolRegistry
from harness.vault import Vault

needs_browser = pytest.mark.skipif(find_browser() is None, reason="needs Edge or Chrome on this PC")

CARD = """<!doctype html><title>Card</title><form method="post" action="/done"><label>Card number<input name="card" autocomplete="off"></label>
<label>Security code<input name="cvc" autocomplete="off"></label><button type="submit">Pay</button></form>"""
POPUP = """<!doctype html><title>Opener</title><h1>Pay with the wallet</h1><button onclick="window.open('/sheet')">Open the sheet</button>
<p id="state">waiting</p><script>window.addEventListener('message', e => { document.getElementById('state').textContent = 'confirmed:' + e.data; });</script>"""
SHEET = """<!doctype html><title>Sheet</title><h1>Wallet sheet</h1><button onclick="opener.postMessage('ok', '*'); window.close()">Confirm payment</button>"""


class Site(http.server.BaseHTTPRequestHandler):
    posts = []
    port = 0

    def log_message(self, *a):
        pass

    def _send(self, body, cookie=""):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        host = self.headers.get("Host", "").split(":")[0]
        if self.path == "/outer":
            self._send(f'<!doctype html><title>Checkout</title><h1>Checkout</h1><iframe src="http://localhost:{Site.port}/card" width="500" height="260"></iframe>'
                       '<button>Place the order</button>')
        elif self.path == "/same":
            self._send('<!doctype html><title>Same site</title><h1>Same</h1><iframe src="/card" width="500" height="260"></iframe>')
        elif self.path == "/card":
            self._send(CARD)
        elif self.path == "/popup":
            self._send(POPUP)
        elif self.path == "/sheet":
            self._send(SHEET)
        elif self.path == "/signed-in":
            self._send("<title>Signed in</title><h1>Welcome</h1>", cookie="sid=abc123; Max-Age=3600; Path=/")
        elif self.path == "/whoami":
            self._send(f"<title>Who</title><h1>cookie:{self.headers.get('Cookie', 'none')}</h1>")
        else:
            self._send("<h1>Other</h1>")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        Site.posts.append({"host": self.headers.get("Host", "").split(":")[0], **{k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}})
        self._send("<title>Thanks</title><h1>Thanks, paid</h1>")


@pytest.fixture
def site():
    Site.posts = []
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Site)
    Site.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{Site.port}"
    server.shutdown()


@pytest.fixture
def agent(tmp_path):
    vault = Vault(tmp_path / "vault")
    secrets = SecretsBroker()
    secrets.attach_vault(vault)
    registry = ToolRegistry()
    browser = register_browser_tools(registry, secrets, profile_dir=tmp_path / "profile", headless=True, allow_private=True)
    yield registry, vault, browser
    browser.close()


def ref_of(text, name):
    for line in text.splitlines():
        if f'"{name}"' in line and line.startswith("["):
            return int(line[1:line.index("]")])
    raise AssertionError(f"no element '{name}' in:\n{text}")


@needs_browser
def test_a_card_field_in_a_frame_of_another_site_is_read_typed_into_and_sent(agent, site):
    registry, vault, browser = agent
    vault.put("card", kind="card", fields={"number": "4242 4242 4242 4242", "cvc": "737"}, hosts=["localhost"], guard=False)
    page = registry.execute("browser_open", {"url": site + "/outer"})
    assert "Place the order" in page and '"Card number" (in localhost)' in page
    out = registry.execute("browser_type", {"ref": ref_of(page, "Card number"), "text": "{{vault:card.number}}"})
    assert "4242" not in out
    registry.execute("browser_type", {"ref": ref_of(page, "Security code"), "text": "{{vault:card.cvc}}"})
    done = registry.execute("browser_click", {"ref": ref_of(page, "Pay")})
    assert "Thanks, paid" in done
    assert Site.posts == [{"host": "localhost", "card": "4242 4242 4242 4242", "cvc": "737"}]


@needs_browser
def test_a_value_goes_to_the_frames_own_site_not_the_pages(agent, site):
    registry, vault, browser = agent
    vault.put("shop", kind="identity", fields={"full_name": "Mario Rossi"}, hosts=["127.0.0.1"])           # the page's site, not the frame's
    page = registry.execute("browser_open", {"url": site + "/outer"})
    out = registry.execute("browser_type", {"ref": ref_of(page, "Card number"), "text": "{{vault:shop.full_name}}"})
    assert "may only be sent to 127.0.0.1, not to localhost" in out["error"]
    assert Site.posts == []


@needs_browser
def test_a_frame_of_the_same_site_is_part_of_the_page(agent, site):
    registry, vault, browser = agent
    vault.put("card", kind="card", fields={"number": "4242424242424242"}, hosts=["127.0.0.1"], guard=False)
    page = registry.execute("browser_open", {"url": site + "/same"})
    assert '"Card number"' in page and "(in " not in page.split('"Card number"')[1].splitlines()[0]
    registry.execute("browser_type", {"ref": ref_of(page, "Card number"), "text": "{{vault:card.number}}"})
    registry.execute("browser_click", {"ref": ref_of(page, "Pay")})
    assert Site.posts[0]["card"] == "4242424242424242" and Site.posts[0]["host"] == "127.0.0.1"


@needs_browser
def test_a_window_the_page_opens_is_followed_and_the_page_is_back_when_it_closes(agent, site):
    registry, vault, browser = agent
    page = registry.execute("browser_open", {"url": site + "/popup"})
    sheet = registry.execute("browser_click", {"ref": ref_of(page, "Open the sheet")})
    assert "Wallet sheet" in sheet and "Confirm payment" in sheet
    back = registry.execute("browser_click", {"ref": ref_of(sheet, "Confirm payment")})
    assert "Pay with the wallet" in back
    assert "confirmed:ok" in registry.execute("browser_read", {})


# -- a profile that keeps sign-ins ------------------------------------------------------------------------

def kept(tmp_path, site_url=None, **kw):
    return Browser(tmp_path / "kept", headless=True, allow_private=True, persistent=True, **kw)


@needs_browser
def test_a_sign_in_is_there_in_the_next_conversation_when_the_profile_is_kept(tmp_path, site):
    first = kept(tmp_path)
    first.open(site + "/signed-in")
    first.close()
    second = kept(tmp_path)
    try:
        assert "cookie:sid=abc123" in second.open(site + "/whoami")["text"]
    finally:
        second.close()
    thrown = Browser(tmp_path / "other", headless=True, allow_private=True)                    # not kept: nothing is there
    try:
        thrown.open(site + "/signed-in")
    finally:
        thrown.close()
    again = Browser(tmp_path / "other", headless=True, allow_private=True)
    try:
        assert "cookie:none" in again.open(site + "/whoami")["text"]
    finally:
        again.close()


@needs_browser
def test_only_one_browser_at_a_time_uses_the_kept_profile_and_the_other_says_so(tmp_path, site):
    first = kept(tmp_path)
    first.open(site + "/signed-in")
    second = kept(tmp_path)
    try:
        page = second.open(site + "/whoami")
        assert "cookie:none" in page["text"]                                                     # a fresh profile, not the kept one
        assert "in use" in second.notice
    finally:
        second.close()
        first.close()
    assert (tmp_path / "kept").exists()                                                          # the kept profile outlives both


def test_the_notice_reaches_the_model_once(tmp_path):
    class Stub(Browser):
        def open(self, url):
            return {"url": url, "title": "t", "items": [], "more": 0, "text": "page text"}

    registry = ToolRegistry()
    stub = Stub(tmp_path / "k", headless=True)
    register_browser_tools(registry, SecretsBroker(), browser=stub)
    stub.notice = "the kept browser is in use"
    first = registry.execute("browser_open", {"url": "https://shop.example"})
    second = registry.execute("browser_open", {"url": "https://shop.example"})
    assert first.startswith("the kept browser is in use" + chr(10)) and "page text" in first
    assert "kept browser" not in second and "page text" in second


def test_the_window_to_sign_in_and_forgetting_need_the_profile_to_be_free(tmp_path):
    held = ProfileLock(tmp_path / "kept")
    assert held.acquire()
    try:
        with pytest.raises(BrowserError, match="in use"):
            open_signin_window(tmp_path / "kept", "https://accounts.google.com")
        with pytest.raises(BrowserError, match="in use"):
            forget_signins(tmp_path / "kept")
    finally:
        held.release()
    with pytest.raises(BrowserError, match="only web addresses"):
        open_signin_window(tmp_path / "kept", "file:///C:/Windows/win.ini")
    (tmp_path / "kept" / "Default").mkdir(parents=True)
    forget_signins(tmp_path / "kept")
    assert not (tmp_path / "kept").exists()


def test_a_second_lock_on_the_same_profile_is_refused_until_the_first_is_released(tmp_path):
    a, b = ProfileLock(tmp_path / "p"), ProfileLock(tmp_path / "p")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()
