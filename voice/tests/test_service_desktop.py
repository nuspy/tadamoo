"""Dictation and the clipboard history in the voice service: started by `desktop_features.start`, reported by /components/status, stopped in order.
A fake desktop (hooks, clipboard, overlay) and a fake STT stand in for the hardware: no hook is installed, no window opens, nothing is recorded."""
import json
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from callbridge import dictation, engines, models_store as ms
from callbridge import clipboard_history
from callbridge.clipboard_history import ClipStore
from callbridge.config import Config
from callbridge.desktop import Desktop
from callbridge.desktop import tk_overlay
from callbridge.desktop.unsupported import WAYLAND, NullOverlay, UnsupportedDesktop
from voice import desktop_features, service

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}


class FakeHook:
    def __init__(self, name, bindings, journal):
        self.name, self.bindings, self.journal = name, bindings, journal
        self.started = self.stopped = False

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True
        self.journal.append(f"unhook {self.name}")


class FakeClipboard:
    def sequence(self):
        return 1

    def read(self, honor_exclude=True):
        return None

    def modifiers_down(self):
        return False


class FakeOverlay:
    """`mainloop` blocks until `quit`, like the Tk one; `loop_error` makes it raise instead."""

    def __init__(self, journal, loop_error=None):
        self.journal, self.loop_error = journal, loop_error
        self.entered, self._quit = threading.Event(), threading.Event()
        self.thread = None

    def set_dictation(self, active, level=0.0, phase="recording"):
        pass

    def show_clipboard(self, card):
        pass

    def mainloop(self):
        self.thread = threading.current_thread()
        self.entered.set()
        if self.loop_error:
            raise self.loop_error
        self._quit.wait()
        self.journal.append("overlay loop ended")

    def quit(self):
        self.journal.append("overlay quit")
        self._quit.set()


class FakeDesktop(Desktop):
    name = "fake"

    def __init__(self, journal=None, overlay=True, loop_error=None):
        self.journal = [] if journal is None else journal
        self.hooks = []
        self._clip = FakeClipboard()
        self.overlay = FakeOverlay(self.journal, loop_error) if overlay else None

    def available(self):
        return True, ""

    clipboard = property(lambda self: self._clip)
    foreground = property(lambda self: SimpleNamespace(is_terminal=lambda: False))

    def key_hook(self, name, bindings):
        h = FakeHook(name, bindings, self.journal)
        self.hooks.append(h)
        return h

    def new_overlay(self):
        return self.overlay


class FakeStt:
    state, error, pid = "running", None, 1
    info = {"model": "tiny"}

    def load(self):
        pass


@pytest.fixture(autouse=True)
def _no_real_stt_no_env_leaks(monkeypatch):
    monkeypatch.setattr(engines, "_get_stt_engine", lambda c: FakeStt())
    monkeypatch.setattr(dictation, "_current", None)
    monkeypatch.setattr(clipboard_history, "_current", None)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", ms.ROOT_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(ms, "_service_env", {})


def _start(tmp_path, desktop=None, cfg=None, stop=None):
    desktop = desktop or FakeDesktop()
    cfg = cfg or Config()
    stop = stop or threading.Event()
    return desktop_features.start(cfg, stop, desktop=desktop, clip_store=ClipStore(tmp_path / "clip")), desktop, cfg, stop


def test_start_wires_both_with_the_overlay_and_reports_them_active(tmp_path):
    features, desk, cfg, _ = _start(tmp_path)
    assert [h.name for h in desk.hooks] == ["dettatura", "appunti"] and all(h.started for h in desk.hooks)
    assert features.dictation._overlay is desk.overlay and features.clipboard._overlay is desk.overlay
    d, c = features.dictation.status(), features.clipboard.status()
    assert d["active"] is True and d["state"] == "running" and d["hotkey"] == "alt+space"
    assert c["active"] is True and c["state"] == "running" and c["entries"] == 0 and "ctrl+alt" in c["hotkey"]
    assert dictation.current() is features.dictation and clipboard_history.current() is features.clipboard


