"""The agent-desktop backend (tadamoo/computer/agent_desktop.py) against a stand-in program that prints JSON envelopes (tests/fake_agent_desktop.py): no real agent-desktop runs, no input is
sent and no screen is read. What is checked: the choice of backend, the envelopes read (ok, error code, disposition, protocol version), the tree turned into numbered elements, the
commands each tool sends, that typed text goes to the program's standard input and never into its arguments, and that the tools' rules (screen, Stop, corner, vault, record) still hold."""
import json
import os
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_computer import FakeBackend  # noqa: E402

from tadamoo.computer import ComputerPrefs, find_agent_desktop, pick_backend, register_computer_tools  # noqa: E402
from tadamoo.computer.agent_desktop import AdCli, AgentDesktopBackend  # noqa: E402
from tadamoo.computer.backend import UnsupportedBackend  # noqa: E402
from tadamoo.sandbox import cancel_scope  # noqa: E402
from tadamoo.secrets_broker import SecretsBroker  # noqa: E402
from tadamoo.tools.registry import ToolRegistry  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fake_agent_desktop.py")


def ok(command, data):
    return {"version": "2.6", "ok": True, "command": command, "data": data}


def err(command, code, retry="not_delivered", message="no"):
    return {"version": "2.6", "ok": False, "command": command, "error": {"code": code, "message": message, "disposition": {"delivery": retry if retry == "not_delivered" else "delivered_unverified",
                                                                                                                      "retry": "safe" if retry == "not_delivered" else "unsafe"}}}


def box(x, y, w, h):
    return {"x": float(x), "y": float(y), "width": float(w), "height": float(h)}


WINDOWS = [{"id": "w-10", "title": "Notepad", "app_name": "notepad.exe", "bounds": box(100, 100, 800, 600), "is_focused": True, "minimized": False, "accessible": True, "pid": 1, "visible": True},
           {"id": "w-11", "title": "Browser", "app_name": "msedge.exe", "bounds": box(2100, 100, 1200, 800), "is_focused": False, "minimized": False, "accessible": True, "pid": 2, "visible": True},
           {"id": "w-12", "title": "Old", "app_name": "old.exe", "bounds": box(0, 0, 0, 0), "is_focused": False, "minimized": True, "accessible": True, "pid": 3, "visible": True}]

TREE = {"role": "window", "name": "Notepad", "bounds": box(100, 100, 800, 600), "children": [
    {"role": "group", "bounds": box(100, 100, 800, 600), "children": [                                          # an unnamed container with no ref: left out
        {"role": "button", "name": "Save", "ref_id": "@s1:e2", "bounds": box(700, 120, 80, 30), "states": []},
        {"role": "textfield", "name": "Text", "ref_id": "@s1:e3", "value": "hello", "bounds": box(110, 160, 780, 500), "states": ["focused"]},
        {"role": "statictext", "name": "Status", "bounds": box(110, 670, 100, 20)},                            # a label: listed, with no ref
        {"role": "button", "name": "Hidden", "ref_id": "@s1:e4", "bounds": box(0, 0, 10, 10), "states": ["offscreen"]},
        {"role": "button", "name": "Off", "ref_id": "@s1:e5", "bounds": box(800, 120, 50, 30), "states": ["disabled"]},
        {"role": "button", "name": "Far", "ref_id": "@s1:e6", "bounds": box(3000, 200, 50, 20), "states": []}]}]}


