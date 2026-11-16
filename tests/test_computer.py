"""Computer control (tadamoo/computer) with a backend that only records what it was asked: no real input, no real screen. The Windows and X11 backends are
checked for what can be checked without using the machine (structure sizes, key tables, the commands they would run)."""
import json
import struct
import sys
import threading
import zlib

import pytest

from tadamoo.computer import ComputerPrefs, register_computer_tools
from tadamoo.computer.backend import Backend, ComputerError, Monitor, UiNode, UnsupportedBackend, Window, parse_combo, pick_backend
from tadamoo.computer.png import bgra_to_rgb, encode_rgb
from tadamoo.computer.session import MAX_SIDE, Computer, corners
from tadamoo.sandbox import cancel_scope
from tadamoo.secrets_broker import SecretsBroker
from tadamoo.tools.registry import EffectClass, ToolRegistry


class FakeBackend(Backend):
    """Two screens side by side: 1920x1080 at 0,0 and 2560x1440 at 1920,0. Records every call."""
    name = "fake"

    def __init__(self):
        self.events = []
        self.pos = (500, 500)
        self.fail_move_after = None
        self.moves = 0
        self.screens = [Monitor(1, 0, 0, 1920, 1080, True), Monitor(2, 1920, 0, 2560, 1440)]
        self.wins = [Window(10, "Notepad", "notepad.exe", 100, 100, 800, 600, foreground=True), Window(11, "Browser", "msedge.exe", 2100, 100, 1200, 800),
                     Window(12, "Old", "old.exe", 0, 0, 0, 0, minimized=True)]
        self.nodes = [UiNode(1, "Window", "Notepad", 100, 100, 800, 600), UiNode(2, "Button", "Save", 700, 120, 80, 30, depth=1), UiNode(3, "Edit", "Text", 110, 160, 780, 500, depth=1, value="hello"),
                      UiNode(4, "Button", "Far", 3000, 200, 50, 20, depth=1)]
        self.captured = []

    def monitors(self):
        return list(self.screens)

    def capture(self, left, top, width, height, out_width, out_height):
        self.captured.append((left, top, width, height, out_width, out_height))
        return encode_rgb(bytes(out_width * out_height * 3), out_width, out_height)

    def windows(self):
        return list(self.wins)

    def focus(self, window_id):
        self.events.append(("focus", window_id))
        for w in self.wins:
            w.foreground = w.id == window_id

    def ui_tree(self, window_id, limit):
        self.events.append(("tree", window_id, limit))
        return list(self.nodes)

    def cursor(self):
        return self.pos

    def move_to(self, x, y):
        self.moves += 1
        if self.fail_move_after is not None and self.moves > self.fail_move_after:
            raise ComputerError("boom")
        self.pos = (x, y)
        self.events.append(("move", x, y))

    def button(self, name, down):
        self.events.append(("button", name, down))

    def wheel(self, dx, dy):
        self.events.append(("wheel", dx, dy))

    def key(self, name, down):
        self.events.append(("key", name, down))

    def char(self, ch):
        self.events.append(("char", ch))


