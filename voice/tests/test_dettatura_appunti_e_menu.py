"""La dettatura lascia il testo negli appunti e non fa aprire il menu dell'app con Alt."""
import ctypes
from types import SimpleNamespace

from callbridge import keyhook
from callbridge.dictation import DictationManager
from callbridge.keyhook import VK_ALT, VK_CTRL, VK_SHIFT, VK_WIN, needs_menu_mask, parse_hotkey


def test_alt_e_win_vanno_mascherati_ctrl_e_maiusc_no():
    assert needs_menu_mask(parse_hotkey("alt+space")[0])
    assert needs_menu_mask(parse_hotkey("ctrl+alt+up")[0])
    assert needs_menu_mask(frozenset({VK_WIN}))
    assert not needs_menu_mask(frozenset({VK_CTRL}))
    assert not needs_menu_mask(frozenset({VK_SHIFT}))
    assert not needs_menu_mask(frozenset())


def test_il_tasto_di_maschera_e_premuto_e_rilasciato(monkeypatch):
    calls = []
    fake = SimpleNamespace(keybd_event=lambda vk, scan, flags, extra: calls.append((vk, flags)))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=fake), raising=False)
    keyhook.mask_menu_key()
    assert calls == [(0xE8, 0), (0xE8, 0x0002)]


def _manager(keep: bool):
    m = DictationManager.__new__(DictationManager)
    m.cfg = SimpleNamespace(dictation_keep_clipboard=keep)
    return m


def _fake_clip(monkeypatch, content):
    from callbridge import dictation
    log = []

    def write_text(text, exclude=False):
        log.append(("write", text, exclude))
        content[0] = text
        return 1

    monkeypatch.setattr(dictation.winclip, "read", lambda honor_exclude=True: ("text", content[0]))
    monkeypatch.setattr(dictation.winclip, "write_text", write_text)
    monkeypatch.setattr(dictation.winclip, "write_image", lambda *a, **k: 1)
    monkeypatch.setattr(dictation.winclip, "send_paste", lambda: log.append(("paste",)))
    monkeypatch.setattr(dictation.time, "sleep", lambda s: None)
    return log


def test_il_testo_dettato_resta_negli_appunti_e_si_registra_nella_cronologia(monkeypatch):
    content = ["prima"]
    log = _fake_clip(monkeypatch, content)
    _manager(True)._paste("ciao a tutti")
    assert content[0] == "ciao a tutti"
    assert log == [("write", "ciao a tutti", False), ("paste",)]


def test_senza_l_opzione_si_rimette_il_contenuto_di_prima(monkeypatch):
    import threading
    content = ["prima"]
    log = _fake_clip(monkeypatch, content)
    done = threading.Event()
    real = threading.Thread

    class Inline(real):
        def start(self):
            super().start()
            self.join()
            done.set()

    monkeypatch.setattr(threading, "Thread", Inline)
    _manager(False)._paste("ciao a tutti")
    assert done.is_set()
    assert log[0] == ("write", "ciao a tutti", True)
    assert content[0] == "prima"
