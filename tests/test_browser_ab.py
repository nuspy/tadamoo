"""The browser tools on agent-browser (tadamoo/browser_ab.py) against a stand-in program that prints JSON answers (tests/fake_agent_browser.py): no real browser starts. What is checked: the page is
described as the tools always describe it, each tool sends the command it should, a vault value reaches the program on its standard input only and only to the site that receives it (a
payment provider's frame is a site of its own), the page text hides what was typed, the person's rules (private addresses, permission levels, consent banners, dialogs, downloads, pictures) are the
same ones as with TaDaMoo's own driver, and the program's errors come back as advice. The real program is driven by the same tests as the other driver in tests/test_browser.py."""
import json
import os
import sys
from pathlib import Path

import pytest

from tadamoo import browser_consent as bc
from tadamoo.browser import BrowserError, register_browser_tools
from tadamoo.browser_ab import AbCli, AgentBrowser, parse_snapshot
from tadamoo.secrets_broker import SecretsBroker
from tadamoo.tools.registry import ToolRegistry

FAKE = str(Path(__file__).resolve().parent / "fake_agent_browser.py")
SHOP, PAY = "93.184.216.34", "151.101.1.1"                                  # addresses on the internet (no name to look up)

PAGE = """- heading "Checkout" [level=1, ref=e1]
- textbox "Email" [ref=e2]
- textbox "Password" [ref=e3]
- combobox [expanded=false, ref=e4] hint="name:c": Italy
  - option "Italy" [selected, ref=e9]
  - option "France" [ref=e10]
- checkbox "I agree" [checked=false, ref=e5]
- link "A link" [ref=e6, url=https://%s/other?x=1]
- button "Buy" [ref=e7]
- Iframe "payment" [ref=e8]
  - heading "Pay frame" [level=1, ref=e11]
  - textbox "Card number" [ref=e13]
  - button "Pay now" [ref=e12]
""" % SHOP


def snapshot(value="", url=f"https://{SHOP}/checkout"):
    text = PAGE.replace('- textbox "Card number" [ref=e13]', '- textbox "Card number" [ref=e13]' + (f": {value}" if value else ""))
    return {"snapshot": text, "origin": f"https://{SHOP}/"}


class Rig:
    def __init__(self, tmp_path, responses=None, secrets=None, **kw):
        self.state = tmp_path / "state"
        self.state.mkdir(parents=True)
        self.responses = {"snapshot": snapshot(), "get url": {"url": f"https://{SHOP}/checkout"}, "get title": {"title": "Shop"}, "get text": {"text": "Checkout Italy France I agree"},
                          "dialog status": {"hasDialog": False}, "get attr @e2 type": {"value": "email"}, "get attr @e3 type": {"value": "password"}, "get attr @e13 type": {"value": "text"},
                          "get attr @e8 src": {"value": f"https://{PAY}/frame"}, "tab": {"tabs": [{"tabId": "t1", "active": True}]}, "screenshot": {"write_png": True}, **(responses or {})}
        self.write()
        os.environ["FAKE_AB_STATE"] = str(self.state)
        self.downloads = tmp_path / "downloads"
        self.downloads.mkdir(parents=True)
        self.cli = AbCli([sys.executable, FAKE], "tadamoo-test", tmp_path / "ab" / "config.json", tmp_path / "ab" / "home", executable="C:/edge.exe", downloads=str(self.downloads), timeout=20)
        self.secrets = secrets or SecretsBroker({"CARD": "4242 4242 4242 4242", "OPEN": "open-value"}, {"CARD": [PAY]})
        self.asked, self.answer, self.auto, self.audit = [], False, False, []
        self.browser = AgentBrowser(self.cli, headless=True, mask=self.secrets.mask_secrets, confirm=lambda tool, details: (self.asked.append((tool, details)), self.answer)[1],
                                    audit=lambda k, d: self.audit.append((k, d)), auto=lambda: self.auto, rules=lambda: [], sleep=lambda s: None, **kw)
        self.registry = ToolRegistry(default_timeout=None)
        self.pictures = []

        def keep(data, name):
            self.pictures.append((name, data))
            return {"id": "abc123def456", "name": name, "path": str(tmp_path / name)}

        register_browser_tools(self.registry, self.secrets, browser=self.browser, screenshots=keep)

    def write(self):
        (self.state / "responses.json").write_text(json.dumps(self.responses), encoding="utf-8")

    def set(self, key, value):
        self.responses[key] = value
        self.write()

    def calls(self):
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def processes(self):
        return [c for c in self.calls() if "argv" in c]

    def batched(self):
        return [c["batch_command"] for c in self.calls() if "batch_command" in c]

    def sent(self, *words):
        """The processes that ran this command (not a batch)."""
        return [c for c in self.processes() if c["argv"][-len(words):] == list(words) or list(words) == [w for w in c["argv"] if w in words][:len(words)] and words[0] in c["argv"]]

    def call(self, name, **args):
        return self.registry.execute(name, args)


