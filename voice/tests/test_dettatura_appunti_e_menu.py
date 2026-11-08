"""La dettatura lascia il testo negli appunti e non fa aprire il menu dell'app con Alt."""
import ctypes
import sys
from types import SimpleNamespace

import pytest

from callbridge.desktop import parse_hotkey
from callbridge.dictation import DictationManager

solo_windows = pytest.mark.skipif(sys.platform != "win32", reason="tasti di Windows")


@solo_windows
def test_alt_e_win_vanno_mascherati_ctrl_e_maiusc_no():
    from callbridge.desktop.win_keyhook import needs_menu_mask
    assert needs_menu_mask(parse_hotkey("alt+space")[0])
    assert needs_menu_mask(parse_hotkey("ctrl+alt+up")[0])
    assert needs_menu_mask(frozenset({"win"}))
    assert not needs_menu_mask(frozenset({"ctrl"}))
    assert not needs_menu_mask(frozenset({"shift"}))
    assert not needs_menu_mask(frozenset())


@solo_windows
def test_il_tasto_di_maschera_e_premuto_e_rilasciato(monkeypatch):
    calls = []
    fake = SimpleNamespace(keybd_event=lambda vk, scan, flags, extra: calls.append((vk, flags)))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=fake), raising=False)
    from callbridge.desktop import win_keyhook
    win_keyhook.mask_menu_key()
    assert calls == [(0xE8, 0), (0xE8, 0x0002)]


class _FakeClip:
    def __init__(self, content, log):
        self.content, self.log = content, log

    def read(self, honor_exclude=True):
        return ("text", self.content[0])

    def write_text(self, text, exclude=False):
        self.log.append(("write", text, exclude))
        self.content[0] = text
        return 1

    def write_image(self, *a, **k):
        return 1

    def send_paste(self):
        self.log.append(("paste",))


def _manager(keep: bool, clip=None):
    m = DictationManager.__new__(DictationManager)
    m.cfg = SimpleNamespace(dictation_keep_clipboard=keep)
    m._desk = SimpleNamespace(clipboard=clip)
    return m


def _fake_clip(monkeypatch, content):
    from callbridge import dictation
    log = []
    monkeypatch.setattr(dictation.time, "sleep", lambda s: None)
    return log, _FakeClip(content, log)


def test_il_testo_dettato_resta_negli_appunti_e_si_registra_nella_cronologia(monkeypatch):
    content = ["prima"]
    log, clip = _fake_clip(monkeypatch, content)
    _manager(True, clip)._paste("ciao a tutti")
    assert content[0] == "ciao a tutti"
    assert log == [("write", "ciao a tutti", False), ("paste",)]


def test_senza_l_opzione_si_rimette_il_contenuto_di_prima(monkeypatch):
    import threading
    content = ["prima"]
    log, clip = _fake_clip(monkeypatch, content)
    done = threading.Event()
    real = threading.Thread

    class Inline(real):
        def start(self):
            super().start()
            self.join()
            done.set()

    monkeypatch.setattr(threading, "Thread", Inline)
    _manager(False, clip)._paste("ciao a tutti")
    assert done.is_set()
    assert log[0] == ("write", "ciao a tutti", True)
    assert content[0] == "prima"
