"""Cronologia appunti sul livello desktop: appunti, hotkey e overlay FINTI, archivio in cartella temporanea."""
import threading
import time

from callbridge import clipboard_history as ch
from callbridge.clipboard_history import ClipboardHistory, ClipStore
from callbridge.desktop import Desktop
from callbridge.desktop.unsupported import WAYLAND, UnsupportedDesktop


class FakeClip:
    """Appunti in memoria con numero di sequenza e marcatore di esclusione."""

    def __init__(self):
        self.value, self.excluded, self.seq = None, False, 0
        self.log = []
        self.mods = False
        self.selection = None          # cio' che send_copy copia dall'app

    def set(self, text, excluded=False):
        self.value, self.excluded = text, excluded
        self.seq += 1

    def sequence(self):
        return self.seq

    def read(self, honor_exclude=True):
        if self.value is None:
            return None
        if honor_exclude and self.excluded:
            return ("excluded", None)
        return ("text", self.value)

    def write_text(self, text, exclude=False):
        self.log.append(("write", text, exclude))
        self.set(text, exclude)
        return self.seq

    def write_image(self, img, exclude=False):
        return self.seq

    def send_copy(self):
        self.log.append(("copy",))
        if self.selection is not None:
            self.set(self.selection)

    def send_paste(self):
        self.log.append(("paste", self.value))

    def modifiers_down(self):
        return self.mods


class FakeHook:
    def __init__(self, bindings):
        self.bindings, self.started, self.stopped = bindings, False, False

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True


class FakeDesktop(Desktop):
    name = "fake"

    def __init__(self):
        self._clip = FakeClip()
        self.hooks = []

    def available(self):
        return True, ""

    clipboard = property(lambda self: self._clip)
    foreground = property(lambda self: None)

    def key_hook(self, name, bindings):
        self.hooks.append((name, FakeHook(bindings)))
        return self.hooks[-1][1]

    def new_overlay(self):
        return None


class FakeOverlay:
    def __init__(self):
        self.cards = []

    def show_clipboard(self, card):
        self.cards.append(card)


def _hist(tmp_path, monkeypatch):
    monkeypatch.setattr(ch.time, "sleep", lambda s: None)
    desk = FakeDesktop()
    overlay = FakeOverlay()
    h = ClipboardHistory(overlay=overlay, store=ClipStore(tmp_path), desktop=desk)
    h.store.load()
    return h, desk, desk.clipboard, overlay


def test_start_registra_le_quattro_combinazioni_ctrl_alt_frecce(tmp_path, monkeypatch):
    h, desk, clip, _ = _hist(tmp_path, monkeypatch)
    assert h.start() is True
    name, hook = desk.hooks[0]
    assert name == "appunti" and hook.started
    by_key = {b.key: b for b in hook.bindings}
    assert set(by_key) == {"up", "down", "left", "right"}
    for b in hook.bindings:
        assert b.mods == frozenset({"ctrl", "alt"}) and b.exact
    assert by_key["left"].repeat and by_key["right"].repeat
    assert not by_key["up"].repeat and not by_key["down"].repeat
    h.stop()
    assert hook.stopped


def test_su_wayland_la_cronologia_non_parte_e_non_solleva(tmp_path, monkeypatch):
    h = ClipboardHistory(store=ClipStore(tmp_path), desktop=UnsupportedDesktop(WAYLAND))
    assert h.start() is False


def test_cio_che_passa_dagli_appunti_entra_in_cima(tmp_path, monkeypatch):
    h, _, clip, _ = _hist(tmp_path, monkeypatch)
    for t in ("uno", "due", "tre"):
        clip.set(t)
        assert h._capture()["text"] == t
    assert [e["text"] for e in h.store.entries] == ["tre", "due", "uno"]
    assert h._capture() is None                   # stessa sequenza: niente di nuovo
    clip.set("uno")                               # gia' presente: risale
    h._capture()
    assert [e["text"] for e in h.store.entries] == ["uno", "tre", "due"]


