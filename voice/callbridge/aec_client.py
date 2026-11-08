"""Il lato bridge della cancellazione d'eco: avvia l'helper e gli parla.

Regola di questo modulo: **non deve poter rompere l'audio**. Se il venv non
c'è, se l'helper non parte, se muore a metà conversazione o se tarda, il
microfono passa così com'è e si torna al comportamento di prima — che è
imperfetto ma funziona. Un cancellatore d'eco che fa cadere il microfono è
peggio dell'eco.

Il riferimento è il PCM che *sappiamo* di aver suonato: il gateway lo ha già
in mano, perché è lui a consegnarlo. L'alternativa (il loopback WASAPI) darebbe
il segnale davvero uscito dalla scheda, ma introdurrebbe un secondo orologio da
inseguire; qui basta misurare una volta il ritardo fra uscita e rientro.
"""
from __future__ import annotations

import logging
import os
import struct
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("callbridge.aec")

RATE = 16000
BLOCCO = RATE // 100          # 10 ms, l'unità di AEC3


def _python_helper() -> Path | None:
    """L'interprete del venv dedicato, se esiste."""
    base = Path(__file__).resolve().parents[2]
    p = base / "aec-venv" / "Scripts" / "python.exe"
    if p.is_file():
        return p
    p = base / "aec-venv" / "bin" / "python"
    return p if p.is_file() else None


class CancellatoreEco:
    """Toglie dal microfono l'eco di ciò che è appena uscito dalle casse."""

    def __init__(self, ritardo_ms: int = 60) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._ritardo_ms = int(ritardo_ms)
        self.attivo = False

    # --- ciclo di vita ---------------------------------------------------
    def avvia(self) -> bool:
        exe = _python_helper()
        if exe is None:
            log.info("AEC: nessun aec-venv, il microfono passa com'è")
            return False
        script = Path(__file__).resolve().parent / "engines" / "aec_helper.py"
        try:
            self._proc = subprocess.Popen(
                [str(exe), str(script)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if os.name == "nt" else 0))
        except Exception as e:  # noqa: BLE001
            log.warning("AEC: helper non avviato: %s", e)
            return False
        threading.Thread(target=self._leggi_errori, name="aec-err",
                         daemon=True).start()
        self.attivo = True
        self.imposta_ritardo(self._ritardo_ms)
        log.info("AEC: cancellatore attivo (ritardo %d ms)", self._ritardo_ms)
        return True

    def ferma(self) -> None:
        self.attivo = False
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.stdin.write(b"q" + struct.pack("<I", 0))
            p.stdin.flush()
            p.wait(timeout=2)
        except Exception:  # noqa: BLE001 - in chiusura non si insiste
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

    def _leggi_errori(self) -> None:
        p = self._proc
        if p is None or p.stderr is None:
            return
        for riga in p.stderr:
            testo = riga.decode("utf-8", "replace").strip()
            if testo:
                log.info("AEC helper: %s", testo)

    # --- uso ------------------------------------------------------------
    def _scrivi(self, tipo: bytes, payload: bytes) -> None:
        """Manda un messaggio senza risposta. Se fallisce, si spegne e basta."""
        if not self.attivo:
            return
        with self._lock:
            p = self._proc
            if p is None or p.stdin is None:
                return
            try:
                p.stdin.write(tipo + struct.pack("<I", len(payload)) + payload)
                p.stdin.flush()
            except Exception as e:  # noqa: BLE001
                log.warning("AEC: helper caduto (%s): il microfono passa com'è", e)
                self.attivo = False

    def imposta_ritardo(self, ms: int) -> None:
        self._ritardo_ms = int(ms)
        self._scrivi(b"d", struct.pack("<i", int(ms)))

    def riferimento(self, pcm: np.ndarray, rate: int = RATE) -> None:
        """Dichiara ciò che sta uscendo dalle casse."""
        if not self.attivo:
            return
        self._scrivi(b"r", _a_int16(pcm, rate))

    def pulisci(self, pcm16: bytes) -> bytes:
        """Ritorna il microfono senza l'eco. In caso di guaio, lo ritorna uguale."""
        if not self.attivo or not pcm16:
            return pcm16
        # Solo blocchi interi da 10 ms: il resto tornerebbe indietro monco.
        utili = (len(pcm16) // (BLOCCO * 2)) * (BLOCCO * 2)
        if utili <= 0:
            return pcm16
        with self._lock:
            p = self._proc
            if p is None or p.stdin is None or p.stdout is None:
                return pcm16
            try:
                p.stdin.write(b"m" + struct.pack("<I", utili) + pcm16[:utili])
                p.stdin.flush()
                testa = p.stdout.read(5)
                if len(testa) < 5:
                    raise OSError("helper muto")
                n = struct.unpack("<I", testa[1:5])[0]
                pulito = p.stdout.read(n) if n else b""
            except Exception as e:  # noqa: BLE001
                log.warning("AEC: helper caduto (%s): il microfono passa com'è", e)
                self.attivo = False
                return pcm16
        return pulito + pcm16[utili:]

    def pulisci_float(self, pcm: np.ndarray, rate: int) -> np.ndarray:
        """Come `pulisci`, ma nel formato in cui gira l'audio del PC.

        Il cancellatore lavora a 16 kHz in int16 perché è l'unità di AEC3; il
        resto del bridge usa float32 alla frequenza del dispositivo. La
        conversione sta qui e non nel chiamante, così il chiamante può
        ignorare l'esistenza del cancellatore quando non c'è.
        """
        if not self.attivo:
            return pcm
        x = np.asarray(pcm, dtype=np.float32).reshape(-1)
        pulito = self.pulisci(_a_int16(x, rate))
        if not self.attivo:
            # Il cancellatore e' appena caduto: si restituisce l'originale,
            # non il suo giro per int16. Degradare non deve voler dire
            # consegnare un segnale leggermente peggiore per sempre.
            return pcm
        y = np.frombuffer(pulito, dtype="<i2").astype(np.float32) / 32768.0
        if rate != RATE and y.size > 1:
            y = np.interp(np.linspace(0, y.size - 1, x.size),
                          np.arange(y.size), y).astype(np.float32)
        if y.size < x.size:          # coda non multipla di 10 ms: si ricuce
            y = np.concatenate([y, x[y.size:]])
        return y[:x.size]


def _a_int16(pcm: np.ndarray, rate: int) -> bytes:
    """float32 a qualunque frequenza -> int16 mono a 16 kHz."""
    x = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if rate != RATE and len(x) > 1:
        n = max(1, int(len(x) * RATE / rate))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def misura_ritardo(riferimento: np.ndarray, registrato: np.ndarray,
                   rate: int = RATE, massimo_ms: int = 400) -> int:
    """Quanti millisecondi passano fra ciò che esce e il suo rientro.

    Si misura con la cross-correlazione fra il segnale suonato e quello
    ripreso. Serve a far convergere il cancellatore in fretta: senza, AEC3
    cerca l'allineamento da sé, e nel frattempo l'eco passa.
    """
    a = np.asarray(riferimento, dtype=np.float32).reshape(-1)
    b = np.asarray(registrato, dtype=np.float32).reshape(-1)
    if a.size < rate // 10 or b.size < rate // 10:
        return 0
    n = min(a.size, b.size, rate * 3)
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    massimo = int(massimo_ms * rate / 1000)
    corr = np.correlate(b, a[:max(1, n - massimo)], mode="valid")
    if corr.size == 0:
        return 0
    return int(np.argmax(corr) * 1000 / rate)