class Rig:
    def __init__(self, tmp_path, platform="win32", native=True, responses=None):
        self.state = tmp_path / "fake"
        self.state.mkdir(parents=True)
        self.responses = {"version": ok("version", {"version": "1.3.1", "os": "windows"}), "list-windows": ok("list-windows", WINDOWS),
                          "snapshot": ok("snapshot", {"app": "notepad.exe", "complete": True, "ref_count": 4, "snapshot_id": "s1", "tree": TREE, "window": {"id": "w-10"}}),
                          **(responses or {})}
        self.write()
        os.environ["FAKE_AD_STATE"] = str(self.state)
        self.home = tmp_path / "home"
        self.native = FakeBackend() if native else None
        self.backend = AgentDesktopBackend(AdCli([sys.executable, FAKE], self.home, timeout=20), self.native, platform)
        self.audit, self.pictures, self.asked = [], [], []
        self.level = {"monitor": 0, "auto": False, "answer": False}
        self.secrets = SecretsBroker({"OPEN": "open-value", "SITE": "site-value"}, {"SITE": ["example.com"]})
        self.registry = ToolRegistry(default_timeout=None)

        def keep(data, name):
            self.pictures.append((name, data))
            return {"id": "abc123def456", "name": name, "path": str(tmp_path / name)}

        self.computer = register_computer_tools(self.registry, self.secrets, backend=self.backend, monitor=lambda: self.level["monitor"],
                                                confirm=lambda tool, details: (self.asked.append(tool), self.level["answer"])[1], audit=lambda k, d: self.audit.append((k, d)),
                                                screenshots=keep, auto=lambda: self.level["auto"], rules=lambda: [], sleep=lambda s: None)

    def write(self):
        (self.state / "responses.json").write_text(json.dumps(self.responses), encoding="utf-8")

    def set(self, command, answer):
        self.responses[command] = answer
        self.write()

    def calls(self):
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def sent(self, command):
        return [c for c in self.calls() if command in c["argv"]]

    def call(self, name, **args):
        return self.registry.execute(name, args)


@pytest.fixture
def rig(tmp_path):
    yield Rig(tmp_path)


# -- the choice -----------------------------------------------------------------------------------------------------------

def test_agent_desktop_is_taken_when_it_is_installed_and_the_native_backend_stays_the_other_choice(tmp_path, monkeypatch):
    tools = tmp_path / "tools" / "agent-desktop"
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert find_agent_desktop(tools) == ("", "")
    native = pick_backend("auto", tools)
    assert not isinstance(native, AgentDesktopBackend)                                            # nothing installed: this system's own backend
    refused = pick_backend("agent-desktop", tools)
    assert isinstance(refused, UnsupportedBackend) and "not installed" in refused.unavailable()   # asked for by name: said, not replaced
    tools.mkdir(parents=True)
    exe = tools / ("agent-desktop.exe" if sys.platform == "win32" else "agent-desktop")
    exe.write_bytes(b"x")
    assert find_agent_desktop(tools) == (str(exe), "installed")
    chosen = pick_backend("auto", tools)
    assert isinstance(chosen, AgentDesktopBackend) and chosen.cli.command == [str(exe)] and chosen.cli.home == tools / "state"
    assert isinstance(pick_backend("agent-desktop", tools), AgentDesktopBackend)
    assert not isinstance(pick_backend("native", tools), AgentDesktopBackend)                      # native is asked for by name even when agent-desktop is there
    exe.unlink()
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/agent-desktop")
    assert find_agent_desktop(tools) == ("/usr/bin/agent-desktop", "path")                         # else the one on PATH


def test_the_state_of_the_program_stays_in_the_tools_folder(rig):
    rig.call("computer_list_windows")
    assert rig.calls()[-1]["home"] == str(rig.home)


# -- reading ----------------------------------------------------------------------------------------------------------------

def test_the_windows_come_from_the_program(rig):
    out = rig.call("computer_list_windows")
    assert "[10] 'Notepad' - notepad.exe - 800x600 at 100,100 - in front" in out and "[11] 'Browser'" in out and "minimized" in out


def test_the_tree_is_numbered_and_only_what_can_be_seen_or_acted_on_is_listed(rig):
    out = rig.call("computer_ui_tree")
    assert "[1] button 'Save' (80x30 at 700,120)" in out and "[2] textfield 'Text'" in out and "value='hello'" in out
    assert "statictext 'Status'" in out and "[disabled]" in out
    assert "Hidden" not in out and "Off" in out                            # an element the program says is offscreen is left out; a disabled one is listed as such
    sent = rig.sent("snapshot")[-1]["argv"]
    assert sent[:5] == ["snapshot", "--app", "notepad.exe", "--window-id", "w-10"] and "--include-bounds" in sent


