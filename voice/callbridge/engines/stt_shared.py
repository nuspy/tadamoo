"""L'UNICO STT del sistema: un processo whisper condiviso da tutti.

Prima c'erano due whisper identici in memoria: uno nel bridge (sessioni voce,
push-to-talk, WebSocket /stt/stream) e uno nel processo della dettatura. Ora
esiste solo il secondo (`engines/dictation_stt_helper`), e questo adattatore
espone al resto del bridge la STESSA interfaccia che aveva `WhisperSttEngine`:
`load`, `transcribe`, `transcribe_detailed`, `runtime_info`, `reconfigure`,
stato "caricato" (`_model`).

Perche' resta un PROCESSO a parte e non torna in-process: in-process whisper
divideva il GIL col bridge e diventava ~10x piu' lento (vedi l'helper).

Regole:
  - le richieste sono SERIALIZZATE (una alla volta): dettatura, sessioni e
    /stt/stream si mettono in fila sullo stesso lucchetto;
  - il processo e' sempre attivo e caldo: NESSUN metodo qui lo ferma o lo
    scarica, tranne `chiudi()`, riservato all'uscita del bridge;
  - l'unica ricarica e' quella voluta dall'utente: cambiando modello, device
    o precisione dalla GUI, `reconfigure` ricarica SUBITO sul nuovo valore;
  - se il processo muore per un errore, la richiesta successiva lo rialza al
    volo (la dettatura in corso paga il caricamento ma non va persa).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .. import models_store

log = logging.getLogger("callbridge.engines.stt_shared")

_CAMPI = ("stt_model", "stt_device", "stt_gpu_memory", "stt_compute_type")

# --- controllo periodico (vedi `SharedSttEngine.controlla`) ---------------------------
# Ogni quanto il bridge verifica che l'STT sia vivo e ancora in VRAM.
INTERVALLO_CONTROLLO_S = 30.0
# Confermato dall'utente il 18/10/2026: "fuori dalla VRAM" = VRAM dedicata del processo sotto questa quota
# di quella che il processo aveva dichiarato al caricamento (`vram_mb`). Misurato:
# STT medium sano 2058 MB dedicati (dichiarati 1484 MB); STT dopo l'esaurimento
# di memoria del 17/10, 74-78 MB. Una quota del 50% li separa con un margine enorme.
SOGLIA_RESIDENZA = 0.5
# Confermato dall'utente il 18/10/2026: tra due ricariche per "fuori dalla VRAM" passa almeno questo tempo,
# cosi' una GPU davvero piena non fa ricaricare il modello in continuazione.
PAUSA_RICARICA_VRAM_S = 600.0


def vram_dedicata_mb(pid: int) -> float | None:
    """VRAM DEDICATA usata da `pid`, in MB, dal contatore di Windows
    "GPU Process Memory\\Dedicated Usage" (sommata sulle schede). None se non si
    riesce a leggere (non Windows, contatore assente, processo non elencato).

    E' l'unica misura per processo che WDDM da': nvidia-smi risponde [N/A] e
    `torch.cuda.memory_allocated` resta uguale anche quando le allocazioni sono
    state spostate in memoria di sistema. Costa ~1,2 s di `typeperf` in un
    processo a parte."""
    if sys.platform != "win32":
        return None
    kw: dict = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        r = subprocess.run(
            ["typeperf", r"\GPU Process Memory(*)\Dedicated Usage", "-sc", "1"],
            capture_output=True, text=True, timeout=30, **kw)
    except (OSError, subprocess.SubprocessError):
        return None
    righe = [l for l in r.stdout.splitlines() if l.startswith('"')]
    if len(righe) < 2:
        return None
    import csv
    intest, valori = (next(csv.reader([righe[0]])), next(csv.reader([righe[1]])))
    marca = f"pid_{pid}_"
    tot, trovato = 0.0, False
    for h, v in zip(intest[1:], valori[1:]):
        if marca in h:
            trovato = True
            try:
                tot += float(v)
            except ValueError:
                pass
    return tot / (1024 * 1024) if trovato else None


def _comando_helper() -> list[str]:
    """Comando del processo STT (i test lo sostituiscono con un finto helper)."""
    return [sys.executable, "-u", "-m", "callbridge.engines.dictation_stt_helper"]


class SharedSttEngine:
    """Client del processo STT unico (interfaccia di `WhisperSttEngine`)."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._proc: subprocess.Popen | None = None
        # Una richiesta alla volta: trascrizioni, caricamenti e ricariche.
        self._lock = threading.Lock()
        self.state = "stopped"          # stopped | loading | running | error
        self.error: str | None = None
        self.info: dict = {}            # risposta "ready" dell'helper
        self._voluto = self._leggi(cfg)
        self._applicato: tuple | None = None   # cosa ha caricato il processo vivo
        self.last_ms: int | None = None
        self._ultima_ricarica_vram = -1e9

    # --- configurazione ---------------------------------------------------

    @staticmethod
    def _leggi(cfg) -> tuple:
        """(modello, device, memoria, precisione) cosi' come sono in config."""
        return (cfg.stt_model or "medium", cfg.stt_device or "cuda",
                cfg.stt_gpu_memory or "dedicated",
                cfg.stt_compute_type or "auto")

    @staticmethod
    def _device_effettivo(device: str, memory: str) -> str:
        from .device import resolve_device
        return resolve_device(device, memory)

    # --- stato per chi legge ------------------------------------------------

    @property
    def pid(self) -> int | None:
        p = self._proc
        return p.pid if p is not None and p.poll() is None else None

    @property
    def loaded(self) -> bool:
        return self.state == "running" and self.pid is not None

    @property
    def _model(self):
        """Compat con chi controllava `stt_engine._model is not None`."""
        return self.info if self.loaded else None

    @property
    def model_name(self) -> str:
        return self._voluto[0]

    @property
    def device(self) -> str:
        return self._device_effettivo(self._voluto[1], self._voluto[2])

    def runtime_info(self) -> dict:
        """Stato REALE (non l'intenzione): modello, device e precisione di cio'
        che e' caricato adesso, piu' l'intenzione in config per chi li confronta."""
        from .whisper_stt import precisione_effettiva
        modello, dev, mem, comp = self._voluto
        atteso = self._device_effettivo(dev, mem)
        vivo = self.loaded
        return {
            "configured": f"{atteso}/{mem}",
            "would_load_on": atteso,
            "loaded": vivo,
            "state": self.state,
            "running_on": self.info.get("device") if vivo else None,
            "model": (self.info.get("model") if vivo else None) or modello,
            "precision": ((self.info.get("precision") if vivo else None)
                          or precisione_effettiva(atteso, comp)),
            "pid": self.pid,
            "vram_mb": self.info.get("vram_mb") if vivo else None,
            "error": self.error,
        }

    # --- processo -----------------------------------------------------------

    def _spawn_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        modello, dev, mem, comp = self._voluto
        env = models_store.helper_env("stt")
        env["DICT_STT_MODEL"] = modello
        env["DICT_STT_DEVICE"] = dev
        env["DICT_STT_MEMORY"] = mem
        env["DICT_STT_COMPUTE"] = comp
        env.setdefault("PYTHONUNBUFFERED", "1")
        kw: dict = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):    # niente console pop-up
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        radice = Path(__file__).resolve().parents[2]   # call-bridge/
        self._proc = subprocess.Popen(
            _comando_helper(), cwd=str(radice), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, env=env, **kw)
        self._applicato = None
        threading.Thread(target=self._drain_stderr, args=(self._proc,),
                         name="stt-shared-stderr", daemon=True).start()
        log.info("STT: processo avviato (pid %d)", self._proc.pid)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line and "Warning" not in line:
                log.info("STT: %s", line)

    def _request_locked(self, req: dict, payload: bytes = b"") -> dict:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise RuntimeError("processo STT non attivo")
        assert proc.stdin is not None and proc.stdout is not None
        try:
            dati = memoryview((json.dumps(req) + "\n").encode("utf-8") + payload)
            while len(dati):
                scritti = proc.stdin.write(dati)
                if not scritti:
                    raise OSError("scrittura sulla pipe a zero byte")
                dati = dati[scritti:]
            proc.stdin.flush()
        except OSError as e:
            raise RuntimeError(f"processo STT non scrivibile: {e}") from e
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("processo STT terminato (vedi log)")
        resp = json.loads(line.decode("utf-8"))
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "errore sconosciuto")
        return resp

    def _kill_locked(self) -> None:
        proc, self._proc = self._proc, None
        self._applicato = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write(b'{"cmd":"quit"}\n')
                proc.stdin.flush()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # --- caricamento ----------------------------------------------------------

    def _allinea_locked(self) -> None:
        """Porta il processo a cio' che e' voluto: lo avvia se manca, lo rifa da
        capo se modello/device/precisione sono cambiati, non fa nulla se e' gia' cosi'.

        Il cambio NON ricarica il modello dentro il processo vivo: misurato, un
        reload in place lasciava ~1 GB di Private Bytes in piu' (allocatore e
        piani cuDNN del modello vecchio) e, passando a CPU, il contesto CUDA
        (~0,5 GB). Rifare il processo costa ~3 s e restituisce tutto."""
        voluto = self._voluto
        vivo = self._proc is not None and self._proc.poll() is None
        if vivo and self._applicato == voluto:
            self.state = "running"      # nulla da ricaricare (es. A -> B -> A)
            return
        self.state, self.error = "loading", None
        try:
            if vivo:
                self._kill_locked()      # configurazione cambiata: processo nuovo
            self._spawn_locked()
            self.info = self._request_locked({"cmd": "load"})
            self._applicato = voluto
            self.state = "running"
            log.info("STT: whisper '%s' pronto su %s (%s) in %.1fs (VRAM %s MB)",
                     self.info.get("model"), self.info.get("device"),
                     self.info.get("precision"), self.info.get("load_s") or 0,
                     self.info.get("vram_mb"))
            if (self.info.get("device") != "cuda"
                    and self._device_effettivo(voluto[1], voluto[2]) == "cuda"):
                # Ripiego silenzioso su CPU = ~20x piu' lento: va detto forte.
                self.error = ("whisper e' su CPU anche se la config chiede "
                              "cuda: torch/CUDA non disponibile nel processo")
                log.warning("STT: %s", self.error)
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", str(e)
            log.error("STT non caricato: %s", e)
            self._kill_locked()
            raise

    def load(self) -> None:
        """Garantisce processo e modello pronti (bloccante). Idempotente."""
        with self._lock:
            self._allinea_locked()

    def reconfigure(self, cfg) -> None:
        """Il modello, il device o la precisione sono cambiati dalla GUI:
        ricarica SUBITO sul nuovo valore, in background (lo stato passa a
        "loading" e poi a quello reale). Non scarica mai nulla a riposo."""
        nuovo = self._leggi(cfg)
        self.cfg = cfg
        if nuovo == self._voluto:
            return
        self._voluto = nuovo
        self.state = "loading"           # la GUI vede "carico…" prima di tutto
        threading.Thread(target=self._ricarica, name="stt-reload",
                         daemon=True).start()

    def _ricarica(self) -> None:
        try:
            self.load()
        except Exception:  # noqa: BLE001 - gia' registrato in stato ed errore
            pass

    def ricarica_forzata(self, motivo: str = "") -> dict:
        """Rifa il processo (anche se e' sano) e ricarica il modello in VRAM.
        Bloccante; solleva se il caricamento fallisce. Ritorna `runtime_info()`.
        Lo usano «Load models» da sospeso e il controllo periodico."""
        log.info("STT: ricarica forzata%s", f" ({motivo})" if motivo else "")
        with self._lock:
            self.state, self.error = "loading", None
            self._kill_locked()
            self._allinea_locked()
        return self.runtime_info()

    def controlla(self) -> str:
        """Un giro del controllo periodico: l'STT deve essere vivo e residente.

        - processo morto (o mai caricato / caricamento fallito): lo rialza e lo
          ricarica subito;
        - su GPU, VRAM dedicata del processo sotto `SOGLIA_RESIDENZA` di quella
          dichiarata al caricamento: il modello e' uscito dalla VRAM (WDDM lo ha
          spostato) e il processo va rifatto, al piu' una volta ogni
          `PAUSA_RICARICA_VRAM_S`.
        Vale anche da sospeso. Ritorna cosa e' successo: "ok", "occupato",
        "rialzato", "errore", "ricaricato_vram", "fuori_vram_in_pausa"."""
        if self.state == "loading" or self._lock.locked():
            return "occupato"                # un caricamento o una trascrizione in corso
        if self.pid is None:
            log.warning("STT: processo non attivo (stato %s%s): lo rialzo",
                        self.state, f", {self.error}" if self.error else "")
            try:
                self.load()
            except Exception as e:  # noqa: BLE001
                log.error("STT: rialzo fallito: %s", e)
                return "errore"
            log.info("STT: rialzato e ricaricato (pid %s)", self.pid)
            return "rialzato"
        atteso = float(self.info.get("vram_mb") or 0)
        if self.info.get("device") != "cuda" or atteso <= 0:
            return "ok"
        pid = self.pid
        dedicata = vram_dedicata_mb(pid) if pid else None
        if dedicata is None or dedicata >= SOGLIA_RESIDENZA * atteso:
            return "ok"
        if time.monotonic() - self._ultima_ricarica_vram < PAUSA_RICARICA_VRAM_S:
            return "fuori_vram_in_pausa"
        log.warning("STT: modello FUORI dalla VRAM (dedicata %.0f MB, dichiarati %.0f MB, "
                    "soglia %.0f%%): ricarico", dedicata, atteso, SOGLIA_RESIDENZA * 100)
        self._ultima_ricarica_vram = time.monotonic()
        try:
            self.ricarica_forzata("fuori dalla VRAM")
        except Exception as e:  # noqa: BLE001
            log.error("STT: ricarica per VRAM fallita: %s", e)
            return "errore"
        return "ricaricato_vram"

    # --- trascrizione -----------------------------------------------------------

    def _invia(self, cmd: str, audio: np.ndarray, lang: str | None, **extra) -> dict:
        pcm = np.ascontiguousarray(audio, dtype="<f4")
        with self._lock:
            # Morto (o mai partito, o configurazione cambiata nel frattempo): lo
            # si rialza al volo; la richiesta in corso paga il caricamento.
            self._allinea_locked()
            try:
                r = self._request_locked(
                    {"cmd": cmd, "lang": lang or "", "n": int(pcm.size), **extra},
                    pcm.tobytes())
            except Exception:
                if self._proc is not None and self._proc.poll() is not None:
                    self.state, self.error = "error", "processo STT terminato"
                raise
        self.last_ms = int(r.get("ms") or 0)
        return r

    def transcribe_timed(self, audio: np.ndarray, lang: str | None = None
                         ) -> tuple[str, int]:
        """(testo, ms di whisper) — per chi vuole anche il tempo."""
        r = self._invia("transcribe", audio, lang)
        return (r.get("text") or "").strip(), int(r.get("ms") or 0)

    def transcribe(self, audio_f32_16k: np.ndarray, lang: str | None = None) -> str:
        return self.transcribe_timed(audio_f32_16k, lang)[0]

    def transcribe_detailed(self, audio_f32_16k: np.ndarray,
                            lang: str | None = None,
                            word_timestamps: bool = False,
                            detect: bool = False, allowed=None) -> dict:
        """`{text, segments: [{start_s, end_s, text, words?}]}`, come in-process. With `detect` also `language` and
        `language_prob`: what whisper heard when `lang` is empty (else `lang` itself)."""
        extra = {"detect": True, "allowed": list(allowed or ())} if detect else {}
        r = self._invia("transcribe_detailed", audio_f32_16k, lang,
                        words=bool(word_timestamps), **extra)
        out = {"text": (r.get("text") or "").strip(),
               "segments": r.get("segments") or []}
        if detect:
            out["language"], out["language_prob"] = r.get("language") or "", r.get("language_prob") or 0.0
        return out

    # --- uscita ---------------------------------------------------------------

    def chiudi(self) -> None:
        """SOLO all'uscita del bridge (o nei test): chiude il processo.

        Non e' un modo di "fermare l'STT" — quello deve restare sempre acceso —
        ma di non lasciare un orfano quando il bridge finisce."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._kill_locked()
            self.state, self.error, self.info = "stopped", None, {}

    def attendi_pronto(self, timeout: float = 120.0) -> bool:
        """Per i test e la diagnostica: attende che lo stato sia "running"."""
        fine = time.monotonic() + timeout
        while time.monotonic() < fine:
            if self.state in ("running", "error"):
                return self.state == "running"
            time.sleep(0.1)
        return False
