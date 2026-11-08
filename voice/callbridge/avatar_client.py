"""Client del worker avatar video (MuseTalk) — vedi avatar/avatar_worker.py.

L'avatar e' un processo separato (venv dedicato `musetalk-venv`) che mostra la
persona scelta in idle su una webcam virtuale (OBS) e, su richiesta, riproduce
una risposta con il labiale sincronizzato. Questo client lo avvia e gli parla
a righe JSON su stdin/stdout.

Archivio persone (`avatar/persone/<nome>/manifest.json`):
  {
    "name": "alice",
    "voice": {"engine": "chatterbox", "voice": "sara_clone"},
    "clips": ["clips/abito_blu.mp4", "clips/abito_rosso.mp4"]
  }
Il worker sceglie un clip A CASO a ogni attivazione (outfit diverso ogni
volta); la voce collegata viene letta da qui per configurare il TTS.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from .config import Config

log = logging.getLogger("callbridge.avatar")


class AvatarClient:
    """Gestisce il processo avatar_worker e l'archivio persone."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._persona_ready = threading.Event()
        # Fotogrammi in movimento (evento "playing"): distinto da `_speaking`,
        # che scatta all'arrivo del comando, prima della generazione.
        self._playing = threading.Event()
        #: Secondi che il worker ha impiegato a preparare l'ultima battuta
        #: (dal comando al primo fotogramma pronto). E' il preambolo VERO
        #: del labiale: non include l'attesa in coda, che invece pesa sulla
        #: durata della POST e che per anni e' stata scambiata per il
        #: preambolo stesso.
        self.preparazione_s: float = 0.0
        self._active_persona: str | None = None
        # Errore emesso dal worker durante la preparazione: sblocca start()
        # SUBITO invece di lasciarlo in attesa fino al timeout (10 min).
        self._persona_error = threading.Event()
        self.last_error: str | None = None
        # Avvio asincrono (start_async): la GUI fa polling su questi campi
        # invece di tenere aperta una richiesta HTTP per minuti.
        self.preparing: str | None = None   # persona in preparazione
        self._start_thread: threading.Thread | None = None
        # Ultimo evento progress del worker: {phase, done, total, percent, eta_s}
        self.progress: dict | None = None
        # Riproduzione in corso: il worker emette "speaking" all'inizio di una
        # battuta e "idle"/"interrupted" a fine. `_spoken` parte SET (nessuna
        # battuta in corso) cosi' `wait_until_spoken` non blocca a vuoto.
        self._speaking = threading.Event()
        self._spoken = threading.Event()
        self._spoken.set()

    # --- archivio -----------------------------------------------------------

    def avatar_root(self) -> Path:
        return Path(self.cfg.avatar_dir)

    def personas(self) -> list[str]:
        root = self.avatar_root() / "persone"
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir()
                      if (p / "manifest.json").is_file())

    def manifest(self, persona: str) -> dict:
        p = self.avatar_root() / "persone" / persona / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def voice_for(self, persona: str) -> dict:
        """{"engine": "chatterbox"|"kokoro"|"piper", "voice": "..."} dal manifest."""
        try:
            return self.manifest(persona).get("voice") or {}
        except Exception as e:  # noqa: BLE001
            log.warning("Avatar: manifest di %s illeggibile: %s", persona, e)
            return {}

    # --- ciclo di vita del worker -------------------------------------------

    def start(self, persona: str) -> bool:
        """Avvia il worker (se serve) e carica la persona. Bloccante fino a
        persona pronta: la prima preparazione di un clip puo' richiedere anche
        piu' di 10 minuti (dipende da lunghezza e GPU) e il progresso e' visibile
        via self.progress. Non c'e' un timeout ASSOLUTO (romperebbe le prep
        lunghe legittime), ma un watchdog "nessun progresso" evita che un hang
        GPU (worker vivo che non manda mai ready/error) blocchi l'avatar per
        sempre. Ritorna subito False su errore/morte del worker (dettaglio in
        last_error)."""
        from .sospensione import MSG_SOSPESO, in_sospensione
        if in_sospensione():
            self.last_error = MSG_SOSPESO       # niente avatar da sospeso
            return False
        with self._lock:
            self.last_error = None
            if not self._ensure_worker():
                self.last_error = self.last_error or "worker non avviabile (vedi log)"
                return False
            self._persona_ready.clear()
            self._persona_error.clear()
            self.progress = None
            pdir = str(self.avatar_root() / "persone" / persona)
            self._send({"cmd": "persona", "dir": pdir})
        stall_s = getattr(self.cfg, "avatar_prepare_stall_s", 300.0)
        last_progress = self.progress
        last_change = time.monotonic()
        while True:
            if self._persona_ready.wait(timeout=0.5):
                # Persona pronta: registra lo stato attivo QUI (non solo in
                # start_async), cosi' preload/copilota rendono is_ready()==True e
                # /avatar/state "running". Senza, una F1 dopo il preload
                # richiamava start_async inutilmente (worker gia' caldo).
                self._active_persona = persona
                return True
            if self._persona_error.is_set():
                log.error("Avatar: persona '%s' fallita: %s", persona, self.last_error)
                return False
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self.last_error = "il worker avatar e' terminato durante la preparazione"
                log.error("Avatar: %s (persona '%s')", self.last_error, persona)
                return False
            # Watchdog: worker vivo ma nessun avanzamento entro `stall_s` = hang.
            if self.progress != last_progress:
                last_progress = self.progress
                last_change = time.monotonic()
            elif stall_s > 0 and time.monotonic() - last_change > stall_s:
                self.last_error = (
                    f"preparazione avatar bloccata (nessun progresso da {int(stall_s)}s)")
                log.error("Avatar: %s (persona '%s')", self.last_error, persona)
                return False

    def start_async(self, persona: str) -> bool:
        """Avvia la preparazione in un thread e ritorna SUBITO.

        Stato osservabile via `preparing` / `last_error` / `_active_persona`
        (endpoint /avatar/state): la GUI fa polling invece di tenere aperta
        una HTTP per i minuti della prima preparazione. Ritorna False se
        un'altra preparazione e' gia' in corso.
        """
        from .sospensione import MSG_SOSPESO, in_sospensione
        if in_sospensione():
            self.last_error = MSG_SOSPESO
            return False
        if self._start_thread is not None and self._start_thread.is_alive():
            return False
        # Impostato PRIMA di avviare il thread: il primo poll di /avatar/state
        # deve gia' vedere "preparing" (niente finestra in cui sembra fermo).
        self.preparing = persona

        def _run() -> None:
            try:
                ok = self.start(persona)
                if ok:
                    self._active_persona = persona
            finally:
                self.preparing = None

        self._start_thread = threading.Thread(
            target=_run, name="avatar-start", daemon=True)
        self._start_thread.start()
        return True

    def speak_wav(self, wav_path: str, mute: bool = False) -> None:
        """Riproduce la risposta (video lip-sync + audio) — non bloccante.

        Con `mute` il worker anima le labbra ma non suona nulla: la voce esce
        altrove (il telefono, tramite il voice gateway) e riprodurla anche qui
        vorrebbe dire sentirla due volte, sfasata.
        """
        with self._lock:
            if self._proc is None:
                log.warning("Avatar: worker non attivo, speak ignorato")
                return
            # Marca SUBITO "battuta in corso": senza, un wait_until_spoken
            # chiamato prima che arrivi l'evento "speaking" ritornerebbe subito.
            self._spoken.clear()
            self._playing.clear()
            self._send({"cmd": "speak", "wav": wav_path, "mute": bool(mute)})

    def wait_until_playing(self, timeout: float) -> bool:
        """Attende che i fotogrammi comincino a scorrere. True se e' partito.

        Serve a chi manda la voce da un'altra parte (il gateway verso il
        telefono): senza, l'audio parte appena sintetizzato mentre il worker sta
        ancora generando i frame, e si sente la voce prima di vedere la bocca."""
        return self._playing.wait(timeout)

    def is_ready(self, persona: str | None = None) -> bool:
        """True se il worker e' vivo e la persona e' pronta (labiale possibile).

        Con `persona` verifica anche che sia proprio quella caricata; senza,
        basta che una qualsiasi persona sia pronta."""
        if self._proc is None or self._proc.poll() is not None:
            return False
        if not self._persona_ready.is_set():
            return False
        return persona is None or self._active_persona == persona

    def wait_until_spoken(self, timeout: float) -> bool:
        """Attende la fine della battuta corrente (evento idle/interrupted).

        Ritorna True se la riproduzione e' terminata entro `timeout`, False se
        e' scaduto (rete di sicurezza se l'ack si perde)."""
        return self._spoken.wait(timeout)

    def interrupt(self, svuota: bool = False) -> None:
        """Tronca la battuta in corso: audio fermato, ritorno all'idle.

        Con `svuota` butta anche le battute gia' accodate nel worker. Serve
        quando si abbandona una risposta INTERA: senza, il worker riprende
        dalla battuta successiva della stessa risposta e la bocca continua a
        pronunciarla.
        """
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return
            self._send({"cmd": "interrupt", "svuota": bool(svuota)})

    def set_stream(self, on: bool, port: int = 0) -> bool:
        """Dirotta il video verso il telefono (o lo riporta sulla webcam locale).

        Mentre lo stream è attivo il worker NON alimenta la webcam virtuale né
        la finestra: se l'avatar sta parlando in una videochiamata sul telefono,
        sul PC non deve comparire. False = worker non attivo."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return False
            self._send({"cmd": "stream", "on": bool(on), "port": int(port or 0)})
            return True

    def set_preview(self, show: bool) -> None:
        """Mostra/nasconde la finestra video del worker (che resta caldo).

        Usato dalle sessioni PTT avatar: a fine sessione la finestra si chiude,
        ma il worker sopravvive con la persona pronta (riapertura istantanea)."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return
            try:
                self._send({"cmd": "preview", "show": bool(show)})
            except Exception as e:  # noqa: BLE001 - worker morente: ignora
                log.debug("Avatar: set_preview fallito: %s", e)

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._send({"cmd": "quit"})
                    self._proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    self._proc.kill()
                self._proc = None
            self._ready.clear()
            self._persona_ready.clear()
            self._active_persona = None
            self._speaking.clear()
            self._spoken.set()  # sblocca chi attende su wait_until_spoken

    # --- interni --------------------------------------------------------------

    def _ensure_worker(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        root = self.avatar_root()
        python = root / "musetalk-venv" / "Scripts" / "python.exe"
        worker = root / "avatar_worker.py"
        if not python.is_file() or not worker.is_file():
            log.error("Avatar: venv o worker mancanti in %s", root)
            return False
        # Pulizia worker orfani: i riavvii del bridge lasciano vivi i worker
        # precedenti (Windows non uccide i figli), che accumulandosi saturano
        # la VRAM e fanno morire in OOM i worker nuovi durante la preparazione.
        try:
            import psutil
            for p in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cl = p.info.get("cmdline") or []
                    if any("avatar_worker" in c for c in cl):
                        p.kill()
                        log.warning("Avatar: worker orfano %d terminato", p.pid)
                except Exception:  # noqa: BLE001 - processo gia' morto/di altri
                    continue
        except Exception as e:  # noqa: BLE001 - psutil assente: si prosegue
            log.debug("Avatar: pulizia orfani saltata: %s", e)
        from . import models_store
        env = models_store.helper_env("avatar")
        if self.cfg.avatar_audio_device is not None:
            env["AVATAR_AUDIO_DEVICE"] = str(self.cfg.avatar_audio_device)
        if self.cfg.avatar_preview_window:
            # Finestra video locale con l'avatar che parla, oltre alla webcam
            # virtuale (richiesta esplicita: vedere l'avatar senza app di call).
            env["AVATAR_PREVIEW"] = "1"
        fps = int(getattr(self.cfg, "avatar_fps", 0) or 0)
        if fps > 0:
            # Anche il gateway legge questa variabile: i due DEVONO combaciare,
            # altrimenti la traccia WebRTC ripete o scarta fotogrammi.
            env["AVATAR_FPS"] = str(fps)
        if getattr(self.cfg, "avatar_profile", False):
            env["AVATAR_PROFILE"] = "1"
        if self.cfg.avatar_gpu_index is not None:
            # Il worker vedra' SOLO questa GPU (che per lui diventa cuda:0).
            env["CUDA_VISIBLE_DEVICES"] = str(self.cfg.avatar_gpu_index)
        log.info("Avatar: avvio worker (caricamento modelli ~30s)...")
        self._ready.clear()
        self._proc = subprocess.Popen(
            [str(python), str(worker)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            cwd=str(root),
            # env=env era ASSENTE: AVATAR_PREVIEW/AVATAR_AUDIO_DEVICE/
            # CUDA_VISIBLE_DEVICES venivano costruite e mai consegnate.
            env=env,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()
        if not self._ready.wait(timeout=180):
            log.error("Avatar: worker non pronto entro 180s")
            return False
        return True

    def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _reader(self) -> None:
        proc = self._proc
        assert proc and proc.stdout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = msg.get("event")
            if ev == "ready":
                self._ready.set()
            elif ev == "progress":
                # Frequente (2/s): niente INFO, lo consuma /avatar/state.
                self.progress = {k: msg.get(k) for k in
                                 ("phase", "done", "total", "percent", "eta_s")}
            elif ev == "persona_ready":
                log.info("Avatar: persona pronta (clip %s, %s frame)",
                         msg.get("clip"), msg.get("frames"))
                self.progress = None
                self._persona_ready.set()
            elif ev == "error":
                self.last_error = str(msg.get("message") or "errore del worker")
                self._persona_error.set()
                log.error("Avatar: %s", msg.get("message"))
            elif ev == "speaking":
                # Inizio riproduzione di una battuta (audio + labiale).
                self._spoken.clear()
                self._speaking.set()
                log.info("Avatar: evento %s", msg)
            elif ev == "playing":
                # I fotogrammi cominciano DAVVERO a scorrere adesso (dopo la
                # generazione): e' l'istante a cui agganciare una voce che esce
                # altrove, per non sentirla partire prima che la bocca si muova.
                self._playing.set()
                try:
                    self.preparazione_s = float(msg.get("preparazione_s") or 0.0)
                except (TypeError, ValueError):
                    self.preparazione_s = 0.0
            elif ev in ("idle", "interrupted"):
                # Fine (o troncamento) della battuta: sblocca chi attende.
                self._speaking.clear()
                self._spoken.set()
                log.info("Avatar: evento %s", msg)
            else:
                log.info("Avatar: evento %s", msg)

    def _stderr_reader(self) -> None:
        proc = self._proc
        assert proc and proc.stderr
        for line in proc.stderr:
            text = line.rstrip()
            if not text:
                continue
            # Barre di progresso (tqdm) a debug; tutto il resto a INFO: senza,
            # i fallimenti della preparazione restavano invisibili in ptt.log.
            if "%|" in text or "it/s]" in text:
                log.debug("avatar[stderr] %s", text)
            else:
                log.info("avatar[stderr] %s", text)