@pytest.fixture
def rig(tmp_path):
    yield Rig(tmp_path)


def test_the_snapshot_text_is_read_into_nodes_with_the_iframes_above_them():
    nodes = parse_snapshot(PAGE)
    by = {n["ref"]: n for n in nodes if n["ref"]}
    assert by["e2"]["role"] == "textbox" and by["e2"]["name"] == "Email"
    assert by["e4"]["value"] == "Italy" and by["e4"]["role"] == "combobox" and by["e9"]["attrs"] == ["selected", "ref=e9"]
    assert by["e6"]["url"].startswith(f"https://{SHOP}/other") and by["e13"]["frames"] == ["e8"] and by["e8"]["frames"] == []


def test_the_page_is_described_as_the_tools_always_describe_it(rig):
    out = rig.call("browser_open", url=f"https://{SHOP}/checkout")
    assert f"URL: https://{SHOP}/checkout" in out and "Title: Shop" in out
    assert '[1] input[email] "Email"' in out and '[2] input[password] "Password"' in out and "[3] select" in out and "choices: Italy; France" in out and "value=Italy" in out
    assert '[4] input[checkbox] "I agree" unchecked' in out and f'[5] link "A link" href={SHOP}/other' in out and '[6] button "Buy"' in out
    assert f'[7] input[text] "Card number" (in {PAY})' in out and f'[8] button "Pay now" (in {PAY})' in out
    assert "Checkout Italy France I agree" in out
    assert rig.processes()[0]["argv"][-1] == f"https://{SHOP}/checkout"                       # open <url>
    assert "--config" in rig.processes()[0]["argv"] and "--executable-path" in rig.processes()[0]["argv"] and "--headed" not in rig.processes()[0]["argv"]
    assert rig.processes()[0]["home"] == str(rig.cli.home)                                    # its sockets and state stay in TaDaMoo's folder


# -- a person's own words go in the arguments; the vault's never do ---------------------------------------------------------

def test_plain_text_is_filled_clicked_selected_and_pressed(rig):
    out = rig.call("browser_open", url=f"https://{SHOP}/checkout")
    rig.call("browser_type", ref=1, text="mario@example.com")
    assert rig.sent("fill", "@e2", "--", "mario@example.com")
    rig.call("browser_type", ref=1, text=" more", clear=False, submit=True)
    assert rig.sent("type", "@e2", "--", " more") and rig.sent("press", "Enter")
    rig.call("browser_click", ref=6)
    assert rig.sent("click", "@e7")
    rig.call("browser_select", ref=3, value="France")
    assert rig.sent("select", "@e4", "France")
    rig.call("browser_press", key="Tab")
    assert rig.sent("press", "Tab")
    rig.call("browser_back")
    assert rig.sent("back")