def test_a_partial_tree_and_a_page_with_no_actions_are_said(rig):
    rig.set("snapshot", ok("snapshot", {"app": "x", "complete": False, "truncated": True, "snapshot_id": "s1", "tree": TREE}))
    assert "partial" in rig.call("computer_ui_tree")
    page = {"role": "window", "name": "Web", "bounds": box(100, 100, 800, 600), "children": [{"role": "document", "name": "Page", "bounds": box(100, 100, 800, 600)}]}
    rig.set("snapshot", ok("snapshot", {"app": "x", "complete": True, "snapshot_id": "s2", "tree": page}))
    assert "No element offers an action" in rig.call("computer_ui_tree")
    rig.set("snapshot", ok("snapshot", {"app": "x", "complete": True, "snapshot_id": "s3", "identity_unproven": True, "tree": TREE}))
    assert "cannot be acted on by number yet" in rig.call("computer_ui_tree")
    out = rig.call("computer_ui_tree", limit=1)
    assert "more element(s) were not listed" in out


def test_a_minimized_window_is_not_read(rig):
    assert "minimized" in rig.call("computer_ui_tree", window=12)["error"]


# -- acting ---------------------------------------------------------------------------------------------------------------

def test_an_element_is_clicked_by_its_ref_without_the_pointer_and_with_it_only_where_needed(rig):
    rig.call("computer_ui_tree")
    out = rig.call("computer_click", element=1)
    assert out.startswith("Clicked left at element 1 (button 'Save') at screen position 740,135")
    assert rig.sent("click")[-1]["argv"] == ["click", "@s1:e2"]                          # no --headed: it works on the element, not the pointer
    rig.call("computer_ui_tree")
    rig.call("computer_click", element=1, button="right")
    assert rig.sent("right-click")[-1]["argv"] == ["--headed", "right-click", "@s1:e2"]   # on Windows a right click needs real input
    rig.call("computer_ui_tree")
    rig.call("computer_click", element=1, double=True)
    assert rig.sent("double-click")[-1]["argv"] == ["--headed", "double-click", "@s1:e2"]
    rig.call("computer_ui_tree")
    rig.call("computer_click", element=1, button="middle")                                 # no ref path: its position
    assert rig.sent("mouse-click")[-1]["argv"] == ["--headed", "mouse-click", "--xy=740,135", "--button", "middle", "--count", "1"]
    assert rig.audit[-1][1]["by"] == "position"


def test_a_picture_position_is_clicked_with_the_real_pointer_through_the_program(rig):
    rig.call("computer_screenshot", window=10)
    rig.call("computer_click", x=10, y=20)
    assert rig.sent("mouse-click")[-1]["argv"] == ["--headed", "mouse-click", "--xy=110,120", "--button", "left", "--count", "1"]
    rig.call("computer_move", x=5, y=5)
    assert rig.sent("mouse-move")[-1]["argv"] == ["--headed", "mouse-move", "--xy=105,105"]


def test_on_other_systems_input_is_tried_without_the_pointer_first_and_once_with_it_when_refused(tmp_path):
    r = Rig(tmp_path, platform="linux", responses={"right-click": [err("right-click", "POLICY_DENIED"), ok("right-click", {})]})
    r.call("computer_ui_tree")
    assert r.call("computer_click", element=1, button="right").startswith("Clicked right")
    assert [c["argv"] for c in r.sent("right-click")] == [["right-click", "@s1:e2"], ["--headed", "right-click", "@s1:e2"]]


