"""I/O audio sull'endpoint Bluetooth HFP del telefono (host Windows).

- downlink (input): la voce dell'interlocutore -> verso lo STT
- uplink (output): la voce sintetizzata dal TTS -> verso la chiamata

Il link SCO si attiva quando si aprono gli stream: e' normale che il device
sia silenzioso finche' non c'e' una chiamata attiva instradata al PC.
"""

from __future__ import annotations

import logging
import queue
import threading

import numpy as np
import sounddevice as sd
import soxr

from . import ks_native

log = logging.getLogger("callbridge.audio")

# Pattern specifici del telefono target: hanno priorita' nell'autodetect.
# Solo modelli di TELEFONO (non "andrea", che e' anche negli auricolari Buds):
# l'endpoint HFP che serve e' quello del telefono in chiamata, non delle cuffie.
PHONE_NAME_HINTS = ("s25", "s21", "ultra", "galaxy")
# Pattern HFP generici (fallback se il telefono target non e' identificato).
HFP_NAME_HINTS = ("hands-free", "headset", "hands free")
# L'A2DP non porta il microfono: inutilizzabile per le chiamate.
EXCLUDE_HINTS = ("a2dp", "stereo")


def find_hfp_devices(bt_name: str = "") -> tuple[str, str]:
    """Ritorna (capture_path, render_path) dei filtri Kernel Streaming HFP.

    Gli endpoint HFP del telefono su Windows esistono SOLO come device Kernel
    Streaming (driver ``bthhfenum``), NON come endpoint Core Audio/sounddevice
    (PortAudio non li apre: -9996/-9999). Si passano quindi i PATH KS, che
    `HfpAudioIO` riconosce (str -> backend nativo). Solleva RuntimeError se non
    trova la coppia.

    `bt_name` e' il nome Bluetooth dichiarato dal telefono (es. "Andrea's S21
    Ultra"): Windows chiama i suoi endpoint "<nome BT> Hands-Free HF Audio",
    quindi passarlo individua il telefono giusto anche se ne sono appaiati
    piu' d'uno, e senza una lista di modelli cablata nel codice. Senza nome si
    ripiega sull'euristica per modelli noti."""
    return ks_native.find_hfp_paths(bt_name or None)


def negotiate_sample_rate(in_idx: int, out_idx: int, preferred: int) -> int:
    """Ritorna il primo sample rate supportato tra `preferred` e 8 kHz.

    Verifica ANCHE il fallback a 8000 Hz: se non e' supportato non si ripiega
    "alla cieca" (l'errore emergeva dopo, all'apertura degli stream, con un
    messaggio meno chiaro) ma si solleva un RuntimeError esplicito.
    """
    # Device KS (path str): leggi dal pin il sample rate REALE dell'HFP del
    # telefono connesso (wideband 16 kHz o narrowband 8 kHz), cosi' il prodotto
    # si adatta da solo al modello.
    if isinstance(in_idx, str) or isinstance(out_idx, str):
        try:
            _, cap_rates = ks_native.probe_pin(in_idx, 2)
            _, ren_rates = ks_native.probe_pin(out_idx, 1)
            common = set(cap_rates) & set(ren_rates)
            if preferred in common:
                return preferred
            if common:
                return max(common)
        except Exception as e:  # noqa: BLE001
            log.warning("probe del sample rate HFP fallito (%s): uso %d Hz", e, preferred)
        return preferred
    last_err: Exception | None = None
    for rate in (preferred, 8000):
        try:
            sd.check_input_settings(device=in_idx, samplerate=rate, channels=1)
            sd.check_output_settings(device=out_idx, samplerate=rate, channels=1)
            if rate != preferred:
                log.warning("%d Hz non supportato su HFP, uso %d Hz (narrowband)",
                            preferred, rate)
            return rate
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(
        f"Nessun sample rate utilizzabile sui device HFP "
        f"(provati {preferred} e 8000 Hz): {last_err}")


