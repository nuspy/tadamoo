"""Push-to-talk behind the desktop interfaces: the same hotkeys on Windows and X11, a refusal elsewhere.

Everything runs with fakes (no hook, no listener, no key sent), on any operating system. A real hotkey press is not covered.
"""
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from callbridge import ptt
from callbridge.config import Config
from callbridge.desktop import Desktop, DesktopUnsupported, choose
from callbridge.desktop import win_pynput_keys
from callbridge.desktop.unsupported import MACOS, WAYLAND
from callbridge.desktop.win_pynput_keys import PynputHotKeys, to_pynput
from callbridge.desktop.x11 import X11Desktop
from callbridge.desktop.x11_keys import X11KeyHook

DEFAULTS = {
    "shift+f1": ("harness", True, "half", False),
    "shift+f2": ("harness", False, "full", False),
    "ctrl+shift+f4": ("direct", True, "half", False),
    "ctrl+shift+f5": ("direct", False, "full", False),
}


class FakeHook:
    def __init__(self, ok=True):
        self.ok, self.started, self.stopped = ok, False, False

    def start(self):
        self.started = self.ok
        return self.ok

    def stop(self):
        self.stopped = True


class RecordingDesktop:
    """What the manager asks of the desktop."""

    def __init__(self, hook=None, raises=None):
        self.calls, self.hook, self.raises = [], hook or FakeHook(), raises

    def push_to_talk_keys(self, name, hotkeys):
        self.calls.append((name, dict(hotkeys)))
        if self.raises:
            raise self.raises
        return self.hook


def _manager(desktop, **kw):
    cfg = Config()
    cfg.push_to_talk_enabled = True
    m = ptt.PushToTalkManager(cfg, brains=object(), desktop=desktop, **kw)
    m.toggled = []
    m._toggle = lambda brain, half_duplex, mode, avatar=False: m.toggled.append((brain, half_duplex, mode, avatar))
    return m


# --- the manager ---------------------------------------------------------------------------

def test_the_manager_hands_the_desktop_neutral_combinations_and_what_each_press_does():
    d = RecordingDesktop()
    m = _manager(d)
    m.start()
    (name, hotkeys), = d.calls
    assert name == "ptt" and set(hotkeys) == set(DEFAULTS) and m.hotkeys_attive and d.hook.started
    for combo, expected in DEFAULTS.items():        # no Ctrl guard in the callbacks: the desktop keeps the combinations apart
        hotkeys[combo]()
        assert m.toggled[-1] == expected


def test_avatar_and_copilot_hotkeys_exist_only_with_their_parts():
    d = RecordingDesktop()
    m = _manager(d, avatar=object(), copilot=SimpleNamespace(toggle=lambda: d.calls.append("copilot")))
    m.start()
    hotkeys = d.calls[0][1]
    assert {"ctrl+shift+f1", "ctrl+shift+f2", "shift+f4"} <= set(hotkeys)
    hotkeys["ctrl+shift+f1"]()
    assert m.toggled[-1] == ("harness", True, "half", True)
    hotkeys["ctrl+shift+f4"]()
    assert m.toggled[-1] == ("direct", True, "half", True)
    hotkeys["shift+f4"]()
    assert d.calls[-1] == "copilot"


def test_stop_stops_the_hook_and_start_is_not_doubled():
    d = RecordingDesktop()
    m = _manager(d)
    m.start()
    m.start()
    assert len(d.calls) == 1
    m.stop()
    assert d.hook.stopped and not m.hotkeys_attive


@pytest.mark.parametrize("why", [WAYLAND, MACOS, "no X11 display (DISPLAY is not set)"])
def test_a_desktop_without_hotkeys_leaves_them_off_and_the_manager_stays_usable(why, caplog):
    m = _manager(RecordingDesktop(raises=DesktopUnsupported(why)))
    with caplog.at_level("ERROR", logger="callbridge.ptt"):
        m.start()
    assert not m.hotkeys_attive and why in caplog.text
    assert m.session_state()["active"] is False              # the manager itself is intact: sessions start from the GUI and the API


def test_a_hook_that_cannot_listen_leaves_them_off():
    m = _manager(RecordingDesktop(hook=FakeHook(ok=False)))
    m.start()
    assert not m.hotkeys_attive


def test_push_to_talk_off_in_the_config_registers_nothing():
    d = RecordingDesktop()
    m = _manager(d)
    m.cfg.push_to_talk_enabled = False
    m.start()
    assert d.calls == [] and not m.hotkeys_attive


def test_the_module_imports_no_hotkey_library():
    code = "import sys; sys.path.insert(0, 'voice'); import callbridge.ptt; print('pynput' in sys.modules)"
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(root))
    assert out.stdout.strip() == "False", out.stderr


# --- the default body (X11, and any desktop that only has key_hook) --------------------------------

class HookDesktop(Desktop):
    name = "fake"

    def __init__(self):
        self.hooks = []

    def available(self):
        return True, ""

    clipboard = foreground = None

    def key_hook(self, name, bindings):
        self.hooks.append((name, bindings))
        return FakeHook()

    def new_overlay(self):
        return None