def test_il_marcatore_di_esclusione_tiene_fuori_le_password(tmp_path, monkeypatch):
    h, _, clip, _ = _hist(tmp_path, monkeypatch)
    clip.set("hunter2", excluded=True)
    assert h._capture() is None
    assert h.store.entries == []


def test_percorrere_la_cronologia_rimette_l_elemento_negli_appunti_escluso(tmp_path, monkeypatch):
    h, _, clip, overlay = _hist(tmp_path, monkeypatch)
    for t in ("uno", "due", "tre"):
        clip.set(t)
        h._capture()
    h._nav(+1)                                    # precedente = piu' vecchio
    assert clip.log[-1] == ("write", "due", True)
    h._nav(+1)
    assert clip.log[-1] == ("write", "uno", True)
    n = len(clip.log)
    h._nav(+1)                                    # al bordo: non riscrive
    assert len(clip.log) == n
    assert overlay.cards[-1]["title"] == "Il più vecchio"
    h._nav(-1)
    assert clip.log[-1] == ("write", "due", True) and overlay.cards[-1]["pos"] == "2/3"
    # la riscrittura marcata non rientra in cima alla cronologia
    h._capture()
    assert [e["text"] for e in h.store.entries] == ["tre", "due", "uno"]


def test_incolla_l_elemento_selezionato_con_il_tasto_giusto(tmp_path, monkeypatch):
    h, _, clip, overlay = _hist(tmp_path, monkeypatch)
    for t in ("uno", "due"):
        clip.set(t)
        h._capture()
    h._nav(+1)
    clip.set("altro", excluded=True)              # una password copiata nel frattempo
    h._paste()
    assert clip.log[-2:] == [("write", "uno", True), ("paste", "uno")]
    assert overlay.cards[-1]["action"] == "paste"


def test_incolla_aspetta_che_i_modificatori_si_rilascino(tmp_path, monkeypatch):
    h, _, clip, _ = _hist(tmp_path, monkeypatch)
    clip.set("x")
    h._capture()
    calls = []

    def mods():
        calls.append(1)
        return len(calls) < 3
    clip.modifiers_down = mods
    h._paste()
    assert len(calls) == 3 and clip.log[-1] == ("paste", "x")


def test_copia_la_selezione_e_la_registra(tmp_path, monkeypatch):
    h, _, clip, overlay = _hist(tmp_path, monkeypatch)
    clip.selection = "testo selezionato"
    h._copy()
    assert ("copy",) in clip.log
    assert h.store.entries[0]["text"] == "testo selezionato"
    assert overlay.cards[-1]["action"] == "copy"


def test_copia_senza_selezione_lo_dice(tmp_path, monkeypatch):
    h, _, clip, overlay = _hist(tmp_path, monkeypatch)
    t = time.monotonic()
    monkeypatch.setattr(ch.time, "monotonic", iter([t, t + 1, t + 2, t + 3, t + 4]).__next__)
    h._copy()
    assert overlay.cards[-1] == {"action": "empty", "title": "Nessuna selezione da copiare"}


def test_cronologia_vuota(tmp_path, monkeypatch):
    h, _, clip, overlay = _hist(tmp_path, monkeypatch)
    h._nav(+1)
    h._paste()
    assert [c["title"] for c in overlay.cards] == ["Cronologia vuota"] * 2
    assert clip.log == []


def test_il_monitor_registra_le_copie_fatte_dall_utente(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "_POLL_S", 0.01)
    desk = FakeDesktop()
    h = ClipboardHistory(store=ClipStore(tmp_path), desktop=desk)
    h.start()
    try:
        desk.clipboard.set("copiato a mano")
        fine = time.monotonic() + 2
        while time.monotonic() < fine and not h.store.entries:
            threading.Event().wait(0.01)
        assert h.store.entries and h.store.entries[0]["text"] == "copiato a mano"
    finally:
        h.stop()
