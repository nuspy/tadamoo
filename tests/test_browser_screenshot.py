"""Pictures of the page the agent drives: kept as a file of the conversation, asked for on their own or after an action, and never once a vault value was typed."""
import sys
import threading
import http.server
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.attachments import OutputStore
from harness.browser import Browser, BrowserError, find_browser, register_browser_tools
from harness.secrets_broker import SecretsBroker
from harness.tools.registry import ToolRegistry
from harness.vault import Vault

needs_browser = pytest.mark.skipif(find_browser() is None, reason="needs Edge or Chrome on this PC")
PNG = b"\x89PNG\r\n\x1a\n"
PAGE = b"<!doctype html><title>Shop</title><h1>Hotel Cervino</h1><label for='n'>Name</label><input id='n'><a href='/two'>Two</a>"


class Fake:
    """Stands in for the browser: no process, a page that says what it is."""

    def __init__(self):
        self.page_url, self.notice, self.hidden, self.mask, self.taken = "https://hotel.example/rooms", "", set(), (lambda t: t), []

    def screenshot(self, full_page=False):
        if self.hidden:
            raise BrowserError("no screenshot is taken in a browser where a value from the vault was typed")
        self.taken.append(full_page)
        return PNG + b"x" * 2000

    def open(self, url):
        return {"url": url, "title": "T", "items": [], "more": 0, "text": "hello"}

    def _record(self, kind, details):
        self.recorded = (kind, details)

    close = lambda self: None


def tools(saver=True):
    registry, saved = ToolRegistry(), []

    def keep(data, name):
        saved.append((name, data))
        return {"id": "a1b2c3d4e5f6", "name": name, "path": "C:/out/s1/a1b2c3d4e5f6/" + name}
    b = register_browser_tools(registry, None, browser=Fake(), screenshots=keep if saver else None)
    return registry, b, saved


def test_a_screenshot_is_kept_as_a_file_and_the_model_is_told_its_path_and_how_to_look_at_it():
    registry, b, saved = tools()
    out = registry.execute("browser_screenshot", {"full_page": True})
    assert saved[0][0].startswith("screenshot_hotel_example_") and saved[0][0].endswith(".png") and saved[0][1].startswith(PNG)
    assert "[file:a1b2c3d4e5f6:" in out and "Path: C:/out/s1/a1b2c3d4e5f6/" in out and "analyze_image" in out and "output:a1b2c3d4e5f6" in out and "basalt-file:a1b2c3d4e5f6" in out
    assert b.taken == [True] and b.recorded[0] == "browser_screenshot" and b.recorded[1]["bytes"] == len(saved[0][1])      # the trace has it


def test_an_action_that_changes_the_page_can_ask_for_a_picture_of_the_result_and_otherwise_takes_none():
    registry, b, saved = tools()
    plain = registry.execute("browser_open", {"url": "https://hotel.example/"})
    assert "Screenshot" not in plain and saved == []
    shot = registry.execute("browser_open", {"url": "https://hotel.example/", "screenshot": True})
    assert shot.startswith("URL: https://hotel.example/") and "Screenshot of" in shot and len(saved) == 1
    for name in ("browser_open", "browser_click", "browser_select", "browser_press", "browser_back"):
        assert "screenshot" in registry.get(name).parameters["properties"], name
    assert registry.get("browser_screenshot").effect.value == "reversible"


def test_no_picture_is_taken_once_a_vault_value_was_typed_and_the_action_still_returns_its_page():
    registry, b, saved = tools()
    b.hidden.add("4111111111111111")
    assert "Screenshot not taken: no screenshot is taken in a browser where a value from the vault was typed" in registry.execute("browser_screenshot", {})
    out = registry.execute("browser_open", {"url": "https://hotel.example/", "screenshot": True})
    assert "hello" in out and "Screenshot not taken" in out and saved == []


def test_without_a_place_to_keep_files_there_are_no_picture_tools_at_all():
    registry, b, saved = tools(saver=False)
    assert registry.get("browser_screenshot") is None
    assert "screenshot" not in registry.get("browser_open").parameters["properties"]


class Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


@pytest.fixture
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@needs_browser
def test_the_real_browser_gives_a_png_that_is_stored_with_the_conversation_and_stops_after_a_vault_value(tmp_path, site):
    vault = Vault(tmp_path / "vault")
    vault.put("me", kind="identity", fields={"full_name": "Mario Rossi"}, hosts=["127.0.0.1"])
    secrets = SecretsBroker()
    secrets.attach_vault(vault, ask_code=lambda who: "554433")
    store = OutputStore(tmp_path / "out")

    def keep(data, name):
        meta = store.save("s1", name, data)
        return {"id": meta["id"], "name": meta["name"], "path": str(store.get("s1", meta["id"])[1])}
    registry = ToolRegistry()
    browser = register_browser_tools(registry, secrets, profile_dir=tmp_path / "profile", headless=True, allow_private=True, screenshots=keep)
    try:
        out = registry.execute("browser_open", {"url": site, "screenshot": True})
        assert "Hotel Cervino" in out and "Screenshot of" in out
        card = store.list("s1")[0]
        assert (store.get("s1", card["id"])[1]).read_bytes().startswith(PNG) and card["size"] > 500
        whole = registry.execute("browser_screenshot", {"full_page": True})
        assert "[file:" in whole and len(store.list("s1")) == 2
        ref = int([l for l in out.splitlines() if '"Name"' in l][0][1:].split("]")[0])
        registry.execute("browser_type", {"ref": ref, "text": "{{vault:me.full_name}}"})
        refused = registry.execute("browser_screenshot", {})
        assert "Screenshot not taken" in refused and len(store.list("s1")) == 2
    finally:
        browser.close()


@needs_browser
def test_the_agent_gets_the_picture_tools_with_a_place_for_files_and_can_name_a_picture_by_its_output_id(tmp_path, monkeypatch):
    from harness.stack import AgentConfig, build_agent
    monkeypatch.setattr("harness.llm.registry", lambda path=None: None)
    ws = tmp_path / "ws"
    ws.mkdir()

    def built(**kw):
        return build_agent(AgentConfig(workspace=str(ws), endpoint="http://127.0.0.1:1", data_dir=str(tmp_path / "d"), use_memory=False, browser=True, browser_headless=True,
                                       capabilities=True, capabilities_dir=str(tmp_path / "cap"), capabilities_home=str(tmp_path / "home"), **kw))
    assert "browser_screenshot" not in built().agent.tools.names()                      # nowhere to keep the file: no picture tools
    stack = built(attachments_dir=str(tmp_path / "att"), outputs_dir=str(tmp_path / "out"), run_id="s1")
    try:
        assert "browser_screenshot" in stack.agent.tools.names()
        store = OutputStore(tmp_path / "out")
        meta = store.save("s1", "a.png", __import__("base64").b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
        seen = stack.agent.tools.execute("analyze_image", {"path": "output:" + meta["id"], "question": "what?"})
        assert "cannot see images" in seen or "vision" in seen.lower()                    # the file was found and opened; only the model's sight is missing
        assert "not there" in stack.agent.tools.execute("analyze_image", {"path": "output:aaaaaaaaaaaa", "question": "what?"})
    finally:
        stack.close() if hasattr(stack, "close") else None