def test_typed_text_goes_to_the_programs_standard_input_never_into_its_arguments(rig):
    rig.call("computer_ui_tree")
    assert rig.call("computer_type_text", text="héllo\nworld", element=2) == "Typed 11 character(s)."
    sent = rig.sent("type")[-1]
    assert sent["argv"] == ["--headed", "type", "@s1:e3", "--stdin"] and sent["stdin"] == "héllo\nworld"
    rig.call("computer_ui_tree")
    assert rig.call("computer_type_text", text="{{secret:OPEN}}", element=2) == "Typed 10 character(s)."
    assert rig.sent("type")[-1]["stdin"] == "open-value"
    assert "open-value" not in json.dumps([c["argv"] for c in rig.calls()])                    # never in an argument
    assert "open-value" not in json.dumps(rig.audit) and rig.audit[-1][1]["from_vault"] is True
    assert "did not allow it" in rig.call("computer_screenshot")["error"] and rig.asked == ["computer_screenshot"]
    assert "example.com" in rig.call("computer_type_text", text="{{secret:SITE}}")["error"]


def test_text_with_no_element_goes_to_the_element_that_has_the_focus(rig):
    assert rig.call("computer_type_text", text="abc") == "Typed 3 character(s)."
    assert rig.sent("snapshot")[-1]["argv"][:3] == ["snapshot", "--surface", "focused"]
    assert rig.sent("type")[-1]["argv"] == ["--headed", "type", "@s1:e3", "--stdin"]           # the focused element of the tree
    rig.set("snapshot", ok("snapshot", {"complete": True, "snapshot_id": "s9", "tree": {"role": "window", "name": "w", "bounds": box(0, 0, 9, 9)}}))
    assert "no element has the keyboard focus" in rig.call("computer_type_text", text="abc")["error"]


def test_keys_drag_and_scroll_are_one_command_each(rig):
    assert rig.call("computer_key", combo="Ctrl+Shift+S") == "Pressed ctrl+shift+s."
    assert rig.sent("press")[-1]["argv"] == ["press", "ctrl+shift+s"]
    rig.call("computer_key", combo="Enter")
    rig.call("computer_key", combo="Win+E")
    assert [c["argv"][-1] for c in rig.sent("press")][-2:] == ["return", "cmd+e"]
    rig.call("computer_ui_tree")
    rig.call("computer_screenshot", window=10)
    rig.call("computer_ui_tree")
    rig.call("computer_drag", element=1, to_x=300, to_y=200)
    assert rig.sent("drag")[-1]["argv"] == ["--headed", "drag", "--from", "@s1:e2", "--to-xy=400,300"]
    rig.call("computer_scroll", direction="down", amount=3, x=50, y=50)
    assert rig.sent("mouse-wheel")[-1]["argv"] == ["--headed", "mouse-wheel", "--x=150", "--y=150", "--dy=-3", "--dx=0"]
    rig.call("computer_ui_tree")
    rig.call("computer_scroll", direction="up", amount=2, element=2)
    assert rig.sent("scroll")[-1]["argv"] == ["scroll", "@s1:e3", "--direction", "up", "--amount", "2"]


def test_a_dangerous_shortcut_is_refused_by_the_program_and_never_forced(rig):
    rig.set("press", err("press", "POLICY_DENIED", message="blocked shortcut"))
    out = rig.call("computer_key", combo="Alt+F4")
    assert "never forced" in out["error"] and "Nothing was sent" in out["error"]
    assert not [c for c in rig.calls() if "--force" in c["argv"]]


# -- what the program says ----------------------------------------------------------------------------------------------------

def test_an_error_says_what_to_do_and_whether_repeating_is_safe(rig):
    rig.call("computer_ui_tree")
    rig.set("click", err("click", "STALE_REF", message="@s1:e2 not found"))
    out = rig.call("computer_click", element=1)["error"]
    assert "read the tree again" in out and "STALE_REF" in out and "Nothing was sent, so trying again is safe" in out
    rig.call("computer_ui_tree")
    rig.set("click", err("click", "ACTION_FAILED", retry="unsafe"))
    assert "look at the screen before doing it again" in rig.call("computer_click", element=1)["error"]
    rig.call("computer_ui_tree")
    rig.set("click", err("click", "AMBIGUOUS_TARGET"))
    assert "several identical elements" in rig.call("computer_click", element=1)["error"]