def test_a_vault_value_goes_to_the_program_on_its_standard_input_and_only_to_the_site_that_gets_it(rig):
    rig.call("browser_open", url=f"https://{SHOP}/checkout")
    refused = rig.call("browser_type", ref=1, text="{{secret:CARD}}")                          # the e-mail field: the page's site, not the provider's
    assert PAY in refused["error"]
    assert not [c for c in rig.batched() if "4242 4242 4242 4242" in json.dumps(c)]
    rig.set("snapshot", snapshot(value="4242 4242 4242 4242"))                                   # the page now shows what was typed in the card field
    out = rig.call("browser_type", ref=7, text="{{secret:CARD}}")                               # the card field is in the provider's frame
    assert not isinstance(out, dict)
    sent = [c for c in rig.processes() if c["stdin"] and "4242 4242 4242 4242" in c["stdin"]]
    assert len(sent) == 1 and "batch" in sent[0]["argv"] and "--bail" in sent[0]["argv"] and "--omit-command" in sent[0]["argv"]
    assert ["fill", "@e13", "--", "4242 4242 4242 4242"] in rig.batched()
    assert "4242" not in json.dumps([p["argv"] for p in rig.processes()])                        # never in the arguments of any process
    assert "4242" not in out and "(set)" in out                                                   # nor in what the model reads: the field says it is set
    assert "4242" not in json.dumps(rig.audit)


def test_the_program_telling_back_the_command_is_not_trusted_to_keep_a_secret(rig):
    rig.call("browser_open", url=f"https://{SHOP}/checkout")
    rig.set("fill", {"__error__": {"error": "failed to fill with open-value", "code": "x"}})
    out = rig.call("browser_type", ref=1, text="{{secret:OPEN}}")
    assert "open-value" not in json.dumps(out) and "REDACTED" in json.dumps(out)


def test_a_login_from_the_vault_is_filled_in_on_its_site_only_and_never_shown(tmp_path):
    from tadamoo.vault import Vault
    vault = Vault(tmp_path / "vault")
    vault.put("mine", kind="login", fields={"username": "mario@example.com", "password": "hunter2-hunter2"}, hosts=[SHOP])
    vault.put("other", kind="login", fields={"username": "x@example.com", "password": "other-password-9"}, hosts=["example.org"])
    secrets = SecretsBroker()
    secrets.attach_vault(vault, ask_code=lambda who: "554433")
    rig = Rig(tmp_path / "r", secrets=secrets)
    rig.call("browser_open", url=f"https://{SHOP}/checkout")
    out = rig.call("browser_sign_in", entry="mine", submit=True)
    assert not isinstance(out, dict)
    assert ["fill", "@e2", "--", "mario@example.com"] in rig.batched() and ["fill", "@e3", "--", "hunter2-hunter2"] in rig.batched() and ["press", "Enter"] in rig.batched()
    argv = json.dumps([p["argv"] for p in rig.processes()])
    assert "hunter2" not in argv and "mario@example.com" not in argv                              # in no process's arguments
    assert "hunter2" not in out and "mario@example.com" not in out
    assert "may only be sent to" in rig.call("browser_sign_in", entry="other")["error"] or "example.org" in rig.call("browser_sign_in", entry="other")["error"]


# -- pages, tabs, errors -------------------------------------------------------------------------------------------------------

def test_a_window_the_page_opened_becomes_the_page_the_agent_works_on(rig):
    rig.call("browser_open", url=f"https://{SHOP}/checkout")
    rig.set("tab", {"tabs": [{"tabId": "t1", "active": True}, {"tabId": "t2", "active": False}]})
    rig.call("browser_click", ref=6)
    assert rig.sent("tab", "t2")


def test_an_element_number_that_is_not_there_is_refused_with_advice(rig):
    rig.call("browser_open", url=f"https://{SHOP}/checkout")
    assert "read the page again" in rig.call("browser_click", ref=99)["error"]
    rig.set("click", {"__error__": {"error": "Unknown ref: e7", "code": ""}})
    assert "read the page again" in rig.call("browser_click", ref=6)["error"]
    rig.set("click", {"__error__": {"error": "Click blocked: covered by <div#banner>", "code": ""}})
    assert "covered by <div#banner>" in rig.call("browser_click", ref=6)["error"] and "Close or answer" in rig.call("browser_click", ref=6)["error"]


