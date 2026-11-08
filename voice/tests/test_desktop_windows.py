"""Windows: l'adattatore delle primitive esistenti e l'hook con il proc chiamato a mano (nessun hook vero)."""
import ctypes
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="solo Windows")

if sys.platform == "win32":
    from callbridge.desktop import Binding, win_clipboard, win_keyhook
    from callbridge.desktop.windows import WindowsDesktop


def test_la_piattaforma_windows_sopprime_i_tasti():
    d = WindowsDesktop()
    assert d.name == "windows" and d.can_suppress and d.available() == (True, "")
    assert isinstance(d.key_hook("t", [Binding(frozenset({"alt"}), "space", lambda: None)]),
                      win_keyhook.LowLevelKeyHook)


def test_gli_appunti_delegano_alle_funzioni_di_win_clipboard(monkeypatch):
    calls = []
    for name, ret in [("sequence", 7), ("read", ("text", "x")), ("write_text", 9),
                      ("write_image", 10), ("send_copy", None), ("send_paste", None),
                      ("modifiers_down", True), ("foreground_is_terminal", True),
                      ("_foreground", ("cls", "exe.exe"))]:
        monkeypatch.setattr(win_clipboard, name,
                            lambda *a, _n=name, _r=ret: calls.append((_n, a)) or _r)
    d = WindowsDesktop()
    c = d.clipboard
    assert c.sequence() == 7
    assert c.read(False) == ("text", "x")
    assert c.write_text("t", True) == 9 and c.write_image("img", False) == 10
    c.send_copy()
    c.send_paste()
    assert c.modifiers_down() is True
    assert d.foreground.is_terminal() is True
    assert d.foreground.identity() == ("cls", "exe.exe")
    assert calls[:5] == [("sequence", ()), ("read", (False,)), ("write_text", ("t", True)),
                         ("write_image", ("img", False)), ("send_copy", ())]


def test_le_hotkey_si_traducono_in_codici_virtuali():
    h = win_keyhook.LowLevelKeyHook("t", [
        Binding(frozenset({"ctrl", "alt"}), "up", lambda: None, exact=True),
        Binding(frozenset({"alt"}), "space", lambda: None)])
    up, = h._by_vk[0x26]
    assert up.mods == {0x11, 0x12}
    space, = h._by_vk[0x20]
    assert space.mods == {0x12}
    assert win_keyhook.LowLevelKeyHook("t", [Binding(frozenset(), "zzz", lambda: None)]).valid is False


def _proc_hook(binding, monkeypatch):
    h = win_keyhook.LowLevelKeyHook("t", [binding])
    pass_through = object()
    fake = SimpleNamespace(GetAsyncKeyState=lambda vk: 0x8000,
                           CallNextHookEx=lambda *a: pass_through)
    h._user32 = fake
    monkeypatch.setattr(win_keyhook, "mask_menu_key", lambda: None)

    def feed(vk, msg):
        kb = win_keyhook._KBDLLHOOKSTRUCT(vkCode=vk, flags=0)
        return h._proc(0, msg, ctypes.addressof(kb))
    return h, feed, pass_through


LALT, SPACE = 0xA4, 0x20  # LAlt, Space


def test_hold_to_talk_sopprime_pressione_ripetizioni_e_rilascio(monkeypatch):
    h, feed, passa = _proc_hook(Binding(frozenset({"alt"}), "space", lambda: None), monkeypatch)
    assert feed(LALT, 0x0104) is passa                 # Alt da solo passa
    assert feed(SPACE, 0x0104) == 1                    # sopprime
    assert feed(SPACE, 0x0104) == 1                    # auto-repeat inghiottito
    assert feed(SPACE, 0x0105) == 1                    # sopprime anche l'up
    assert feed(SPACE, 0x0104) == 1                    # di nuovo premibile
    events = []
    while not h._events.empty():
        events.append(h._events.get())
    assert len(events) == 4                            # mask, press, mask, press (no release callback)


def test_senza_i_modificatori_il_tasto_passa(monkeypatch):
    h, feed, passa = _proc_hook(Binding(frozenset({"alt"}), "space", lambda: None), monkeypatch)
    assert feed(SPACE, 0x0100) is passa


def test_con_suppress_falso_il_tasto_arriva_all_app(monkeypatch):
    h, feed, passa = _proc_hook(
        Binding(frozenset({"alt"}), "space", lambda: None, lambda: None, suppress=False), monkeypatch)
    feed(LALT, 0x0104)
    assert feed(SPACE, 0x0104) is passa
    assert feed(SPACE, 0x0104) is passa
    assert feed(SPACE, 0x0105) is passa
    fns = []
    while not h._events.empty():
        fns.append(h._events.get())
    assert len(fns) == 2                               # press e release, nessuna maschera del menu


def test_exact_non_scatta_con_modificatori_in_piu(monkeypatch):
    h, feed, passa = _proc_hook(
        Binding(frozenset({"ctrl", "alt"}), "up", lambda: None, exact=True), monkeypatch)
    for vk in (0xA2, 0xA4, 0xA0):                      # ctrl, alt, shift
        feed(vk, 0x0104)
    assert feed(0x26, 0x0104) is passa
