"""Dettatura sul livello desktop con hotkey, appunti, overlay, microfono e STT FINTI."""
import threading
from types import SimpleNamespace

import numpy as np

from callbridge import dictation, engines
from callbridge.desktop import Desktop, DesktopUnsupported
from callbridge.desktop.unsupported import WAYLAND, UnsupportedDesktop
from callbridge.stt_client import STT_RATE


class FakeHook:
    def __init__(self, name, bindings, ok=True):
        self.name, self.bindings, self.ok = name, bindings, ok
        self.started = self.stopped = False

    def start(self):
        self.started = True
        return self.ok

    def stop(self):
        self.stopped = True


class FakeClipboard:
    def __init__(self, content="prima", terminal=False):
        self.content, self.log, self.terminal = content, [], terminal
        self.pasted = threading.Event()

    def read(self, honor_exclude=True):
        return ("text", self.content)

    def write_text(self, text, exclude=False):
        self.log.append(("write", text, exclude))
        self.content = text
        return 1

    def write_image(self, img, exclude=False):
        self.log.append(("image", exclude))
        return 1

    def send_paste(self):
        # i tasti scelti dalla piattaforma: terminale o app normale
        self.log.append(("paste", "terminal" if self.terminal else "normal"))
        self.pasted.set()


class FakeDesktop(Desktop):
    name = "fake"

    def __init__(self, clipboard=None, hook_ok=True):
        self._clip = clipboard or FakeClipboard()
        self.hooks = []
        self.hook_ok = hook_ok

    def available(self):
        return True, ""

    clipboard = property(lambda self: self._clip)
    foreground = property(lambda self: SimpleNamespace(is_terminal=lambda: False))

    def key_hook(self, name, bindings):
        h = FakeHook(name, bindings, self.hook_ok)
        self.hooks.append(h)
        return h

    def new_overlay(self):
        return None


class FakeStt:
    state, error, pid = "running", None, 1
    info = {"model": "tiny"}

    def __init__(self, text="ciao a tutti"):
        self.text, self.loaded = text, threading.Event()
        self.heard = []

    def load(self):
        self.loaded.set()

    def transcribe_timed(self, audio, language):
        self.heard.append((len(audio), language))
        return self.text, 7


class FakeStream:
    def __init__(self, cb):
        self.cb, self.started, self.stopped, self.closed = cb, False, False, False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeOverlay:
    def __init__(self):
        self.calls = []

    def set_dictation(self, active, level=0.0, phase="recording"):
        self.calls.append((active, phase))


def _cfg(keep=True, hotkey="alt+space", enabled=True):
    return SimpleNamespace(dictation_enabled=enabled, dictation_hotkey=hotkey,
                           dictation_keep_clipboard=keep, stt_language="it",
                           stt_model="tiny")


def _manager(monkeypatch, desk, stt=None, cfg=None, overlay=None):
    stt = stt or FakeStt()
    monkeypatch.setattr(engines, "_get_stt_engine", lambda c: stt)
    monkeypatch.setattr(dictation, "_current", None)
    monkeypatch.setattr(dictation.time, "sleep", lambda s: None)
    streams = []

    def open_stream(cb):
        streams.append(FakeStream(cb))
        return streams[-1]
    m = dictation.DictationManager(cfg or _cfg(), overlay=overlay, desktop=desk,
                                   open_stream=open_stream)
    return m, stt, streams


def _speak(stream, seconds=0.5):
    n = int(STT_RATE * seconds)
    stream.cb(np.full((n, 1), 0.1, dtype=np.float32), n, None, None)


def test_start_registra_la_hotkey_del_config_e_carica_lo_stt(monkeypatch):
    desk = FakeDesktop()
    m, stt, _ = _manager(monkeypatch, desk, cfg=_cfg(hotkey="ctrl+shift+f9"))
    assert m.start() is True and m.active
    hook = desk.hooks[0]
    assert hook.name == "dettatura" and hook.started
    (b,) = hook.bindings
    assert (b.mods, b.key) == (frozenset({"ctrl", "shift"}), "f9")
    assert b.on_press == m._begin and b.on_release == m._end
    assert stt.loaded.wait(2.0)
    assert m.status()["hotkey"] == "ctrl+shift+f9"
    m.stop()
    assert hook.stopped and not m.active


