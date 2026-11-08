"""Voice gateway per l'app Android (Task #20).

WebSocket server che fa da ponte tra l'app remota (raggiungibile via Tailscale
o qualsiasi rete) e Handy/Hermes. Replica il comportamento di Shift+F1 ma con
l'audio che arriva/parte dall'app invece che dal mic/casse del PC.

UN TURNO PER VOLTA (sia "hold" sia "continua"):
  app -> audio (frame PCM16) -> [STT Handy] -> testo utente
       -> [Hermes] -> testo risposta (streaming)
       -> [TTS Handy] -> audio risposta (streaming)
  -> rimanda all'app: transcript (tuo testo), reply (testo a pezzi), audio.

Protocollo (JSON testo + frame binari):
  app->gw  {"type":"auth","token":"..."}
  app->gw  {"type":"start","mode":"hold"|"continuous","lang":"it",
            "voice":"im_nicola","sample_rate":16000}
  app->gw  <frame binari int16 LE mono @ sample_rate>   (audio del turno)
  app->gw  {"type":"end"}                # SOLO modalita' hold (pulsante rilasciato)
                                          # in "continuous" il turno si chiude
                                          # da solo sulla pausa (VAD lato Handy)
  gw->app  {"type":"transcript","text":"..."}      # cosa hai detto
  gw->app  {"type":"reply","chunk":"..."}          # risposta di Hermes, a pezzi
  gw->app  {"type":"audio_start","sample_rate":24000}
  gw->app  <frame binari int16 LE mono>            # audio della risposta
  gw->app  {"type":"audio_end"}
  app->gw  {"type":"file","path":"..."}             # allegato (gia' su /upload)
  gw->app  {"type":"file_ok","name":"..."}          # allegato preso in carico
  app->gw  {"type":"brain","brain":"chat"|"hermes"} # cambia CHI risponde, a caldo
  gw->app  {"type":"brain","brain":"..."}           # conferma di chi risponde
  gw->app  {"type":"done"}                          # turno finito (pronto al prossimo)
  gw->app  {"type":"error","message":"..."}
"""
from __future__ import annotations

import json
import logging
import os
import queue
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
import requests

from .brains import (make_full_brain, make_lmstudio_brain,
                     respond_stream)
from .config import Config
from .handy_client import ensure_models_loaded
from .stt_client import STT_RATE, HandySttClient
from . import chunk_policy
from .tts_client import HandyTtsClient
from .turn_engine import ReplyProcessor, configure_tts
from .interruption_policy import Esito, classifica
from .util import coalesce_utterances, drain_queue

log = logging.getLogger("callbridge.gateway")


