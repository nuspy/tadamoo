"""Dictation and the clipboard history of the voice service, in the user's interactive session.

Both need the desktop (global hotkeys, clipboard, paste) and neither depends on the voice-function plan, so they start with the service as soon
as the HTTP server is up. Each starts on its own: one that cannot start logs why and the other, and the service, go on. The overlay is a Tk
window and its loop must run on the main thread: `serve` calls `Features.run_ui` there. Settings read again from voice.json reach `sync`."""
from __future__ import annotations

import logging
import threading

import voice  # noqa: F401  (puts voice/ on sys.path so that `callbridge` imports by that name)

log = logging.getLogger("voice.desktop")


class Features:
    """What the service started: the overlay, the dictation manager and the clipboard history (each None when it could not be built)."""

    def __init__(self, config, stop_event: threading.Event):
        self.config = config
        self.stop_event = stop_event
        self.overlay = None
        self.dictation = None
        self.clipboard = None
        self._lock = threading.Lock()
        self._wanted = None                 # (enabled, hotkey) the dictation was last started or stopped for
        self._stopped = False

    def sync(self) -> None:
        """Apply a changed dictation setting (on or off, another hotkey) without restarting the service."""
        with self._lock:
            d = self.dictation
            if d is None or self._stopped:
                return
            wanted = (bool(self.config.dictation_enabled), self.config.dictation_hotkey)
            if wanted == self._wanted:
                return
            self._wanted = wanted
            try:
                if d.active:
                    d.stop()
                d.start()
            except Exception as e:  # noqa: BLE001 (a failing hotkey change must not reach the request that read the settings)
                log.error("dictation: the new settings were not applied: %s", e)

    def run_ui(self, stop: threading.Event) -> None:
        """Hold the calling (main) thread until `stop`: the overlay loop when there is one, else a plain wait. A loop that raises or returns
        early leaves the plain wait; dictation and the history do not depend on the overlay."""
        if self.overlay is not None:
            try:
                self.overlay.mainloop()
            except Exception as e:  # noqa: BLE001 (no display, a Tk error)
                log.error("overlay loop ended with an error, running without it: %s", e)
        while not stop.wait(0.5):
            pass

    def stop(self) -> None:
        """Unhook the hotkeys and end the overlay loop. Safe to call twice."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        for name, part in (("dictation", self.dictation), ("clipboard history", self.clipboard)):
            if part is None:
                continue
            try:
                part.stop()
            except Exception as e:  # noqa: BLE001
                log.error("%s did not stop cleanly: %s", name, e)
        if self.overlay is not None:
            try:
                self.overlay.quit()
            except Exception as e:  # noqa: BLE001
                log.error("overlay did not stop cleanly: %s", e)


def start(config, stop_event: threading.Event, desktop=None, clip_store=None) -> Features:
    """Build the overlay and start dictation (when `dictation_enabled`) and the clipboard history. `desktop` and `clip_store` are for tests."""
    features = Features(config, stop_event)
    try:
        if desktop is None:
            from callbridge.desktop import platform
            desktop = platform()
        usable, why = desktop.available()
    except Exception as e:  # noqa: BLE001
        log.error("desktop features are off: no desktop layer (%s)", e)
        return features
    if usable:
        try:
            features.overlay = desktop.new_overlay()
        except Exception as e:  # noqa: BLE001
            log.warning("no overlay window: %s", e)
    else:
        log.warning("no overlay window and no global hotkeys: %s", why)
    try:
        from callbridge.dictation import DictationManager
        features.dictation = DictationManager(config, overlay=features.overlay, desktop=desktop)
        features._wanted = (bool(config.dictation_enabled), config.dictation_hotkey)
        features.dictation.start()
    except Exception as e:  # noqa: BLE001
        log.error("dictation is off: %s", e)
    try:
        from callbridge.clipboard_history import ClipboardHistory
        features.clipboard = ClipboardHistory(overlay=features.overlay, store=clip_store, desktop=desktop)
        features.clipboard.start()
    except Exception as e:  # noqa: BLE001
        log.error("clipboard history is off: %s", e)
    if features.overlay is not None:
        threading.Thread(target=_quit_on_stop, args=(features.overlay, stop_event), name="voice-overlay-quit", daemon=True).start()
    return features


def _quit_on_stop(overlay, stop_event: threading.Event) -> None:
    stop_event.wait()
    overlay.quit()