def resample(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Ricampiona audio mono float32 da `from_rate` a `to_rate` (soxr, alta qualita')."""
    if from_rate == to_rate:
        return samples
    return soxr.resample(samples, from_rate, to_rate).astype(np.float32)


class HfpAudioIO:
    """Apre gli stream HFP e fornisce read (downlink) / write (uplink) a blocchi.

    Uso:
        with HfpAudioIO(in_idx, out_idx, rate, block_ms) as io:
            chunk = io.read()          # np.ndarray float32 mono
            io.write(tts_chunk)        # np.ndarray float32 mono
    """

    def __init__(self, in_idx: int, out_idx: int, sample_rate: int, block_ms: int,
                 aec=None) -> None:
        self.in_idx = in_idx
        self.out_idx = out_idx
        self.sample_rate = sample_rate
        self._block_ms = block_ms
        self.block_frames = int(sample_rate * block_ms / 1000)
        #: Cancellatore d'eco, se acceso. Questo e' il punto naturale dove
        #: metterlo: qui passano sia cio' che esce (il riferimento) sia cio'
        #: che entra, e sono gli unici due segnali che servono. None = il
        #: microfono passa com'e', che e' il comportamento storico.
        self._aec = aec
        self._istream: sd.InputStream | None = None
        self._ostream: sd.OutputStream | None = None
        # CALLBACK mode, non blocking. Gli endpoint Bluetooth HFP del telefono
        # su Windows esistono solo sotto l'host API WDM-KS, che PortAudio NON
        # apre in modalità blocking ("Blocking API not supported yet",
        # PaErrorCode -9999). In callback sì: gli stream sono guidati da
        # PortAudio e read()/write() passano da code, senza cambiare l'interfaccia
        # per l'orchestrator.
        self._in_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=64)
        self._out_buf = np.zeros(0, dtype=np.float32)  # uplink sounddevice (float)
        self._out_bytes = bytearray()  # uplink KS: PCM int16 gia' pronto (no numpy nel TX)
        self._out_lock = threading.Lock()
        # Backend: se i device sono PATH (str) è l'HFP del telefono, che su
        # Windows si apre solo via Kernel Streaming nativo (sounddevice/PortAudio
        # non lo vede). Se sono indici (int) è un device locale via sounddevice.
        self._ks = isinstance(in_idx, str) or isinstance(out_idx, str)
        self._cap: "ks_native.KsStream | None" = None
        self._ren: "ks_native.KsStream | None" = None
        self._tx_run = False
        self._tx_thread: threading.Thread | None = None
        #: diagnostica uplink: blocchi scritti e underrun (silenzio per buffer vuoto)
        self._tx_writes = 0
        self._tx_underruns = 0

    def _cb_in(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        try:
            self._in_q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # il consumatore è indietro: si scarta questo blocco

    def _cb_out(self, outdata, frames, time_info, status) -> None:  # noqa: ARG002
        with self._out_lock:
            n = min(frames, len(self._out_buf))
            if n:
                outdata[:n, 0] = self._out_buf[:n]
                self._out_buf = self._out_buf[n:]
            if n < frames:
                outdata[n:, 0] = 0.0  # underrun: silenzio, meglio di un gracchio

    def _enter_ks(self) -> "HfpAudioIO":
        # Rileva AUTOMATICAMENTE i pin di streaming e il sample rate del telefono
        # connesso: il prodotto gira su molti modelli (wideband 16 kHz o
        # narrowband 8 kHz, con pin id diversi). KsStream tiene piu' pacchetti in
        # volo (ritmo real-time); il RUN ritenta finche' il canale SCO e' pronto.
        cap_pin, cap_rates = ks_native.probe_pin(self.in_idx, 2)   # OUT = downlink
        ren_pin, ren_rates = ks_native.probe_pin(self.out_idx, 1)  # IN  = uplink
        common = set(cap_rates) & set(ren_rates)
        if self.sample_rate not in common:
            rate = (self.sample_rate if self.sample_rate in cap_rates
                    else (max(common) if common else cap_rates[0]))
            log.info("HFP: adatto il sample rate a %d Hz (cap=%s ren=%s)",
                     rate, cap_rates, ren_rates)
            self.sample_rate = rate
            self.block_frames = int(rate * self._block_ms / 1000)
        self._cap = ks_native.KsStream(self.in_idx, cap_pin, self.sample_rate, 1,
                                       self.block_frames, is_capture=True, npackets=16)
        self._ren = ks_native.KsStream(self.out_idx, ren_pin, self.sample_rate, 1,
                                       self.block_frames, is_capture=False, npackets=16)
        self._ren.prime_render()  # silenzio iniziale: avvia il flusso di uplink
        self._tx_run = True
        self._tx_thread = threading.Thread(target=self._tx_loop, name="hfp-ks-tx",
                                           daemon=True)
        self._tx_thread.start()
        log.info("Stream HFP aperti via Kernel Streaming (rate=%d, block=%d frame)",
                 self.sample_rate, self.block_frames)
        return self

    def _tx_loop(self) -> None:
        """Alimenta l'uplink a ritmo costante. Lavora su PCM int16 gia' pronto
        (`_out_bytes`): niente numpy qui dentro, cosi' l'overhead per blocco resta
        minimo e la scrittura tiene la velocita' piena del device (altrimenti il
        device va in underrun e la voce si spezza). `write_block` e' bloccante sul
        multi-buffer e CADENZA il loop al real-time."""
        nb = self.block_frames * 2  # mono, 16 bit
        silence = bytes(nb)
        while self._tx_run:
            with self._out_lock:
                if len(self._out_bytes) >= nb:
                    chunk = bytes(self._out_bytes[:nb])
                    del self._out_bytes[:nb]
                else:
                    chunk = None
            if chunk is None:
                chunk = silence
                self._tx_underruns += 1
            if self._ren is not None:
                self._ren.write_block(chunk)
                self._tx_writes += 1

    def __enter__(self) -> "HfpAudioIO":
        if self._ks:
            return self._enter_ks()
        self._istream = sd.InputStream(
            device=self.in_idx, samplerate=self.sample_rate,
            channels=1, dtype="float32", blocksize=self.block_frames,
            callback=self._cb_in,
        )
        self._ostream = sd.OutputStream(
            device=self.out_idx, samplerate=self.sample_rate,
            channels=1, dtype="float32", blocksize=self.block_frames,
            callback=self._cb_out,
        )
        self._istream.start()
        self._ostream.start()
        log.info("Stream HFP aperti in callback mode (rate=%d, block=%d frame)",
                 self.sample_rate, self.block_frames)
        return self

    def __exit__(self, *exc) -> None:
        if self._ks:
            self._tx_run = False
            if self._tx_thread is not None:
                self._tx_thread.join(timeout=1.0)
            for s in (self._cap, self._ren):
                if s is not None:
                    try:
                        s.close()
                    except Exception:  # noqa: BLE001 - cleanup best-effort
                        pass
            self._cap = self._ren = None
            log.info("Stream HFP (KS) chiusi")
            return
        for stream in (self._istream, self._ostream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001 - cleanup best-effort
                    pass
        self._istream = self._ostream = None
        log.info("Stream HFP chiusi")

    def read(self) -> np.ndarray:
        """Legge un blocco di downlink (dal callback). float32 mono (block_frames,).

        Timeout: se il device non consegna (SCO non ancora attivo, o silenzio),
        ritorna un blocco di zeri invece di bloccare il ciclo per sempre."""
        if self._ks:
            raw = self._cap.read_block(timeout_ms=1000) if self._cap else None
            if not raw:
                x = np.zeros(self.block_frames, dtype=np.float32)
            else:
                x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if len(x) < self.block_frames:
                    x = np.concatenate(
                        [x, np.zeros(self.block_frames - len(x), dtype=np.float32)])
                elif len(x) > self.block_frames:
                    x = x[:self.block_frames]
            if self._aec is not None:
                x = self._aec.pulisci_float(x, self.sample_rate)
            return x
        try:
            x = self._in_q.get(timeout=1.0)
        except queue.Empty:
            x = np.zeros(self.block_frames, dtype=np.float32)
        if self._aec is not None:
            x = self._aec.pulisci_float(x, self.sample_rate)
        return x

    def write(self, samples: np.ndarray) -> None:
        """Accoda un blocco di uplink (lo consuma il callback). float32 mono."""
        if self._aec is not None:
            # Il riferimento si dichiara PRIMA di suonare: il cancellatore
            # deve sapere cosa sta per uscire quando quel suono rientrera'.
            self._aec.riferimento(samples, self.sample_rate)
        if self._ks:
            # Converti QUI (nel thread produttore) in PCM int16: il thread TX
            # deve restare senza numpy per tenere il ritmo del device.
            pcm = (np.clip(samples.ravel(), -1.0, 1.0) * 32767).astype(np.int16)
            with self._out_lock:
                self._out_bytes += pcm.tobytes()
            return
        with self._out_lock:
            self._out_buf = np.concatenate(
                [self._out_buf, samples.astype(np.float32).ravel()])