def test_dictation_off_in_the_settings_stays_off_and_says_so_while_the_history_runs(tmp_path):
    cfg = Config()
    cfg.dictation_enabled = False
    features, desk, _, _ = _start(tmp_path, cfg=cfg)
    assert [h.name for h in desk.hooks] == ["appunti"]
    d = features.dictation.status()
    assert d["active"] is False and d["state"] == "disabled" and "dictation_enabled=false" in d["detail"]
    assert features.clipboard.status()["active"] is True


def test_a_platform_that_refuses_leaves_both_off_with_the_reason_and_does_not_raise(tmp_path):
    features, _, _, _ = _start(tmp_path, desktop=UnsupportedDesktop(WAYLAND))
    assert features.overlay is None
    d, c = features.dictation.status(), features.clipboard.status()
    assert (d["active"], d["state"]) == (False, "unsupported") and "Wayland" in d["detail"]
    assert (c["active"], c["state"]) == (False, "unsupported") and "Wayland" in c["detail"]
    features.stop()                                           # nothing to unhook, no overlay to end


def test_a_desktop_that_cannot_even_be_asked_leaves_the_features_empty(tmp_path):
    class Broken(FakeDesktop):
        def available(self):
            raise RuntimeError("no way")
    features, _, _, _ = _start(tmp_path, desktop=Broken())
    assert features.dictation is None and features.clipboard is None and features.overlay is None


def test_a_part_that_fails_to_build_does_not_stop_the_other(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "_get_stt_engine", lambda c: (_ for _ in ()).throw(RuntimeError("stt broke")))
    features, desk, _, _ = _start(tmp_path)
    assert features.dictation is None and features.clipboard.status()["active"] is True


def test_stop_unhooks_everything_and_ends_the_overlay(tmp_path):
    features, desk, _, _ = _start(tmp_path)
    features.stop()
    features.stop()                                           # twice is fine
    assert all(h.stopped for h in desk.hooks)
    assert desk.journal.count("overlay quit") == 1
    assert features.dictation.status()["active"] is False and features.clipboard.status()["active"] is False


def test_the_stop_event_ends_the_overlay_loop_on_the_calling_thread(tmp_path):
    features, desk, _, stop = _start(tmp_path)
    done = threading.Event()
    runner = threading.Thread(target=lambda: (features.run_ui(stop), done.set()), daemon=True)
    runner.start()
    assert desk.overlay.entered.wait(2) and not done.is_set()
    assert desk.overlay.thread is runner                      # the loop runs on the thread that called run_ui
    stop.set()
    assert done.wait(5)
    assert "overlay loop ended" in desk.journal


def test_an_overlay_loop_that_raises_falls_back_to_the_wait_and_keeps_the_hotkeys(tmp_path):
    desk = FakeDesktop(loop_error=RuntimeError("no display"))
    features, desk, _, stop = _start(tmp_path, desktop=desk)
    done = threading.Event()
    runner = threading.Thread(target=lambda: (features.run_ui(stop), done.set()), daemon=True)
    runner.start()
    assert desk.overlay.entered.wait(2)
    time.sleep(0.2)
    assert not done.is_set()                                  # still holding the thread, as the old wait did
    assert not any(h.stopped for h in desk.hooks)             # and nothing was unhooked
    assert features.dictation.status()["active"] is True
    stop.set()
    assert done.wait(5)


def test_without_an_overlay_run_ui_is_a_plain_wait(tmp_path):
    features, _, _, stop = _start(tmp_path, desktop=FakeDesktop(overlay=False))
    assert features.overlay is None
    done = threading.Event()
    threading.Thread(target=lambda: (features.run_ui(stop), done.set()), daemon=True).start()
    time.sleep(0.2)
    assert not done.is_set()
    stop.set()
    assert done.wait(5)


def test_a_changed_dictation_setting_is_applied_without_a_restart(tmp_path):
    features, desk, cfg, _ = _start(tmp_path)
    first = desk.hooks[0]
    features.sync()
    assert len(desk.hooks) == 2 and not first.stopped         # nothing changed: nothing restarted
    cfg.dictation_hotkey = "ctrl+shift+f9"
    features.sync()
    assert first.stopped and desk.hooks[2].name == "dettatura" and desk.hooks[2].bindings[0].key == "f9"
    cfg.dictation_enabled = False
    features.sync()
    assert desk.hooks[2].stopped and features.dictation.status()["state"] == "disabled"
    cfg.dictation_enabled = True
    features.sync()
    assert features.dictation.status()["active"] is True