def decode(png):
    """Width, height and RGB bytes of a PNG made by png.py (8-bit RGB, filter 0)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(png):
        size, kind = struct.unpack(">I4s", png[pos:pos + 8])
        data = png[pos + 8:pos + 8 + size]
        assert zlib.crc32(kind + data) & 0xFFFFFFFF == struct.unpack(">I", png[pos + 8 + size:pos + 12 + size])[0]
        if kind == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            idat += data
        pos += 12 + size
    raw = zlib.decompress(idat)
    stride = width * 3 + 1
    rows = [raw[y * stride + 1:(y + 1) * stride] for y in range(height)]
    assert all(raw[y * stride] == 0 for y in range(height))
    return width, height, b"".join(rows)


@pytest.fixture
def rig(tmp_path):
    backend = FakeBackend()
    audit = []
    pictures = []
    state = {"monitor": 0, "auto": False, "answer": False, "asked": []}

    def keep(data, name):
        pictures.append((name, data))
        return {"id": "abc123def456", "name": name, "path": str(tmp_path / name)}

    def confirm(tool, details):
        state["asked"].append((tool, details))
        return state["answer"]

    secrets = SecretsBroker({"OPEN": "open-value", "SITE": "site-value"}, {"SITE": ["example.com"]})
    registry = ToolRegistry(default_timeout=None)
    computer = register_computer_tools(registry, secrets, backend=backend, monitor=lambda: state["monitor"], confirm=confirm, audit=lambda k, d: audit.append((k, d)),
                                       screenshots=keep, auto=lambda: state["auto"], rules=lambda: [], sleep=lambda s: None)
    return registry, backend, computer, audit, pictures, state


def call(registry, name, **args):
    return registry.execute(name, args)


# -- the pieces ----------------------------------------------------------------------------------------------------------

def test_key_combinations():
    assert parse_combo("Ctrl+Shift+S") == ["ctrl", "shift", "s"]
    assert parse_combo("Enter") == ["enter"] and parse_combo("alt + tab") == ["alt", "tab"] and parse_combo("Cmd+Space") == ["win", "space"]
    assert parse_combo("F5") == ["f5"] and parse_combo("ctrl+plus") == ["ctrl", "+"]
    assert parse_combo("alt") == ["alt"]                                       # a modifier alone is a key too (Alt or Win tapped)
    for bad in ("", "ctrl+ctrl+a", "a+b", "ctrl+banana", "hyper+a"):
        with pytest.raises(ComputerError):
            parse_combo(bad)


def test_the_png_is_a_real_picture_without_pillow():
    rgb = bytes(range(256)) * 3
    w, h, back = decode(encode_rgb(rgb, 16, 16))
    assert (w, h, back) == (16, 16, rgb)
    assert bgra_to_rgb(bytes([1, 2, 3, 255, 4, 5, 6, 255])) == bytes([3, 2, 1, 6, 5, 4])
    with pytest.raises(ValueError):
        encode_rgb(b"xx", 16, 16)


def test_only_the_corners_the_pointer_can_be_pushed_into_count():
    two = [Monitor(1, 0, 0, 1920, 1080), Monitor(2, 1920, 0, 2560, 1440)]
    found = set(corners(two))
    assert (0, 0) in found and (4479, 0) in found and (4479, 1439) in found and (0, 1079) in found
    assert (1919, 0) not in found and (1920, 0) not in found and (1919, 1079) not in found     # the seam is not a corner
    assert (1920, 1439) in found                                                                # the notch under the shorter screen is
    assert corners([Monitor(1, 0, 0, 100, 50)]) == [(0, 0), (0, 49), (99, 0), (99, 49)]


# -- the tools ----------------------------------------------------------------------------------------------------------

def test_the_tools_sit_in_the_effect_classes_and_are_marked_like_the_browser_ones(rig):
    registry = rig[0]
    got = {n: registry.get(n) for n in registry.names()}
    assert set(got) == {"computer_screenshot", "computer_list_windows", "computer_focus_window", "computer_ui_tree", "computer_move", "computer_click", "computer_drag", "computer_scroll",
                        "computer_type_text", "computer_key", "computer_wait"}
    assert got["computer_screenshot"].effect == EffectClass.REVERSIBLE
    assert all(got[n].effect == EffectClass.PURE for n in ("computer_list_windows", "computer_ui_tree", "computer_wait"))
    assert all(got[n].effect == EffectClass.IRREVERSIBLE for n in ("computer_click", "computer_drag", "computer_type_text", "computer_key", "computer_move", "computer_scroll", "computer_focus_window"))
    assert all(got[n].untrusted for n in got)
    assert all(got[n].outward for n in ("computer_click", "computer_drag", "computer_type_text", "computer_key"))
    assert not got["computer_ui_tree"].outward
    assert "Slower" in got["computer_click"].description and "say so" in got["computer_click"].description          # the honest description


def test_group_rules_and_the_history_know_the_computer_tools():
    from tadamoo.permissions import group_of
    from tadamoo.rewind import KNOWN_EFFECTS
    assert group_of("group:computer") == ("computer_*",)
    assert KNOWN_EFFECTS["computer_click"] == "irreversible" and KNOWN_EFFECTS["computer_ui_tree"] == "pure"


def test_a_picture_is_scaled_kept_as_a_file_and_gives_the_scale_for_clicks(rig):
    registry, backend, computer, audit, pictures, state = rig
    state["monitor"] = 2
    out = call(registry, "computer_screenshot")
    assert backend.captured == [(1920, 0, 2560, 1440, MAX_SIDE, 882)]
    assert "screen 2, 2560x1440 pixels shown as 1568x882" in out and "[file:abc123def456:" in out and "analyze_image (path output:abc123def456)" in out
    width, height, _ = decode(pictures[0][1])
    assert (width, height) == (MAX_SIDE, 882)
    assert audit[-1][0] == "screenshot"
    call(registry, "computer_click", x=784, y=441)                        # the middle of the picture is the middle of screen 2
    assert ("move", 1920 + 1280, 720) in backend.events


def test_a_click_needs_a_picture_or_an_element_first_and_stays_inside_the_picture(rig):
    registry = rig[0]
    assert "take a picture first" in call(registry, "computer_click", x=1, y=1)["error"]
    call(registry, "computer_screenshot")
    assert "outside the last picture" in call(registry, "computer_click", x=5000, y=1)["error"]
    assert "say where" in call(registry, "computer_click")["error"]


def test_the_agent_stays_on_the_screen_the_person_gave_it(rig):
    registry, backend, computer, audit, pictures, state = rig
    state["monitor"] = 1
    assert "limited the agent to screen 1" in call(registry, "computer_screenshot", monitor=2)["error"]
    listed = call(registry, "computer_list_windows")
    assert "Notepad" in listed and "Browser" not in listed and "Old" in listed             # the window on screen 2 is not listed; a minimized one is
    assert "does not work on" in call(registry, "computer_focus_window", window=11)["error"]
    assert "not on the screen" in call(registry, "computer_screenshot", window=11)["error"]
    tree = call(registry, "computer_ui_tree")
    assert "Save" in tree and "Far" not in tree                                           # an element on screen 2 is dropped
    state["monitor"] = 9
    assert "screen 9 is not there" in call(registry, "computer_screenshot")["error"]
    state["monitor"] = 0
    assert "Browser" in call(registry, "computer_list_windows")
    assert "[file:" in call(registry, "computer_screenshot", monitor=2)                      # with all screens given, one can be asked for


def test_a_window_picture_is_the_window_and_gives_its_own_coordinates(rig):
    registry, backend, *_ = rig
    out = call(registry, "computer_screenshot", window=10)
    assert backend.captured[-1] == (100, 100, 800, 600, 800, 600) and "window 'Notepad'" in out
    call(registry, "computer_click", x=10, y=20)
    assert ("move", 110, 120) in backend.events
    assert "minimized" in call(registry, "computer_screenshot", window=12)["error"]


def test_the_tree_is_numbered_and_an_element_is_clicked_by_number_at_its_centre(rig):
    registry, backend, computer, audit, *_ = rig
    tree = call(registry, "computer_ui_tree")
    assert "[2] Button 'Save' (80x30 at 700,120)" in tree and "[3] Edit 'Text'" in tree and "value='hello'" in tree
    assert backend.events[0] == ("tree", 10, 150)
    out = call(registry, "computer_click", element=2, button="right", double=True)
    assert out.startswith("Clicked right twice at element 2 (Button 'Save') at screen position 740,135")
    assert ("move", 740, 135) in backend.events
    assert [e for e in backend.events if e[0] == "button"] == [("button", "right", True), ("button", "right", False)] * 2
    assert "no element 2" in call(registry, "computer_click", element=2)["error"]          # the numbers end after a click
    assert "no element 99" in call(registry, "computer_click", element=99)["error"]


def test_move_scroll_and_drag(rig):
    registry, backend, *_ = rig
    call(registry, "computer_screenshot", window=10)
    call(registry, "computer_move", x=50, y=60)
    call(registry, "computer_scroll", direction="down", amount=3, x=60, y=70)
    assert backend.events[-4:] == [("move", 160, 170), ("wheel", 0, 1), ("wheel", 0, 1), ("wheel", 0, 1)]
    call(registry, "computer_scroll", direction="left", amount=999)
    assert backend.events.count(("wheel", -1, 0)) == 30
    backend.events.clear()
    out = call(registry, "computer_drag", x=10, y=10, to_x=410, to_y=10)
    assert out.startswith("Dragged from (10, 10)")
    kinds = [e[0] for e in backend.events]
    assert kinds[0] == "move" and kinds[1] == "button" and backend.events[1] == ("button", "left", True) and backend.events[-1] == ("button", "left", False)
    assert backend.events[-2] == ("move", 510, 110)


def test_a_drag_that_fails_halfway_still_lets_go_of_the_button(rig):
    registry, backend, *_ = rig
    call(registry, "computer_screenshot", window=10)
    backend.fail_move_after = 3
    out = call(registry, "computer_drag", x=10, y=10, to_x=700, to_y=10)
    assert "boom" in out["error"]
    assert backend.events[-1] == ("button", "left", False)


def test_typing_keys_and_the_wait(rig):
    registry, backend, computer, audit, *_ = rig
    out = call(registry, "computer_type_text", text="héllo\nnext\tè")
    assert out == "Typed 12 character(s)."
    assert [e[1] for e in backend.events if e[0] == "char"] == list("héllo") + list("next") + ["è"]
    assert [e for e in backend.events if e[0] == "key" and e[2]] == [("key", "enter", True), ("key", "tab", True)]
    assert "héllo" not in json.dumps(audit)                                                # the record has the length, never the text
    backend.events.clear()
    assert call(registry, "computer_key", combo="Ctrl+Shift+S", times=2) == "Pressed ctrl+shift+s 2 times."
    assert backend.events[:6] == [("key", "ctrl", True), ("key", "shift", True), ("key", "s", True), ("key", "s", False), ("key", "shift", False), ("key", "ctrl", False)]
    assert "not a modifier" in call(registry, "computer_key", combo="a+b")["error"]
    assert call(registry, "computer_wait", seconds=0.6) == "Waited 0.6 second(s)."
    assert call(registry, "computer_wait", seconds=500) == "Waited 30 second(s)."
    assert "at most" in call(registry, "computer_type_text", text="x" * 2001)["error"]


def test_typing_into_an_element_clicks_it_first(rig):
    registry, backend, *_ = rig
    call(registry, "computer_ui_tree")
    backend.events.clear()
    call(registry, "computer_type_text", text="ab", element=3)
    assert backend.events[0] == ("move", 500, 410) and backend.events[-2:] == [("char", "a"), ("char", "b")]


def test_a_value_from_the_vault_is_typed_without_being_seen_and_the_next_picture_asks(rig):
    registry, backend, computer, audit, pictures, state = rig
    assert call(registry, "computer_type_text", text="{{secret:OPEN}}") == "Typed 10 character(s)."
    assert [e[1] for e in backend.events if e[0] == "char"] == list("open-value")
    assert "open-value" not in json.dumps(audit) and audit[-1][1]["from_vault"] is True
    out = call(registry, "computer_screenshot")                                          # the person is asked and says no
    assert "did not allow it" in out["error"] and state["asked"][0][0] == "computer_screenshot" and not pictures
    state["answer"] = True
    assert "[file:" in call(registry, "computer_screenshot")
    state["answer"], state["auto"] = False, True                                          # Auto: taken without asking
    n = len(state["asked"])
    assert "[file:" in call(registry, "computer_screenshot") and len(state["asked"]) == n


def test_a_secret_limited_to_a_site_is_not_typed_into_a_program(rig):
    registry, backend, *_ = rig
    out = call(registry, "computer_type_text", text="{{secret:SITE}}")
    assert "example.com" in out["error"] and not [e for e in backend.events if e[0] == "char"]


def test_the_action_can_bring_a_picture_along(rig):
    registry, backend, computer, audit, pictures, state = rig
    call(registry, "computer_screenshot", window=10)
    out = call(registry, "computer_click", x=1, y=1, screenshot=True)
    assert out.startswith("Clicked left at (1, 1)") and "[file:abc123def456:" in out and len(pictures) == 2


# -- the two stops ----------------------------------------------------------------------------------------------------------

def test_the_mouse_in_a_corner_stops_every_call_for_a_while(rig):
    registry, backend, computer, audit, pictures, state = rig
    clock = {"t": 100.0}
    computer.clock = lambda: clock["t"]
    call(registry, "computer_screenshot", window=10)
    backend.pos = (2, 3)                                                                  # the person pushes the mouse into the top left corner
    out = call(registry, "computer_click", x=5, y=5)
    assert out["error"].startswith("Stopped: the person moved the mouse into a corner") and "wait until they ask" in out["error"]
    assert ("corner_stop", {"x": 2, "y": 3}) in audit
    backend.pos = (500, 500)                                                              # they take it away: still stopped for a while
    for name, args in (("computer_click", {"x": 5, "y": 5}), ("computer_type_text", {"text": "a"}), ("computer_screenshot", {}), ("computer_list_windows", {}), ("computer_wait", {"seconds": 1})):
        assert "Stopped" in call(registry, name, **args)["error"], name
    assert not [e for e in backend.events if e[0] in ("button", "char")]
    clock["t"] += 21
    assert call(registry, "computer_click", x=5, y=5).startswith("Clicked")


def test_the_agent_does_not_aim_at_a_corner(rig):
    registry, backend, *_ = rig
    call(registry, "computer_screenshot", monitor=1)
    assert "corners of the screen" in call(registry, "computer_click", x=2, y=2)["error"]
    call(registry, "computer_screenshot", monitor=2)
    assert "corners of the screen" in call(registry, "computer_move", x=1567, y=881)["error"]


def test_stop_of_the_turn_ends_a_call_and_the_ones_behind_it(rig):
    registry, backend, computer, audit, *_ = rig
    call(registry, "computer_screenshot", window=10)
    stop = threading.Event()
    with cancel_scope(stop):
        assert call(registry, "computer_click", x=5, y=5).startswith("Clicked")
        stop.set()
        for name, args in (("computer_click", {"x": 5, "y": 5}), ("computer_key", {"combo": "a"}), ("computer_type_text", {"text": "hi"}), ("computer_wait", {"seconds": 5})):
            assert "pressed Stop" in call(registry, name, **args)["error"], name
    backend.events.clear()
    stop2 = threading.Event()
    with cancel_scope(stop2):
        original = computer.sleep
        computer.sleep = lambda s: stop2.set() if len([e for e in backend.events if e[0] == "wheel"]) >= 5 else None      # Stop arrives while a long scroll runs
        out = call(registry, "computer_scroll", direction="down", amount=30)
        computer.sleep = original
    assert "pressed Stop" in out["error"] and len([e for e in backend.events if e[0] == "wheel"]) < 30


def test_a_system_failure_is_told_not_raised(rig):
    registry, backend, *_ = rig
    backend.windows = lambda: (_ for _ in ()).throw(OSError("access denied"))
    assert "could not do that: OSError: access denied" in call(registry, "computer_list_windows")["error"]


# -- the systems -------------------------------------------------------------------------------------------------------------

def test_an_unsupported_system_says_so_clearly(tmp_path):
    registry = ToolRegistry(default_timeout=None)
    register_computer_tools(registry, None, backend=UnsupportedBackend("this Linux session has no X11 display (it is Wayland)"), sleep=lambda s: None)
    out = registry.execute("computer_list_windows", {})
    assert out == {"error": "computer control is not available here: this Linux session has no X11 display (it is Wayland)"}
    assert "not available here" in registry.execute("computer_key", {"combo": "a"})["error"]
    assert "not available here" in registry.execute("computer_wait", {"seconds": 1})["error"]


def test_the_backend_is_picked_by_system(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert "not supported on this system (darwin)" in pick_backend().unavailable()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert "Wayland" in pick_backend().unavailable()
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert "xdotool is not installed" in pick_backend().unavailable()


class Runner:
    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, argv):
        import subprocess
        self.calls.append(argv)
        out = self.answers.get(argv[1] if argv[0] == "xdotool" else argv[0], "")
        return subprocess.CompletedProcess(argv, 0, out.encode() if isinstance(out, str) else out, b"")


def test_x11_runs_xdotool_for_input_and_reads_screens_and_windows():
    from tadamoo.computer.x11 import X11Backend
    run = Runner({"xrandr": "Screen 0: minimum 8 x 8\nHDMI-1 connected 2560x1440+1920+0 (normal)\neDP-1 connected primary 1920x1080+0+0 (normal)\nDP-2 disconnected (normal)\n",
                  "getmouselocation": "X=12\nY=34\nSCREEN=0\n", "getactivewindow": "77\n", "search": "77\n78\n", "getwindowname": "A title\n",
                  "getwindowgeometry": "WINDOW=77\nX=5\nY=6\nWIDTH=700\nHEIGHT=500\nSCREEN=0\n", "getwindowpid": "999999999\n"})
    b = X11Backend(run)
    assert [(m.index, m.left, m.width, m.primary) for m in b.monitors()] == [(1, 0, 1920, True), (2, 1920, 2560, False)]
    assert b.cursor() == (12, 34)
    wins = b.windows()
    assert [(w.id, w.title, w.left, w.width, w.foreground) for w in wins] == [(77, "A title", 5, 700, True), (78, "A title", 5, 700, False)]
    run.calls.clear()
    b.move_to(10, 20)
    b.button("right", True)
    b.button("right", False)
    b.wheel(0, 1)
    b.wheel(-1, 0)
    b.key("ctrl", True)
    b.key("pagedown", False)
    b.key("f5", True)
    b.char("é")
    b.focus(77)
    assert run.calls == [["xdotool", "mousemove", "10", "20"], ["xdotool", "mousedown", "3"], ["xdotool", "mouseup", "3"], ["xdotool", "click", "5"], ["xdotool", "click", "6"],
                         ["xdotool", "keydown", "ctrl"], ["xdotool", "keyup", "Next"], ["xdotool", "keydown", "F5"], ["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", "é"],
                         ["xdotool", "windowactivate", "--sync", "77"]]
    with pytest.raises(ComputerError, match="no accessibility tree"):
        b.ui_tree(77, 10)


def test_x11_names_what_is_missing_for_pictures(monkeypatch):
    from tadamoo.computer.x11 import X11Backend
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ComputerError, match="install ImageMagick"):
        X11Backend(Runner()).capture(0, 0, 10, 10, 10, 10)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/import" if name == "import" else None)
    run = Runner({"import": b"\x89PNGdata"})
    assert X11Backend(run).capture(100, 50, 800, 600, 400, 300) == b"\x89PNGdata"
    assert run.calls[0][:3] == ["import", "-silent", "-window"] and "800x600+100+50" in run.calls[0] and "400x300!" in run.calls[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_the_windows_backend_has_the_structure_sizes_and_key_tables_windows_wants():
    import ctypes
    from tadamoo.computer import windows as w
    assert ctypes.sizeof(w.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert ctypes.sizeof(w.MONITORINFO) == 40 and ctypes.sizeof(w.BITMAPINFOHEADER) == 40
    assert w.virtual_key("enter") == 0x0D and w.virtual_key("a") == 0x41 and w.virtual_key("5") == 0x35 and w.virtual_key("f12") == 0x7B and w.virtual_key("win") == 0x5B
    assert w.utf16_units("a") == [0x61] and w.utf16_units("\U0001F600") == [0xD83D, 0xDE00]
    with pytest.raises(ComputerError):
        w.virtual_key("banana")
    for combo in ("Ctrl+Shift+S", "Alt+F4", "Win+D", "PageDown", "ctrl+/", "f9"):
        for key in parse_combo(combo):
            w.virtual_key(key)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_the_accessibility_reader_script_is_valid_powershell():
    import shutil
    import subprocess
    from tadamoo.computer.windows import SCRIPT
    shell = shutil.which("powershell")
    if not shell:
        pytest.skip("no PowerShell")
    check = "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$e); $e.Count" % SCRIPT
    done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", check], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "0", done.stdout + done.stderr


# -- the setting -------------------------------------------------------------------------------------------------------------

def test_the_choice_of_screen_is_kept_and_read_each_time(tmp_path):
    prefs = ComputerPrefs(tmp_path / "computer.json")
    assert prefs.read() == {"enabled": False, "monitor": 0, "backend": "auto"}
    assert prefs.write(True, 2) == {"enabled": True, "monitor": 2, "backend": "auto"}
    assert prefs.monitor() == 2
    assert prefs.write(True, 99)["monitor"] == 0 and prefs.write(True, True)["monitor"] == 0        # not a screen number: all screens
    assert prefs.write(True, 1, "native")["backend"] == "native" and prefs.write(True, 1, "banana")["backend"] == "auto"
    (tmp_path / "computer.json").write_text("not json", encoding="utf-8")
    assert prefs.read() == {"enabled": False, "monitor": 0, "backend": "auto"}


# -- the agent stack -----------------------------------------------------------------------------------------------------------

def test_the_options_reach_the_agent_and_the_tools_are_there_only_when_asked(tmp_path, monkeypatch):
    from tadamoo.headless import config_from_args, parse_args
    from tadamoo.stack import build_agent
    monkeypatch.setattr("tadamoo.computer.tools.pick_backend", lambda *a, **k: FakeBackend())
    prefs = tmp_path / "computer.json"
    prefs.write_text(json.dumps({"enabled": True, "monitor": 2}), encoding="utf-8")
    base = ["-p", "task", "--workspace", str(tmp_path), "--no-memory"]
    off = build_agent(config_from_args(parse_args(base)), client=object())
    assert not [n for n in off.agent.tools.names() if n.startswith("computer_")]
    on = build_agent(config_from_args(parse_args([*base, "--computer", "--computer-prefs", str(prefs)])), client=object())
    names = [n for n in on.agent.tools.names() if n.startswith("computer_")]
    assert "computer_click" in names and "computer_ui_tree" in names and "computer_screenshot" not in names     # no folder for pictures here: the model looks through the tree