def test_a_delivery_that_was_not_confirmed_is_said(rig):
    rig.call("computer_ui_tree")
    rig.set("click", ok("click", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}))
    assert rig.call("computer_click", element=1).endswith("It was delivered but not confirmed: look at the screen.")


def test_a_program_of_another_protocol_or_that_does_not_speak_json_is_refused_clearly(rig):
    rig.set("list-windows", {"version": "3.0", "ok": True, "command": "list-windows", "data": []})
    assert "speaks version 3.0" in rig.call("computer_list_windows")["error"]
    rig.set("list-windows", {"raw": "not json"})
    assert "did not answer with JSON" in rig.call("computer_list_windows")["error"]


def test_a_program_that_is_missing_is_said(tmp_path):
    backend = AgentDesktopBackend(AdCli([str(tmp_path / "nothing-here")], None), None)
    registry = ToolRegistry(default_timeout=None)
    register_computer_tools(registry, None, backend=backend, sleep=lambda s: None)
    assert "could not be started" in registry.execute("computer_list_windows", {})["error"]


def test_a_program_that_does_not_answer_is_stopped(rig):
    rig.set("list-windows", {"sleep": 5})
    rig.backend.cli.timeout = 0.6
    assert "did not answer" in rig.call("computer_list_windows")["error"]


# -- the rules of the tools ----------------------------------------------------------------------------------------------------

def test_the_screen_the_person_gave_the_agent_still_limits_the_tree_and_the_windows(rig):
    rig.level["monitor"] = 1
    assert "Browser" not in rig.call("computer_list_windows")
    assert "Far" not in rig.call("computer_ui_tree")
    rig.level["monitor"] = 0
    assert "Far" in rig.call("computer_ui_tree")


def test_stop_ends_a_slow_read_but_never_real_input_halfway(rig):
    rig.set("snapshot", {"sleep": 5, "then": ok("snapshot", {})})
    stop = threading.Event()
    with cancel_scope(stop):
        threading.Timer(0.5, stop.set).start()
        out = rig.call("computer_ui_tree")
    assert "pressed Stop" in out["error"]
    rig.set("snapshot", ok("snapshot", {"app": "x", "complete": True, "snapshot_id": "s1", "tree": TREE}))
    rig.call("computer_ui_tree")
    rig.set("click", {"sleep": 1.0, "then": ok("click", {})})
    stop2 = threading.Event()
    with cancel_scope(stop2):
        threading.Timer(0.4, stop2.set).start()
        out = rig.call("computer_click", element=1)
    assert isinstance(out, str) and out.startswith("Clicked")                         # the click finished: a press is not cut off halfway
    with cancel_scope(stop2):                                                           # and the next call, which has not started, is refused
        assert "pressed Stop" in rig.call("computer_list_windows")["error"]


def test_the_mouse_in_a_corner_still_stops_everything_through_the_native_cursor(rig):
    rig.native.pos = (2, 3)
    assert "Stopped: the person moved the mouse" in rig.call("computer_list_windows")["error"]
    assert not rig.sent("list-windows")                                               # not even asked


def test_without_a_native_backend_there_is_no_corner_stop_but_the_rest_works(tmp_path):
    r = Rig(tmp_path, native=False, responses={"list-displays": ok("list-displays", [{"id": "m1", "bounds": box(0, 0, 3840, 2160), "is_primary": True, "scale": 1.5}])})
    assert "Notepad" in r.call("computer_list_windows")
    assert not r.backend.can_see_cursor()