def test_the_null_overlay_and_the_tk_overlay_without_tk_return_from_their_loop_on_quit(monkeypatch):
    monkeypatch.setattr(tk_overlay, "_TK_OK", False)
    for overlay in (NullOverlay(), tk_overlay.TkOverlay(tk_overlay.OverlayStyle("e", "u", {}, {}))):
        done = threading.Event()
        threading.Thread(target=lambda o=overlay: (o.mainloop(), done.set()), daemon=True).start()
        time.sleep(0.1)
        assert not done.is_set()
        overlay.quit()
        assert done.wait(3), type(overlay).__name__


# --- the whole service, in this process, with the fake desktop -----------------------------------
def _get(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=AUTH)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def test_serve_starts_both_reports_them_in_components_status_and_stops_in_order(tmp_path, monkeypatch):
    settings = tmp_path / "voice.json"
    settings.write_text(json.dumps({"models_dir": str(tmp_path / "models")}), encoding="utf-8")
    token = tmp_path / "svc.token"
    token.write_text(TOKEN, encoding="utf-8")
    info = tmp_path / "voice-service.json"
    desk = FakeDesktop()
    stops = []
    monkeypatch.setattr(service, "_install_stop", lambda s, d: stops.append(s))        # keeps the service's stop event; no signal handler is set
    real_start = desktop_features.start

    def start(config, stop_event, **kw):
        return real_start(config, stop_event, desktop=desk, clip_store=ClipStore(tmp_path / "clip"))
    monkeypatch.setattr(desktop_features, "start", start)
    monkeypatch.setattr(service, "_remove_info", lambda path, orig=service._remove_info: (desk.journal.append("info removed"), orig(path)))
    args = SimpleNamespace(host="127.0.0.1", port=0, token_file=str(token), info_file=str(info), settings=str(settings), default_models_dir="",
                           no_gateway=True, no_desktop=False)
    result = []
    runner = threading.Thread(target=lambda: result.append(service.serve(args)), daemon=True)
    runner.start()
    try:
        assert desk.overlay.entered.wait(30)
        assert desk.overlay.thread is runner                  # the overlay loop is on the thread that runs serve
        port = json.loads(info.read_text(encoding="utf-8"))["port"]
        comp = _get(port, "/components/status")
        assert comp["dictation"]["active"] is True and comp["dictation"]["hotkey"] == "alt+space"
        assert comp["clipboard_history"]["active"] is True and comp["clipboard_history"]["entries"] == 0
    finally:
        for _ in range(100):
            if stops:
                break
            time.sleep(0.05)
        stops[0].set()
        runner.join(30)
    assert result == [0] and not runner.is_alive()
    j = desk.journal
    assert j.index("unhook dettatura") < j.index("info removed") and j.index("unhook appunti") < j.index("info removed")
    assert "overlay loop ended" in j and all(h.stopped for h in desk.hooks)


def test_no_desktop_flag_starts_nothing(tmp_path, monkeypatch):
    settings = tmp_path / "voice.json"
    settings.write_text("{}", encoding="utf-8")
    token = tmp_path / "svc.token"
    token.write_text(TOKEN, encoding="utf-8")
    stops = []
    monkeypatch.setattr(service, "_install_stop", lambda s, d: stops.append(s))
    monkeypatch.setattr(desktop_features, "start", lambda *a, **k: pytest.fail("the desktop features must not start"))
    args = SimpleNamespace(host="127.0.0.1", port=0, token_file=str(token), info_file=str(tmp_path / "i.json"), settings=str(settings),
                           default_models_dir="", no_gateway=True, no_desktop=True)
    result = []
    runner = threading.Thread(target=lambda: result.append(service.serve(args)), daemon=True)
    runner.start()
    for _ in range(300):
        if (tmp_path / "i.json").exists():
            break
        time.sleep(0.1)
    stops[0].set()
    runner.join(30)
    assert result == [0]