def test_a_command_whose_result_was_lost_is_read_again_only_when_it_only_reads(rig):
    rig.set("open", [{"__error__": {"error": "Failed to read", "code": "outcome_unknown"}}, {"url": f"https://{SHOP}/checkout"}])
    assert "Title: Shop" in rig.call("browser_open", url=f"https://{SHOP}/checkout")
    assert len(rig.sent("open", f"https://{SHOP}/checkout")) == 2
    rig.set("click", {"__error__": {"error": "Failed to read", "code": "outcome_unknown"}})
    out = rig.call("browser_click", ref=6)["error"]
    assert "may or may not have happened" in out and len(rig.sent("click", "@e7")) == 1


def test_a_program_that_is_missing_or_does_not_answer_is_said(tmp_path):
    cli = AbCli([str(tmp_path / "nothing")], "s", tmp_path / "c.json", tmp_path / "h")
    registry = ToolRegistry(default_timeout=None)
    register_browser_tools(registry, None, browser=AgentBrowser(cli, headless=True))
    assert "could not be started" in registry.execute("browser_open", {"url": f"https://{SHOP}/"})["error"]
    r = Rig(tmp_path / "x", {"open": {"__error__": {"error": "nope", "code": ""}}})
    assert "nope" in r.call("browser_open", url=f"https://{SHOP}/")["error"]


# -- the person's rules ------------------------------------------------------------------------------------------------------------

def test_an_address_inside_this_pc_or_its_network_is_asked_about_as_with_the_own_driver(rig):
    out = rig.call("browser_open", url="http://127.0.0.1:7232/")
    assert "inside this PC or its network" in out["error"] or "127.0.0.1" in out["error"]
    assert rig.asked[0][0] == "browser_private_page" and not rig.sent("open", "http://127.0.0.1:7232/")
    rig.answer = True
    assert "Title:" in rig.call("browser_open", url="http://127.0.0.1:7232/")
    assert rig.audit[-1][0] == "browser_private_page" or rig.sent("open", "http://127.0.0.1:7232/")
    for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "data:text/html,x"):
        assert "error" in rig.call("browser_open", url=bad)


def test_a_page_that_ends_up_on_a_private_address_is_taken_off_it(rig):
    rig.set("get url", {"url": "http://192.168.1.1/admin"})
    out = rig.call("browser_open", url=f"https://{SHOP}/")
    assert isinstance(out, dict) and "192.168.1.1" in out["error"] and rig.sent("open", "about:blank")


def test_a_picture_after_a_vault_value_was_typed_asks_first_at_any_level_but_auto(rig):
    rig.call("browser_open", url=f"https://{SHOP}/checkout")
    out = rig.call("browser_screenshot")
    assert "Screenshot of" in out and rig.pictures
    rig.call("browser_type", ref=7, text="{{secret:CARD}}")
    n = len(rig.pictures)
    out = rig.call("browser_screenshot")
    assert "did not allow it" in (out["error"] if isinstance(out, dict) else out)
    assert len(rig.pictures) == n and rig.asked[-1][0] == "browser_screenshot"
    rig.auto = True
    assert "Screenshot of" in rig.call("browser_screenshot") and len(rig.pictures) == n + 1
    rig.call("browser_screenshot", full_page=True)
    assert [c for c in rig.processes() if "screenshot" in c["argv"] and "--full" in c["argv"]]


