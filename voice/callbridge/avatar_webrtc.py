"""Video dell'avatar verso l'app, in WebRTC.

Due pezzi:

  - `FrameServer`: accetta la connessione del worker avatar (processo separato,
    venv suo) e ne riceve i fotogrammi. Il worker non conosce aiortc: manda
    pixel su una socket locale e basta, così quella dipendenza resta in un
    ambiente solo. Il formato è I420 (YUV 4:2:0 planare), non BGR: pesa la
    metà sulla socket (1,5 byte per pixel invece di 3) ed è già quello che
    vuole l'encoder, quindi sparisce anche la conversione — che misurata
    costava PIÙ della codifica stessa (2,4 ms contro 1,4 ms per fotogramma).
  - `AvatarTrack` + `apri_sessione`: impacchettano l'ultimo fotogramma in una
    traccia video WebRTC e negoziano la connessione con l'app.

Perché WebRTC e non fotogrammi JPEG sul WebSocket: a 480p e 25 fps il flusso
grezzo sarebbe di alcuni Mbit/s costanti, mentre H.264 sta sotto il megabit e
regge meglio una rete mobile. In cambio serve la negoziazione ICE, che è la
parte che può fallire — vedi `apri_sessione`.

Perché H.264 e non VP8: non per la banda (misurati 430 kbit/s per VP8 contro
882 di x264) ma per il DECODER del telefono. H.264 ha un decoder hardware su
ogni dispositivo Android; VP8 spesso finisce in software, e a quel punto è la
CPU del telefono a singhiozzare, non la rete. `_preferisci_h264` lo impone
qui; l'app fa lo stesso sulla sua offerta, così la scelta non dipende
dall'ordine in cui le due parti elencano i codec.

H.265 sarebbe più efficiente ancora (misurati 170 kbit/s) ma aiortc non ha il
payloader RTP per HEVC — ci sono solo h264, vpx, opus, g711, g722 — e andrebbe
scritto l'RFC 7798 da zero, per giunta senza garanzia che la WebView lo accetti.

Il protocollo con l'app viaggia sul WebSocket che già esiste:
  app->gw  {"type":"video_start","sdp":"<offerta>","persona":"Shan"}
  gw->app  {"type":"video_answer","sdp":"<risposta>"}
  app->gw  {"type":"video_stop"}
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import threading
import time

import numpy as np

log = logging.getLogger("callbridge.avatar_webrtc")

#: Fotogrammi al secondo del WORKER, a titolo informativo: qui NON detta il
#: ritmo. La traccia e' scandita da `next_timestamp()` di aiortc, che pubblica
#: a cadenza sua l'ultimo fotogramma disponibile. Se il worker ne genera meno
#: (per esempio 16 per alleggerire la GPU) la traccia ripubblica lo stesso
#: fotogramma, che per l'encoder costa quasi nulla — quindi i due valori non
#: devono combaciare.
FPS = int(os.environ.get("AVATAR_FPS") or 25)

#: Intestazione dei fotogrammi sulla socket locale: magic + larghezza + altezza.
#: Il magic serve a non interpretare come dimensioni i byte di un worker vecchio
#: rimasto in giro, che manderebbe BGR con un'intestazione di 8 byte: senza, si
#: leggerebbero pixel a caso come w/h e l'errore comparirebbe molto più tardi.
_MAGIC = b"AVI4"
_INTESTAZIONE = struct.Struct("<4sII")


class FrameServer:
    """Riceve i fotogrammi del worker avatar su una socket TCP locale.

    Tiene solo l'ULTIMO fotogramma: in una conversazione dal vivo un
    fotogramma vecchio non serve, e accodarli produrrebbe soltanto ritardo
    crescente quando la rete rallenta.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._ultimo: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._accetta, name="avatar-frames",
                         daemon=True).start()
        log.info("server fotogrammi avatar in ascolto su 127.0.0.1:%d", self.port)

    @property
    def ultimo(self) -> np.ndarray | None:
        with self._lock:
            return self._ultimo

    def _accetta(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            log.info("worker avatar collegato al server fotogrammi")
            threading.Thread(target=self._leggi, args=(conn,),
                             name="avatar-frames-rx", daemon=True).start()

    def _leggi(self, conn: socket.socket) -> None:
        try:
            with conn:
                while not self._stop.is_set():
                    testa = self._esatti(conn, _INTESTAZIONE.size)
                    if testa is None:
                        break
                    magic, w, h = _INTESTAZIONE.unpack(testa)
                    if magic != _MAGIC:
                        log.error("worker con protocollo fotogrammi vecchio "
                                  "(magic %r): aggiornare il worker avatar", magic)
                        break
                    # I420 vuole lati pari: il worker li arrotonda già, ma se
                    # arrivasse un dispari il reshape darebbe un errore oscuro.
                    if not (0 < w <= 4096 and 0 < h <= 4096 and w % 2 == 0
                            and h % 2 == 0):
                        log.warning("dimensioni non valide (%dx%d): chiudo", w, h)
                        break
                    # I420: piano Y intero + U e V a quarto di risoluzione,
                    # cioe' w*h*3/2 byte contro i w*h*3 del BGR.
                    dati = self._esatti(conn, w * h * 3 // 2)
                    if dati is None:
                        break
                    frame = np.frombuffer(dati, dtype=np.uint8).reshape(h * 3 // 2, w)
                    with self._lock:
                        self._ultimo = frame
        except OSError as e:
            log.info("worker avatar scollegato: %s", e)
        finally:
            log.info("connessione fotogrammi chiusa")

    @staticmethod
    def _esatti(conn: socket.socket, n: int) -> bytes | None:
        pezzi, letti = [], 0
        while letti < n:
            b = conn.recv(n - letti)
            if not b:
                return None
            pezzi.append(b)
            letti += len(b)
        return b"".join(pezzi)

    def close(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


def _nero(w: int = 854, h: int = 480) -> np.ndarray:
    """Fotogramma nero in I420.

    Nero non è "tutto zero": in YUV lo zero sui piani U e V è un verde acido.
    Il neutro cromatico sta a 128, quindi Y=0 e U=V=128.
    """
    img = np.empty((h * 3 // 2, w), dtype=np.uint8)
    img[:h] = 0          # luminanza
    img[h:] = 128        # crominanza neutra
    return img


#: Un solo nero riusato: a video spento verrebbe riallocato 25 volte al secondo.
_NERO = _nero()


def make_track(frames: FrameServer):
    """Traccia video che pubblica l'ultimo fotogramma del worker.

    Importa aiortc QUI dentro: è una dipendenza pesante e il bridge deve poter
    partire anche senza, con il solo video disabilitato.
    """
    from aiortc import VideoStreamTrack
    from av import VideoFrame

    class AvatarTrack(VideoStreamTrack):
        kind = "video"

        async def recv(self):
            # `next_timestamp` detta il ritmo: senza, si spedirebbe a raffica.
            pts, time_base = await self.next_timestamp()
            img = frames.ultimo
            if img is None:
                img = _NERO
            # Nessuna conversione: il worker manda già il formato dell'encoder.
            vf = VideoFrame.from_ndarray(img, format="yuv420p")
            vf.pts, vf.time_base = pts, time_base
            return vf

    return AvatarTrack()


#: Frequenza della traccia audio. 48 kHz e' il rate nativo di Opus: qualunque
#: altro valore lo farebbe ricampionare due volte (una da noi verso la traccia,
#: una dall'encoder). I motori TTS danno 22-24 kHz, quindi si ricampiona una
#: volta in ingresso, con swresample (via PyAV), non a mano.
RATE_AUDIO = 48000

#: Durata di un pacchetto audio. 20 ms e' il valore che usano tutti i codec
#: vocali (Opus incluso) e quello che aiortc si aspetta: pacchetti piu' corti
#: moltiplicano l'intestazione RTP, piu' lunghi aggiungono ritardo.
PACCHETTO_S = 0.02


class AudioBuffer:
    """Coda dei campioni della voce, letta dalla traccia a tempo reale.

    La voce dell'avatar nasce a raffica (XTTS sintetizza a ~4 volte il tempo
    reale) e va consumata al ritmo dell'orologio: questa e' la cerniera fra i
    due. Chi scrive non attende, chi legge ottiene sempre esattamente i
    campioni che gli servono — silenzio se la coda e' vuota, perche' una
    traccia WebRTC non puo' interrompersi: smettere di produrre pacchetti
    significa, per il ricevente, una connessione che si e' guastata.
    """

    def __init__(self, limite_s: float = 30.0) -> None:
        self._coda = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._limite = int(limite_s * RATE_AUDIO)
        self._ricampionatori: dict = {}

    def _ricampiona(self, x: np.ndarray, rate: int) -> np.ndarray:
        """Porta `x` a RATE_AUDIO. Lo stato del ricampionatore si conserva:
        ricrearlo a ogni segmento lascerebbe un microscopico buco fra l'uno e
        l'altro (la coda del filtro), udibile come un clic su ogni frase."""
        if rate == RATE_AUDIO:
            return x
        from av import AudioFrame, AudioResampler
        r = self._ricampionatori.get(rate)
        if r is None:
            r = AudioResampler(format="flt", layout="mono", rate=RATE_AUDIO)
            self._ricampionatori[rate] = r
        f = AudioFrame.from_ndarray(np.ascontiguousarray(
            x.reshape(1, -1), dtype=np.float32), format="flt", layout="mono")
        f.sample_rate = rate
        fuori = r.resample(f)
        if not isinstance(fuori, list):          # PyAV vecchie: un frame solo
            fuori = [fuori] if fuori is not None else []
        pezzi = [g.to_ndarray().reshape(-1) for g in fuori]
        return (np.concatenate(pezzi).astype(np.float32) if pezzi
                else np.zeros(0, dtype=np.float32))

    def scrivi(self, samples, rate: int) -> None:
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return
        with self._lock:
            x = self._ricampiona(x, int(rate))
            self._coda = np.concatenate((self._coda, x))
            # Tetto di sicurezza: se per un guasto nessuno leggesse (traccia
            # chiusa, app scomparsa) la coda crescerebbe senza fine.
            if self._coda.size > self._limite:
                self._coda = self._coda[-self._limite:]
                log.warning("audio: coda oltre %.0fs, scartata la parte vecchia",
                            self._limite / RATE_AUDIO)

    def leggi(self, n: int) -> np.ndarray:
        with self._lock:
            if self._coda.size >= n:
                fuori, self._coda = self._coda[:n], self._coda[n:]
                return fuori
            fuori = np.zeros(n, dtype=np.float32)
            fuori[:self._coda.size] = self._coda
            self._coda = np.zeros(0, dtype=np.float32)
            return fuori

    def svuota(self) -> None:
        """Butta la voce non ancora uscita. Serve all'interruzione: senza,
        l'avatar continuerebbe a finire la frase per tutti i secondi che ha
        gia' in coda, dopo che l'utente ha ripreso la parola."""
        with self._lock:
            self._coda = np.zeros(0, dtype=np.float32)

    @property
    def in_coda_s(self) -> float:
        with self._lock:
            return self._coda.size / RATE_AUDIO


def make_audio_track(buffer: AudioBuffer):
    """Traccia audio che pubblica la voce del buffer, 20 ms per volta.

    Il ritmo lo detta l'orologio, come fa `AudioStreamTrack` di aiortc: si
    dorme fino all'istante del pacchetto successivo. Senza, si spedirebbe a
    raffica e il ricevente scarterebbe tutto per jitter.
    """
    import fractions

    from aiortc import MediaStreamTrack
    from av import AudioFrame

    class AvatarAudioTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self._pts = 0
            self._t0: float | None = None

        async def recv(self):
            n = int(RATE_AUDIO * PACCHETTO_S)
            if self._t0 is None:
                self._t0 = time.time()
            else:
                attesa = self._t0 + (self._pts + n) / RATE_AUDIO - time.time()
                if attesa > 0:
                    await asyncio.sleep(attesa)
            x = np.clip(buffer.leggi(n), -1.0, 1.0)
            pcm = (x * 32767.0).astype(np.int16)
            # STEREO con i due canali identici, non mono. L'encoder Opus di
            # aiortc lavora in stereo e ricampionerebbe da se': misurato, quel
            # passaggio applica il guadagno di conservazione della potenza
            # (1/radice di 2) e la voce arriva 3 dB piu' piano — rms 0,148
            # invece di 0,21 su un seno di ampiezza 0,3. Dandogli lo stereo
            # gia' fatto non converte nulla e il livello resta quello giusto.
            # Non costa banda: due canali uguali hanno il "side" a zero, e
            # Opus lo codifica con pochissimi bit.
            pcm = np.repeat(pcm.reshape(-1, 1), 2, axis=1).reshape(1, -1)
            frame = AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
            frame.sample_rate = RATE_AUDIO
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, RATE_AUDIO)
            self._pts += n
            return frame

    return AvatarAudioTrack()


class SessioneVideo:
    """Una connessione WebRTC verso l'app."""

    def __init__(self, frames: FrameServer, audio: bool = True) -> None:
        self.frames = frames
        # Buffer della voce. Esiste sempre; diventa la strada dell'audio solo
        # se l'app lo chiede nella sua offerta (vedi `_negozia`). Chi scrive
        # (il gateway) guarda `audio_attivo` per sapere se mandare la voce qui
        # o, come prima, in PCM sul WebSocket.
        self.audio = AudioBuffer() if audio else None
        self.audio_attivo = False
        self.pc = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def apri(self, offerta_sdp: str, tipo: str = "offer") -> str:
        """Negozia la connessione e restituisce l'SDP di risposta.

        Gira su un event loop suo, in un thread dedicato: il gateway è
        sincrono (websockets in modalità bloccante) e non ha un loop asyncio
        da condividere.
        """
        pronto = threading.Event()
        risposta: dict = {}
        errore: dict = {}

        def _run() -> None:
            asyncio.set_event_loop(asyncio.new_event_loop())
            self._loop = asyncio.get_event_loop()
            try:
                risposta["sdp"] = self._loop.run_until_complete(
                    self._negozia(offerta_sdp, tipo))
            except Exception as e:  # noqa: BLE001 - lo riferiamo all'app
                errore["e"] = e
            finally:
                pronto.set()
                if not errore:
                    self._loop.run_forever()   # tiene viva la connessione

        self._thread = threading.Thread(target=_run, name="avatar-webrtc",
                                        daemon=True)
        self._thread.start()
        if not pronto.wait(timeout=30):
            raise TimeoutError("negoziazione WebRTC non conclusa")
        if errore:
            raise errore["e"]
        return risposta["sdp"]

    @staticmethod
    def _preferisci_h264(pc) -> None:
        """Mette H.264 in cima ai codec video, o non tocca niente.

        Non è un'ottimizzazione di banda: è per il decoder hardware del
        telefono (vedi la nota in cima al modulo). Se qualcosa non torna si
        lascia decidere la negoziazione come prima — meglio un video in VP8
        che nessun video.
        """
        try:
            from aiortc import RTCRtpSender
            tutti = RTCRtpSender.getCapabilities("video").codecs
            h264 = [c for c in tutti if c.mimeType == "video/H264"]
            if not h264:
                log.warning("nessun codec H.264 disponibile: lascio la scelta "
                            "alla negoziazione")
                return
            # rtx serve alla ritrasmissione dei pacchetti persi: toglierlo
            # renderebbe il video peggiore proprio sulla rete mobile.
            rtx = [c for c in tutti if c.mimeType == "video/rtx"]
            for tr in pc.getTransceivers():
                if tr.kind == "video":
                    tr.setCodecPreferences(h264 + rtx)
            log.info("video: preferenza H.264 (%d profili)", len(h264))
        except Exception as e:  # noqa: BLE001 - preferenza best-effort
            log.warning("preferenza H.264 non applicata: %s", e)

    @staticmethod
    def _preferisci_opus(pc) -> None:
        """Mette Opus in cima ai codec audio, o non tocca niente.

        aiortc offrirebbe anche PCMU/PCMA (G.711): 64 kbit/s per una banda di
        3,4 kHz, cioe' una voce da telefono anni Ottanta. Opus fa 24-32 kbit/s
        a banda piena, e sul telefono e' decodificato in hardware o quasi.
        """
        try:
            from aiortc import RTCRtpSender
            tutti = RTCRtpSender.getCapabilities("audio").codecs
            opus = [c for c in tutti if c.mimeType == "audio/opus"]
            if not opus:
                log.warning("nessun codec Opus: lascio scegliere la negoziazione")
                return
            for tr in pc.getTransceivers():
                if tr.kind == "audio":
                    tr.setCodecPreferences(opus)
            log.info("audio: preferenza Opus")
        except Exception as e:  # noqa: BLE001 - preferenza best-effort
            log.warning("preferenza Opus non applicata: %s", e)

    async def _negozia(self, offerta_sdp: str, tipo: str) -> str:
        from aiortc import (RTCConfiguration, RTCPeerConnection,
                            RTCSessionDescription)
        # Nessun server STUN. Senza configurazione aiortc usa quello di Google,
        # che qui non serve a niente — l'app e il PC si parlano su rete locale o
        # dentro la tailnet, dove bastano i candidati host — e che su una
        # macchina senza uscita verso internet puo' bruciare tutti e cinque i
        # secondi dell'attesa di raccolta ICE prima di arrendersi. L'app fa gia'
        # lo stesso dalla sua parte (`iceServers: []` in controller.ts).
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        # Anche il video si aggiunge solo se l'offerta lo prevede, per la
        # stessa ragione dell'audio (una risposta SDP non inventa flussi). Il
        # caso che lo rende necessario e' l'offerta di SOLO AUDIO: serve alla
        # conversazione continua senza video, perche' la cancellazione d'eco
        # del telefono e del browser vede unicamente l'audio che passa da una
        # traccia WebRTC — quella riprodotta da un AudioContext non la tocca.
        # Senza questa condizione un'offerta senza `m=video` lasciava un
        # transceiver senza controparte e la negoziazione cadeva.
        self.video_attivo = "m=video" in offerta_sdp or "m=" not in offerta_sdp
        if self.video_attivo:
            self.pc.addTrack(make_track(self.frames))
        # La traccia audio si aggiunge SOLO se l'offerta la prevede. Una
        # risposta SDP non puo' inventare flussi che l'offerta non contiene:
        # con un'app vecchia (che offre solo video) aggiungerla qui produrrebbe
        # un transceiver senza controparte, e la negoziazione fallirebbe —
        # cioe' si perderebbe anche il video, che prima funzionava. Cosi'
        # invece la compatibilita' e' automatica: chi non chiede audio continua
        # a riceverlo in PCM sul WebSocket.
        if self.audio is not None and "m=audio" in offerta_sdp:
            self.pc.addTrack(make_audio_track(self.audio))
            self.audio_attivo = True
        if self.video_attivo:
            self._preferisci_h264(self.pc)
        if self.audio_attivo:
            self._preferisci_opus(self.pc)

        @self.pc.on("connectionstatechange")
        async def _stato() -> None:  # noqa: ANN202 - callback aiortc
            log.info("WebRTC: stato %s", self.pc.connectionState)

        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=offerta_sdp, type=tipo))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        # Si aspetta che la raccolta ICE finisca: l'SDP va mandato completo,
        # perché il canale di segnalazione non trasporta candidati sciolti.
        atteso = time.monotonic() + 5.0
        while self.pc.iceGatheringState != "complete" and time.monotonic() < atteso:
            await asyncio.sleep(0.1)
        return self.pc.localDescription.sdp

    def scrivi_audio(self, samples, rate: int) -> bool:
        """Mette la voce nella traccia. False se l'audio non passa da qui
        (app che non l'ha offerto, o funzione disattivata): il chiamante deve
        allora mandarla in PCM sul WebSocket, come prima."""
        if not self.audio_attivo or self.audio is None:
            return False
        self.audio.scrivi(samples, rate)
        return True

    def svuota_audio(self) -> None:
        if self.audio is not None:
            self.audio.svuota()

    def chiudi(self) -> None:
        self.audio_attivo = False
        if self.audio is not None:
            self.audio.svuota()
        if self.pc is None or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.pc.close(), self._loop).result(5)
        except Exception as e:  # noqa: BLE001 - chiusura best-effort
            log.debug("chiusura WebRTC: %s", e)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self.pc = None
