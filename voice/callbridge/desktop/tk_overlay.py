"""Overlay Tk (Windows e X11): piccola finestra di stato sempre in primo piano.

Stati: "listening" (blu fisso), "reasoning" (ambra pulsante), "speaking"
(viola pulsante), "dictation" e "transcribing" (dettatura), "hidden"
(finestra nascosta). In piu' la finestrella della cronologia appunti.

Deve girare sul THREAD PRINCIPALE (tkinter non e' thread-safe): `main.py`
crea l'overlay e chiama `mainloop()`. Gli altri thread cambiano stato solo
tramite `set_state_fn()` (assegnazione di un callable, letta dal tick di tk).
Se tkinter non e' disponibile, l'overlay diventa un no-op silenzioso.

Cio' che dipende dal sistema (font, icone, finestrella che non prende il
focus) arriva da `OverlayStyle`.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("callbridge.overlay")

try:
    import tkinter as tk
    _TK_OK = True
except Exception as e:  # noqa: BLE001 - ambiente senza Tk
    log.warning("tkinter non disponibile, overlay disattivato: %s", e)
    _TK_OK = False

_BLUE = "#2563eb"    # ascolto
_AMBER = "#d97706"   # reasoning (il cervello elabora)
_PURPLE = "#7c3aed"  # il cervello parla
_GREEN = "#16a34a"   # dettatura (Alt+Spazio)
_SLATE = "#475569"   # appunti: niente da mostrare
_W, _H = 150, 112
_CLIP_W = 380         # finestrella della cronologia appunti
_CLIP_HIDE_MS = 2200  # sparisce da sola dopo l'ultimo tasto

# Colore + testo per stato; l'icona e' dello stile.
_STATES = {
    "listening": (_BLUE, "In ascolto"),
    "reasoning": (_AMBER, "Elaboro…"),
    "speaking": (_PURPLE, "Parlo"),
    "dictation": (_GREEN, "Dettatura"),
    "transcribing": (_AMBER, "Trascrivo…"),
}

# Cronologia appunti: colore per azione (Ctrl+Alt+frecce).
_CLIP_COLORS = {"copy": _GREEN, "prev": _BLUE, "next": _BLUE,
                "paste": _PURPLE, "empty": _SLATE}


@dataclass
class OverlayStyle:
    """Font, icone e comportamento della finestra che cambiano da un sistema all'altro."""
    emoji_font: str
    ui_font: str
    state_icons: dict[str, str]
    clip_icons: dict[str, str]
    # Rende un Toplevel incapace di prendere il focus.
    no_activate: Callable[[object], None] = field(default=lambda win: None)