def test_a_consent_banner_is_dealt_with_by_the_same_fixed_rules(rig):
    banner = {"name": "cookiebanner", "known": True, "news": False, "ticked": False,
              "buttons": [{"i": 0, "label": "Accept all", "attrs": ""}, {"i": 1, "label": "Necessary only", "attrs": ""}]}
    rig.set("eval", [{"result": f"https://{SHOP}"}, {"result": json.dumps({"banners": [banner], "hint": False})}, {"result": [12, 34]}, {"result": json.dumps({"banners": [], "hint": False})}])
    out = rig.call("browser_open", url=f"https://{SHOP}/checkout")                            # the level is not Auto: the necessary cookies only
    assert 'consent banner: clicked "Necessary only"' in out
    assert ["mouse", "move", "12", "34"] in rig.batched() and ["mouse", "down"] in rig.batched()
    assert rig.audit[-1][0] == "browser_consent_click" and rig.audit[-1][1]["policy"] == "necessary"


def test_a_question_dialog_is_answered_by_the_rule_and_an_alert_is_accepted(rig):
    rig.set("dialog status", {"hasDialog": True, "type": "confirm", "message": "Leave the page?"})
    out = rig.call("browser_open", url=f"https://{SHOP}/checkout")
    assert rig.sent("dialog", "dismiss") and 'dialog "Leave the page?": dismissed' in out and rig.asked[-1][0] == "browser_dialog"
    rig.auto = True
    out = rig.call("browser_read")
    assert rig.sent("dialog", "accept") and "accepted" in out
    rig.set("dialog status", {"hasDialog": True, "type": "alert", "message": "Hi"})
    rig.call("browser_read")
    assert len(rig.sent("dialog", "accept")) >= 2


def test_a_download_is_kept_or_removed_by_the_same_rule_after_it_arrives(rig):
    rig.call("browser_open", url=f"https://{SHOP}/checkout")
    (rig.downloads / "invoice.pdf").write_bytes(b"pdf")
    out = rig.call("browser_read")
    assert 'download of "invoice.pdf"' in out and "removed" in out and not (rig.downloads / "invoice.pdf").exists() and rig.asked[-1][0] == "browser_download"
    rig.auto = True
    (rig.downloads / "ok.pdf").write_bytes(b"pdf")
    out = rig.call("browser_read")
    assert "it was kept" in out and (rig.downloads / "ok.pdf").exists()
    assert rig.audit[-1][0] == "browser_download"


def test_the_browser_is_closed_with_the_conversation_unless_it_is_someone_elses(rig, tmp_path):
    rig.browser.close()
    assert rig.sent("close")
    other = Rig(tmp_path / "ext")
    other.cli.cdp_url = other.browser.cdp_url = "http://127.0.0.1:9222"
    other.browser.close()
    assert not other.sent("close")                                                             # a browser somebody else started is never ended from here
    assert "--cdp" in AbCli(["x"], "s", tmp_path / "c.json", None, cdp_url="http://127.0.0.1:9222").argv(["snapshot"])
    assert "--executable-path" not in AbCli(["x"], "s", tmp_path / "c.json", None, executable="e", cdp_url="9222").argv(["snapshot"])
    shown = AbCli(["x"], "s", tmp_path / "c2.json", None, headed=True, profile="P").argv(["snapshot"])
    assert "--headed" in shown and shown[shown.index("--profile") + 1] == "P" and (tmp_path / "c2.json").read_text(encoding="utf-8") == "{}"


def test_a_pages_request_for_a_permission_is_refused_by_a_switch_unless_the_person_chose_grant(tmp_path):
    ask = Rig(tmp_path / "a")
    argv = ask.cli.argv(["snapshot"])
    assert argv[argv.index("--args") + 1] == "--deny-permission-prompts"
    grant = Rig(tmp_path / "g", permissions="grant")
    assert grant.cli.argv(["snapshot"])[grant.cli.argv(["snapshot"]).index("--args") + 1] == "--use-fake-ui-for-media-stream"
    auto = Rig(tmp_path / "u")
    auto.auto = True                                                                           # `follow` at the Auto level is grant
    assert "--use-fake-ui-for-media-stream" in auto.cli.argv(["snapshot"])
    external = Rig(tmp_path / "e")
    external.cli.cdp_url = "http://127.0.0.1:9222"
    assert "--args" not in external.cli.argv(["snapshot"])                                     # a browser somebody else started keeps its own settings


