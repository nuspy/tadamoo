"""TTS voci CLONATE nativo: Chatterbox Multilingual via helper subprocess.

Ripristina la sintesi con voci clonate che Handy faceva con Chatterbox, ma
pilotata direttamente dal bridge. L'helper (`chatterbox_tts_helper.py`,
vendorizzato accanto a questo file) gira in un venv dedicato con `chatterbox-tts`
e parla il protocollo stdio binario storico:

  stdin:  {"text","voice","lang","speed"}\\n   |   {"cmd":"quit"}\\n
  stdout: [u32 LE count][u32 LE sample_rate][count * f32 LE]   (count==0 = errore)
  stderr: log

`voice` e' il nome (senza estensione) di un wav di riferimento in
`<model_dir>/cloned_voices/` (env `KOKORO_MODEL_DIR`). Il modello si carica alla
prima sintesi (subprocess tenuto caldo); i conditionals della voce sono in cache
lato helper (cambiare voce costa solo la prima frase).

`ChatterboxTtsEngine.synth()` ha la STESSA firma di `KokoroTtsEngine.synth()`,
cosi' il `TtsRouter` puo' smistare kokoro/chatterbox in modo trasparente per
`/tts/test`, `/tts/stream` e l'avatar.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger("callbridge.engines.chatterbox")


def _default_venv_python() -> str:
    """Python del venv Chatterbox lasciato da Handy (riuso: ~3 GB gia' scaricati)."""
    return os.path.expandvars(
        r"%APPDATA%\com.pais.handy\chatterbox-venv\Scripts\python.exe")


def _default_helper() -> str:
    """Helper vendorizzato accanto a questo modulo (self-contained)."""
    return os.path.join(os.path.dirname(__file__), "chatterbox_tts_helper.py")


def _read_exact(pipe, n: int) -> bytes:
    """Legge esattamente `n` byte dallo stdout binario dell'helper (o EOF)."""
    chunks: list[bytes] = []
    got = 0
    while got < n:
        b = pipe.read(n - got)
        if not b:
            break  # EOF: subprocess morto
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


class ChatterboxTtsEngine:
    """Helper Chatterbox in subprocess, caldo e condiviso tra le sessioni."""

    def __init__(self, model_dir: str, venv_python: str = "",
                 helper: str = "", device: str = "cuda",
                 memory: str = "dedicated") -> None:
        self.model_dir = model_dir
        self._python = venv_python or _default_venv_python()
        self._helper = helper or _default_helper()
        self.device = device
        self.memory = memory
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()   # una richiesta alla volta sulla pipe
        self._sr: int | None = None

    @property
    def available(self) -> bool:
        """True se venv e helper esistono su disco (per /tts/models is_downloaded)."""
        return os.path.isfile(self._python) and os.path.isfile(self._helper)

    def load(self) -> None:
        """Avvia il subprocess helper se non e' gia' vivo (best-effort caldo)."""
        with self._lock:
            self._ensure_proc_locked()

    def _ensure_proc_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if not os.path.isfile(self._python):
            raise FileNotFoundError(
                f"Python del venv Chatterbox non trovato: {self._python} "
                "(imposta chatterbox_venv_python)")
        if not os.path.isfile(self._helper):
            raise FileNotFoundError(
                f"Helper Chatterbox non trovato: {self._helper}")
        env = os.environ.copy()
        # L'helper cerca cloned_voices/ sotto KOKORO_MODEL_DIR: stessa cartella
        # che il compat server usa per elencare/salvare le voci clonate.
        env["KOKORO_MODEL_DIR"] = self.model_dir
        env.setdefault("PYTHONUNBUFFERED", "1")
        # Device selezionato (cuda+dedicated -> cuda; altrimenti cpu). L'helper
        # onora CHATTERBOX_DEVICE al posto dell'auto-detect.
        from .device import resolve_device
        env["CHATTERBOX_DEVICE"] = resolve_device(self.device, self.memory)
        popen_kw: dict = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):  # niente console pop-up
            popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        t0 = time.perf_counter()
        log.info("Chatterbox: avvio helper (%s)...", os.path.basename(self._helper))
        self._proc = subprocess.Popen(
            [self._python, "-u", self._helper],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, **popen_kw)
        threading.Thread(target=self._drain_stderr, daemon=True,
                         name="chatterbox-stderr").start()
        log.info("Chatterbox: helper avviato in %.2fs (modello si carica alla "
                 "prima sintesi)", time.perf_counter() - t0)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            try:
                line = raw.decode("utf-8", "replace").rstrip()
            except Exception:  # noqa: BLE001
                continue
            if line:
                log.debug("chatterbox: %s", line)

    def synth(self, text: str, voice: str, lang: str, speed: float):
        """Sintetizza `text` con la voce clonata `voice` -> (float32 mono, sr).

        Firma identica a KokoroTtsEngine.synth. Thread-safe: serializza le
        richieste sulla singola pipe dell'helper. Su errore (count==0 o pipe
        morta) solleva RuntimeError, che il chiamante gestisce come le altre
        sintesi fallite.
        """
        text = (text or "").strip()
        if not text:
            return np.zeros(0, dtype=np.float32), int(self._sr or 24000)
        with self._lock:
            self._ensure_proc_locked()
            proc = self._proc
            assert proc is not None and proc.stdin is not None and proc.stdout is not None
            req = json.dumps({"text": text, "voice": voice or "",
                              "lang": lang or "it", "speed": float(speed)},
                             ensure_ascii=False) + "\n"
            try:
                proc.stdin.write(req.encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._kill_locked()
                raise RuntimeError(f"helper Chatterbox non scrivibile: {e}") from e

            header = _read_exact(proc.stdout, 8)
            if len(header) < 8:
                self._kill_locked()
                raise RuntimeError("helper Chatterbox: EOF (processo morto)")
            count, sr = struct.unpack("<II", header)
            if count == 0:
                # L'helper segnala errore col count 0 (dettagli su stderr/log).
                raise RuntimeError("sintesi Chatterbox fallita (vedi log)")
            data = _read_exact(proc.stdout, count * 4)
            if len(data) < count * 4:
                self._kill_locked()
                raise RuntimeError("helper Chatterbox: audio troncato (EOF)")
            self._sr = int(sr)
            samples = np.frombuffer(data, dtype="<f4").astype(np.float32)
            return samples, int(sr)

    def _kill_locked(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    if proc.stdin:
                        proc.stdin.write(b'{"cmd":"quit"}\n')
                        proc.stdin.flush()
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    self._kill_locked()
            self._proc = None