def test_the_default_builds_exact_unsuppressed_bindings_and_skips_a_key_it_does_not_know(caplog):
    d = HookDesktop()
    with caplog.at_level("ERROR", logger="callbridge.desktop"):
        d.push_to_talk_keys("ptt", {"shift+f1": lambda: None, "ctrl+shift+f4": lambda: None, "shift+f13": lambda: None})
    (name, bindings), = d.hooks
    assert name == "ptt" and "shift+f13" in caplog.text
    by_key = {b.key: b for b in bindings}
    assert set(by_key) == {"f1", "f4"}
    assert by_key["f1"].mods == {"shift"} and by_key["f4"].mods == {"ctrl", "shift"}
    assert all(b.exact and not b.suppress and not b.repeat for b in bindings)


def K(name):
    return SimpleNamespace(name=name)


def _x11_hook(hotkeys):
    hook = X11Desktop(which=lambda n: None, has_module=lambda n: True).push_to_talk_keys("ptt", hotkeys)
    assert isinstance(hook, X11KeyHook)
    hook._listener_factory = lambda on_press, on_release: SimpleNamespace(start=lambda: None, stop=lambda: None)
    assert hook.start()
    return hook


def _drain(hook):
    done = threading.Event()
    hook._events.put(done.set)                       # the worker runs the callbacks in order: this one comes last
    assert done.wait(2.0)


def test_on_x11_shift_f1_and_ctrl_shift_f1_do_not_fire_each_other():
    fired = []
    hook = _x11_hook({"shift+f1": lambda: fired.append("harness"), "ctrl+shift+f1": lambda: fired.append("avatar")})
    hook.on_press(K("shift_l"))
    hook.on_press(K("f1"))
    _drain(hook)
    assert fired == ["harness"]
    hook.on_release(K("f1"))
    hook.on_release(K("shift_l"))
    time.sleep(0.2)                                  # the release is real once no repeat follows within 60 ms
    hook.on_press(K("ctrl_l"))
    hook.on_press(K("shift_l"))
    hook.on_press(K("f1"))
    _drain(hook)
    assert fired == ["harness", "avatar"]
    hook.stop()


def test_on_x11_a_held_key_fires_once():
    fired = []
    hook = _x11_hook({"shift+f2": lambda: fired.append(1)})
    hook.on_press(K("shift"))
    for _ in range(5):                               # auto-repeat
        hook.on_press(K("f2"))
    _drain(hook)
    assert fired == [1]
    hook.stop()


def test_wayland_and_the_others_refuse_with_the_reason():
    for sys_platform, env, why in (("linux", {"XDG_SESSION_TYPE": "wayland"}, WAYLAND),
                                   ("darwin", {}, MACOS),
                                   ("linux", {}, "no X11 display")):
        with pytest.raises(DesktopUnsupported, match=why):
            choose(sys_platform, env).push_to_talk_keys("ptt", {"shift+f1": lambda: None})


def test_x11_session_gets_the_x11_body():
    d = choose("linux", {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})
    assert isinstance(d, X11Desktop)


# --- the Windows body (runs anywhere: pynput and the Ctrl state are faked) ---------------------------

def test_the_combinations_become_pynput_hotkeys():
    assert to_pynput("shift+f1") == "<shift>+<f1>"
    assert to_pynput("Ctrl + Shift+F24") == "<ctrl>+<shift>+<f24>"
    assert to_pynput("alt+a") == "<alt>+a"


class FakePynput:
    def __init__(self, hotkeys):
        self.hotkeys, self.started, self.stopped = hotkeys, False, False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _win_hook(hotkeys, ctrl, **kw):
    made = []
    hook = PynputHotKeys("ptt", hotkeys, factory=lambda hk: made.append(FakePynput(hk)) or made[-1],
                         ctrl_down=lambda: ctrl["down"], **kw)
    return hook, made


def test_windows_ignores_a_combination_without_ctrl_while_ctrl_is_held():
    fired, ctrl = [], {"down": False}
    hook, made = _win_hook({"shift+f1": lambda: fired.append("harness"),
                            "ctrl+shift+f4": lambda: fired.append("direct")}, ctrl)
    assert hook.start() and made[0].started
    assert set(made[0].hotkeys) == {"<shift>+<f1>", "<ctrl>+<shift>+<f4>"}
    made[0].hotkeys["<shift>+<f1>"]()
    ctrl["down"] = True
    made[0].hotkeys["<shift>+<f1>"]()                # Ctrl+Shift+F1 also fires <shift>+<f1> in pynput: ignored
    made[0].hotkeys["<ctrl>+<shift>+<f4>"]()         # a Ctrl combination is not guarded
    assert fired == ["harness", "direct"]
    assert hook.start() and len(made) == 1           # already listening
    hook.stop()
    assert made[0].stopped
    hook.stop()


def test_windows_says_so_when_pynput_cannot_start(caplog):
    def broken(hotkeys):
        raise ValueError("bad hotkey")
    hook = PynputHotKeys("ptt", {"shift+f1": lambda: None}, factory=broken)
    with caplog.at_level("ERROR", logger="callbridge.desktop.pttkeys"):
        assert hook.start() is False
    assert "bad hotkey" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows has the Windows desktop")
def test_the_windows_desktop_uses_the_pynput_listener_for_push_to_talk():
    from callbridge.desktop.windows import WindowsDesktop
    hook = WindowsDesktop().push_to_talk_keys("ptt", {"shift+f1": lambda: None})
    assert isinstance(hook, win_pynput_keys.PynputHotKeys)
