"""The copilot's window: a private Tk window of suggested replies, in a thread of its own (the service has no main loop to borrow).

The window is meant for the person only. With `hide_from_capture` it is excluded from screen sharing and recording through the desktop layer
(`Desktop.exclude_from_capture`); on a system that cannot do that it is not opened, because a window that shows what it should not is worse than
none. A photograph of the screen is not stopped by this. Not proven here: it has never been opened in a real call."""
from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger("callbridge.desktop.suggestions")

_BG, _FG = "#0b1020", "#e6e9f2"
_TAGS = {"hdr": "#9ecbff", "ctx": "#e6e9f2", "reply": "#86efac", "note": "#fca5a5"}
_MARKS = ("-", "•", "*")


def line_tag(line: str) -> str:
    """How a line of a suggestion is shown: a reply (starts with a dash or is the REPLIES line's content) or context."""
    low = line.strip().lower()
    if low.startswith(("replies", "risposte")):
        return "ctx"
    return "reply" if line.lstrip().startswith(_MARKS) else "ctx"


def reply_text(line: str) -> str:
    """The text of a reply line without its dash, "" when the line is not a reply."""
    return line.strip().lstrip("-•* ").strip() if line_tag(line) == "reply" else ""


class TkSuggestions:
    def __init__(self, desktop, hide_from_capture: bool = True, font_size: int = 22, on_speak=None, on_count=None, count: int = 3,
                 audio_reply: bool = False) -> None:
        self.desktop, self.hide, self.font_size = desktop, hide_from_capture, font_size
        self.on_speak, self.on_count, self.count, self.audio_reply = on_speak, on_count, count, audio_reply
        self._q: "queue.Queue" = queue.Queue()
        self._ready = threading.Event()
        self._result: tuple[bool, str] = (False, "the window did not start")
        self._thread: threading.Thread | None = None

    def open(self) -> tuple[bool, str]:
        self._thread = threading.Thread(target=self._run, name="copilot-window", daemon=True)
        self._thread.start()
        self._ready.wait(8.0)
        return self._result

    def _post(self, fn) -> None:
        self._q.put(fn)

    def add_entry(self, entry) -> None:
        self._post(lambda: self._show(entry, new=True))

    def update_entry(self, entry) -> None:
        self._post(lambda: self._show(entry, new=False))

    def add_note(self, text: str) -> None:
        self._post(lambda: self._write(f"\n[{text}]\n", "note"))

    def close(self) -> None:
        self._post(lambda: self._root.destroy())

    # ---- in the Tk thread
    def _run(self) -> None:
        try:
            import tkinter as tk
            from tkinter import font as tkfont
        except Exception as e:  # noqa: BLE001
            self._result = (False, f"Tk is not available: {e}")
            self._ready.set()
            return
        root = self._root = tk.Tk()
        root.title("Copilot")
        root.configure(bg=_BG)
        root.attributes("-topmost", True)
        root.geometry(f"720x900+{max(0, root.winfo_screenwidth() - 740)}+40")
        self._font = tkfont.Font(family="Segoe UI", size=self.font_size)
        bar = tk.Frame(root, bg=_BG)
        bar.pack(side="bottom", fill="x")
        for label, cmd in (("A-", lambda: self._resize(-2)), ("A+", lambda: self._resize(2)), ("-", lambda: self._count(-1)), ("+", lambda: self._count(1))):
            tk.Button(bar, text=label, command=cmd, bg="#141a2e", fg=_FG, relief="flat", padx=14, pady=8).pack(side="left", padx=2, pady=4)
        self._label = tk.Label(bar, text=f"{self.count}", bg=_BG, fg=_FG)
        self._label.pack(side="left", padx=6)
        self._audio = tk.BooleanVar(value=self.audio_reply)
        tk.Checkbutton(bar, text="Reply with audio (click a reply)", variable=self._audio, bg=_BG, fg=_FG, selectcolor="#141a2e",
                       activebackground=_BG, activeforeground=_FG).pack(side="left", padx=10)
        self._text = tk.Text(root, wrap="word", bg=_BG, fg=_FG, font=self._font, padx=14, pady=12, borderwidth=0, state="disabled")
        self._text.pack(fill="both", expand=True)
        for tag, color in _TAGS.items():
            self._text.tag_config(tag, foreground=color)
        self._text.tag_bind("reply", "<Button-1>", self._clicked)
        root.update_idletasks()
        handle = root.winfo_id()
        if self.hide and not self.desktop.exclude_from_capture(handle, True):
            self._result = (False, "this system cannot hide the window from screen capture, so it was not opened")
            root.destroy()
            self._ready.set()
            return
        self._result = (True, "")
        self._ready.set()
        self._pump()
        root.mainloop()

    def _pump(self) -> None:
        try:
            while True:
                self._q.get_nowait()()
        except queue.Empty:
            pass
        except Exception as e:  # noqa: BLE001 (the window was destroyed)
            log.debug("window update: %s", e)
            return
        self._root.after(50, self._pump)

    def _write(self, text: str, tag: str) -> None:
        self._text.config(state="normal")
        self._text.insert("end", text, (tag,))
        self._text.config(state="disabled")
        self._text.see("end")

    def _show(self, entry, new: bool) -> None:
        self._text.config(state="normal")
        mark = f"e{entry.seq}"
        if new:
            self._text.insert("end", f"\n> {entry.utterance}\n", ("hdr",))
            self._text.mark_set(mark, "end-1c")
            self._text.mark_gravity(mark, "left")
            self._text.insert("end", "...\n", ("ctx",))
        else:
            self._text.delete(mark, "end-1c")
            for line in entry.body.split("\n"):
                self._text.insert("end", line + "\n", (line_tag(line),))
        self._text.config(state="disabled")
        self._text.see("end")

    def _clicked(self, event) -> None:
        if not self._audio.get() or self.on_speak is None:
            return
        line = self._text.get(f"@{event.x},{event.y} linestart", f"@{event.x},{event.y} lineend")
        if reply_text(line):
            self.on_speak(reply_text(line))

    def _resize(self, delta: int) -> None:
        self._font.configure(size=max(10, min(72, self._font.cget("size") + delta)))

    def _count(self, delta: int) -> None:
        self.count = max(1, min(6, self.count + delta))
        self._label.config(text=str(self.count))
        if self.on_count is not None:
            self.on_count(self.count)
