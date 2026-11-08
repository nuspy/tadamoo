"""Linux X11: righe di comando, hotkey e appunti con il livello dei processi e di pynput FINTO.

Scritto per X11 e provato solo con finti: nessun comando vero parte e nessun
tasto viene intercettato o inviato.
"""
import io
import subprocess
import threading
from types import SimpleNamespace

import pytest

from callbridge.desktop import Binding, DesktopUnsupported
from callbridge.desktop import x11_tools
from callbridge.desktop.x11 import X11Desktop
from callbridge.desktop.x11_clipboard import X11Clipboard, X11Foreground, modifiers_pressed
from callbridge.desktop.x11_keys import KeySender, X11KeyHook, classify
from callbridge.desktop.x11_tools import X11Error


# --- tool eseguiti ---------------------------------------------------------------

def test_run_tool_in_lettura_cattura_l_output(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return SimpleNamespace(stdout=b"ok")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert x11_tools.run_tool(["xclip", "-o"]) == b"ok"
    assert seen["stdout"] == subprocess.PIPE and seen["input"] is None


def test_run_tool_in_scrittura_non_tiene_le_pipe_aperte(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: seen.update(kw) or SimpleNamespace(stdout=None))
    assert x11_tools.run_tool(["xclip", "-i"], b"dati") == b""
    assert seen["input"] == b"dati" and seen["stdout"] == subprocess.DEVNULL


def test_run_tool_errori_diventano_x11error(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(X11Error, match="xclip"):
        x11_tools.run_tool(["xclip"])


# --- finti ----------------------------------------------------------------------

class FakeX:
    """Risponde ai comandi come xclip/xsel/xprop e li registra."""

    def __init__(self, targets=None, text=None, png=None):
        self.cmds = []
        self.targets = targets if targets is not None else []
        self.text = text
        self.png = png

    def __call__(self, cmd, data=None):
        self.cmds.append((cmd, data))
        if cmd[0] == "xclip" and "-o" in cmd:
            t = cmd[cmd.index("-t") + 1]
            if t == "TARGETS":
                if not self.targets:
                    raise X11Error("xclip failed")
                return "\n".join(self.targets).encode()
            return self.png if t == "image/png" else self.text
        if cmd[0] == "xsel" and "--output" in cmd:
            return self.text or b""
        if cmd[0] == "xclip" and "-i" in cmd:
            t = cmd[cmd.index("-t") + 1]
            if t == "image/png":
                self.png, self.targets = data, ["TARGETS", "image/png"]
            else:
                self.text, self.targets = data, ["TARGETS", "UTF8_STRING"]
        if cmd[0] == "xsel" and "--input" in cmd:
            self.text = data
        return b""


class FakeKeys:
    def __init__(self):
        self.chords = []

    def chord(self, keys):
        self.chords.append(keys)


def _clip(tool="xclip", x=None, term=False, mods=lambda: False):
    x = x or FakeX()
    keys = FakeKeys()
    fg = SimpleNamespace(is_terminal=lambda: term)
    c = X11Clipboard(fg, keys, which=lambda n: "/usr/bin/" + n if n == tool else None,
                     run=x, modifiers=mods)
    return c, x, keys


# --- appunti ---------------------------------------------------------------------

def test_senza_xclip_ne_xsel_si_dice_cosa_installare():
    c, _, _ = _clip(tool="nessuno")
    with pytest.raises(DesktopUnsupported, match="xclip or xsel"):
        c.read()


def test_lettura_testo_con_xclip():
    c, x, _ = _clip(x=FakeX(["TARGETS", "UTF8_STRING"], "ciao è".encode()))
    assert c.read() == ("text", "ciao è")
    assert x.cmds[0][0] == ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"]
    assert x.cmds[1][0] == ["xclip", "-selection", "clipboard", "-o", "-t", "UTF8_STRING"]


def test_appunti_vuoti_si_leggono_come_vuoti():
    c, _, _ = _clip(x=FakeX([]))
    assert c.read() is None


def test_lettura_immagine_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(buf, "PNG")
    c, x, _ = _clip(x=FakeX(["TARGETS", "image/png"], png=buf.getvalue()))
    kind, img = c.read()
    assert kind == "image" and img.size == (3, 2)
    assert x.cmds[1][0][-1] == "image/png"


def test_il_testo_ha_la_precedenza_sull_immagine():
    c, _, _ = _clip(x=FakeX(["TARGETS", "image/png", "UTF8_STRING"], b"t", b"png"))
    assert c.read() == ("text", "t")


def test_scrittura_testo_con_xclip():
    c, x, _ = _clip()
    seq = c.write_text("ciao")
    assert seq > 0
    assert x.cmds[0] == (["xclip", "-selection", "clipboard", "-i", "-t", "UTF8_STRING"],
                         b"ciao")


def test_scrittura_immagine_come_png():
    from PIL import Image
    c, x, _ = _clip()
    assert c.write_image(Image.new("RGB", (2, 2), "blue")) > 0
    cmd, data = x.cmds[0]
    assert cmd == ["xclip", "-selection", "clipboard", "-i", "-t", "image/png"]
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_xsel_gestisce_solo_il_testo():
    c, x, _ = _clip(tool="xsel", x=FakeX(text=b"abc"))
    assert c.read() == ("text", "abc")
    assert c.write_text("zzz") > 0
    assert (["xsel", "--clipboard", "--input"], b"zzz") in x.cmds
    assert c.write_image(object()) == 0


def test_scrittura_fallita_ritorna_zero():
    def fail(cmd, data=None):
        raise X11Error("xclip failed")
    c = X11Clipboard(SimpleNamespace(), FakeKeys(), which=lambda n: n, run=fail)
    assert c.write_text("x") == 0


def test_il_numero_di_sequenza_cambia_solo_se_il_contenuto_cambia():
    x = FakeX(["TARGETS", "UTF8_STRING"], b"uno")
    c, _, _ = _clip(x=x)
    a = c.sequence()
    assert c.sequence() == a
    x.text = b"due"
    b = c.sequence()
    assert b != a and c.sequence() == b
    assert c.write_text("tre") != b


def test_marcatore_dei_password_manager_si_rispetta():
    c, _, _ = _clip(x=FakeX(["TARGETS", "UTF8_STRING", "x-kde-passwordManagerHint"], b"segreta"))
    assert c.read() == ("excluded", None)
    assert c.read(honor_exclude=False) == ("text", "segreta")


def test_cio_che_scriviamo_come_escluso_si_rilegge_escluso():
    c, _, _ = _clip()
    c.write_text("passaggio temporaneo", exclude=True)
    assert c.read() == ("excluded", None)
    assert c.read(honor_exclude=False) == ("text", "passaggio temporaneo")
    c.write_text("passaggio temporaneo", exclude=False)
    assert c.read() == ("text", "passaggio temporaneo")


def test_un_contenuto_nuovo_non_e_escluso():
    x = FakeX()
    c, _, _ = _clip(x=x)
    c.write_text("a", exclude=True)
    x.text, x.targets = b"b", ["TARGETS", "UTF8_STRING"]
    assert c.read() == ("text", "b")


# --- tasti di incolla e copia -----------------------------------------------------

def test_incolla_con_ctrl_v_nelle_app_normali():
    c, _, keys = _clip(term=False)
    c.send_paste()
    c.send_copy()
    assert keys.chords == [("ctrl", "v"), ("ctrl", "c")]


def test_nei_terminali_si_usa_ctrl_maiusc():
    c, _, keys = _clip(term=True)
    c.send_paste()
    c.send_copy()
    assert keys.chords == [("ctrl", "shift", "v"), ("ctrl", "shift", "c")]


def test_xdotool_con_clearmodifiers():
    runs = []
    KeySender(which=lambda n: "/usr/bin/xdotool", run=runs.append).chord(("ctrl", "shift", "v"))
    assert runs == [["xdotool", "key", "--clearmodifiers", "ctrl+shift+v"]]


def test_senza_xdotool_si_usa_pynput():
    log = []

    class Ctl:
        def pressed(self, *mods):
            log.append(("pressed", mods))
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            log.append("up")

        def press(self, k):
            log.append(("press", k))

        def release(self, k):
            log.append(("release", k))

    kb = SimpleNamespace(Controller=Ctl, Key=SimpleNamespace(ctrl="K_CTRL", shift="K_SHIFT"))
    KeySender(which=lambda n: None, keyboard_module=kb).chord(("ctrl", "shift", "v"))
    assert log == [("pressed", ("K_CTRL", "K_SHIFT")), ("press", "v"), ("release", "v"), "up"]


# --- modificatori premuti -----------------------------------------------------------

class FakeDisplay:
    def __init__(self, down_codes):
        self.keymap = [0] * 32
        for code in down_codes:
            self.keymap[code // 8] |= 1 << (code % 8)
        self.closed = False

    def query_keymap(self):
        return self.keymap

    def keysym_to_keycode(self, sym):
        return {"Control_L": 37, "Alt_L": 64, "Shift_L": 50}.get(sym, 0)

    def close(self):
        self.closed = True


def test_modificatori_premuti_dalla_mappa_dei_tasti():
    d = FakeDisplay([64])
    assert modifiers_pressed(lambda: d, lambda name: name) is True and d.closed
    assert modifiers_pressed(lambda: FakeDisplay([]), lambda name: name) is False
    assert modifiers_pressed(lambda: FakeDisplay([37]), lambda name: name) is True


def test_modificatori_illeggibili_valgono_nessuno_premuto():
    def boom():
        raise RuntimeError("no display")
    c, _, _ = _clip(mods=boom)
    assert c.modifiers_down() is False


# --- finestra in primo piano --------------------------------------------------------

def _fg(terminal_class='"gnome-terminal-server", "Gnome-terminal"', active=b"0x4a00007"):
    def run(cmd):
        if cmd[:3] == ["xprop", "-root", "_NET_ACTIVE_WINDOW"]:
            return b"_NET_ACTIVE_WINDOW(WINDOW): window id # " + active + b"\n"
        if cmd[:2] == ["xprop", "-id"]:
            assert cmd[2] == active.decode()
            return ("WM_CLASS(STRING) = " + terminal_class + "\n").encode()
        raise X11Error("unexpected " + " ".join(cmd))
    return X11Foreground(run)


def test_classe_della_finestra_attiva():
    assert _fg().identity() == ("gnome-terminal", "gnome-terminal-server")
    assert _fg().is_terminal() is True
    assert _fg('"firefox", "Firefox"').is_terminal() is False
    assert _fg('"code", "Code"').is_terminal() is False


def test_finestra_attiva_con_xdotool_se_xprop_non_la_da():
    seen = []

    def run(cmd):
        seen.append(cmd)
        if cmd[:2] == ["xprop", "-root"]:
            return b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n"
        if cmd[0] == "xdotool":
            return b"73400327\n"
        return b'WM_CLASS(STRING) = "konsole", "konsole"\n'
    assert X11Foreground(run).is_terminal() is True
    assert ["xprop", "-id", "73400327", "WM_CLASS"] in seen


def test_nessuna_finestra_non_e_un_terminale():
    def run(cmd):
        raise X11Error("no")
    assert X11Foreground(run).identity() == ("", "")
    assert X11Foreground(run).is_terminal() is False


# --- hotkey -----------------------------------------------------------------------

def K(name=None, char=None):
    return SimpleNamespace(name=name) if name else SimpleNamespace(char=char)


def test_classificazione_dei_tasti_di_pynput():
    assert classify(K("ctrl_l")) == ("mod", "ctrl")
    assert classify(K("alt")) == ("mod", "alt")
    assert classify(K("shift_r")) == ("mod", "shift")
    assert classify(K("cmd")) == ("mod", "win")
    assert classify(K("alt_gr")) is None
    assert classify(K("space")) == ("key", "space")
    assert classify(K("up")) == ("key", "up")
    assert classify(K("f5")) == ("key", "f5")
    assert classify(K("caps_lock")) is None
    assert classify(K(char="A")) == ("key", "a")
    assert classify(K(char="\x01")) == ("key", "a")      # Ctrl+A
    assert classify(K(char="é")) is None
    assert classify(SimpleNamespace(char=None)) is None


class FakeListener:
    def __init__(self, on_press, on_release):
        self.on_press, self.on_release = on_press, on_release
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeTimer:
    """Timer che scatta a mano."""
    made = []

    def __init__(self, interval, fn, args=()):
        self.fn, self.args, self.cancelled, self.started = fn, args, False, False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn(*self.args)


def _hook(bindings):
    FakeTimer.made = []
    h = X11KeyHook("t", bindings, listener_factory=FakeListener, timer_factory=FakeTimer)
    assert h.start()
    return h


def _wait(ev):
    assert ev.wait(2.0), "callback non eseguito"


def test_tieni_premuto_e_rilascia_come_la_dettatura():
    events, press, release = [], threading.Event(), threading.Event()
    b = Binding(frozenset({"alt"}), "space",
                lambda: (events.append("down"), press.set()),
                lambda: (events.append("up"), release.set()))
    h = _hook([b])
    assert h._listener.started
    h.on_press(K("alt_l"))
    h.on_press(K("space"))
    _wait(press)
    h.on_release(K("space"))
    FakeTimer.made[-1].fire()               # il rilascio e' vero: nessun auto-repeat
    _wait(release)
    assert events == ["down", "up"]
    listener = h._listener
    h.stop()
    assert listener.stopped and h._listener is None


def test_l_auto_repeat_non_e_un_rilascio():
    events, press, release = [], threading.Event(), threading.Event()
    b = Binding(frozenset({"alt"}), "space",
                lambda: (events.append("down"), press.set()),
                lambda: (events.append("up"), release.set()))
    h = _hook([b])
    h.on_press(K("alt"))
    h.on_press(K("space"))
    _wait(press)
    for _ in range(3):                       # release + press ripetuti dal server
        h.on_release(K("space"))
        h.on_press(K("space"))
    for t in FakeTimer.made:
        t.fire()                             # i timer sono stati annullati
    h.on_release(K("space"))
    FakeTimer.made[-1].fire()
    _wait(release)
    assert events == ["down", "up"]


def test_senza_i_modificatori_giusti_non_scatta():
    hits = []
    b = Binding(frozenset({"ctrl", "alt"}), "up", lambda: hits.append(1), exact=True)
    h = _hook([b])
    h.on_press(K("ctrl"))
    h.on_press(K("up"))
    h.on_release(K("up"))
    assert hits == [] and FakeTimer.made == []


def test_exact_rifiuta_modificatori_in_piu():
    hits = []
    b = Binding(frozenset({"ctrl", "alt"}), "up", lambda: hits.append(1), exact=True)
    h = _hook([b])
    for k in ("ctrl", "alt", "shift"):
        h.on_press(K(k))
    h.on_press(K("up"))
    assert hits == []


def test_non_exact_accetta_modificatori_in_piu():
    got = threading.Event()
    h = _hook([Binding(frozenset({"alt"}), "up", got.set)])
    for k in ("alt", "shift"):
        h.on_press(K(k))
    h.on_press(K("up"))
    _wait(got)


def test_repeat_richiama_on_press_a_ogni_ripetizione():
    hits = []
    done = threading.Event()

    def cb():
        hits.append(1)
        if len(hits) == 3:
            done.set()
    h = _hook([Binding(frozenset({"ctrl", "alt"}), "left", cb, exact=True, repeat=True)])
    h.on_press(K("ctrl"))
    h.on_press(K("alt"))
    for _ in range(3):
        h.on_press(K("left"))
    _wait(done)
    assert hits == [1, 1, 1]


def test_un_callback_che_fallisce_non_ferma_gli_altri():
    ok = threading.Event()

    def boom():
        raise RuntimeError("x")
    h = _hook([Binding(frozenset(), "a", boom), Binding(frozenset(), "b", ok.set)])
    h.on_press(K(char="a"))
    h.on_press(K(char="b"))
    _wait(ok)


def test_listener_che_non_parte_si_dichiara():
    def no_display(on_press, on_release):
        raise RuntimeError("no X server")
    h = X11KeyHook("t", [Binding(frozenset(), "a", lambda: None)], listener_factory=no_display)
    assert h.start() is False


def test_binding_non_valida_non_parte():
    h = X11KeyHook("t", [Binding(frozenset(), "nonesiste", lambda: None)],
                   listener_factory=FakeListener)
    assert not h.valid and h.start() is False


# --- insieme -----------------------------------------------------------------------

def test_x11_dice_cosa_manca():
    d = X11Desktop(which=lambda n: None, has_module=lambda n: False)
    ok, why = d.available()
    assert not ok and "xclip or xsel" in why and "xprop" in why and "pynput" in why
    d = X11Desktop(which=lambda n: "/usr/bin/" + n, has_module=lambda n: True)
    assert d.available() == (True, "")


def test_x11_costruisce_hook_e_overlay_senza_emoji():
    d = X11Desktop(which=lambda n: None, has_module=lambda n: True)
    assert isinstance(d.key_hook("t", [Binding(frozenset(), "a", lambda: None)]), X11KeyHook)
    ov = d.new_overlay()
    icons = list(ov._style.state_icons.values()) + list(ov._style.clip_icons.values())
    assert icons and all(ord(c) < 0x1F000 for i in icons for c in i)
    assert d.can_suppress is False
