"""The browser the agent drives: it reads a page, fills a form and sends it; a vault value reaches only a site its entry names."""
import http.server
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.browser import Browser, BrowserError, check_url, find_browser, register_browser_tools
from harness.secrets_broker import SecretsBroker
from harness.tools.registry import ToolRegistry
from harness.vault import Vault

needs_browser = pytest.mark.skipif(find_browser() is None, reason="needs Edge or Chrome on this PC")

HOME = """<!doctype html><title>Shop</title><h1>Hotel Cervino</h1><p>Book a room.</p>
<a href="/form" target="_blank">Reserve</a><a href="/other">Other</a>"""
FORM = """<!doctype html><title>Reserve</title><h1>Reserve</h1>
<form method="post" action="/done">
<label for="n">Full name</label><input id="n" name="name">
<label for="c">Card number</label><input id="c" name="card" autocomplete="off">
<label for="p">Password</label><input id="p" name="password" type="password">
<label for="k">Country</label><select id="k" name="country"><option value="it">Italy</option><option value="ch">Switzerland</option></select>
<label><input type="checkbox" name="terms" value="yes"> I accept</label>
<button type="submit">Pay now</button></form>"""


LOGIN = """<!doctype html><title>Sign in</title><h1>Sign in</h1>
<form method="post" action="/done">
<label for="e">Email or username</label><input id="e" name="user">
<label for="p">Password</label><input id="p" name="password" type="password">
<button type="submit">Log in</button></form>"""
STEP_ONE = """<!doctype html><title>Sign in</title><form method="post" action="/done"><label for="e">Phone, email or username</label><input id="e" name="user"><button>Next</button></form>"""


class Shop(http.server.BaseHTTPRequestHandler):
    posts = []

    def log_message(self, *a):
        pass

    def _send(self, body):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"/": HOME, "/form": FORM, "/login": LOGIN, "/step": STEP_ONE}.get(self.path, "<h1>Other</h1>"))

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        Shop.posts.append({k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()})
        self._send("<title>Thanks</title><h1>Thanks, paid</h1>")


@pytest.fixture
def shop():
    Shop.posts = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def agent(tmp_path):
    vault = Vault(tmp_path / "vault")
    secrets = SecretsBroker()
    secrets.attach_vault(vault, ask_code=lambda who: "554433")
    registry = ToolRegistry()
    browser = register_browser_tools(registry, secrets, profile_dir=tmp_path / "profile", headless=True, allow_private=True)
    yield registry, vault, browser
    browser.close()


def ref_of(text, name):
    for line in text.splitlines():
        if f'"{name}"' in line and line.startswith("["):
            return int(line[1:line.index("]")])
    raise AssertionError(f"no element '{name}' in:\n{text}")


def test_only_web_addresses_outside_this_pc_are_opened():
    for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "data:text/html,x", "about:blank", "http://127.0.0.1/", "http://localhost:7232/", "http://192.168.1.1/",
                "http://169.254.169.254/"):
        with pytest.raises(BrowserError):
            check_url(bad)


@needs_browser
def test_a_page_is_read_as_numbered_elements_and_text(agent, shop):
    registry, vault, browser = agent
    page = registry.execute("browser_open", {"url": shop})
    assert "Title: Shop" in page and "Hotel Cervino" in page and 'link "Reserve"' in page and "Book a room" in page


@needs_browser
def test_a_form_is_filled_and_sent_and_a_link_that_wants_a_new_tab_opens_here(agent, shop):
    registry, vault, browser = agent
    page = registry.execute("browser_open", {"url": shop})
    form = registry.execute("browser_click", {"ref": ref_of(page, "Reserve")})
    assert "Title: Reserve" in form                                              # the link had target=_blank
    registry.execute("browser_type", {"ref": ref_of(form, "Full name"), "text": "Mario Rossi"})
    registry.execute("browser_select", {"ref": ref_of(form, "Country"), "value": "Switzerland"})
    registry.execute("browser_click", {"ref": ref_of(form, "I accept")})
    done = registry.execute("browser_click", {"ref": ref_of(form, "Pay now")})
    assert "Thanks, paid" in done
    assert Shop.posts == [{"name": "Mario Rossi", "card": "", "password": "", "country": "ch", "terms": "yes"}]


@needs_browser
def test_a_vault_value_is_typed_on_its_site_and_never_shown(agent, shop):
    registry, vault, browser = agent
    vault.put("me", kind="identity", fields={"full_name": "Mario Rossi"}, hosts=["127.0.0.1"])
    vault.put("pw", kind="login", fields={"username": "mr", "password": "hunter2hunter2"}, hosts=["127.0.0.1"])
    form = registry.execute("browser_open", {"url": shop + "/form"})
    out = registry.execute("browser_type", {"ref": ref_of(form, "Full name"), "text": "{{vault:me.full_name}}"})
    assert "Mario Rossi" not in out and 'value=(set)' in out
    out = registry.execute("browser_type", {"ref": ref_of(form, "Password"), "text": "{{vault:pw.password}}"})
    assert "hunter2" not in out and "(set)" in out
    registry.execute("browser_click", {"ref": ref_of(form, "Pay now")})
    assert Shop.posts[0]["name"] == "Mario Rossi" and Shop.posts[0]["password"] == "hunter2hunter2"


