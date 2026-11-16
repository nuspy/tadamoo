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