def test_the_option_that_is_asked_for_by_name_is_kept_beside_the_others(tmp_path):
    from tadamoo.browser import BACKENDS, BrowserPrefs
    prefs = BrowserPrefs(tmp_path / "browser.json")
    assert prefs.read()["backend"] == "agent-browser" and BACKENDS == ("agent-browser", "tadamoo")      # agent-browser is the default
    assert prefs.write(True, False, backend="tadamoo")["backend"] == "tadamoo"
    assert prefs.write(True, True)["backend"] == "tadamoo"                                              # a field left out stays
    assert prefs.write(True, True, backend="nonsense")["backend"] == "tadamoo"
    (tmp_path / "browser.json").write_text("not json", encoding="utf-8")
    assert prefs.read()["backend"] == "agent-browser"


def test_the_agent_stack_takes_agent_browser_when_it_is_found_and_the_option_says_so(tmp_path, monkeypatch):
    from tadamoo import vendored
    from tadamoo.headless import config_from_args, parse_args
    from tadamoo.stack import build_agent
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(vendored, "ROOT", tmp_path / "program")
    monkeypatch.setattr("shutil.which", lambda name: None)
    (tmp_path / "work").mkdir()
    base = ["-p", "task", "--workspace", str(tmp_path / "work"), "--no-memory", "--browser", "--browser-cdp-url", "http://127.0.0.1:9"]

    def driver(*extra):
        agent = build_agent(config_from_args(parse_args([*base, *extra])), client=object())
        return type(agent.browser).__name__

    assert driver() == "Browser"                                                            # the program is not there: the own driver
    tools = tmp_path / "data" / "tools" / "agent-browser"
    tools.mkdir(parents=True)
    (tools / vendored.exe_names("agent-browser")[0]).write_bytes(b"x")
    assert driver() == "RoutedBrowser" and driver("--browser-backend", "agent-browser") == "RoutedBrowser"     # the default: agent-browser, the own driver for the vault's steps
    assert driver("--browser-vault-backend", "browser") == "AgentBrowser"                                         # agent-browser for everything
    assert driver("--browser-backend", "tadamoo") == "Browser" and driver("--browser-backend", "tadamoo", "--browser-vault-backend", "browser") == "Browser"


# -- the vault's steps with the own driver: the page is moved to it ---------------------------------------------------------------

class FakeOwn:
    """TaDaMoo's own driver as the router needs it: starts, takes cookies, opens a page, reads it, types."""

    def __init__(self):
        self.calls, self.hidden, self.notice, self.page_url, self.mask = [], set(), "", "", (lambda t: t)
        self.cookies = []
        self.cdp = self
        self.typed = []

    def call(self, method, params=None, **k):
        self.calls.append((method, params))
        self.cookies += (params or {}).get("cookies", [])
        return {}

    def start(self):
        self.calls.append(("start",))

    def open(self, url):
        self.calls.append(("open", url))
        self.page_url = url
        return self.snapshot()

    def snapshot(self):
        return {"url": self.page_url, "host": "93.184.216.34", "title": "Shop", "text": "Checkout",
                "items": [{"ref": 1, "tag": "input", "type": "text", "name": "Email"}, {"ref": 2, "tag": "input", "type": "text", "name": "Card number", "frame": PAY}]}

    def host_for(self, ref):
        return PAY if ref == 2 else SHOP

    def insecure_public(self, host):
        return False

    def type(self, ref, text, submit=False, clear=True, secret=False):
        self.typed.append((ref, text, secret))
        return self.snapshot()

    def close(self):
        self.calls.append(("close",))