class TkOverlay:
    """Finestrella sempre-in-primo-piano con l'icona di stato."""

    def __init__(self, style: OverlayStyle) -> None:
        self._style = style
        # Stato corrente fornito dall'esterno: "hidden" | "listening" | "speaking".
        self.state_fn: Callable[[], str] = lambda: "hidden"
        self._root = None
        self._label = None
        self._status = None
        self._level_canvas = None
        self._frame = 0
        self._visible = False
        # Dettatura (Alt+Spazio): attiva + livello voce (0..1). Impostati da
        # DictationManager da un thread di lavoro; letti dal _tick (Tk).
        self._dict_active = False
        self._dict_level = 0.0
        self._dict_phase = "recording"   # recording | transcribing
        # Finestrella della cronologia appunti (Toplevel creato al primo uso).
        self._clip_win = None
        self._clip_hide_job = None
        self._clip_img = None            # ref alla PhotoImage (GC = immagine vuota)
        # Comandi da eseguire sul thread Tk (es. aprire/aggiornare la finestra
        # copilota): i thread di lavoro accodano qui, il _tick li esegue.
        self._cmd_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._quit = threading.Event()

    def set_state_fn(self, fn: Callable[[], str]) -> None:
        """Imposta la sorgente di stato (chiamabile da qualsiasi thread)."""
        self.state_fn = fn

    def set_dictation(self, active: bool, level: float = 0.0,
                      phase: str = "recording") -> None:
        """Segnala che la dettatura (Alt+Spazio) e' in corso + livello voce
        (0..1). `phase`: "recording" (tasto premuto) o "transcribing" (whisper
        sta lavorando, dopo il rilascio). Ha precedenza sull'overlay della conversazione.
        Thread-safe (solo assegnazioni, lette dal _tick)."""
        self._dict_active = bool(active)
        self._dict_level = max(0.0, min(1.0, float(level)))
        self._dict_phase = phase

    def show_clipboard(self, card: dict) -> None:
        """Mostra la finestrella della cronologia appunti (thread-safe).

        `card`: {"action": copy|prev|next|paste|empty, "title": str,
        "pos": "3/42", "kind": text|image, "text": str, "png": bytes|None,
        "size": "1920×1080"}."""
        self.post(lambda: self._clip_render(card))

    def post(self, fn: Callable[[], None]) -> None:
        """Esegue `fn` sul thread Tk (thread-safe). No-op se Tk assente."""
        if _TK_OK:
            self._cmd_queue.put(fn)
        else:
            log.debug("post() ignorato: Tk non disponibile")

    def get_root(self):
        """Ritorna il root Tk (disponibile dentro un `post`, non prima)."""
        return self._root

    def quit(self) -> None:
        """Ends `mainloop`: sets a flag the Tk tick reads (thread-safe)."""
        self._quit.set()

    # --- ciclo di vita (thread principale) -------------------------------

    def mainloop(self) -> None:
        if not _TK_OK:
            self._block_no_ui()
            return
        try:
            self._root = tk.Tk()
            self._root.overrideredirect(True)         # senza bordi/barra
            self._root.attributes("-topmost", True)
            try:
                self._root.attributes("-alpha", 0.0)  # parte invisibile
            except tk.TclError:
                pass
            self._root.configure(bg=_BLUE)

            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()
            x = (sw - _W) // 2
            y = sh - _H - 70
            self._root.geometry(f"{_W}x{_H}+{x}+{y}")

            self._label = tk.Label(
                self._root, text=self._style.state_icons.get("listening", ""), bg=_BLUE, fg="white",
                font=(self._style.emoji_font, 40),
            )
            self._label.pack(pady=(10, 0))
            self._status = tk.Label(
                self._root, text="", bg=_BLUE, fg="white",
                font=(self._style.ui_font, 9),
            )
            self._status.pack()
            # Barra livello voce (usata solo in dettatura): riempimento bianco
            # proporzionale al volume del microfono.
            self._level_canvas = tk.Canvas(
                self._root, width=_W - 40, height=6, bg=_BLUE,
                highlightthickness=0, bd=0)

            self._root.withdraw()  # nascosta finche' non parte una sessione
            self._tick()
            self._root.mainloop()
            self._destroy()
        except tk.TclError as e:
            # Sessione 0 / nessun desktop: nessun display Tk. Non far cadere il
            # processo PTT (le hotkey funzionano comunque): blocca senza UI.
            log.warning("Overlay Tk non inizializzabile (%s): proseguo senza UI", e)
            self._root = None
            self._block_no_ui()

    def _destroy(self) -> None:
        try:
            self._root.destroy()
        except tk.TclError:
            pass
        self._root = None

    def _block_no_ui(self) -> None:
        """Blocca il thread principale senza UI (fino a `quit`), cosi' il processo resta vivo."""
        self._quit.wait()

    # --- aggiornamento periodico ----------------------------------------

    def _tick(self) -> None:
        if self._quit.is_set():
            self._root.quit()
            return
        # Esegue i comandi accodati da altri thread (sul thread Tk).
        while True:
            try:
                self._cmd_queue.get_nowait()()
            except queue.Empty:
                break
            except Exception:  # noqa: BLE001 - un comando non deve fermare il tick
                log.exception("Comando overlay fallito")

        # La dettatura (Alt+Spazio) ha precedenza sull'overlay della conversazione.
        if self._dict_active:
            state = ("transcribing" if self._dict_phase == "transcribing"
                     else "dictation")
        else:
            try:
                state = self.state_fn() or "hidden"
            except Exception:  # noqa: BLE001 - state_fn puo' riferire a sessioni morte
                state = "hidden"

        if state == "hidden":
            if self._visible:
                self._root.withdraw()
                self._visible = False
        else:
            if not self._visible:
                self._root.deiconify()
                self._root.attributes("-topmost", True)
                self._visible = True

            color, txt = _STATES.get(state, _STATES["listening"])
            icon = self._style.state_icons.get(state, "")
            if state in ("listening", "dictation"):
                alpha = 0.93  # fisso mentre ascolta / detta
            else:
                # reasoning e speaking: pulsazione dell'opacita'
                alpha = 0.6 + 0.4 * (0.5 + 0.5 * math.sin(self._frame / 3.0))

            self._root.configure(bg=color)
            self._label.configure(bg=color, text=icon)
            self._status.configure(bg=color, text=txt)
            try:
                self._root.attributes("-alpha", alpha)
            except tk.TclError:
                pass

            # Barra livello voce: visibile solo in dettatura.
            if state == "dictation":
                if not self._level_canvas.winfo_ismapped():
                    self._level_canvas.pack(pady=(4, 0))
                bw = _W - 40
                fill = int(bw * self._dict_level)
                self._level_canvas.configure(bg=color)
                self._level_canvas.delete("all")
                self._level_canvas.create_rectangle(
                    0, 0, bw, 6, fill="#0b5a2a", outline="")
                if fill > 0:
                    self._level_canvas.create_rectangle(
                        0, 0, fill, 6, fill="white", outline="")
            elif self._level_canvas.winfo_ismapped():
                self._level_canvas.pack_forget()

        self._frame += 1
        self._root.after(80, self._tick)

    # --- cronologia appunti (thread Tk) ------------------------------------

    def _clip_render(self, card: dict) -> None:
        if self._root is None:
            return
        action = card.get("action", "")
        color = _CLIP_COLORS.get(action, _BLUE)
        icon = self._style.clip_icons.get(action, "")
        if self._clip_win is None:
            self._clip_build()
        w = self._clip_win
        for part in (w, w.head, w.icon, w.title, w.pos, w.body, w.meta):
            part.configure(bg=color)
        w.icon.configure(text=icon)
        w.title.configure(text=card.get("title") or "")
        w.pos.configure(text=card.get("pos") or "")
        self._clip_img = None
        png = card.get("png")
        if card.get("kind") == "image" and png:
            import base64
            try:
                self._clip_img = tk.PhotoImage(data=base64.b64encode(png))
                w.body.configure(image=self._clip_img, text="")
            except tk.TclError:
                w.body.configure(image="", text="(immagine)")
        else:
            w.body.configure(image="", text=card.get("text") or "")
        meta = card.get("size") or ""
        w.meta.configure(text=meta)
        if meta:
            w.meta.pack(fill="x", padx=12, pady=(0, 8))
        else:
            w.meta.pack_forget()
        # Bordo inferiore centrato, sopra l'overlay cervello/dettatura.
        w.update_idletasks()
        h = w.winfo_reqheight()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"{_CLIP_W}x{h}+{(sw - _CLIP_W) // 2}+{sh - h - _H - 90}")
        w.deiconify()
        w.attributes("-topmost", True)
        if self._clip_hide_job is not None:
            self._root.after_cancel(self._clip_hide_job)
        self._clip_hide_job = self._root.after(_CLIP_HIDE_MS, self._clip_hide)

    def _clip_hide(self) -> None:
        self._clip_hide_job = None
        if self._clip_win is not None:
            self._clip_win.withdraw()
        self._clip_img = None

    def _clip_build(self) -> None:
        w = tk.Toplevel(self._root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", 0.95)
        except tk.TclError:
            pass
        w.withdraw()
        w.head = tk.Frame(w)
        w.head.pack(fill="x", padx=10, pady=(8, 4))
        w.icon = tk.Label(w.head, font=(self._style.emoji_font, 18), fg="white")
        w.icon.pack(side="left")
        w.title = tk.Label(w.head, font=(self._style.ui_font, 10, "bold"), fg="white")
        w.title.pack(side="left", padx=(6, 0))
        w.pos = tk.Label(w.head, font=(self._style.ui_font, 10), fg="white")
        w.pos.pack(side="right")
        w.body = tk.Label(w, font=(self._style.ui_font, 10), fg="white", justify="left",
                          anchor="w", wraplength=_CLIP_W - 24)
        w.body.pack(fill="x", padx=12, pady=(0, 8))
        w.meta = tk.Label(w, font=(self._style.ui_font, 9), fg="white", anchor="w")
        self._clip_win = w
        self._style.no_activate(w)