def test_dettatura_disattivata_o_hotkey_senza_tasto_non_parte(monkeypatch):
    desk = FakeDesktop()
    m, _, _ = _manager(monkeypatch, desk, cfg=_cfg(enabled=False))
    assert m.start() is False and desk.hooks == []
    assert m.start(force=True) is True
    m2, _, _ = _manager(monkeypatch, FakeDesktop(), cfg=_cfg(hotkey="alt+"))
    assert m2.start() is False


def test_hook_che_non_parte_lascia_la_dettatura_ferma(monkeypatch):
    m, _, _ = _manager(monkeypatch, FakeDesktop(hook_ok=False))
    assert m.start() is False and not m.active
    assert m.status()["state"] == "stopped"


def test_su_wayland_la_dettatura_dice_perche_non_parte(monkeypatch):
    m, _, _ = _manager(monkeypatch, UnsupportedDesktop(WAYLAND))
    assert m.start() is False
    st = m.status()
    assert st["state"] == "unsupported" and "Wayland" in st["detail"]


def test_il_ciclo_tieni_premuto_trascrivi_incolla(monkeypatch):
    clip = FakeClipboard()
    desk = FakeDesktop(clip)
    overlay = FakeOverlay()
    m, stt, streams = _manager(monkeypatch, desk, overlay=overlay)
    m.start()
    (b,) = desk.hooks[0].bindings
    b.on_press()
    assert streams[0].started and m._recording
    _speak(streams[0])
    b.on_release()
    assert streams[0].stopped and streams[0].closed
    assert clip.pasted.wait(2.0)
    assert stt.heard == [(int(STT_RATE * 0.5), "it")]
    # keep_clipboard: il testo resta negli appunti, non marcato da escludere
    assert clip.log == [("write", "ciao a tutti", False), ("paste", "normal")]
    assert m.last["audio_s"] == 0.5
    assert (True, "transcribing") in overlay.calls and overlay.calls[-1][0] is False


def test_incolla_con_i_tasti_che_la_piattaforma_sceglie(monkeypatch):
    clip = FakeClipboard(terminal=True)
    m, _, streams = _manager(monkeypatch, FakeDesktop(clip))
    m._paste("ls")
    assert clip.log[-1] == ("paste", "terminal")


def test_senza_keep_il_contenuto_di_prima_torna(monkeypatch):
    clip = FakeClipboard("prima")
    done = threading.Event()
    real = threading.Thread

    class Inline(real):
        def start(self):
            super().start()
            self.join()
            done.set()
    monkeypatch.setattr(threading, "Thread", Inline)
    m, _, _ = _manager(monkeypatch, FakeDesktop(clip), cfg=_cfg(keep=False))
    m._paste("dettato")
    assert done.is_set()
    assert clip.log[0] == ("write", "dettato", True)
    assert clip.log[-1] == ("write", "prima", True) and clip.content == "prima"


def test_registrazione_troppo_breve_o_frase_fantasma_non_incollano(monkeypatch):
    clip = FakeClipboard()
    m, stt, streams = _manager(monkeypatch, FakeDesktop(clip))
    m._begin()
    _speak(streams[0], 0.1)
    m._end()
    assert stt.heard == [] and clip.log == []
    # frase-fantasma di whisper sul silenzio
    m2, _, _ = _manager(monkeypatch, FakeDesktop(clip), stt=FakeStt("Sottotitoli a cura di Qualcuno"))
    m2._transcribe_and_paste(np.zeros(STT_RATE, dtype=np.float32))
    assert clip.log == []


def test_microfono_non_disponibile_non_rompe(monkeypatch):
    desk = FakeDesktop()
    m, _, _ = _manager(monkeypatch, desk)

    def broken(cb):
        raise OSError("nessun microfono")
    m._open_stream = broken
    m._begin()
    assert not m._recording


def test_niente_dettatura_senza_appunti_su_sistema_non_supportato(monkeypatch):
    m, _, _ = _manager(monkeypatch, UnsupportedDesktop(WAYLAND))
    try:
        m._paste("x")
    except DesktopUnsupported as e:
        assert "Wayland" in str(e)
    else:
        raise AssertionError("il paste su un sistema non supportato deve rifiutare")