def test_pictures_come_from_the_native_backend_or_else_from_the_program(tmp_path):
    r = Rig(tmp_path, responses={"list-displays": ok("list-displays", [{"id": "m1", "bounds": box(0, 0, 3840, 2160), "is_primary": True, "scale": 1.5}])})
    assert "[file:" in r.call("computer_screenshot", monitor=1) and r.native.captured
    plain = Rig(tmp_path / "plain", native=False, responses={"list-displays": ok("list-displays", [{"id": "m1", "bounds": box(0, 0, 640, 480), "is_primary": True, "scale": 1.0}]),
                                                              "screenshot": {"write_png": [640, 480], "version": "2.6", "ok": True, "command": "screenshot", "data": {}}})
    out = plain.call("computer_screenshot")
    assert "[file:" in out and "640x480" in out
    assert plain.sent("screenshot")[-1]["argv"][:3] == ["screenshot", "--screen", "0"]
    width, height = struct.unpack(">II", plain.pictures[-1][1][16:24])
    assert (width, height) == (640, 480)
    assert "only a whole screen" in plain.call("computer_screenshot", window=10)["error"]


def test_the_clipboard_tools_exist_only_with_this_backend_and_never_take_a_vault_value(rig):
    assert {"computer_clipboard_get", "computer_clipboard_set"} <= set(rig.registry.names())
    assert "computer_clipboard_get" not in ToolRegistry().names()
    rig.set("clipboard-get", ok("clipboard-get", {"type": "text", "text": "copied text with open-value inside"}))
    assert rig.call("computer_clipboard_get") == "copied text with [REDACTED:OPEN] inside"
    rig.set("clipboard-get", ok("clipboard-get", {"type": "text", "found": False}))
    assert rig.call("computer_clipboard_get") == "The clipboard has no text."
    assert rig.call("computer_clipboard_set", text="plain") == "Put 5 character(s) on the clipboard."
    assert rig.sent("clipboard-set")[-1]["stdin"] == "plain" and rig.sent("clipboard-set")[-1]["argv"] == ["clipboard-set", "--stdin"]
    assert "not put on the clipboard" in rig.call("computer_clipboard_set", text="{{secret:OPEN}}")["error"]
    assert all("open-value" not in (c["stdin"] or "") for c in rig.calls())


def test_the_tools_of_the_native_backend_do_not_have_a_clipboard(tmp_path):
    registry = ToolRegistry(default_timeout=None)
    register_computer_tools(registry, None, backend=FakeBackend(), sleep=lambda s: None)
    assert not [n for n in registry.names() if "clipboard" in n]


def test_the_program_shipped_with_tadamoo_is_taken_unless_one_in_the_data_folder_or_none_at_all(tmp_path, monkeypatch):
    from tadamoo import vendored
    monkeypatch.setattr(vendored, "ROOT", tmp_path / "program")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    names = vendored.exe_names("agent-desktop")
    assert vendored.find("agent-desktop", tmp_path / "data")[1] == "path"                       # nothing shipped, nothing downloaded: one on PATH
    shipped = tmp_path / "program" / "tools" / "vendored" / "agent-desktop"
    shipped.mkdir(parents=True)
    (shipped / names[0]).write_bytes(b"x")
    assert vendored.find("agent-desktop", tmp_path / "data") == (str(shipped / names[0]), "bundled")
    assert find_agent_desktop(tmp_path / "data")[1] == "bundled" and isinstance(pick_backend("auto", tmp_path / "data"), AgentDesktopBackend)
    mine = tmp_path / "data"
    mine.mkdir()
    (mine / names[0]).write_bytes(b"y")
    assert vendored.find("agent-desktop", mine) == (str(mine / names[0]), "installed")           # an update the person made comes first
    monkeypatch.setattr("shutil.which", lambda name: None)
    (mine / names[0]).unlink()
    (shipped / names[0]).unlink()
    assert vendored.find("agent-desktop", mine) == ("", "")


def test_the_backend_choice_is_kept_beside_the_screen_choice(tmp_path):
    prefs = ComputerPrefs(tmp_path / "computer.json")
    assert prefs.write(True, 2, "agent-desktop")["backend"] == "agent-desktop"
    assert prefs.read() == {"enabled": True, "monitor": 2, "backend": "agent-desktop"}