def routed(tmp_path, route):
    from tadamoo.browser_route import RoutedBrowser
    rig = Rig(tmp_path)
    own = FakeOwn()
    router = RoutedBrowser(rig.browser, own, route)
    registry = ToolRegistry(default_timeout=None)
    register_browser_tools(registry, rig.secrets, browser=router)
    rig.set("cookies get", {"cookies": [{"name": "sid", "value": "abc", "domain": SHOP, "path": "/", "httpOnly": True, "secure": True, "expires": 2000000000.0, "session": False},
                                        {"name": "tmp", "value": "1", "domain": SHOP, "path": "/", "httpOnly": False, "secure": False, "expires": -1.0, "session": True}, {"name": "", "domain": "x"}]})
    return rig, own, router, registry


def test_with_the_own_driver_for_the_vault_the_page_is_moved_at_the_first_vault_step(tmp_path):
    rig, own, router, registry = routed(tmp_path, "tadamoo")
    registry.execute("browser_open", {"url": f"https://{SHOP}/checkout"})
    registry.execute("browser_type", {"ref": 1, "text": "mario@example.com"})                  # an ordinary word: agent-browser types it
    assert rig.sent("fill", "@e2", "--", "mario@example.com") and not own.calls
    out = registry.execute("browser_type", {"ref": 7, "text": "{{secret:CARD}}"})              # the card, in the provider's frame
    assert isinstance(out, dict) and out["error"].startswith("Nothing was typed. This step uses the vault")
    assert 'input[text] "Card number"' in out["error"] and "new numbers" in out["error"]        # the page as the own driver reads it
    assert not [c for c in rig.calls() if "4242" in json.dumps(c)]                              # agent-browser never saw the value
    assert rig.sent("close") and ("open", f"https://{SHOP}/checkout") in own.calls and own.calls[0] == ("start",)
    cookies = own.cookies
    assert [c["name"] for c in cookies] == ["sid", "tmp"] and cookies[0]["httpOnly"] and cookies[0]["secure"] and cookies[0]["expires"] == 2000000000.0 and "expires" not in cookies[1]
    ask = registry.execute("browser_type", {"ref": 2, "text": "{{secret:CARD}}"})              # asked again with the new numbers: the own driver types it
    assert not isinstance(ask, dict) and own.typed == [(2, "4242 4242 4242 4242", True)]
    registry.execute("browser_open", {"url": f"https://{SHOP}/next"})
    assert ("open", f"https://{SHOP}/next") in own.calls and not rig.sent("open", f"https://{SHOP}/next")       # from then on the own driver does everything
    assert router.moved_at == f"https://{SHOP}/checkout"
    router.close()
    assert ("close",) in own.calls


def test_a_value_for_the_wrong_site_is_refused_before_anything_is_moved(tmp_path):
    rig, own, router, registry = routed(tmp_path, "tadamoo")
    registry.execute("browser_open", {"url": f"https://{SHOP}/checkout"})
    out = registry.execute("browser_type", {"ref": 1, "text": "{{secret:CARD}}"})              # the shop's field: the entry names the provider
    assert PAY in out["error"] and not own.calls and not rig.sent("close") and router.active is rig.browser


def test_with_the_same_driver_for_the_vault_agent_browser_does_it_itself(tmp_path):
    rig, own, router, registry = routed(tmp_path, "browser")
    registry.execute("browser_open", {"url": f"https://{SHOP}/checkout"})
    out = registry.execute("browser_type", {"ref": 7, "text": "{{secret:CARD}}"})
    assert not isinstance(out, dict) and ["fill", "@e13", "--", "4242 4242 4242 4242"] in rig.batched() and not own.calls and router.active is rig.browser


def test_the_vault_choice_is_kept_beside_the_driver_choice(tmp_path):
    from tadamoo.browser import BrowserPrefs
    prefs = BrowserPrefs(tmp_path / "browser.json")
    assert prefs.read()["vault_backend"] == "tadamoo"                                           # the own driver until a real payment provider test has been done
    assert prefs.write(True, False, vault_backend="browser")["vault_backend"] == "browser"
    assert prefs.write(True, False, backend="tadamoo")["vault_backend"] == "browser"            # a field left out stays
    assert prefs.write(True, False, vault_backend="x")["vault_backend"] == "browser"
