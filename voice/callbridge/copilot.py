"""Copilota da conversazione (Task #22) — hotkey Shift+F4.

Apre una finestra desktop che ascolta l'INTERLOCUTORE di una chiamata/riunione
sul PC e suggerisce, per ogni suo enunciato: contesto breve + 2-3 risposte +
(più avanti) dati/ricerche online. La voce dell'utente NON viene mai catturata.

Filtro della voce dell'utente = LOOPBACK: si cattura l'audio in USCITA del PC
(ciò che esce da casse/cuffie = voce dell'interlocutore). Il microfono non è
coinvolto, quindi la voce dell'utente è esclusa per costruzione (niente ML,
niente calibrazione). Cattura via libreria `soundcard` (WASAPI loopback).

Thread/Tk:
  - thread `copilot-capture`: loopback -> resample 16k -> Handy STT.
  - thread `copilot-brain`:   STT -> coalescing -> cervello -> coda UI.
  - thread Tk (dell'overlay): apre/aggiorna la finestra; tutti gli update UI
    passano per `overlay.post(...)` (eseguito nel `_tick` dell'overlay).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np

from .audio_hfp import resample
from .config import Config
from .handy_client import ensure_models_loaded
from .stt_client import STT_RATE, HandySttClient
from .turn_engine import configure_tts
from .util import coalesce_utterances

log = logging.getLogger("callbridge.copilot")


@dataclass
class CopilotEntry:
    seq: int
    utterance: str        # enunciato dell'interlocutore (header)
    body: str = ""        # risposta del cervello accumulata
    done: bool = False


def _copilot_respond(brain, text: str, cfg: Config, n: int,
                     project_id: str | None = None) -> Iterator[str | dict]:
    """Interroga il cervello col preambolo COPILOTA (n = numero di risposte).

    Se la sessione è collegata a un progetto, il preambolo include la
    conoscenza del progetto pertinente a questo enunciato (estratti filtrati
    sui permessi dell'operatore, o il puntatore alla wiki in single-user).
    I suggerimenti sono per l'operatore: se ne legge uno all'interlocutore
    (anche con l'avatar) la decisione su cosa condividere resta sua."""
    try:
        pre = cfg.copilot_preamble.format(n=n)
    except Exception:  # noqa: BLE001 - preambolo senza placeholder
        pre = cfg.copilot_preamble
    if project_id:
        from .knowledge_client import project_context
        pre = project_context(cfg, project_id, query=text) + pre
    yield from brain.ask_stream(pre + text)


class CopilotSession:
    """Pipeline di lavoro: loopback -> STT -> cervello -> coda UI (no Tk qui)."""

    def __init__(self, cfg: Config, brain, ui_post: Callable[[Callable[[], None]], None],
                 window: "CopilotWindow", avatar=None,
                 project_id: str | None = None) -> None:
        self.cfg = cfg
        self.brain = brain
        self.ui_post = ui_post
        self.window = window
        # Progetto collegato (conoscenza wikibridge) + trascrizione della
        # sessione, accumulata per "Salva riunione nel wiki".
        self.project_id = project_id
        self.transcript: list[dict] = []
        self._started_at = time.time()
        # AvatarClient (o None): se attivo, "rispondi con audio" passa dal
        # worker video (labiale sincronizzato sulla webcam virtuale) invece
        # che dalle casse, con la voce collegata alla persona nel manifest.
        self.avatar = avatar
        self._avatar_voice: dict = {}
        self._stop = threading.Event()
        self._stt: HandySttClient | None = None
        self._threads: list[threading.Thread] = []
        self._seq = 0
        self.num_responses = int(cfg.copilot_num_responses)
        self._tts = None
        self._tts_lock = threading.Lock()

    def set_num_responses(self, n: int) -> None:
        self.num_responses = max(1, min(6, int(n)))

    def start(self) -> None:
        self._stt = HandySttClient(self.cfg.handy_stt_url())
        try:
            self._stt.start()
        except (TimeoutError, OSError) as e:
            log.error("Copilota: STT non raggiungibile: %s", e)
            self.ui_post(lambda: self.window.add_note(f"STT non raggiungibile: {e}"))
            return
        self._threads = [
            threading.Thread(target=self._capture, name="copilot-capture", daemon=True),
            threading.Thread(target=self._brain_loop, name="copilot-brain", daemon=True),
        ]
        for t in self._threads:
            t.start()
        # Avatar video: avvio in background (la prima preparazione di un clip
        # puo' richiedere minuti; nel frattempo il copilota testo funziona).
        if self.avatar is not None and self.cfg.avatar_persona:
            persona = self.cfg.avatar_persona
            self._avatar_voice = self.avatar.voice_for(persona)

            def _warm() -> None:
                if self.avatar.start(persona):
                    self.ui_post(lambda: self.window.add_note(
                        f"Avatar '{persona}' attivo sulla webcam virtuale"))
                else:
                    self.ui_post(lambda: self.window.add_note(
                        f"Avatar '{persona}' non disponibile (vedi log)"))
            threading.Thread(target=_warm, name="avatar-warmup", daemon=True).start()
        log.info("Copilota: sessione avviata (loopback -> STT -> cervello)")

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        if self._stt is not None:
            self._stt.close()
        if self._tts is not None:
            self._tts.close()
        if self.avatar is not None:
            # Worker CONDIVISO: non spegnerlo (lo userebbe il PTT-avatar), solo
            # tronca l'eventuale battuta e torna in idle. Lo stop esplicito resta
            # via /avatar/stop o alla chiusura del processo.
            self.avatar.interrupt()
        log.info("Copilota: sessione terminata")

    # --- rispondi con audio (TTS della risposta scelta) -------------------

    def speak(self, text: str) -> None:
        """Sintetizza `text` con Handy TTS e lo riproduce sul device scelto.

        Per parlare DENTRO la call: impostare `copilot_tts_output_device` su un
        cavo audio virtuale usato come microfono dall'app di chiamata.
        """
        text = (text or "").strip().lstrip("-•* ").strip()
        if text:
            threading.Thread(target=self._speak_worker, args=(text,),
                             name="copilot-speak", daemon=True).start()

    def _speak_worker(self, text: str) -> None:
        import queue as _q

        import sounddevice as sd
        from .tts_client import HandyTtsClient
        with self._tts_lock:
            try:
                if self._tts is None:
                    ensure_models_loaded(self.cfg)
                    self._tts = HandyTtsClient(self.cfg.handy_tts_url())
                    self._tts.start()
                    # Con l'avatar attivo la voce e' quella collegata alla
                    # persona (manifest); altrimenti la voce di default.
                    voice = (self._avatar_voice.get("voice")
                             or self.cfg.default_voice)
                    # configure_tts passa SEMPRE lo speed di config: qui prima
                    # veniva dimenticato, resettando la velocita' a 1.0.
                    configure_tts(self._tts, self.cfg, voice, self.cfg.copilot_lang)
                self._tts.send_text(text)
                self._tts.flush()
                # raccogli i segmenti fino a ~quiet di silenzio
                chunks, rate, last = [], 24000, time.monotonic()
                grace = 30.0   # la prima sintesi puo' essere lenta
                while time.monotonic() - last < grace:
                    try:
                        seg = self._tts.audio.get(timeout=0.3)
                    except _q.Empty:
                        if chunks and time.monotonic() - last > 4.0:
                            break
                        continue
                    chunks.append(seg.samples)
                    rate = seg.sample_rate
                    last = time.monotonic()
                    grace = 6.0
                if not chunks:
                    return
                pcm = np.concatenate(chunks)
                if self.avatar is not None and self.cfg.avatar_persona:
                    # Avatar video: salva il wav e lascia al worker la
                    # riproduzione sincronizzata (video labiale + audio).
                    import glob
                    import os as _os
                    import tempfile
                    import time as _time
                    import wave
                    # Pota i vecchi avatar_say_*.wav (>5 min): il worker li legge
                    # in modo asincrono, quindi non si cancella quello corrente.
                    _tmp = tempfile.gettempdir()
                    _cutoff = _time.time() - 300
                    for _old in glob.glob(_os.path.join(_tmp, "avatar_say_*.wav")):
                        try:
                            if _os.path.getmtime(_old) < _cutoff:
                                _os.remove(_old)
                        except OSError:
                            pass
                    fd, wav_path = tempfile.mkstemp(suffix=".wav",
                                                    prefix="avatar_say_")
                    _os.close(fd)
                    with wave.open(wav_path, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(rate)
                        w.writeframes((np.clip(pcm, -1, 1) * 32767)
                                      .astype("<i2").tobytes())
                    self.avatar.speak_wav(wav_path)
                else:
                    sd.play(pcm, rate, device=self.cfg.copilot_tts_output_device)
                    sd.wait()
            except Exception as e:  # noqa: BLE001 - TTS/audio non disponibili
                log.warning("Copilota: riproduzione risposta fallita: %s", e)

    # --- cattura loopback -------------------------------------------------

    def _capture(self) -> None:
        # soundcard usa COM (WASAPI) e lo inizializza SOLO nel thread che lo
        # importa per primo. Dalla seconda attivazione (thread nuovo, modulo
        # gia' in sys.modules) ogni chiamata fallirebbe con 0x800401f0
        # "CoInitialize has not been called": in quel caso COM va inizializzato
        # a mano QUI. Mai pre-inizializzarlo prima del primo import: l'init
        # interno di soundcard riceverebbe S_FALSE e andrebbe in errore.
        import sys as _sys
        if "soundcard" in _sys.modules:
            try:
                import ctypes
                ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
            except Exception:  # noqa: BLE001 - non Windows
                pass
        try:
            import soundcard as sc
        except ImportError:
            log.error("Copilota: libreria 'soundcard' assente (pip install soundcard)")
            self.ui_post(lambda: self.window.add_note(
                "Manca la libreria 'soundcard': esegui  pip install soundcard"))
            return
        in_rate = self.cfg.copilot_loopback_samplerate
        block = max(1, int(in_rate * self.cfg.block_ms / 1000))
        while not self._stop.is_set():
            try:
                spk = sc.default_speaker()
                mic = sc.get_microphone(str(spk.name), include_loopback=True)
                with mic.recorder(samplerate=in_rate, channels=1) as rec:
                    log.info("Copilota: loopback da '%s' @%dHz", spk.name, in_rate)
                    while not self._stop.is_set():
                        data = rec.record(numframes=block)
                        mono = data[:, 0] if getattr(data, "ndim", 1) > 1 else data
                        pcm = mono.astype(np.float32)
                        if in_rate != STT_RATE:
                            pcm = resample(pcm, in_rate, STT_RATE)
                        if self._stt is not None:
                            self._stt.send_audio(pcm)
            except Exception as e:  # noqa: BLE001 - device cambiato/occupato
                if self._stop.is_set():
                    break
                log.warning("Copilota: cattura loopback interrotta (%s), riprovo", e)
                time.sleep(1.0)

    # --- STT -> cervello --------------------------------------------------

    def _brain_loop(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._stt.utterances.get(timeout=0.4)
            except queue.Empty:
                continue
            text = coalesce_utterances(self._stt.utterances, first,
                                       self.cfg.ptt_turn_pause_s, stop=self._stop)
            if not text:
                continue
            self._seq += 1
            entry = CopilotEntry(seq=self._seq, utterance=text)
            self.transcript.append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "start_s": round(time.time() - self._started_at, 1),
                "speaker": "Interlocutore",
                "text": text,
            })
            self.ui_post(lambda e=entry: self.window.add_entry(e))
            try:
                for chunk in _copilot_respond(self.brain, text, self.cfg, self.num_responses,
                                              project_id=self.project_id):
                    if self._stop.is_set():
                        break
                    if chunk and isinstance(chunk, str):
                        entry.body += chunk
                        self.ui_post(lambda e=entry: self.window.update_entry(e))
            except Exception as e:  # noqa: BLE001 - errore lato cervello
                entry.body += f"\n[errore: {e}]"
                self.ui_post(lambda e=entry: self.window.update_entry(e))
            finally:
                entry.done = True
                self.ui_post(lambda e=entry: self.window.update_entry(e))


class CopilotWindow:
    """Finestra Tk (vive SOLO sul thread Tk). Testo grande scrollabile con
    autoscroll a velocità regolabile e dimensione font regolabile."""

    _BG = "#0b1020"
    _FG = "#e6e9f2"

    def __init__(self, cfg: Config, on_speak=None, on_num_change=None) -> None:
        self.cfg = cfg
        self.on_speak = on_speak            # callback(text) per leggere una risposta
        self.on_num_change = on_num_change  # callback(n) quando cambia il counter
        self.root = None
        self.win = None
        self.text = None
        self._font = None
        self._speed = float(cfg.copilot_scroll_speed)
        self._paused = False
        self._fps = 30
        self._after_id = None
        self._speed_lbl = None
        self._capture_btn = None
        self._hidden_capture = bool(cfg.copilot_hide_from_capture)
        self._num = int(cfg.copilot_num_responses)
        self._num_lbl = None
        self._audio_reply = bool(cfg.copilot_audio_reply)

    # --- ciclo di vita (thread Tk) ---------------------------------------

    def open(self, root) -> None:
        import tkinter as tk
        from tkinter import font as tkfont
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.title("Copilota — conversazione")
        self.win.configure(bg=self._BG)
        self.win.attributes("-topmost", True)
        sw = root.winfo_screenwidth()
        self.win.geometry(f"720x900+{max(0, sw - 740)}+40")

        self._font = tkfont.Font(family="Segoe UI", size=self.cfg.copilot_font_size)

        bar = tk.Frame(self.win, bg=self._BG)
        bar.pack(side="bottom", fill="x")
        def mkbtn(txt, cmd):
            return tk.Button(bar, text=txt, command=cmd, bg="#141a2e", fg=self._FG,
                             activebackground="#2563eb", relief="flat", padx=10, pady=6)
        mkbtn("A−", self._dec_font).pack(side="left", padx=2, pady=4)
        mkbtn("A+", self._inc_font).pack(side="left", padx=2, pady=4)
        mkbtn("« lento", self._dec_speed).pack(side="left", padx=2, pady=4)
        mkbtn("» veloce", self._inc_speed).pack(side="left", padx=2, pady=4)
        mkbtn("⏯ pausa", self._toggle_pause).pack(side="left", padx=2, pady=4)
        self._capture_btn = mkbtn("", self._toggle_capture)
        self._capture_btn.pack(side="left", padx=2, pady=4)
        self._speed_lbl = tk.Label(bar, text="", bg=self._BG, fg="#7c8194")
        self._speed_lbl.pack(side="right", padx=8)

        # Seconda barra: numero di risposte (counter) + rispondi con audio.
        bar2 = tk.Frame(self.win, bg=self._BG)
        bar2.pack(side="bottom", fill="x")
        tk.Label(bar2, text="Risposte:", bg=self._BG, fg=self._FG).pack(side="left", padx=(8, 2))
        mkbtn("−", self._dec_num).pack(side="left", padx=2, pady=4)
        self._num_lbl = tk.Label(bar2, text=str(self._num), bg=self._BG, fg=self._FG,
                                 width=2, font=("Segoe UI", 12, "bold"))
        self._num_lbl.pack(side="left")
        mkbtn("+", self._inc_num).pack(side="left", padx=2, pady=4)
        self._audio_var = tk.BooleanVar(value=self._audio_reply)
        tk.Checkbutton(
            bar2, text="🔊 Rispondi con audio (clicca una risposta)",
            variable=self._audio_var, command=self._on_audio_toggle,
            bg=self._BG, fg=self._FG, selectcolor="#141a2e",
            activebackground=self._BG, activeforeground=self._FG,
        ).pack(side="left", padx=12)

        wrap = tk.Frame(self.win, bg=self._BG)
        wrap.pack(side="top", fill="both", expand=True)
        sb = tk.Scrollbar(wrap)
        sb.pack(side="right", fill="y")
        self.text = tk.Text(wrap, wrap="word", bg=self._BG, fg=self._FG,
                            insertbackground=self._FG, font=self._font,
                            padx=14, pady=12, spacing1=2, spacing3=6,
                            yscrollcommand=sb.set, state="disabled", borderwidth=0)
        self.text.pack(side="left", fill="both", expand=True)
        sb.config(command=self.text.yview)
        # tag solo-colore: così cambiare la dimensione del Font le aggiorna tutte
        self.text.tag_config("hdr", foreground="#9ecbff")
        self.text.tag_config("ctx", foreground="#e6e9f2")
        self.text.tag_config("reply", foreground="#86efac")
        self.text.tag_config("data", foreground="#67e8f9")
        self.text.tag_config("note", foreground="#fca5a5")
        # Click su una risposta -> la legge ad alta voce (se "rispondi con audio").
        self.text.tag_bind("reply", "<Button-1>", self._on_reply_click)
        self.text.tag_bind("reply", "<Enter>",
                           lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind("reply", "<Leave>",
                           lambda e: self.text.config(cursor=""))

        self._update_speed_lbl()
        # Nasconde la finestra dalla cattura schermo (deve essere già mappata).
        self.win.update_idletasks()
        self._apply_capture_affinity()
        self._autoscroll()

    def _apply_capture_affinity(self) -> None:
        """SetWindowDisplayAffinity sul top-level: esclude la finestra dalla
        condivisione schermo/registrazione (Win10 2004+). No-op se non Windows."""
        import ctypes
        WDA_NONE = 0x00000000
        WDA_EXCLUDEFROMCAPTURE = 0x00000011
        try:
            user32 = ctypes.windll.user32
            child = self.win.winfo_id()
            hwnd = user32.GetParent(child) or child   # top-level reale del Toplevel Tk
            aff = WDA_EXCLUDEFROMCAPTURE if self._hidden_capture else WDA_NONE
            ok = user32.SetWindowDisplayAffinity(hwnd, aff)
            if not ok:
                log.warning("Copilota: SetWindowDisplayAffinity fallito (%s)",
                            ctypes.WinError())
        except Exception as e:  # noqa: BLE001 - non Windows o API assente
            log.warning("Copilota: esclusione dalla cattura non applicata: %s", e)
        self._update_capture_btn()

    def _toggle_capture(self) -> None:
        self._hidden_capture = not self._hidden_capture
        self._apply_capture_affinity()

    def _update_capture_btn(self) -> None:
        if self._capture_btn is not None:
            self._capture_btn.config(
                text="🙈 invisibile" if self._hidden_capture else "👁 visibile")

    def close(self) -> None:
        if self._after_id is not None and self.root is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001
                pass
            self._after_id = None
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:  # noqa: BLE001
                pass
        self.win = None
        self.text = None

    # --- contenuto (thread Tk) -------------------------------------------

    def add_entry(self, e: CopilotEntry) -> None:
        if self.text is None:
            return
        self.text.config(state="normal")
        self.text.insert("end", f"\n▶ {e.utterance}\n", ("hdr",))
        self.text.mark_set(f"bs{e.seq}", "end-1c")
        self.text.mark_gravity(f"bs{e.seq}", "left")
        self.text.insert("end", "…\n", ("ctx",))
        self.text.config(state="disabled")

    def update_entry(self, e: CopilotEntry) -> None:
        if self.text is None:
            return
        self.text.config(state="normal")
        try:
            self.text.delete(f"bs{e.seq}", "end-1c")
        except Exception:  # noqa: BLE001 - mark mancante
            self.text.config(state="disabled")
            return
        for line in e.body.split("\n"):
            up = line.strip().upper()
            if up.startswith("CONTESTO"):
                tag = "ctx"
            elif up.startswith("RISPOSTE") or line.lstrip().startswith(("-", "•", "*")):
                tag = "reply"
            elif up.startswith("DATI"):
                tag = "data"
            else:
                tag = "ctx"
            self.text.insert("end", line + "\n", (tag,))
        self.text.config(state="disabled")

    def add_note(self, msg: str) -> None:
        if self.text is None:
            return
        self.text.config(state="normal")
        self.text.insert("end", f"\n[{msg}]\n", ("note",))
        self.text.config(state="disabled")

    # --- controlli --------------------------------------------------------

    def _inc_font(self) -> None:
        if self._font:
            self._font.configure(size=min(72, self._font.cget("size") + 2))

    def _dec_font(self) -> None:
        if self._font:
            self._font.configure(size=max(10, self._font.cget("size") - 2))

    def _inc_speed(self) -> None:
        self._speed = min(5.0, round(self._speed + 0.25, 2))
        self._update_speed_lbl()

    def _dec_speed(self) -> None:
        self._speed = max(0.0, round(self._speed - 0.25, 2))
        self._update_speed_lbl()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self._update_speed_lbl()

    def _update_speed_lbl(self) -> None:
        if self._speed_lbl is not None:
            state = "in pausa" if self._paused else f"velocità {self._speed:.2f}/s"
            self._speed_lbl.config(text=state)

    def _inc_num(self) -> None:
        self._set_num(self._num + 1)

    def _dec_num(self) -> None:
        self._set_num(self._num - 1)

    def _set_num(self, n: int) -> None:
        self._num = max(1, min(6, n))
        if self._num_lbl is not None:
            self._num_lbl.config(text=str(self._num))
        if self.on_num_change is not None:
            self.on_num_change(self._num)

    def _on_audio_toggle(self) -> None:
        self._audio_reply = bool(self._audio_var.get())

    def _on_reply_click(self, event) -> None:
        if not self._audio_reply or self.on_speak is None or self.text is None:
            return
        idx = self.text.index(f"@{event.x},{event.y}")
        line = self.text.get(f"{idx} linestart", f"{idx} lineend").strip()
        if line and not line.upper().startswith("RISPOSTE"):
            self.on_speak(line)

    def _autoscroll(self) -> None:
        if self.text is not None and not self._paused and self._speed > 0:
            try:
                top, bottom = self.text.yview()
                if bottom < 1.0:
                    self.text.yview_moveto(min(top + self._speed / self._fps, 1.0))
            except Exception:  # noqa: BLE001
                pass
        if self.root is not None and self.win is not None:
            self._after_id = self.root.after(int(1000 / self._fps), self._autoscroll)


class CopilotController:
    """Crea/distrugge sessione + finestra al toggle della hotkey Shift+F4."""

    def __init__(self, cfg: Config, brain, overlay, avatar=None) -> None:
        self.cfg = cfg
        self.brain = brain
        self.overlay = overlay
        # AvatarClient CONDIVISO (uno solo per processo, vincolo VRAM): iniettato
        # da main.py. None se avatar disabilitato.
        self.avatar = avatar
        self._lock = threading.Lock()
        self._session: CopilotSession | None = None
        self._window: CopilotWindow | None = None
        # Progetto collegato (wikibridge) e ultima sessione conservata per
        # "Salva riunione nel wiki" anche dopo la chiusura del copilota.
        self.project_id: str | None = None
        self._last_session: CopilotSession | None = None
        self.save_state: dict = {"state": "idle"}

    @property
    def is_active(self) -> bool:
        """True se la finestra/sessione copilota e' aperta (per la GUI)."""
        return self._session is not None

    def set_project(self, project_id: str | None) -> None:
        self.project_id = (project_id or "").strip() or None
        if self._session is not None:
            self._session.project_id = self.project_id

    def save_meeting_async(self, project_id: str | None = None,
                           title: str | None = None) -> tuple[bool, str]:
        """Analizza la trascrizione della sessione (attiva o ultima) e la
        inserisce nel wiki del progetto. Asincrono: lo stato avanza in
        save_state (idle|saving|done|error)."""
        if project_id:
            self.set_project(project_id)
        session = self._session or self._last_session
        if session is None or not session.transcript:
            return False, "nessuna trascrizione: attiva il copilota e parla prima"
        if not self.project_id:
            return False, "nessun progetto collegato: indica project_id"
        if self.save_state.get("state") == "saving":
            return False, "salvataggio già in corso"
        segments = list(session.transcript)
        source = ("avatar" if session.avatar is not None
                  and self.cfg.avatar_persona else "copilot")
        proj = self.project_id
        meeting_title = (title or "").strip() or \
            f"Sessione copilota {time.strftime('%Y-%m-%d %H:%M')}"
        self.save_state = {"state": "saving", "project_id": proj}

        def _worker() -> None:
            from . import knowledge_client
            from .analysis import analyze_transcript
            try:
                # la pagina della riunione sarà visibile all'operatore (e ai
                # partecipanti riconosciuti): conoscenza coi suoi permessi
                analysis = analyze_transcript(
                    self.cfg, segments, kind="riunione (videochiamata)",
                    preamble=knowledge_client.project_context(
                        self.cfg, proj, knowledge_client.transcript_query(segments))
                    or None)
                path = knowledge_client.push_meeting(
                    self.cfg, proj,
                    meeting_id=f"copilot-{time.strftime('%Y%m%d-%H%M%S')}",
                    title=meeting_title,
                    date=time.strftime("%Y-%m-%d"),
                    source=source,
                    participants=["Interlocutore"],
                    analysis=analysis)
                if path:
                    self.save_state = {"state": "done", "project_id": proj,
                                       "wiki_path": path}
                else:
                    self.save_state = {"state": "error",
                                       "error": "wikibridge non raggiungibile (:8775)"}
            except Exception as e:  # noqa: BLE001
                log.error("salvataggio riunione copilota fallito: %s", e)
                self.save_state = {"state": "error", "error": str(e)}

        threading.Thread(target=_worker, name="copilot-save-meeting",
                         daemon=True).start()
        return True, "salvataggio avviato"

    def toggle(self) -> None:
        with self._lock:
            if self._session is None:
                from .sospensione import richiedi_attivi
                richiedi_attivi()       # da sospeso il copilota non si apre
                self._activate()
            else:
                self._deactivate()

    def _activate(self) -> None:
        # Usa l'AvatarClient CONDIVISO (iniettato); niente istanza dedicata, per
        # non aprire un secondo worker MuseTalk (doppia VRAM).
        avatar = self.avatar if self.cfg.avatar_persona else None
        session = CopilotSession(self.cfg, self.brain, self.overlay.post, None,
                                 avatar=avatar, project_id=self.project_id)
        window = CopilotWindow(self.cfg, on_speak=session.speak,
                               on_num_change=session.set_num_responses)
        session.window = window
        self._session = session
        self._window = window
        # apre la finestra sul thread Tk (il root esiste già nel mainloop)
        self.overlay.post(lambda: window.open(self.overlay.get_root()))
        session.start()
        log.info("Copilota: attivato (Shift+F4)")

    def _deactivate(self) -> None:
        if self._session is not None:
            self._session.stop()
            self._last_session = self._session   # trascrizione salvabile dopo
            self._session = None
        win = self._window
        self._window = None
        if win is not None:
            self.overlay.post(win.close)
        log.info("Copilota: disattivato")