def _pcm16_to_f32(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _f32_to_pcm16(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _dump_attivo() -> bool:
    """Vero se va salvato su disco l'audio dei turni (diagnostica)."""
    if os.environ.get("CB_DUMP_TURNS"):
        return True
    return (Path(__file__).resolve().parents[1] / "dump-turni.on").exists()


def _normalize_audio(samples: np.ndarray) -> np.ndarray:
    """Toglie il DC offset e attenua SOLO se il segnale clippa.

    Mai amplificare: portare un blocco quasi muto a fondo scala trasforma il
    rumore di fondo in "voce" per lo STT (allucinazioni nella trascrizione),
    e un guadagno diverso per blocco da 100 ms introduce discontinuita'.
    """
    if samples.size == 0:
        return samples
    samples = samples - np.mean(samples)
    peak = float(np.max(np.abs(samples)))
    if peak > 1.0:
        samples = samples / peak
    return samples


# L'estrazione dei tag media [[media:image|video:SRC]] e la sanitizzazione per
# il TTS vivono ora in turn_engine.ReplyProcessor (condiviso con le altre pipeline).


# Sotto questa soglia una trascrizione non e' un'interruzione ma rumore:
# respiro, eco dell'avatar, ronzio. Sette caratteri lasciano passare un
# "basta", "aspetta", "no no" — cioe' le interruzioni vere — e fermano
# "ok", "mm", "eh" e le frasi-fantasma dei silenzi.
_MIN_BARGEIN_CHARS = 7

#: Quanto testo si accumula prima di mandarlo in sintesi, col motore acceso.
#: La soglia adattiva di `chunk_policy` esiste per non intasare il labiale, ma
#: la misura di fase 0b ha stabilito che MuseTalk prepara una battuta in un
#: decimo di secondo: quel che cronometrava era la propria coda. Col motore la
#: battuta e' anche l'unita' di taglio, quindi corta e' meglio: ~40 caratteri
#: sono due secondi di parlato, ed e' la grana con cui si puo' interrompere.
DUPLEX_MIN_CARATTERI = 40


def _parole(s: str) -> set:
    import re as _re
    return set(_re.findall(r"\w+", (s or "").lower()))


def _e_eco(testo: str, detto: str) -> bool:
    """True se `testo` e' con ogni probabilita' l'ECO di cio' che l'avatar
    sta dicendo, non una vera interruzione dell'utente.

    In full-duplex a viva voce il microfono del telefono risente l'avatar:
    lo STT lo trascrive e il barge-in scatta su se stesso. Risultato: ogni
    frase veniva troncata e la successiva si sovrapponeva. La cancellazione
    d'eco del telefono aiuta ma non basta quando il volume e' alto.

    Il confronto e' sulle PAROLE, non sui caratteri: lo STT trascrive l'eco
    in modo approssimativo, ma le parole piene restano le stesse.
    """
    p = _parole(testo)
    if not p or not detto:
        return False
    comuni = p & _parole(detto)
    return len(comuni) / len(p) >= 0.6


def _frase_vera(coda, detto: str = "") -> str:
    """Prima utterance in coda che vale come interruzione, "" se non ce n'e'.

    Ispeziona SENZA consumare: la coda serve poi al turno successivo, e
    toglierne un elemento qui farebbe perdere quello che l'utente ha detto.
    """
    try:
        from .engines.whisper_stt import _e_allucinazione
    except Exception:  # noqa: BLE001 - motore non importabile
        def _e_allucinazione(_t):  # type: ignore[misc]
            return False
    try:
        for t in list(coda.queue):
            if not isinstance(t, str):
                continue
            testo = t.strip()
            # Le frasi-fantasma dei silenzi ("Sottotitoli e revisione a cura
            # di...") sono lunghe e passerebbero la soglia: non sono voce.
            if (len(testo) >= _MIN_BARGEIN_CHARS
                    and not _e_allucinazione(testo)
                    and not _e_eco(testo, detto)):
                return testo
    except Exception:  # noqa: BLE001 - coda di un tipo inatteso
        return "" if coda.empty() else "?"
    return ""


class _Connection:
    """Gestisce una connessione app: piu' turni in sequenza."""

    def __init__(self, ws, cfg: Config, brain, brain_chat=None) -> None:
        self.ws = ws
        self.cfg = cfg
        # Due cervelli, come le hotkey sul PC: `brain` e' l'agente COMPLETO
        # (ACP/Hermes, con tool e skill), `brain_chat` la voce diretta su LM
        # Studio (non agentica, near-real-time). L'app sceglie col campo
        # `brain` del messaggio video_start: "hermes" oppure "chat".
        self.brain_hermes = brain
        self.brain_chat = brain_chat
        self.brain = brain
        self.stt: HandySttClient | None = None
        self.tts: HandyTtsClient | None = None
        # Allegati che l'app ha mandato e che il prossimo enunciato deve
        # citare: (percorso, descrizione). Era una sola immagine; ora sono
        # piu' d'uno e di qualunque tipo, perche' si puo' allegare un
        # documento e poi chiedere qualcosa a voce.
        self._allegati: list[tuple[str, str]] = []
        #: Registrazione della voce consegnata: si accende creando il file
        #: `call-bridge/dump-voce.on`, come si fa gia' per i turni in entrata.
        self._dump_voce = None
        self._dump_voce_attivo = (
            Path(__file__).resolve().parents[1] / "dump-voce.on").exists()
        self._video = None        # SessioneVideo (WebRTC) verso l'app
        self._frames = None       # FrameServer che riceve i fotogrammi dal worker
        #: Come il CLIENT renderizza il lipsync. Lo dichiara lui, perche' e' una
        #: sua capacita': "musetalk" = il PC genera il video (come oggi);
        #: "visemi" = il PC manda la timeline dei visemi e il client anima la
        #: foto; "wav2lip" = il client genera tutto dalla foto+audio, e il PC
        #: non manda ne' video ne' visemi. Default dal config.
        self._render_mode = getattr(cfg, "render_mode_default", "musetalk")
        #: Lingua del turno corrente, per la fonemizzazione dei visemi.
        self._lang_turno = (cfg.default_lang or "it").split("-")[0]
        # Testo che l'avatar sta pronunciando nel turno corrente: serve a
        # distinguere la propria eco da una vera interruzione.
        self._ultima_risposta = ""

    def _send_json(self, obj: dict) -> None:
        self.ws.send(json.dumps(obj))

    def _handle_image_msg(self, msg: dict) -> None:
        """Salva un'immagine inviata dall'app; sara' allegata al prossimo enunciato."""
        from . import media as media_mod
        data = msg.get("data")
        if not data:
            return
        try:
            percorso = media_mod.save_incoming_image(
                data, msg.get("mime", "image/jpeg"))
            self._allegati.append((percorso, "un'immagine"))
            log.info("Gateway: immagine allegata ricevuta (%s)", percorso)
        except Exception as e:  # noqa: BLE001
            log.warning("Gateway: immagine non salvata: %s", e)

    def _handle_file_msg(self, msg: dict) -> None:
        """Prende in carico un file che l'app ha gia' caricato sul media server.

        Il file NON passa di qui: l'app lo manda con un POST su /upload e su
        questo WebSocket arriva solo il percorso che quel POST ha restituito.
        Farlo viaggiare in base64 nel messaggio costerebbe un terzo di byte in
        piu' e tratterrebbe l'intero file in memoria due volte.

        Il percorso si accetta SOLO se sta nella cartella degli allegati.
        Arriva dalla rete: senza questo controllo basterebbe scriverci dentro
        un percorso qualsiasi per farsi leggere dall'agente un file del PC che
        nessuno ha mai mandato.
        """
        from . import media as media_mod
        percorso = (msg.get("path") or "").strip()
        if not percorso:
            return
        try:
            p = Path(percorso).resolve()
            radice = Path(media_mod._INCOMING_DIR).resolve()
            if radice not in p.parents or not p.is_file():
                log.warning("Gateway: allegato rifiutato (fuori dalla cartella "
                            "degli allegati o inesistente): %s", percorso)
                self._send_json({"type": "error", "message": "allegato rifiutato"})
                return
        except OSError as e:
            log.warning("Gateway: allegato non verificabile: %s", e)
            return
        self._allegati.append((str(p), f"il file «{p.name}»"))
        log.info("Gateway: allegato ricevuto (%s, %.1f KB)", p.name,
                 p.stat().st_size / 1024)
        self._send_json({"type": "file_ok", "name": p.name})

    def _with_attachment(self, utterance: str) -> str:
        """Antepone all'enunciato il riferimento agli allegati dell'app.

        Si passa il PERCORSO, non il contenuto: l'agente ha gli strumenti
        per leggere un file, e infilare un PDF dentro il prompt lo
        riempirebbe di testo che nessuno ha chiesto."""
        if self._allegati:
            righe = [f"[L'utente ha allegato {che}: {dove}]"
                     for dove, che in self._allegati]
            self._allegati = []
            return "\n".join(righe) + "\n" + utterance
        return utterance

    def _emit_media(self, kind: str, src: str) -> None:
        """Manda all'app un media generato da Hermes (via URL del media server)."""
        from . import media as media_mod
        if src.startswith(("http://", "https://")):
            self._send_json({"type": "media", "kind": kind, "url": src})
            return
        token = media_mod.register_file(src)
        if token:
            self._send_json({"type": "media", "kind": kind,
                             "token": token, "port": self.cfg.gateway_media_port})
        else:
            log.warning("Gateway: media non trovato: %s", src)

    def _stream_reply(self, utterance: str, bargein=None, motore=None) -> None:
        """Hermes -> filtro tag media -> testo all'app + TTS; media -> URL all'app.

        L'app riceve il testo GREZZO (rende il markdown); il TTS riceve solo il
        testo SANITIZZATO. Testo, media e frasi TTS li produce ora
        turn_engine.ReplyProcessor (condiviso), che risolve alla radice il
        problema del gateway che pronunciava markdown/emoji/blocchi <think>.
        """
        self._ultima_risposta = ""      # nuovo turno: nuova eco da riconoscere
        self._caratteri_tts = 0         # quanto testo e' andato in sintesi
        proc = ReplyProcessor(extract_media=True,
                              min_caratteri=(DUPLEX_MIN_CARATTERI
                                             if motore is not None else None))

        def _emit(rc) -> None:
            for kind, src in rc.media:
                self._emit_media(kind, src)
            if rc.app_text:
                self._ultima_risposta += " " + rc.app_text
                self._send_json({"type": "reply", "chunk": rc.app_text})
            for sentence in rc.tts_sentences:
                self._caratteri_tts += len(sentence)
                # Col motore duplex il testo non va dritto al TTS: passa dal
                # copione, che ne governa la consegna. Il TTS resta lo stesso,
                # cambia chi decide quando l'audio esce.
                if motore is not None:
                    motore.dillo(sentence)
                else:
                    self.tts.send_text(sentence)

        try:
            for chunk in respond_stream(self.brain, self._with_attachment(utterance),
                                        self.cfg, session_tag="app"):
                if bargein and bargein():
                    return
                if not isinstance(chunk, str):
                    continue  # metadati (es. voice_change): non sono testo da leggere
                _emit(proc.feed(chunk))
            _emit(proc.flush())
            if motore is not None:
                motore.fine_risposta()
            else:
                self.tts.flush()
        except Exception as e:  # noqa: BLE001 - errore lato Hermes
            log.error("Gateway: Hermes ha fallito: %s", e)
            self._send_json({"type": "error", "message": f"Hermes: {e}"})

    def run(self) -> None:
        # 1) autenticazione
        if not self._authenticate():
            return
        # 2) Scalda SOLO lo STT, e nel BRIDGE non qui: STT e TTS si usano via
        #    WebSocket su :8720, quindi caricarli in questo processo sarebbe una
        #    seconda copia in VRAM mai usata. Il TTS non si tocca: quale motore
        #    serva dipende dalla persona del turno, e imporne uno adesso vuol
        #    dire caricare Kokoro per poi sfrattarlo appena parla un avatar con
        #    voce XTTS — due motori in memoria proprio nel momento peggiore.
        if not ensure_models_loaded(self.cfg, tts=False, in_process=False):
            log.warning("Gateway: preload modelli fallito (Handy non pronto?)")
        # 3) apri STT/TTS verso Handy (riusati per tutti i turni)
        # Lo STT parte con la lingua di default; `_stt_per_lingua` lo riapre se
        # l'app ne chiede un'altra. Senza lingua Whisper la indovina, e su
        # enunciati brevi sbaglia — un "mi senti" trascritto in cinese.
        self._stt_lang = (self.cfg.default_lang or "it").split("-")[0]
        # (engine, voce) della persona a video: chi si vede deve parlare con la
        # propria voce, non con quella di default del bridge.
        self._voce_persona: tuple[str, str] | None = None
        self.stt = HandySttClient(self.cfg.handy_stt_url_lang(self._stt_lang))
        self.tts = HandyTtsClient(self.cfg.handy_tts_url())
        try:
            self.stt.start()
            self.tts.start()
        except (TimeoutError, OSError) as e:
            self._send_json({"type": "error", "message": f"Handy non raggiungibile: {e}"})
            self._close()
            return
        self._send_json({"type": "ready"})
        log.info("Gateway: connessione app pronta")

        # 4) ciclo dei turni / sessioni
        try:
            while True:
                start = self._wait_start()
                if start is None:
                    break  # connessione chiusa
                if start.get("typed") is not None:
                    self._do_typed_turn(start)      # testo scritto/corretto a mano
                elif start.get("duplex"):
                    self._do_duplex_session(start)  # auricolare/bluetooth: full-duplex
                else:
                    self._do_turn(start)            # viva voce: a turni (hold/continuous)
        except Exception as e:  # noqa: BLE001 - connessione chiusa o errore
            log.info("Gateway: connessione terminata (%s)", e)
        finally:
            if self._video is not None:
                self._video_stop()
            if self._frames is not None:
                self._frames.close()
            self._close()

    def _authenticate(self) -> bool:
        try:
            raw = self.ws.recv(timeout=10)
        except Exception:  # noqa: BLE001
            return False
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            msg = {}
        # Token vuoto in config = auth disattivata (sistema locale): si accetta
        # qualunque messaggio {type: auth}, cosi' il protocollo dell'app resta lo
        # stesso (attende comunque auth_ok).
        if msg.get("type") == "auth" and (
            not self.cfg.gateway_auth_token
            or msg.get("token") == self.cfg.gateway_auth_token
        ):
            self._send_json({"type": "auth_ok"})
            return True
        self._send_json({"type": "error", "message": "auth fallita"})
        return False

    def _wait_start(self) -> dict | None:
        """Attende un messaggio {start}; None se la connessione si chiude."""
        while True:
            try:
                raw = self.ws.recv()
            except Exception:  # noqa: BLE001 - chiusura
                return None
            if isinstance(raw, (bytes, bytearray)):
                continue  # audio fuori turno: ignora
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if self._messaggio_collaterale(msg):
                continue
            if msg.get("type") == "start":
                return msg
            # Testo scritto (o corretto) dall'app: stesso turno di una battuta
            # parlata, ma l'enunciato arriva gia' pronto e si salta lo STT.
            if msg.get("type") == "text":
                return {"typed": (msg.get("text") or "").strip(),
                        "lang": msg.get("lang"), "voice": msg.get("voice")}
            if msg.get("type") == "close":
                return None

    def _stt_per_lingua(self, lang: str) -> None:
        """Riapre lo STT se la lingua della sessione e' cambiata."""
        codice = (lang or "").strip().lower().split("-")[0]
        if not codice or codice == self._stt_lang:
            return
        log.info("Gateway: STT -> lingua %s (era %s)", codice, self._stt_lang)
        try:
            self.stt.close()
        except Exception:  # noqa: BLE001 - chiusura best-effort
            pass
        self._stt_lang = codice
        self._lang_turno = codice          # i visemi seguono la stessa lingua
        self.stt = HandySttClient(self.cfg.handy_stt_url_lang(codice))
        self.stt.start()

    def _do_turn(self, start: dict) -> None:
        """Viva voce: un turno alla volta (hold = fino a {end}; continuous = fino a pausa)."""
        mode = start.get("mode", "hold")
        lang = start.get("lang") or self.cfg.default_lang
        self._stt_per_lingua(lang)
        voice = start.get("voice") or self.cfg.default_voice
        in_rate = int(start.get("sample_rate") or STT_RATE)
        self._configura_voce(voice, lang)
        drain_queue(self.stt.utterances)

        # 1) raccogli audio fino a fine turno (testo COMPLETO, non solo le prime parole)
        utterance = self._capture_utterance(mode, in_rate)
        if not utterance:
            self._send_json({"type": "transcript", "text": ""})
            self._send_json({"type": "done"})
            return
        self._send_json({"type": "transcript", "text": utterance})

        # Modalita' "detta e correggi": si restituisce la sola trascrizione e ci
        # si ferma. L'app la mette in un campo di testo, l'utente la sistema e
        # la rimanda con {"type":"text"}. Serve quando il riconoscimento sbaglia
        # un nome o una parola: correggerla a voce costa piu' che scriverla.
        if start.get("transcribe_only") or mode == "transcribe":
            self._send_json({"type": "done"})
            return

        # 2) Hermes (streaming) -> testo+media all'app + TTS
        self._stream_reply(utterance)

        # 3) inoltra TUTTO l'audio TTS (fino a fine sintesi, non solo l'inizio)
        self._forward_tts_audio()
        self._send_json({"type": "done"})

    def _scegli_cervello(self, quale: str | None) -> None:
        """Applica la modalita' chiesta dall'app: "chat" oppure "hermes".

        "chat"   -> voce diretta su LM Studio, non agentica: risponde in
                    un paio di secondi, e' quella che serve a conversare.
        "hermes" -> agente completo via ACP, con tool e skill: piu' lento
                    (l'avvio dell'agente puo' durare minuti) ma sa agire.

        Prima il campo veniva solo LOGGATO e il gateway rispondeva sempre
        con l'agente completo: l'app chiedeva "chat" e restava ad aspettare.
        """
        quale = (quale or "").strip().lower()
        if quale == "chat" and self.brain_chat is not None:
            self.brain = self.brain_chat
        elif quale == "hermes":
            self.brain = self.brain_hermes
        elif quale == "chat":
            log.warning("Gateway: chiesto brain 'chat' ma non e' disponibile: "
                        "uso l'agente completo")
            self.brain = self.brain_hermes
        else:
            return                      # campo assente: si tiene quello corrente
        log.info("Gateway: cervello del turno -> %s", quale)

    def _messaggio_collaterale(self, msg: dict) -> bool:
        """Gestisce i messaggi che l'app puo' mandare ANCHE a sessione aperta.

        Ritorna True se il messaggio e' stato consumato.

        Serve perche' `video_start` non arriva piu' solo fra un turno e
        l'altro: da quando il video parte insieme all'audio, arriva mentre la
        sessione e' gia' in corso. I loop di ricezione delle sessioni
        conoscevano solo stop/close/image e lo buttavano via in silenzio —
        l'app restava ad aspettare la risposta SDP per trenta secondi e poi
        diceva "video non disponibile" con tutto perfettamente funzionante.

        `video_start` gira in un thread suo: la negoziazione attende la
        raccolta ICE (fino a 5 secondi) e qui bloccherebbe la ricezione
        dell'audio, cioe' farebbe sparire la voce dell'utente per quel tempo.
        """
        tipo = msg.get("type")
        if tipo == "image":
            self._handle_image_msg(msg)
            return True
        if tipo == "file":
            self._handle_file_msg(msg)
            return True
        if tipo == "video_start":
            threading.Thread(target=self._video_start, args=(msg,),
                             name="gw-video-start", daemon=True).start()
            return True
        if tipo == "video_stop":
            self._video_stop()
            return True
        if tipo == "render_mode":
            # Il client dichiara come renderizza il lipsync. Il gateway lo
            # ricorda per decidere se aprire il video (musetalk) o mandare i
            # visemi (visemi), o niente dei due (wav2lip).
            m = (msg.get("mode") or "").strip()
            if m in ("musetalk", "visemi", "wav2lip"):
                self._render_mode = m
                log.info("Gateway: motore di rendering -> %s", m)
                self._send_json({"type": "render_mode", "mode": m})
            return True
        if tipo == "brain":
            # Cambio di cervello A CALDO. Prima la scelta viaggiava solo
            # nell'handshake WebRTC (`video_start`), quindi passare da chat ad
            # Hermes voleva dire chiudere e riaprire il flusso: riquadro nero,
            # nuova negoziazione ICE, e il primo fotogramma da aspettare di
            # nuovo. Qui invece si cambia l'interlocutore e basta.
            #
            # Sicuro anche a turno in corso: `_stream_reply` legge `self.brain`
            # UNA volta, all'inizio, quindi la risposta che si sta generando
            # arriva intera da chi l'ha cominciata e il cambio vale dal turno
            # successivo. Il contrario — troncare a meta' — sarebbe peggio
            # dell'attesa che si vuole togliere.
            self._scegli_cervello(msg.get("brain"))
            self._send_json({"type": "brain",
                             "brain": "hermes" if self.brain is self.brain_hermes
                                      else "chat"})
            return True
        return False

    def _video_start(self, msg: dict) -> None:
        """Accende l'avatar video verso l'app e negozia la connessione WebRTC.

        Il worker avatar vive nel BRIDGE (uno solo, per la VRAM): qui lo si
        comanda via HTTP sul control server. `brain` sceglie chi risponde —
        "chat" e' l'avatar diretto, "hermes" l'agente completo.
        """
        try:
            from .avatar_webrtc import FrameServer, SessioneVideo
        except ImportError as e:
            self._send_json({"type": "error",
                             "message": f"video non disponibile (aiortc): {e}"})
            return
        persona = (msg.get("persona") or "").strip()
        sdp = msg.get("sdp") or ""
        if not sdp:
            self._send_json({"type": "error", "message": "offerta SDP mancante"})
            return
        base = f"http://127.0.0.1:{self.cfg.control_port}"
        try:
            if self._frames is None:
                self._frames = FrameServer()
            # 1) Si negozia SUBITO. La preparazione dell'avatar puo' durare
            #    minuti (la prima volta per clip); se si aspettasse quella prima
            #    di rispondere, l'app andrebbe in timeout e direbbe "video non
            #    disponibile" pur essendo tutto a posto. Meglio: connessione
            #    stabilita ora (riquadro nero), fotogrammi appena ci sono.
            self._video = SessioneVideo(
                self._frames,
                audio=getattr(self.cfg, "avatar_audio_webrtc", True))
            risposta = self._video.apri(sdp, msg.get("sdp_type") or "offer")
            self._send_json({"type": "video_answer", "sdp": risposta})
            # 2) L'avatar si accende in un thread, cosi' il loop dei messaggi
            #    resta libero: l'utente puo' parlare mentre il video si prepara.
            threading.Thread(target=self._video_accendi_avatar, args=(base, persona),
                             name="gw-avatar-up", daemon=True).start()
            log.info("Gateway: video avatar negoziato (persona=%s, brain=%s)",
                     persona or "corrente", msg.get("brain") or "chat")
            self._scegli_cervello(msg.get("brain"))
        except Exception as e:  # noqa: BLE001 - l'app deve sapere che non parte
            log.error("Gateway: avvio video fallito: %s", e)
            self._send_json({"type": "error", "message": f"video: {e}"})

    def _configura_voce(self, voice: str, lang: str) -> None:
        """Applica la voce del turno: quella dell'avatar attivo ha precedenza.

        Si risolve anche a VIDEO SPENTO. Prima la voce della persona si leggeva
        solo accendendo il video, quindi senza video il turno usava la voce
        delle impostazioni dell'app — `im_nicola` di default, che e' Kokoro e
        per giunta maschile mentre l'avatar e' Shan.

        Non era solo un timbro sbagliato: e' un MOTORE diverso, e i motori TTS
        non stanno in memoria insieme. Il router scaricava XTTS per caricare
        Kokoro, e appena l'avatar del PC riparlava si rifaceva il giro al
        contrario — misurato dal vivo: `motore 'xtts' pronto in 70.7s`. Quei
        cambi sono il grosso dell'"elaborazione lunghissima" dopo che la
        risposta e' gia' a schermo, e colpivano anche l'avatar sul PC perche'
        il router e' lo stesso.

        Il costo e' una GET su localhost per turno, contro decine di secondi
        di ricarica motore: non c'e' partita.
        """
        if self._voce_persona is None:
            self._risolvi_voce_avatar()
        if self._voce_persona:
            engine, voce = self._voce_persona
            configure_tts(self.tts, self.cfg, voce, lang, engine)
            return
        configure_tts(self.tts, self.cfg, voice, lang)

    def _risolvi_voce_avatar(self) -> None:
        """Legge engine+voce dell'avatar attivo sul PC, se ce n'e' uno."""
        base = f"http://127.0.0.1:{self.cfg.control_port}"
        persona = self._persona_corrente(base)
        if persona:
            self._leggi_voce_persona(base, persona)

    def _apri_sessione_persona(self, persona: str) -> None:
        """Carica sul cervello l'identita' della persona a video.

        E' il pezzo che rende l'avatar sul telefono lo STESSO del PC:
        `begin_session` legge dal manifest personalita' e system prompt,
        recupera la memoria delle conversazioni passate e collega il sapere
        (RAG). Il cervello del gateway veniva creato e usato nudo, quindi
        rispondeva come un assistente qualunque.

        Solo sul cervello CHAT: l'agente ACP la sua identita' ce l'ha nel
        proprio config e non espone questo hook.
        """
        b = self.brain_chat
        if not persona or b is None or not hasattr(b, "begin_session"):
            return
        try:
            b.begin_session(persona)
            log.info("Gateway: identita' '%s' caricata sul cervello chat", persona)
        except Exception as e:  # noqa: BLE001 - meglio generico che muto
            log.warning("Gateway: identita' di '%s' non caricata: %s", persona, e)

    def _persona_corrente(self, base: str) -> str:
        """Nome della persona gia' caricata nel worker avatar ("" se nessuna)."""
        try:
            st = requests.get(f"{base}/avatar/state", timeout=10).json()
            return str(st.get("persona") or "")
        except Exception as e:  # noqa: BLE001 - si resta sulla voce di default
            log.warning("Gateway: persona corrente non leggibile: %s", e)
            return ""

    def _leggi_voce_persona(self, base: str, persona: str) -> None:
        """Memorizza engine+voce della persona a video (da /personas)."""
        if not persona:
            return
        try:
            dati = requests.get(f"{base}/personas", timeout=10).json()
            elenco = dati if isinstance(dati, list) else dati.get("personas", [])
            for p in elenco:
                if str(p.get("name", "")).lower() != persona.lower():
                    continue
                v = p.get("voice") or {}
                voce, engine = v.get("voice") or "", v.get("engine") or ""
                if voce:
                    self._voce_persona = (engine, voce)
                    log.info("Gateway: voce di %s -> %s/%s", persona, engine or "auto", voce)
                return
        except Exception as e:  # noqa: BLE001 - si resta sulla voce di default
            log.warning("Gateway: voce di %s non leggibile: %s", persona, e)

    @staticmethod
    def _avanzamento(st: dict) -> str:
        """Avanzamento della preparazione in una frase leggibile.

        `progress` arriva come dizionario (fase, percentuale, stima): mandarlo
        cosi' com'e' riempirebbe lo schermo dell'app di parentesi graffe.
        """
        prog = st.get("progress")
        if isinstance(prog, dict) and prog.get("percent") is not None:
            testo = f"preparo l'avatar — {float(prog['percent']):.0f}%"
            eta = prog.get("eta_s")
            if eta:
                testo += f" (~{int(float(eta))}s)"
            return testo
        if st.get("preparing"):
            return f"preparo {st['preparing']}…"
        return "preparo l'avatar…"

    @staticmethod
    def _motivo(r: "requests.Response") -> str:
        """Motivo leggibile di una risposta fallita.

        Flask serve una pagina HTML sugli errori: riversarla nell'app riempie lo
        schermo di markup non renderizzato. Si prende il campo `error` del JSON
        quando c'e', altrimenti si dice solo cosa e' andato storto.
        """
        try:
            corpo = r.json()
            if isinstance(corpo, dict) and corpo.get("error"):
                return str(corpo["error"])
        except ValueError:
            pass
        if r.status_code == 404:
            return "il bridge non conosce questa funzione (va riavviato)"
        return f"errore {r.status_code}"

    def _video_accendi_avatar(self, base: str, persona: str) -> None:
        """Prepara la persona e dirotta i fotogrammi verso l'app (in background).

        Manda `video_state` all'app a ogni passo: senza, davanti a un riquadro
        nero per due minuti non si distingue una preparazione lunga da un guasto.
        """
        try:
            # L'app non manda quasi mai il nome della persona: accende "il
            # video" e basta. Senza risolverlo qui, la voce del turno resta
            # quella scelta nelle impostazioni dell'app (di default `im_nicola`,
            # un uomo) mentre a schermo c'e' Shan. Si risolve SOLO per leggerne
            # la voce: /avatar/start resta riservato a una persona chiesta
            # esplicitamente, altrimenti si rifarebbe la preparazione di una
            # persona gia' pronta.
            self._leggi_voce_persona(base, persona or self._persona_corrente(base))
            # Stessa IDENTITA' del PC, non solo la stessa faccia: personalita',
            # system prompt, memoria delle conversazioni e sapere collegato
            # stanno in `begin_session`, che il gateway non chiamava mai. Senza,
            # l'avatar sul telefono non sapeva nemmeno il proprio nome e
            # rispondeva come un assistente generico, mentre sul PC era Shan.
            self._apri_sessione_persona(persona or self._persona_corrente(base))
            if persona:
                r = requests.post(f"{base}/avatar/start", json={"persona": persona},
                                  timeout=30)
                if r.status_code == 404:
                    self._send_json({"type": "error",
                                     "message": f"persona '{persona}' non trovata"})
                    return
            # Attende che il worker sia davvero pronto (start e' asincrono).
            scaduta = time.time() + 600
            ultimo = ""
            while time.time() < scaduta:
                st = requests.get(f"{base}/avatar/state", timeout=10).json()
                if st.get("error"):
                    self._send_json({"type": "error",
                                     "message": f"avatar: {st['error']}"})
                    return
                if st.get("running") and (not persona or st.get("persona") == persona):
                    break
                stato = self._avanzamento(st)
                if stato != ultimo:
                    ultimo = stato
                    self._send_json({"type": "video_state", "state": "preparing",
                                     "detail": stato})
                time.sleep(1.0)
            else:
                self._send_json({"type": "error",
                                 "message": "avatar: preparazione troppo lunga"})
                return
            if self._frames is None or self._video is None:
                return  # l'utente ha gia' spento il video
            r = requests.post(f"{base}/avatar/stream",
                              json={"on": True, "port": self._frames.port}, timeout=30)
            if not r.ok:
                self._send_json({"type": "error",
                                 "message": f"avatar non pronto: {self._motivo(r)}"})
                return
            self._send_json({"type": "video_state", "state": "live"})
            log.info("Gateway: fotogrammi avatar in streaming verso l'app")
        except Exception as e:  # noqa: BLE001 - l'app deve saperlo
            log.error("Gateway: accensione avatar fallita: %s", e)
            self._send_json({"type": "error", "message": f"video: {e}"})

    def _video_stop(self) -> None:
        """Spegne il video e restituisce i fotogrammi alla webcam del PC."""
        try:
            requests.post(f"http://127.0.0.1:{self.cfg.control_port}/avatar/stream",
                          json={"on": False}, timeout=15)
        except requests.RequestException as e:
            log.debug("stop stream avatar: %s", e)
        if self._video is not None:
            self._video.chiudi()
            self._video = None
        self._voce_persona = None
        self._send_json({"type": "video_stopped"})
        log.info("Gateway: video avatar spento")

    def _do_typed_turn(self, start: dict) -> None:
        """Turno da testo gia' pronto: niente microfono, il resto e' identico."""
        testo = (start.get("typed") or "").strip()
        if not testo:
            self._send_json({"type": "done"})
            return
        lang = start.get("lang") or self.cfg.default_lang
        voice = start.get("voice") or self.cfg.default_voice
        self._configura_voce(voice, lang)
        log.info("Gateway: turno da testo scritto (%d caratteri)", len(testo))
        self._stream_reply(testo)
        self._forward_tts_audio()
        self._send_json({"type": "done"})

    def _do_duplex_session(self, start: dict) -> None:
        """Auricolare/Bluetooth: sessione full-duplex (parla e ascolta insieme).

        Un thread riceve l'audio dall'app e lo passa allo STT in continuo; il
        loop principale, a ogni enunciato finalizzato, interroga Hermes e
        rimanda testo+audio. Nessun mute durante il TTS (no eco con auricolare/BT).
        Barge-in: se l'utente riparla mentre l'assistente risponde, si interrompe.
        Termina su {stop}/{close} dall'app.
        """
        from .audio_hfp import resample

        lang = start.get("lang") or self.cfg.default_lang
        voice = start.get("voice") or self.cfg.default_voice
        in_rate = int(start.get("sample_rate") or STT_RATE)
        self._configura_voce(voice, lang)
        drain_queue(self.stt.utterances)
        stop = threading.Event()

        def _receiver() -> None:
            # Il full-duplex non registrava NIENTE sull'audio in arrivo: con
            # l'utente che dice "non mi sente" era impossibile distinguere
            # l'app che non manda, la rete che perde, o lo STT che non
            # trascrive. Un riepilogo ogni 5 secondi con il livello del
            # segnale separa i tre casi in un colpo solo.
            ultimo_log = time.monotonic()
            n_blocchi = n_campioni = 0
            somma_q = picco = 0.0
            while not stop.is_set():
                try:
                    raw = self.ws.recv(timeout=0.4)
                except TimeoutError:
                    continue
                except Exception:  # noqa: BLE001 - chiusura
                    stop.set()
                    break
                if isinstance(raw, (bytes, bytearray)):
                    samples = _pcm16_to_f32(bytes(raw))
                    if in_rate != STT_RATE:
                        samples = resample(samples, in_rate, STT_RATE)
                    # Stessa pulizia dei turni: via la componente continua e
                    # attenuazione solo se il segnale clippa (non amplifica
                    # mai). Nel duplex non veniva applicata, quindi lo STT
                    # riceveva un audio diverso — e peggiore — rispetto al
                    # percorso a turni, senza che nulla lo spiegasse.
                    samples = _normalize_audio(samples)
                    self.stt.send_audio(samples)
                    n_blocchi += 1
                    n_campioni += len(samples)
                    if len(samples):
                        somma_q += float(np.square(samples).sum())
                        picco = max(picco, float(np.abs(samples).max()))
                else:
                    # I messaggi di controllo si gestiscono QUI, subito dopo
                    # aver distinto testo da binario. Stavano nel ramo `else`
                    # del log periodico qui sotto: nell'iterazione in cui il
                    # log scattava, un `stop`, un `video_start` o un `brain`
                    # veniva letto e buttato via senza che nessuno se ne
                    # accorgesse. Un messaggio perso ogni cinque secondi, ma
                    # sempre e solo quello arrivato nell'istante sbagliato:
                    # il tipo di difetto che sembra casuale.
                    try:
                        msg = json.loads(raw)
                    except Exception:  # noqa: BLE001 - non e' JSON: si ignora
                        msg = None
                    if msg is not None:
                        if msg.get("type") in ("stop", "close"):
                            stop.set()
                            break
                        self._messaggio_collaterale(msg)
                ora = time.monotonic()
                if ora - ultimo_log >= 5.0:
                    if n_campioni:
                        rms = (somma_q / n_campioni) ** 0.5
                        log.info("Gateway duplex: %d blocchi, %.1fs di audio "
                                 "(picco %.3f, rms %.4f)",
                                 n_blocchi, n_campioni / STT_RATE, picco, rms)
                        if picco < 0.02:
                            log.warning("Gateway duplex: audio quasi muto "
                                        "(picco %.3f): microfono coperto, "
                                        "permesso negato o guadagno a zero", picco)
                    else:
                        log.warning("Gateway duplex: NESSUN audio dall'app "
                                    "negli ultimi 5s")
                    ultimo_log = ora
                    n_blocchi = n_campioni = 0
                    somma_q = picco = 0.0

        motore = self._crea_motore()
        rt = threading.Thread(target=_receiver, name="gw-duplex-rx", daemon=True)
        rt.start()
        if motore is not None:
            threading.Thread(target=self._ciclo_orecchie,
                             args=(motore, stop), name="gw-orecchie",
                             daemon=True).start()
        self._send_json({"type": "session_started"})
        log.info("Gateway: sessione full-duplex avviata")
        #: Quante volte si e' riconosciuta la PROPRIA voce rientrare dal
        #: microfono. Non e' una curiosita' statistica: e' la misura che dice
        #: se la conversazione continua e' sostenibile su questo apparecchio,
        #: in questa stanza, a questo volume. La cancellazione d'eco dipende
        #: da hardware e ambiente, quindi non e' una preferenza da chiedere
        #: all'utente ma una cosa da misurare e da dire.
        self._eco_viste = 0
        #: Quel che l'utente ha detto mentre l'avatar parlava e che si e'
        #: deciso di trattare dopo (ACCODA). Ha la precedenza sul microfono:
        #: e' piu' vecchio di qualunque cosa stia arrivando adesso.
        self._da_trattare: list[str] = []
        try:
            while not stop.is_set():
                if self._da_trattare:
                    utterance = self._da_trattare.pop(0)
                else:
                    try:
                        first = self.stt.utterances.get(timeout=0.4)
                    except queue.Empty:
                        continue
                    utterance = coalesce_utterances(
                        self.stt.utterances, first,
                        self.cfg.ptt_turn_pause_s, stop=stop)
                if not utterance:
                    continue
                self._send_json({"type": "transcript", "text": utterance})

                # Barge-in SOLO su una frase vera. Prima bastava una qualsiasi
                # utterance in coda: in full-duplex il microfono resta aperto
                # mentre il cervello genera (secondi), quindi un respiro, un
                # rumore o l'eco dell'avatar bastavano a troncare la risposta
                # PRIMA del TTS — l'avatar restava muto e sembrava "solo un
                # filmato". Visto dal vivo: turni con rms 0,005 trascritti
                # come "in ascolto." che interrompevano ogni turno.
                def bargein(solo_stop: bool = False) -> bool:
                    if stop.is_set():
                        return True
                    if solo_stop:
                        # Col motore le frasi le giudica il classificatore:
                        # qui resta solo la fine della sessione.
                        return False
                    # `_ultima_risposta`: cio' che l'avatar sta dicendo ORA.
                    # Serve a riconoscere la propria eco e non scambiarla per
                    # un'interruzione dell'utente.
                    return bool(_frase_vera(self.stt.utterances,
                                            self._ultima_risposta))

                # NIENTE barge-in durante la GENERAZIONE: non si interrompe
                # cio' che non si e' ancora sentito. Qui l'audio non e' ancora
                # partito, quindi una trascrizione in coda e' rumore o eco, non
                # una volonta' di interrompere — e troncava la risposta prima
                # del TTS, lasciando l'avatar muto. Se l'utente parla davvero
                # ora, la frase resta in coda e diventa il turno successivo.
                if motore is not None:
                    # Col motore la voce esce mentre il cervello genera: non
                    # c'e' piu' una fase "adesso parlo" dopo una fase "adesso
                    # penso". Il barge-in vale da subito, perche' la prima
                    # battuta puo' essere gia' uscita mentre l'ultima e' ancora
                    # da scrivere.
                    # Turno nuovo: la voce riparte piena, qualunque cosa sia
                    # successa in quello prima.
                    motore.rialza()
                    self._send_json({"type": "audio_start",
                                     "sample_rate": 24000})
                    self._stream_reply(utterance, motore=motore)
                    fermato = self._attendi_motore(motore, bargein)
                    if self._caratteri_tts and not motore.battute_ricevute:
                        # Testo mandato in sintesi, zero audio tornato: il
                        # turno e' passato in silenzio. Senza questa riga il
                        # sintomo e' solo "l'avatar non ha risposto".
                        log.warning("Gateway: %d caratteri in sintesi e nessuna "
                                    "battuta dal TTS", self._caratteri_tts)
                    self._send_json({"type": "audio_end"})
                    # Col motore la decisione l'ha gia' presa il
                    # classificatore, frase per frase, mentre l'avatar parlava:
                    # rifarla qui sulla coda grezza vorrebbe dire giudicare
                    # due volte lo stesso materiale con criteri diversi — ed e'
                    # esattamente il difetto che la fase 1 aveva corretto.
                    self._send_json({"type": "interrupt" if fermato
                                     else "turn_done"})
                    continue
                else:
                    self._stream_reply(utterance)
                    # Da qui in poi la voce si sente: il barge-in torna a valere.
                    self._forward_tts_audio(barge_in=bargein)
                # se interrotti da barge-in: svuota l'audio residuo e avvisa l'app;
                # altrimenti segnala la fine del turno (l'app torna "in ascolto").
                #
                # Si usa lo STESSO filtro del barge-in, non `utterances.empty()`.
                # Con la coda grezza bastava un residuo qualunque — l'eco della
                # voce dell'avatar, un colpo di tosse, una frase-fantasma di
                # Whisper — per far scattare l'interruzione della risposta
                # appena conclusa: roba che `_frase_vera` aveva gia' esaminato e
                # SCARTATO un istante prima, durante la riproduzione. Due
                # decisioni sullo stesso materiale con criteri diversi, e la
                # seconda annullava il lavoro della prima.
                if _frase_vera(self.stt.utterances, self._ultima_risposta):
                    # Si ferma tutta la catena, nell'ordine in cui produce:
                    # prima la sintesi (di la' dal socket continuerebbe a
                    # macinare il testo gia' inviato), poi cio' che e' gia'
                    # audio, poi la bocca dell'avatar.
                    self.tts.abort()
                    drain_queue(self.tts.audio)
                    self._ferma_avatar()
                    # Con l'audio in WebRTC la voce non e' piu' "gia' arrivata
                    # all'app": una parte e' nel buffer della traccia, che la
                    # sta pubblicando a tempo reale. Svuotare la coda del TTS
                    # non basta — l'avatar finirebbe la frase per tutti i
                    # secondi che ha davanti, proprio mentre l'utente riprende
                    # la parola. Sul WebSocket il problema non c'era: i byte
                    # erano gia' fuori e li fermava l'app.
                    if self._video is not None:
                        self._video.svuota_audio()
                    self._send_json({"type": "interrupt"})
                else:
                    self._send_json({"type": "turn_done"})
        finally:
            stop.set()
            if motore is not None:
                log.info("Gateway: motore duplex — %s", motore.stato())
                motore.ferma()
            if self._dump_voce is not None:
                try:
                    self._dump_voce.close()
                except Exception:  # noqa: BLE001
                    pass
                self._dump_voce = None
            self._send_json({"type": "done"})
            log.info("Gateway: sessione full-duplex terminata")

    def _capture_utterance(self, mode: str, in_rate: int) -> str:
        """Riceve l'audio dall'app e ritorna l'enunciato trascritto COMPLETO.

        Raccoglie TUTTI i segmenti finalizzati dallo STT fino a una pausa di
        silenzio credibile (`ptt_turn_pause_s`), non solo il primo:
          hold       -> dopo {end}, attende che lo STT finisca di trascrivere;
          continuous -> chiude il turno dopo la pausa naturale (come Shift+F1).
        
        MIGLIORAMENTO: normalizza l'audio in ingresso per migliorare la qualità
        della trascrizione STT.
        """
        from .audio_hfp import resample

        quiet = self.cfg.ptt_turn_pause_s
        no_speech_timeout = 15.0     # niente parlato: esci
        post_end_timeout = 8.0       # hold: dopo {end} ma niente trascritto
        t_start = time.monotonic()
        pieces: list[str] = []
        last_utt: float | None = None
        ended = False
        end_time: float | None = None
        # Diagnostica del livello in ingresso: quando l'app "non viene sentita"
        # bisogna poter distinguere un microfono muto da uno STT che sbaglia.
        n_campioni = 0
        picco = 0.0
        somma_q = 0.0
        # Interruttore a file (call-bridge/dump-turni.on) o CB_DUMP_TURNS: tiene
        # una copia su disco dell'audio ricevuto. E' l'unico modo per distinguere
        # "il telefono manda audio sporco" da "lo STT sbaglia", visto che di
        # mezzo c'e' un ricampionamento dentro l'app. Sta su file per poterlo
        # accendere senza riconfigurare il servizio che supervisiona il gateway.
        dump: list[np.ndarray] | None = [] if _dump_attivo() else None
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            # 1) preleva tutti gli enunciati gia' pronti
            try:
                while True:
                    t = self.stt.utterances.get_nowait()
                    if t:
                        pieces.append(t)
                        last_utt = time.monotonic()
            except queue.Empty:
                pass
            # 2) fine turno: pausa di silenzio dopo aver parlato
            if pieces and last_utt and (time.monotonic() - last_utt) >= quiet:
                break
            # 3) timeout di sicurezza se non arriva nulla
            if not pieces:
                if mode != "continuous" and ended and end_time and \
                        (time.monotonic() - end_time) >= post_end_timeout:
                    break
                if (time.monotonic() - t_start) >= no_speech_timeout:
                    break
            # 4) ricevi audio / controllo
            try:
                raw = self.ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001 - chiusura
                break
            if isinstance(raw, (bytes, bytearray)):
                # Converti PCM16 -> float32
                samples = _pcm16_to_f32(bytes(raw))
                # Resample a 16kHz se necessario
                if in_rate != STT_RATE:
                    samples = resample(samples, in_rate, STT_RATE)
                # Normalizza l'audio (rimuovi DC offset, normalizza ampiezza)
                samples = _normalize_audio(samples)
                if samples.size:
                    n_campioni += samples.size
                    picco = max(picco, float(np.max(np.abs(samples))))
                    somma_q += float(np.sum(np.square(samples, dtype=np.float64)))
                    if dump is not None:
                        dump.append(samples.copy())
                self.stt.send_audio(samples)
            else:
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if msg.get("type") == "end":
                    ended = True
                    end_time = time.monotonic()
                    # Pulsante rilasciato: l'enunciato e' finito e lo sappiamo
                    # gia'. Si chiude subito invece di aspettare che il VAD
                    # deduca il silenzio — attesa inutile in walkie-talkie, e
                    # per giunta i 0.5s di zeri che si inviavano prima erano
                    # meno di `stt_endpoint_silence_s` (0.7s): l'enunciato non
                    # si chiudeva affatto e il turno tornava vuoto.
                    self.stt.flush()
                else:
                    # anche a viva voce il video parte col tasto premuto,
                    # quindi `video_start` arriva a turno gia' aperto
                    self._messaggio_collaterale(msg)
        if n_campioni:
            rms = (somma_q / n_campioni) ** 0.5
            log.info("Gateway: turno di %.1fs dal telefono (picco %.3f, rms %.4f)",
                     n_campioni / STT_RATE, picco, rms)
            if picco < 0.02:
                log.warning("Gateway: audio dall'app quasi muto (picco %.3f): "
                            "microfono coperto, permesso negato o guadagno a zero",
                            picco)
        else:
            log.warning("Gateway: nessun campione audio ricevuto dall'app")
        if dump:
            self._salva_dump(np.concatenate(dump))
        return " ".join(pieces).strip()

    @staticmethod
    def _salva_dump(samples: np.ndarray) -> None:
        """Scrive su disco l'audio del turno (WAV 16 kHz), per analisi offline."""
        cartella = Path(__file__).resolve().parents[1] / "turni"
        cartella.mkdir(exist_ok=True)
        nome = cartella / f"turno-{time.strftime('%H%M%S')}.wav"
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        with wave.open(str(nome), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(STT_RATE)
            w.writeframes(pcm.tobytes())
        log.info("Gateway: turno salvato in %s", nome)

    def _emit_visemi(self, seg) -> None:
        """In modo 'visemi', manda la timeline delle forme di bocca per questo
        segmento — testo e durata dell'audio che sta per partire.

        Va chiamato PRIMA di `_send_pcm`: il client riceve i visemi appena
        prima dell'audio corrispondente e li aggancia all'inizio della sua
        riproduzione. Fuori dal modo 'visemi' non fa nulla, quindi si puo'
        chiamare senza guardie sul percorso audio.
        """
        if self._render_mode != "visemi":
            return
        testo = (getattr(seg, "testo", "") or "").strip()
        if not testo:
            return
        dur = len(seg.samples) / max(getattr(seg, "sample_rate", 24000), 1)
        try:
            from .viseme_gen import timeline
            self._send_json(timeline(testo, dur, self._lang_turno))
        except Exception as e:  # noqa: BLE001 - i visemi non devono fermare l'audio
            log.warning("Gateway: visemi non generati (%s)", e)

    def _forward_tts_audio(self, barge_in=None,
                           start_timeout: float = 20.0, quiet: float = 5.0) -> None:
        """Inoltra all'app TUTTI i segmenti audio TTS fino a fine sintesi.

        Attende fino a `start_timeout` il primo segmento (Hermes + sintesi), poi
        inoltra finche' la coda resta vuota per `quiet` secondi. `barge_in` (se
        dato) e' un callable: se ritorna True si interrompe subito.

        `quiet` era 2 s: troppo poco. Il TTS non dice quando ha finito, quindi
        il silenzio fra due segmenti e' l'unico indizio — ma XTTS impiega
        regolarmente piu' di 2 s a sintetizzare la frase seguente, e il turno
        veniva chiuso lasciando indietro il resto della risposta ("alcune parti
        mancano"). A 5 s la coda si svuota davvero; il costo e' un'attesa piu'
        lunga prima di `turn_done`, che in full-duplex non si sente perche' il
        microfono e' gia' aperto.
        
        MODIFICA: invia l'audio in streaming continuo invece che a segmenti,
        evitando il tempo di attesa del download file sull'app Android.
        """
        t0 = time.monotonic()
        first = None
        while time.monotonic() - t0 < start_timeout:
            if barge_in and barge_in():
                self._send_json({"type": "audio_end"})
                return
            try:
                first = self.tts.audio.get(timeout=0.2)
                break
            except queue.Empty:
                continue
        if first is None:
            # Anche senza audio l'app deve uscire dallo stato "parla":
            # senza questo il pulsante restava viola a tempo indeterminato.
            self._send_json({"type": "audio_end"})
            return  # nessun audio (risposta vuota)
        
        # Labiale PRIMA della voce: `_muovi_labbra` ritorna quando i fotogrammi
        # cominciano a scorrere, cosi' i due partono insieme. Al contrario si
        # sentiva la risposta e solo dopo si vedeva muovere la bocca, perche' il
        # worker deve generare i frame mentre l'audio e' gia' pronto.
        # PRIMO segmento: si attende che i fotogrammi partano, cosi' voce e
        # bocca cominciano insieme.
        t_attesa = time.monotonic()
        preambolo = self._muovi_labbra(first, attendi=True)
        attesa = time.monotonic() - t_attesa

        # `audio_start` va mandato QUI, non prima dell'attesa del labiale.
        # Stava sopra, e per l'app significa "sto parlando": il pulsante
        # diventava viola e poi restava muto per tutti i secondi in cui il
        # worker prepara i fotogrammi. Visto dal vivo: "icona passa a viola
        # senza parlare, passano 4-5 secondi, poi l'avatar inizia a parlare".
        # Adesso il viola compare insieme al primo suono, e quei secondi
        # restano arancioni — che e' la verita': si sta ancora preparando.
        # Alla politica dei chunk si passa il preambolo VERO, quello misurato
        # dal worker, non la durata di questa attesa.
        #
        # Erano stati confusi, e il costo era alto. L'attesa qui include il
        # tempo passato in coda dietro le battute gia' accodate nel worker;
        # misurato con tools/misura-labiale.py: MuseTalk prepara una battuta in
        # 0,10-0,17 s, ma la stessa POST costa 2 s quando la coda non e' vuota.
        # Siccome `min_caratteri()` cresce con questo numero e battute piu'
        # lunghe allungano la coda, il circolo si chiudeva su se stesso: nei log
        # si leggono preamboli da 6 a 12 secondi e soglie da 200-385 caratteri,
        # cioe' risposte che per venti secondi non si potevano interrompere.
        #
        # Zero (avatar spento) resta un dato valido, non un campione da saltare:
        # senza labiale i chunk tornano corti, il che migliora l'interruzione.
        chunk_policy.osserva_labiale(preambolo)
        if attesa > 0.5:
            # Si registrano ENTRAMBI: l'attesa e' cio' che l'utente subisce, il
            # preambolo e' cio' che il worker ha davvero impiegato. Quando i due
            # divergono molto, la differenza e' coda — cioe' battute precedenti
            # ancora in lavorazione, non lentezza di MuseTalk.
            log.info("Gateway: %.1fs di attesa per l'avvio del labiale "
                     "(preambolo vero %.2fs, il resto e' coda); "
                     "politica chunk: %s", attesa, preambolo,
                     chunk_policy.stato())
        self._send_json({"type": "audio_start", "sample_rate": first.sample_rate})
        self._emit_visemi(first)
        self._send_pcm(first.samples, first.sample_rate)
        t_inizio = time.monotonic()
        inviato_s = len(first.samples) / max(first.sample_rate, 1)
        last = time.monotonic()
        while True:
            if barge_in and barge_in():
                break
            try:
                seg = self.tts.audio.get(timeout=0.2)
                # Segmenti SUCCESSIVI: non si aspetta. Il worker impiega ~3 s
                # in piu' della durata dell'audio per ogni battuta (feature
                # whisper + generazione), e aspettarne l'avvio accumulava quel
                # ritardo su ogni frase: la voce si fermava e riprendeva molto
                # dopo. Il worker le accoda da solo; la voce scorre e la bocca
                # insegue, che e' il compromesso giusto in conversazione.
                self._muovi_labbra(seg, attendi=False)
                self._emit_visemi(seg)
                self._send_pcm(seg.samples, seg.sample_rate)
                inviato_s += len(seg.samples) / max(seg.sample_rate, 1)
                last = time.monotonic()
            except queue.Empty:
                if time.monotonic() - last >= quiet:
                    # `quiet` da solo chiudeva il turno mentre la sintesi era
                    # solo lenta: i segmenti rimasti in coda finivano poi nel
                    # turno DOPO, sentito come "non legge tutta la risposta e
                    # al turno successivo legge la parte che manca". Si chiude
                    # solo se l'audio ottenuto e' vicino a quello che il testo
                    # mandato lascia prevedere.
                    attesi = getattr(self, "_caratteri_tts", 0)
                    atteso_s = (attesi / chunk_policy.caratteri_al_secondo()
                                if attesi else 0.0)
                    if atteso_s and inviato_s < atteso_s * 0.8                             and time.monotonic() - t_inizio < atteso_s * 3 + 30:
                        continue
                    if atteso_s and inviato_s < atteso_s * 0.8:
                        log.warning("Gateway: turno chiuso incompleto, %.1fs di "
                                    "audio contro %.1fs attesi da %d caratteri",
                                    inviato_s, atteso_s, attesi)
                    break
        
        # Invia audio_end solo alla FINE dello streaming
        self._send_json({"type": "audio_end"})

    def _crea_motore(self):
        """Il motore di consegna, se acceso in configurazione. Altrimenti None.

        Sta dietro un interruttore perche' sostituisce il percorso audio di una
        conversazione intera: finche' non ha superato le prove sul campo, il
        vecchio deve restare raggiungibile. E' un interruttore temporaneo, non
        una configurazione da mantenere: due modi di fare la stessa cosa sono
        due cose da provare per sempre.
        """
        if not getattr(self.cfg, "duplex_motore_nuovo", False):
            return None
        from .duplex_engine import (ANTICIPO_WEBRTC_S, ANTICIPO_WS_S,
                                    MotoreDuplex)

        # Con la traccia WebRTC si sa quanto audio e' ancora in coda dall'altra
        # parte, quindi si puo' tenere l'anticipo piu' corto.
        con_traccia = (self._video is not None
                       and getattr(self._video, "audio_attivo", False))
        motore = MotoreDuplex(
            self.tts,
            consegna=lambda pcm, rate: self._send_pcm(pcm, rate),
            muovi_labbra=self._labiale_da_pcm,
            anticipo_s=ANTICIPO_WEBRTC_S if con_traccia else ANTICIPO_WS_S,
            su_dump=self._registra_voce,
        )
        motore.avvia()
        log.info("Gateway: motore duplex acceso (anticipo %.2fs, %s)",
                 motore._anticipo, "traccia WebRTC" if con_traccia else "PCM sul WS")
        return motore

    def _ciclo_orecchie(self, motore, stop) -> None:
        """Abbassa la voce appena l'utente apre bocca, e la rialza da solo.

        `speech_start` esisteva gia' e non lo consumava nessuno: lo STT lo
        emette ~100 ms dopo l'inizio della voce, cioe' molto prima della
        trascrizione (che con l'attesa di fine frase costa quasi un secondo).
        E' il segnale giusto per il ducking proprio perche' non sa ancora COSA
        e' stato detto: abbassare la voce non e' una decisione, e' cortesia.
        Decidere — ignorare, fermarsi, accodare, integrare — viene dopo, con la
        frase in mano.

        Il rilascio e' a tempo: se nei 2,5 secondi dopo non arriva nessun
        enunciato, era un rumore e si risale. Cosi' l'unico caso in cui la voce
        resta bassa e' quello in cui l'utente sta davvero parlando.

        Nota: finche' non c'e' cancellazione d'eco (fase 6), a vivavoce questo
        scatta anche sulla PROPRIA voce che rientra dal microfono. E' il caso
        in cui il danno e' minimo — la voce si abbassa di 12 dB e risale da
        sola dopo 2,5 s — e in cui il rimedio, anzi, aiuta: meno eco da
        cancellare.
        """
        ultimo = 0.0
        while not stop.is_set():
            try:
                self.stt.speech_events.get(timeout=0.2)
                ultimo = time.monotonic()
                if not motore.abbassato:
                    motore.abbassa()
                    log.info("Gateway: voce abbassata (l'utente parla)")
            except queue.Empty:
                if motore.abbassato and time.monotonic() - ultimo > 2.5:
                    motore.rialza()
                    log.info("Gateway: voce di nuovo piena")

    def _attendi_motore(self, motore, bargein) -> bool:
        """Accompagna la risposta fino in fondo, decidendo cosa fare di ciò che
        l'utente dice nel frattempo. Ritorna True se si è stati fermati.

        Qui sta la differenza fra questo e un barge-in: le frasi che arrivano
        mentre l'avatar parla non si dividono in "interrompe / non interrompe",
        ma nei quattro esiti di `interruption_policy`. Si consumano davvero
        dalla coda — una frase giudicata qui non deve essere giudicata una
        seconda volta a fine turno — e quelle da rimandare finiscono in
        `_da_trattare`, che il ciclo dei turni svuota prima di riascoltare il
        microfono.

        Non c'e' un timeout sulla durata: a fermare il turno sono la fine della
        risposta o l'utente. C'e' invece una guardia sul silenzio prolungato,
        perche' un TTS che non consegna piu' niente non deve lasciare la
        sessione appesa.
        """
        fermo_da = time.monotonic()
        consegnati = motore.consegnati_s
        while True:
            if bargein and bargein(solo_stop=True):
                motore.taglia(anche_in_corso=True)
                self._ferma_avatar()
                if self._video is not None:
                    self._video.svuota_audio()
                return True
            testo = self._prossima_frase()
            if testo:
                esito, perche = classifica(
                    testo,
                    detto=motore.copione.detto(6.0),
                    anticipo_s=motore.copione.anticipo_s(),
                    integra_da_s=getattr(self.cfg, "duplex_integra_da_s", 2.5),
                    # Solo col cervello `chat`: Hermes puo' aver gia' eseguito
                    # azioni, e riscrivere il seguito di un agente e' un
                    # problema di un'altra categoria.
                    consenti_cambia=(getattr(self.cfg, "duplex_integra", False)
                                     and hasattr(self.brain,
                                                 "respond_continuazione")))
                log.info("Gateway: sopra la voce %r -> %s (%s, %.1fs in casa)",
                         testo[:48], esito.value, perche,
                         motore.copione.anticipo_s())
                if esito is Esito.IGNORA:
                    # Era un cenno o la propria eco: si torna al volume pieno,
                    # altrimenti la voce resterebbe bassa per 2,5s a ogni "sì".
                    motore.rialza()
                    if perche.startswith("eco") and self._conta_eco():
                        motore.taglia(anche_in_corso=True)
                        self._ferma_avatar()
                        return True
                elif esito is Esito.FERMA:
                    # Si taglia a voce gia' abbassata: il ducking e' partito
                    # ~un secondo fa, sul `speech_start`. Rialzarla qui la
                    # farebbe risalire per un istante prima di tacere.
                    motore.taglia(anche_in_corso=True)
                    self._ferma_avatar()
                    if self._video is not None:
                        self._video.svuota_audio()
                    self._da_trattare.append(testo)
                    log.info("Gateway: interrotto — %s", motore.stato())
                    return True
                elif esito is Esito.CAMBIA:
                    if not self._integra(motore, testo):
                        self._da_trattare.append(testo)
                else:
                    # ACCODA: l'avatar finisce quel che stava dicendo e poi
                    # risponde.
                    self._da_trattare.append(testo)
            if motore.consegnati_s > consegnati + 1e-6:
                consegnati = motore.consegnati_s
                fermo_da = time.monotonic()
            # "Il cervello ha finito" non basta: il TTS sta ancora
            # sintetizzando il testo che ha gia' ricevuto. Con la sintesi a
            # 1,46x il tempo reale, fra l'ultima parola scritta e l'ultimo
            # campione prodotto passano secondi. Si aspetta che il TTS taccia
            # davvero — la stessa guardia, e per la stessa ragione, che
            # `_forward_tts_audio` chiama `quiet`.
            # La prima battuta di una sessione puo' tardare molto piu' delle
            # successive: se l'avatar parla con una voce XTTS, quel primo
            # `send_text` carica il modello. Dopo, la sintesi e' regolare e
            # cinque secondi di silenzio vogliono davvero dire "ha finito".
            quiete = 5.0 if motore.battute_ricevute else 45.0
            finita = (motore._risposta_finita.is_set()
                      and motore.copione.anticipo_s() <= 1e-6
                      and motore.silenzio_dal_tts() > quiete)
            if finita:
                return False
            if time.monotonic() - fermo_da > (25.0 if motore.battute_ricevute
                                              else 60.0):
                log.warning("Gateway: nessun audio da 25s — %s", motore.stato())
                return False
            time.sleep(0.05)

    def _integra(self, motore, nuova: str) -> bool:
        """Riscrive il seguito della risposta tenendo conto dell'interruzione.

        È il caso in cui l'utente non vuole fermarti e non vuole aspettare: ha
        detto qualcosa che riguarda ciò che stai per dire. Si può fare solo
        perché la generazione corre più veloce del parlato — misurato, 1,46x —
        e perché il Copione tiene in casa il non-ancora-detto: si taglia il
        futuro, si lascia finire la frase in corso (i secondi che servono al
        cervello per scrivere il seguito) e si sostituisce il resto.

        Solo col cervello `chat`: con Hermes l'agente può aver già eseguito
        azioni, e riscriverne la continuazione è un problema di un'altra
        categoria. Ritorna False quando non si può fare, e il chiamante
        degrada ad ACCODA.
        """
        brain = self.brain
        if not hasattr(brain, "respond_continuazione"):
            return False
        detto = motore.copione.detto()
        previsto = motore.copione.previsto()
        if not previsto.strip():
            # Niente da riscrivere: era già tutto fuori.
            return False
        scartate = motore.riscrivi()
        try:
            brain.correggi_ultima_risposta(detto)
        except Exception as e:  # noqa: BLE001 - la storia non deve far cadere il turno
            log.warning("Gateway: storia non corretta: %s", e)
        # L'app mostra il testo generato: senza questo resterebbe a schermo
        # un seguito che nessuno pronuncerà mai.
        # Lo spazio in coda non e' un vezzo: l'app riparte da questo testo e
        # ci accoda i pezzi della continuazione, e senza separatore la
        # giunzione si legge "Prima di tutto,Perfetto".
        self._send_json({"type": "reply_reset",
                         "text": detto + (" " if detto else "")})
        log.info("Gateway: riscrivo il seguito (%d battute in riserva) — %r",
                 len(scartate), nuova[:48])

        proc = ReplyProcessor(extract_media=True, continuazione=True,
                              min_caratteri=DUPLEX_MIN_CARATTERI)
        self._ultima_risposta = detto

        def _emit(rc) -> None:
            for kind, src in rc.media:
                self._emit_media(kind, src)
            if rc.app_text:
                self._ultima_risposta += " " + rc.app_text
                self._send_json({"type": "reply", "chunk": rc.app_text})
            for frase in rc.tts_sentences:
                self._caratteri_tts += len(frase)
                motore.dillo(frase)

        def _scrivi() -> None:
            try:
                for chunk in brain.respond_continuazione(detto, previsto, nuova):
                    if isinstance(chunk, str):
                        _emit(proc.feed(chunk))
                _emit(proc.flush())
            except Exception as e:  # noqa: BLE001 - errore del cervello
                log.error("Gateway: continuazione fallita: %s", e)
            finally:
                motore.fine_risposta()

        # Si scrive in un thread perche' nel frattempo la bocca deve poter
        # ripescare dalla riserva: e' la regola del doppio copione, ed e' cio'
        # che impedisce all'avatar di ammutolire se la riscrittura tarda.
        threading.Thread(target=_scrivi, name="gw-continuazione",
                         daemon=True).start()
        return True

    def _conta_eco(self) -> bool:
        """Registra un'eco riconosciuta; True se conviene tornare ai turni.

        Tre eco in una sessione vogliono dire che il microfono risente
        l'altoparlante abbastanza da farsi trascrivere: il filtro testuale le
        ha prese tutte, ma e' una rete a valle, e basta una frase in cui
        l'avatar dice qualcosa di diverso da quel che si sente perche' passi.
        A quel punto la conversazione continua diventa un'altalena di
        auto-interruzioni, e i turni sono semplicemente meglio.

        Si degrada da soli e lo si dice: e' una misura, non una preferenza.
        """
        self._eco_viste += 1
        if self._eco_viste < 3:
            return False
        log.warning("Gateway: torno ai turni — l'avatar si risente dal "
                    "microfono (%d eco riconosciute)", self._eco_viste)
        self._send_json({
            "type": "conversazione", "modo": "turni",
            "motivo": "L'avatar si risente dal microfono: torno ai turni. "
                      "Con auricolare o cuffie puoi riattivare la modalità "
                      "continua."})
        return True

    def _prossima_frase(self) -> str:
        """Toglie dalla coda la prossima trascrizione, "" se non ce n'e'.

        A differenza di `_frase_vera` questa CONSUMA: la frase viene giudicata
        subito e il suo destino deciso una volta sola. `_frase_vera` guardava
        senza togliere perche' non aveva un posto dove mettere cio' che non
        interrompe; adesso quel posto e' `_da_trattare`.
        """
        try:
            testo = self.stt.utterances.get_nowait()
        except queue.Empty:
            return ""
        return testo.strip() if isinstance(testo, str) else ""

    def _labiale_da_pcm(self, pcm, rate: int, attendi: bool) -> float:
        """Adatta la firma del labiale a quella che il motore si aspetta."""
        from types import SimpleNamespace
        return self._muovi_labbra(
            SimpleNamespace(samples=pcm, sample_rate=rate), attendi=attendi)

    def _registra_voce(self, pcm, rate: int) -> None:
        """Scrive su file la voce DAVVERO consegnata, se `dump-voce.on` esiste.

        Gemello di `dump-turni.on`, che registra cio' che entra. Qui si registra
        cio' che esce, dopo tagli e — piu' avanti — dopo il ducking: e' l'unico
        modo di giudicare a orecchio quello che il motore combina. Una giunzione
        stonata o un silenzio di troppo non si vedono in nessun log.
        """
        if not self._dump_voce_attivo:
            return
        try:
            if self._dump_voce is None:
                import wave
                base = Path(__file__).resolve().parents[1]
                cartella = base / "voce"
                cartella.mkdir(exist_ok=True)
                nome = cartella / f"voce-{time.strftime('%H%M%S')}.wav"
                w = wave.open(str(nome), "wb")
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                self._dump_voce = w
                log.info("Gateway: registro la voce consegnata in %s", nome)
            self._dump_voce.writeframes(_f32_to_pcm16(pcm))
        except Exception as e:  # noqa: BLE001 - la registrazione non deve disturbare
            log.debug("Gateway: registrazione voce fallita: %s", e)
            self._dump_voce_attivo = False

    def _ferma_avatar(self) -> None:
        """Interrompe il labiale, comprese le battute gia' accodate nel worker.

        Il gateway non lo faceva affatto: al barge-in svuotava la coda audio e
        avvisava l'app, ma il worker continuava ad animare tutti i WAV gia'
        inviati. L'audio non si sentiva (`mute=True`), quindi il difetto era
        silenzioso — ma la bocca continuava a pronunciare una risposta
        abbandonata, e il labiale del turno successivo si accodava dietro.
        """
        try:
            requests.post(
                f"http://127.0.0.1:{self.cfg.control_port}/avatar/interrupt",
                json={"svuota": True}, timeout=4)
        except Exception as e:  # noqa: BLE001 - l'interruzione non deve fallire qui
            log.debug("Gateway: avatar non interrotto: %s", e)

    def _muovi_labbra(self, seg, attendi: bool = True) -> None:
        """Fa animare all'avatar la battuta gia' inviata al telefono.

        Senza questo l'avatar resta un video di sottofondo: gira in idle mentre
        la voce arriva dall'app, e non muove mai la bocca. Si passa il WAV al
        worker in modalita' muta — la voce l'ha gia' sentita l'utente dal
        telefono. Non blocca il turno: se il labiale fallisce, l'audio e' gia'
        partito e la conversazione va avanti lo stesso.
        """
        if self._video is None:
            # 0.0 e non None: il chiamante passa questo valore a
            # `chunk_policy.osserva_labiale`, che lo confronta con una soglia.
            # Zero e' anche il dato giusto: senza avatar il preambolo non c'e'.
            return 0.0
        try:
            cartella = Path(tempfile.gettempdir()) / "callapicall-lipsync"
            cartella.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=cartella, prefix="seg-",
                                             suffix=".wav", delete=False) as f:
                percorso = f.name
            with wave.open(percorso, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(seg.sample_rate)
                w.writeframes(_f32_to_pcm16(seg.samples))
            # Il timeout supera l'attesa chiesta al bridge: se scadesse prima,
            # si tornerebbe qui a mandare la voce mentre il video sta ancora
            # per partire — cioe' proprio lo sfasamento da evitare.
            r = requests.post(
                f"http://127.0.0.1:{self.cfg.control_port}/avatar/lipsync",
                json={"wav": percorso, "mute": True,
                      "attendi_avvio": bool(attendi), "attesa_max_s": 8.0},
                timeout=12 if attendi else 4)
            return float((r.json() or {}).get("preparazione_s") or 0.0)
        except Exception as e:  # noqa: BLE001 - il turno non deve morire qui
            log.warning("Gateway: labiale non inviato all'avatar: %s", e)
        return 0.0

    def _send_pcm(self, samples, rate: int = 24000,
                  chunk_bytes: int = 8192) -> None:
        """Manda la voce all'app: traccia WebRTC se c'e', altrimenti PCM sul WS.

        Con la traccia audio il vantaggio non e' la banda (Opus a 24-32 kbit/s
        contro i 384 del PCM16 a 24 kHz, comunque poco su una rete casalinga):
        e' che la sincronia col labiale la fa il telefono, sui timestamp RTP e
        i rapporti RTCP, invece di dipendere da quando i frame binari escono da
        questo WebSocket. E l'app non deve piu' accodare campioni a mano in un
        AudioContext, dove un ritardo di rete si sente come un buco.

        Il PCM resta per le app che non offrono audio nella loro SDP: la
        compatibilita' non e' un ripiego teorico, e' il caso di chiunque non
        abbia ancora aggiornato.
        """
        video = self._video
        if video is not None and video.scrivi_audio(samples, rate):
            return
        pcm = _f32_to_pcm16(samples)
        if len(pcm) <= chunk_bytes:
            self.ws.send(pcm)
            return
        for i in range(0, len(pcm), chunk_bytes):
            self.ws.send(pcm[i:i + chunk_bytes])

    def _close(self) -> None:
        if self.stt is not None:
            self.stt.close()
        if self.tts is not None:
            self.tts.close()


def run_gateway(cfg: Config) -> None:
    """Avvia il voice gateway WebSocket (bloccante)."""
    from websockets.sync.server import serve

    from .media import start_media_server

    # Server HTTP per i media (immagini/video generati da Hermes).
    try:
        start_media_server(cfg.gateway_host, cfg.gateway_media_port,
                           cfg.avatar_dir, cfg.control_port)
    except OSError as e:  # porta occupata: i media non funzioneranno, ma la voce sì
        log.error("Media server non avviato (%s): media disabilitati", e)

    brain = make_full_brain(cfg, label="gateway")
    # Cervello della voce DIRETTA, lo stesso di Ctrl+Shift+F4 sul PC. Si crea
    # qui una volta sola: e' leggero (nessun processo agente) e senza di lui
    # l'app che chiede "chat" veniva servita comunque dall'agente completo,
    # che con l'avvio dell'ACP puo' metterci minuti.
    try:
        brain_chat = make_lmstudio_brain(cfg)
    except Exception as e:  # noqa: BLE001 - l'app resta usabile con l'agente
        log.warning("Gateway: cervello chat non disponibile: %s", e)
        brain_chat = None

    def handler(ws):
        try:
            _Connection(ws, cfg, brain, brain_chat).run()
        except Exception as e:  # noqa: BLE001 - non far cadere il server
            log.error("Gateway: errore connessione: %s", e)

    log.info("Voice gateway in ascolto su ws://%s:%d", cfg.gateway_host, cfg.gateway_port)
    with serve(handler, cfg.gateway_host, cfg.gateway_port, max_size=None) as server:
        server.serve_forever()
