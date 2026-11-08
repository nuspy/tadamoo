"""Engine TTS generico pilotato da un helper in un venv separato.

Generalizzazione di `ChatterboxTtsEngine`: XTTS, CosyVoice, Orpheus e Dia
hanno stack torch/CUDA incompatibili fra loro e col processo del bridge, quindi
girano ognuno nel proprio venv come subprocess, parlando lo STESSO protocollo
stdio binario degli helper storici:

  stdin:  {"text","voice","lang","speed"}\\n   |   {"cmd":"quit"}\\n
  stdout: [u32 LE count][u32 LE sample_rate][count * f32 LE]   (count==0 = errore)
  stderr: log

`synth()` ha la firma di `KokoroTtsEngine.synth()`, cosi' il `TtsRouter` smista
in modo trasparente per `/tts/test`, `/tts/stream` e l'avatar.

Streaming (motori con `streaming=True` nel catalogo): la richiesta porta
`"stream": true` e l'helper risponde con N blocchi `[count][sr][pcm]` mentre
genera, chiusi da `[0xFFFFFFFF][0]`; un `[0][0]` a meta' e' un errore.
`synth_stream()` li restituisce uno alla volta.

Il subprocess si avvia pigramente alla prima sintesi e resta caldo: il primo
uso paga il caricamento del modello (decine di secondi per i modelli grossi),
i successivi no.

I pesi si leggono solo da `<models folder>/tts/<id modello>/`: se il modello non
c'e', `ModelNotInstalled` parte PRIMA di avviare l'helper. Nulla si scarica.
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

from .. import models_store
from . import catalog
from .device import resolve_device

log = logging.getLogger("callbridge.engines.venv_tts")

# Il primo `synth` carica il modello: i modelli grossi (Dia 1.6B, Orpheus 3B)
# possono metterci parecchio, quindi l'attesa dell'header e' generosa.
_TIMEOUT_PRIMA_SINTESI = 600.0
_TIMEOUT_SINTESI = 180.0
# Fine dello stream (vedi docstring del modulo).
_FINE_STREAM = 0xFFFFFFFF


def _read_exact(pipe, n: int) -> bytes:
    chunks: list[bytes] = []
    got = 0
    while got < n:
        b = pipe.read(n - got)
        if not b:
            break                      # EOF: subprocess morto
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


class VenvTtsEngine:
    """Helper TTS in subprocess, caldo e condiviso tra le sessioni."""

    def __init__(self, spec: catalog.EngineSpec, cfg) -> None:
        self.spec = spec
        self._cfg = cfg
        self._python = catalog.venv_python(cfg, spec)
        self._helper = catalog.helper_path(spec)
        self.device = getattr(cfg, f"{spec.id}_device", "cuda")
        self.memory = getattr(cfg, f"{spec.id}_gpu_memory", "dedicated")
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()   # una richiesta alla volta sulla pipe
        self._sr: int | None = None
        self._prima = True
        self._env_cfg = self._valori_env(cfg)

    def _valori_env(self, cfg) -> dict[str, str]:
        """Variabili d'ambiente dell'helper che vengono dalla config."""
        return {env: str(getattr(cfg, campo, "") or "")
                for env, campo in (self.spec.cfg_env or {}).items()}

    @property
    def streaming(self) -> bool:
        return bool(self.spec.streaming)

    @property
    def available(self) -> bool:
        return bool(self._python and os.path.isfile(self._python)
                    and os.path.isfile(self._helper))

    def load(self) -> None:
        """Avvia il subprocess (NON carica il modello: quello e' alla 1a sintesi)."""
        with self._lock:
            self._ensure_proc_locked()

    def _ensure_proc_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        models_store.require(catalog.model_id(self._cfg, self.spec))
        if not os.path.isfile(self._python):
            raise FileNotFoundError(
                f"venv {self.spec.name} non trovato: {self._python}")
        if not os.path.isfile(self._helper):
            raise FileNotFoundError(
                f"helper {self.spec.name} non trovato: {self._helper}")
        # Gli helper trovano pesi e voci clonate sotto BASALT_VOICE_MODELS_DIR.
        env = models_store.helper_env("tts")
        env["TTS_VENVS_DIR"] = catalog.venv_root(self._cfg)
        env.setdefault("PYTHONUNBUFFERED", "1")
        # coqui-TTS, all'import, chiama `espeak-ng --version` e ne prende il
        # quarto token: se espeak-ng e' nel PATH ma non trova la sua data dir
        # stampa una riga VUOTA e l'indicizzazione esplode con IndexError —
        # l'import di TTS fallisce e XTTS non carica ("sintesi fallita:
        # IndexError"). La build Windows di espeak segfaulta senza
        # ESPEAK_DATA_PATH, quindi glielo si passa (lo stesso percorso che
        # serve ai visemi). Se in config non c'e', si toglie espeak dal PATH
        # dell'helper: coqui-TTS regge la sua ASSENZA, non la sua rottura.
        dati_espeak = (getattr(self._cfg, "espeak_data_path", "") or "").strip()
        if dati_espeak and os.path.isdir(dati_espeak):
            env["ESPEAK_DATA_PATH"] = dati_espeak
            exe = (getattr(self._cfg, "espeak_ng_exe", "") or "").strip()
            if exe and os.path.isfile(exe):
                env["PATH"] = os.path.dirname(exe) + os.pathsep + env.get("PATH", "")
        else:
            import shutil
            trovato = shutil.which("espeak-ng") or shutil.which("espeak")
            if trovato:
                d = os.path.dirname(os.path.realpath(trovato))
                cand = os.path.join(d, "espeak-ng-data")
                if os.path.isdir(cand):
                    env["ESPEAK_DATA_PATH"] = cand
        env["TTS_DEVICE"] = resolve_device(self.device, self.memory)
        for k, v in (self.spec.extra_env or {}).items():
            env[str(k)] = str(v)
        env.update(self._env_cfg)
        env.update(models_store.OFFLINE_ENV)    # mai un download, qualunque cosa abbiano messo sopra
        popen_kw: dict = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):   # niente console pop-up
            popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        t0 = time.perf_counter()
        log.info("%s: avvio helper (%s)...", self.spec.name,
                 os.path.basename(self._helper))
        self._proc = subprocess.Popen(
            [self._python, "-u", self._helper],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, env=env, **popen_kw)
        self._prima = True
        threading.Thread(target=self._drain_stderr, args=(self._proc,),
                         daemon=True, name=f"{self.spec.id}-stderr").start()
        log.info("%s: helper avviato in %.2fs (il modello si carica alla prima "
                 "sintesi)", self.spec.name, time.perf_counter() - t0)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            try:
                line = raw.decode("utf-8", "replace").rstrip()
            except Exception:  # noqa: BLE001
                continue
            if line:
                log.debug("%s: %s", self.spec.id, line)

    def synth(self, text: str, voice: str, lang: str, speed: float):
        """Sintetizza -> (float32 mono, sample_rate). Thread-safe."""
        text = (text or "").strip()
        if not text:
            return np.zeros(0, dtype=np.float32), int(self._sr or 24000)
        with self._lock:
            self._ensure_proc_locked()
            proc = self._proc
            assert proc is not None and proc.stdin is not None
            assert proc.stdout is not None
            req = json.dumps({"text": text, "voice": voice or "",
                              "lang": lang or "it", "speed": float(speed)},
                             ensure_ascii=False) + "\n"
            try:
                proc.stdin.write(req.encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._kill_locked()
                raise RuntimeError(
                    f"helper {self.spec.name} non scrivibile: {e}") from e

            header = _read_exact(proc.stdout, 8)
            self._prima = False
            if len(header) < 8:
                self._kill_locked()
                raise RuntimeError(
                    f"helper {self.spec.name}: EOF (processo morto, vedi log)")
            count, sr = struct.unpack("<II", header)
            if count == 0:
                raise RuntimeError(
                    f"sintesi {self.spec.name} fallita (vedi log)")
            data = _read_exact(proc.stdout, count * 4)
            if len(data) < count * 4:
                self._kill_locked()
                raise RuntimeError(
                    f"helper {self.spec.name}: audio troncato (EOF)")
            self._sr = int(sr)
            return np.frombuffer(data, dtype="<f4").astype(np.float32), int(sr)

    def synth_stream(self, text: str, voice: str, lang: str, speed: float):
        """Generatore di (float32 mono, sample_rate) mentre l'helper genera.

        Tiene il lucchetto della pipe per tutta la frase: se chi consuma smette
        di iterare (interruzione), i blocchi rimasti vengono comunque letti e
        buttati fino alla fine dello stream, altrimenti la richiesta successiva
        leggerebbe l'audio di questa."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._ensure_proc_locked()
            proc = self._proc
            assert proc is not None and proc.stdin is not None
            assert proc.stdout is not None
            req = json.dumps({"text": text, "voice": voice or "",
                              "lang": lang or "it", "speed": float(speed),
                              "stream": True}, ensure_ascii=False) + "\n"
            try:
                proc.stdin.write(req.encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._kill_locked()
                raise RuntimeError(
                    f"helper {self.spec.name} non scrivibile: {e}") from e
            consegna = True
            while True:
                header = _read_exact(proc.stdout, 8)
                self._prima = False
                if len(header) < 8:
                    self._kill_locked()
                    raise RuntimeError(
                        f"helper {self.spec.name}: EOF (processo morto, vedi log)")
                count, sr = struct.unpack("<II", header)
                if count == _FINE_STREAM:
                    return
                if count == 0:
                    raise RuntimeError(
                        f"sintesi {self.spec.name} fallita (vedi log)")
                data = _read_exact(proc.stdout, count * 4)
                if len(data) < count * 4:
                    self._kill_locked()
                    raise RuntimeError(
                        f"helper {self.spec.name}: audio troncato (EOF)")
                self._sr = int(sr)
                if consegna:
                    try:
                        yield (np.frombuffer(data, dtype="<f4").astype(np.float32),
                               int(sr))
                    except GeneratorExit:
                        # Chi consuma ha smesso: si svuota il resto in
                        # silenzio, la pipe deve restare allineata.
                        consegna = False

    def reconfigure(self, cfg) -> None:
        """Nuovo device/memoria o nuovi parametri dell'helper (es. il modello di
        Qwen3-TTS): il subprocess va ricreato, perche' si passano all'avvio via
        env, quindi si chiude e riparte al prossimo uso."""
        self._cfg = cfg
        self._python = catalog.venv_python(cfg, self.spec)
        nuovo_dev = getattr(cfg, f"{self.spec.id}_device", "cuda")
        nuova_mem = getattr(cfg, f"{self.spec.id}_gpu_memory", "dedicated")
        nuovo_env = self._valori_env(cfg)
        if (nuovo_dev, nuova_mem) != (self.device, self.memory)                 or nuovo_env != self._env_cfg:
            self.device, self.memory = nuovo_dev, nuova_mem
            self._env_cfg = nuovo_env
            self.close()

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
