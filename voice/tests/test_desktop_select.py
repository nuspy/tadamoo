"""Scelta dell'implementazione desktop per sistema e sessione, messaggi di Unsupported."""
import pytest

from callbridge import desktop
from callbridge.desktop import DesktopUnsupported, factory, parse_hotkey
from callbridge.desktop.unsupported import MACOS, WAYLAND, NullOverlay, UnsupportedDesktop


def test_la_sessione_si_riconosce_dalle_variabili():
    st = factory.session_type
    assert st({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}) == "x11"
    assert st({"DISPLAY": ":0"}) == "x11"
    assert st({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}) == "wayland"
    assert st({"WAYLAND_DISPLAY": "wayland-0"}) == "wayland"
    # XWayland: DISPLAY c'e' ma la sessione e' Wayland
    assert st({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}) == "wayland"
    assert st({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-0",
               "DISPLAY": ":0"}) == "x11"
    assert st({}) == "none"
    assert st({"XDG_SESSION_TYPE": "tty"}) == "none"


@pytest.mark.parametrize("env", [
    {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"},
    {"WAYLAND_DISPLAY": "wayland-0"},
])
def test_wayland_non_e_supportato_e_lo_dice(env):
    d = factory.choose("linux", env)
    assert isinstance(d, UnsupportedDesktop)
    ok, why = d.available()
    assert not ok and why == WAYLAND and "Wayland" in why and "X11" in why
    with pytest.raises(DesktopUnsupported, match="Wayland"):
        d.key_hook("t", [])
    for call in (d.clipboard.sequence, d.clipboard.read, d.clipboard.send_paste,
                 d.clipboard.send_copy, d.clipboard.modifiers_down,
                 lambda: d.clipboard.write_text("x"), d.foreground.is_terminal,
                 d.foreground.identity):
        with pytest.raises(DesktopUnsupported, match="X11"):
            call()


def test_l_overlay_di_un_sistema_non_supportato_non_ferma_il_servizio():
    ov = factory.choose("linux", {"XDG_SESSION_TYPE": "wayland"}).new_overlay()
    assert isinstance(ov, NullOverlay)
    ov.set_dictation(True, 0.5)
    ov.show_clipboard({})
    ov.post(lambda: None)
    assert ov.get_root() is None


def test_linux_senza_display_non_e_supportato():
    d = factory.choose("linux", {})
    assert isinstance(d, UnsupportedDesktop)
    assert "DISPLAY" in d.available()[1]


def test_macos_ha_il_suo_messaggio():
    d = factory.choose("darwin", {"DISPLAY": ":0"})
    ok, why = d.available()
    assert not ok and why == MACOS and "macOS" in why
    with pytest.raises(DesktopUnsupported, match="macOS"):
        d.key_hook("t", [])


def test_altri_sistemi():
    ok, why = factory.choose("freebsd14", {}).available()
    assert not ok and "freebsd14" in why


def test_x11_si_sceglie_con_una_sessione_x11(monkeypatch):
    d = factory.choose("linux", {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})
    assert d.name == "x11" and d.can_suppress is False
    assert factory.choose("linux2", {"DISPLAY": ":1"}).name == "x11"


def test_windows_si_sceglie_su_win32(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(factory, "_load_windows", lambda: sentinel)
    assert factory.choose("win32", {}) is sentinel


def test_windows_senza_pywin32_diventa_non_supportato(monkeypatch):
    def manca():
        raise ImportError("No module named 'win32clipboard'")
    monkeypatch.setattr(factory, "_load_windows", manca)
    d = factory.choose("win32", {})
    assert isinstance(d, UnsupportedDesktop) and "win32clipboard" in d.available()[1]


def test_platform_si_crea_una_volta(monkeypatch):
    monkeypatch.setattr(factory, "_cached", None)
    calls = []
    monkeypatch.setattr(factory, "choose",
                        lambda p, e: calls.append(p) or UnsupportedDesktop("x"))
    assert desktop.platform() is desktop.platform()
    assert len(calls) == 1
    monkeypatch.setattr(factory, "_cached", None)


def test_hotkey_nomi_neutri():
    assert parse_hotkey("alt+space") == (frozenset({"alt"}), "space")
    assert parse_hotkey("Ctrl + Alt + Up") == (frozenset({"ctrl", "alt"}), "up")
    assert parse_hotkey("control+super+f9") == (frozenset({"ctrl", "win"}), "f9")
    assert parse_hotkey("alt+spazio") == (frozenset({"alt"}), "space")
    assert parse_hotkey("shift+escape") == (frozenset({"shift"}), "esc")
    assert parse_hotkey("ctrl+k") == (frozenset({"ctrl"}), "k")
    assert parse_hotkey("ctrl+alt")[1] is None
    assert parse_hotkey("alt+nonesiste")[1] is None
    assert parse_hotkey("") == (frozenset(), None)
