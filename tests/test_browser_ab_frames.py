"""A card field in the frame of a payment provider, with the real agent-browser and a real Edge or Chrome (skipped where either is missing): the page is on one site (127.0.0.1) and its frame on
another (localhost), as a shop and its payment provider are. A vault value reaches only the site that receives it: the card number goes into the provider's frame when the entry names the provider,
is refused in the shop's own field, and arrives at the provider's server (the frame's form is sent to it) and nowhere else; it is in no process's arguments and not in what the model reads.
The program is TADAMOO_TEST_AGENT_BROWSER, else the shipped one, else the one on PATH. Only local ports are used."""
import http.server
import json
import os
import threading
import urllib.parse

import pytest

from tadamoo import vendored
from tadamoo.browser import find_browser, register_browser_tools
from tadamoo.browser_ab import AbCli, AgentBrowser, session_name
from tadamoo.secrets_broker import SecretsBroker
from tadamoo.tools.registry import ToolRegistry

EXE = os.environ.get("TADAMOO_TEST_AGENT_BROWSER") or vendored.find("agent-browser")[0]
pytestmark = pytest.mark.skipif(not EXE or find_browser() is None, reason="needs the agent-browser program (TADAMOO_TEST_AGENT_BROWSER) and Edge or Chrome")

CARD = "4242 4242 4242 4242"


def server(handler_class):
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


class Provider(http.server.BaseHTTPRequestHandler):
    received = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"""<!doctype html><title>Pay</title><form method="post" action="/charge"><label for="c">Card number</label><input id="c" name="card" autocomplete="off">
<button type="submit">Pay now</button></form>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Provider.received.append({k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode(), keep_blank_values=True).items()})
        body = b"<!doctype html><title>Paid</title><h1>Paid</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_a_card_goes_into_the_providers_frame_and_only_there(tmp_path):
    Provider.received = []
    provider = server(Provider)

    class Shop(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = (f"""<!doctype html><title>Shop</title><h1>Checkout</h1><label for="e">Email</label><input id="e" name="email">
<iframe src="http://localhost:{provider.server_address[1]}/" title="payment" width="500" height="220"></iframe>""").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    shop = server(Shop)
    secrets = SecretsBroker({"CARD": CARD}, {"CARD": ["localhost"]})                       # the entry names the provider's site, not the shop's
    sent = []

    class Spy(AbCli):
        def argv(self, args):
            out = super().argv(args)
            sent.append(out)
            return out

    cli = Spy([EXE], session_name(), tmp_path / "ab" / "config.json", tmp_path / "ab" / "home", executable=find_browser(), headed=False, downloads=str(tmp_path / "dl"))
    browser = AgentBrowser(cli, headless=True, allow_private=True, mask=secrets.mask_secrets)
    registry = ToolRegistry()
    register_browser_tools(registry, secrets, browser=browser)
    try:
        page = registry.execute("browser_open", {"url": f"http://127.0.0.1:{shop.server_address[1]}/"})
        email = next(int(line[1:line.index("]")]) for line in page.splitlines() if line.startswith("[") and '"Email"' in line)
        card = next(int(line[1:line.index("]")]) for line in page.splitlines() if line.startswith("[") and '"Card number"' in line)
        pay = next(int(line[1:line.index("]")]) for line in page.splitlines() if line.startswith("[") and '"Pay now"' in line)
        assert "(in localhost)" in page.splitlines()[[i for i, l in enumerate(page.splitlines()) if '"Card number"' in l][0]]       # the frame's site is its own
        refused = registry.execute("browser_type", {"ref": email, "text": "{{secret:CARD}}"})
        assert "localhost" in refused["error"] and not Provider.received                         # the shop's own field is not the provider's site
        typed = registry.execute("browser_type", {"ref": card, "text": "{{secret:CARD}}"})
        assert not isinstance(typed, dict) and CARD not in typed and "4242" not in typed
        after = registry.execute("browser_click", {"ref": pay})
        assert Provider.received == [{"card": CARD}]                                              # the provider's server got it, from the frame's own form
        assert "4242" not in after and "4242" not in json.dumps(sent)                             # nothing read back, nothing in any process's arguments
    finally:
        browser.close()
        shop.shutdown()
        provider.shutdown()


def test_the_page_is_moved_to_the_own_driver_at_the_first_vault_step_with_its_cookies(tmp_path):
    """Both real: agent-browser reads the shop, the person's choice sends the vault's step to TaDaMoo's own driver, which opens the same address with the same cookies (an HttpOnly one too)
    and types the card; the shop's server gets the card and the cookie agent-browser's visit received (it gives a new one only to a visitor with none)."""
    from tadamoo.browser import Browser
    from tadamoo.browser_route import RoutedBrowser
    got, visits = [], []

    class Shop(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, cookie=""):
            self.send_response(200)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            has = "sid=" in self.headers.get("Cookie", "")
            if self.path != "/":                                                              # the browser asking for an icon is not a visit
                return self._send(b"", "")
            visits.append(has)
            self._send(b"""<!doctype html><title>Shop</title><h1>Pay</h1><form method="post" action="/charge"><label for="c">Card number</label><input id="c" name="card" autocomplete="off">
<button type="submit">Pay now</button></form>""", "" if has else f"sid=visit-{len(visits)}; Path=/; HttpOnly; SameSite=Lax")

        def do_POST(self):
            fields = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode(), keep_blank_values=True).items()}
            got.append({**fields, "cookie": self.headers.get("Cookie", "")})
            self._send(b"<!doctype html><title>Paid</title><h1>Paid</h1>")

    shop = server(Shop)
    secrets = SecretsBroker({"CARD": CARD}, {"CARD": ["127.0.0.1"]})
    cli = AbCli([EXE], session_name(), tmp_path / "ab" / "config.json", tmp_path / "ab" / "home", executable=find_browser(), headed=False, downloads=str(tmp_path / "dl"))
    primary = AgentBrowser(cli, headless=True, allow_private=True, mask=secrets.mask_secrets)
    own = Browser(tmp_path / "own", True, "", True, secrets.mask_secrets)
    router = RoutedBrowser(primary, own, "tadamoo")
    registry = ToolRegistry()
    register_browser_tools(registry, secrets, browser=router)

    def ref(page, name):
        return next(int(line[1:line.index("]")]) for line in page.splitlines() if line.startswith("[") and name in line)

    try:
        page = registry.execute("browser_open", {"url": f"http://127.0.0.1:{shop.server_address[1]}/"})
        assert router.active is primary and visits[0] is False                                  # the first visit had no cookie and was given one
        before = len(visits)
        refused = registry.execute("browser_type", {"ref": ref(page, '"Card number"'), "text": "{{secret:CARD}}"})
        assert refused["error"].startswith("Nothing was typed. This step uses the vault") and router.active is own and not got
        assert len(visits) > before and all(visits[before:])                                    # the own driver came with the cookie: the server did not give it a new one
        moved = refused["error"]
        card, pay = ref(moved, '"Card number"'), ref(moved, '"Pay now"')                       # the numbers the own driver gave
        assert not isinstance(registry.execute("browser_type", {"ref": card, "text": "{{secret:CARD}}"}), dict)
        registry.execute("browser_click", {"ref": pay})
        assert len(got) == 1 and got[0]["card"] == CARD and "sid=visit-1" in got[0]["cookie"]  # the card, and the cookie agent-browser's visit received
    finally:
        router.close()
        shop.shutdown()