@needs_browser
def test_a_value_is_not_typed_on_a_site_the_entry_does_not_name(agent, shop):
    registry, vault, browser = agent
    vault.put("me", kind="identity", fields={"full_name": "Mario Rossi"}, hosts=["trusted.example"])
    form = registry.execute("browser_open", {"url": shop + "/form"})
    out = registry.execute("browser_type", {"ref": ref_of(form, "Full name"), "text": "{{vault:me.full_name}}"})
    assert out["error"].startswith("'me' may only be sent to trusted.example")
    line = [l for l in registry.execute("browser_read", {}).splitlines() if '"Full name"' in l][0]
    assert "value=" not in line


@needs_browser
def test_a_guarded_card_is_typed_only_after_an_approved_payment(agent, shop):
    registry, vault, browser = agent
    vault.put("trip", kind="card", fields={"holder": "Mario Rossi", "number": "4242 4242 4242 4242"}, hosts=["127.0.0.1"], guard=True)
    form = registry.execute("browser_open", {"url": shop + "/form"})
    out = registry.execute("browser_type", {"ref": ref_of(form, "Card number"), "text": "{{vault:trip.number}}"})
    assert "approved payment" in out["error"]
    vault.arm("trip", 60)
    out = registry.execute("browser_type", {"ref": ref_of(form, "Card number"), "text": "{{vault:trip.number}}"})
    assert "4242" not in out
    registry.execute("browser_click", {"ref": ref_of(form, "Pay now")})
    assert Shop.posts[0]["card"] == "4242 4242 4242 4242"


@needs_browser
def test_a_second_factor_code_is_asked_and_typed_without_being_shown(agent, shop):
    registry, vault, browser = agent
    vault.put("bank", kind="login", fields={"username": "mr"}, second_factor="manual", hosts=["127.0.0.1"])
    form = registry.execute("browser_open", {"url": shop + "/form"})
    out = registry.execute("browser_type", {"ref": ref_of(form, "Card number"), "text": "{{vault:bank.totp}}"})
    assert "554433" not in out
    registry.execute("browser_click", {"ref": ref_of(form, "Pay now")})
    assert Shop.posts[0]["card"] == "554433"


@needs_browser
def test_a_stale_element_number_is_refused_with_advice(agent, shop):
    registry, vault, browser = agent
    registry.execute("browser_open", {"url": shop})
    out = registry.execute("browser_click", {"ref": 99})
    assert "read the page again" in out["error"]
    assert "URL: about:blank" in registry.execute("browser_back", {})           # the page before was the empty one


@needs_browser
def test_a_login_from_the_vault_is_filled_in_and_never_shown(agent, shop):
    registry, vault, browser = agent
    vault.put("mysite", kind="login", fields={"username": "mario@example.com", "password": "hunter2hunter2"}, hosts=["127.0.0.1"])
    page = registry.execute("browser_open", {"url": shop + "/login"})
    out = registry.execute("browser_sign_in", {"entry": "mysite"})
    assert "mario@example.com" not in out and "hunter2" not in out and out.count("value=(set)") == 2
    registry.execute("browser_click", {"ref": ref_of(page, "Log in")})
    assert Shop.posts == [{"user": "mario@example.com", "password": "hunter2hunter2"}]


@needs_browser
def test_sign_in_can_submit_and_a_two_step_page_gets_the_user_name_first(agent, shop):
    registry, vault, browser = agent
    vault.put("mysite", kind="login", fields={"username": "mario", "password": "hunter2hunter2"}, hosts=["127.0.0.1"])
    registry.execute("browser_open", {"url": shop + "/step"})
    done = registry.execute("browser_sign_in", {"entry": "mysite", "submit": True})
    assert "Thanks, paid" in done and Shop.posts == [{"user": "mario"}]
    Shop.posts.clear()
    registry.execute("browser_open", {"url": shop + "/login"})
    assert "Thanks, paid" in registry.execute("browser_sign_in", {"entry": "mysite", "submit": True})
    assert Shop.posts == [{"user": "mario", "password": "hunter2hunter2"}]


@needs_browser
def test_sign_in_is_refused_on_a_site_the_login_does_not_name_and_for_what_is_not_a_login(agent, shop):
    registry, vault, browser = agent
    vault.put("other", kind="login", fields={"username": "mario", "password": "hunter2hunter2"}, hosts=["bank.example"])
    vault.put("card", kind="card", fields={"number": "4242424242424242"}, hosts=["127.0.0.1"])
    registry.execute("browser_open", {"url": shop + "/login"})
    assert "may only be sent to bank.example" in registry.execute("browser_sign_in", {"entry": "other"})["error"]
    assert "not a login" in registry.execute("browser_sign_in", {"entry": "card"})["error"]
    assert "no vault entry" in registry.execute("browser_sign_in", {"entry": "ghost"})["error"]
    registry.execute("browser_open", {"url": shop})
    assert "No sign-in field" in registry.execute("browser_sign_in", {"entry": "other"})["error"]
