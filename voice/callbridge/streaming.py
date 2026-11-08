"""Sessione di streaming audio <-> STT/TTS <-> Hermes.

Modello a thread condiviso tra le telefonate (`CallSession`, vedi
orchestrator.py) e il push-to-talk (`PushToTalkSession`, vedi ptt.py):

  - downlink: audio in  -> (resample 16 kHz) -> Handy STT WebSocket
  - brain:    enunciato STT -> Hermes (streaming) -> Handy TTS WebSocket
  - uplink:   Handy TTS WebSocket -> (resample) -> audio out

Le sottoclassi possono aggiungere thread extra tramite `_extra_threads()`
(es. il watcher di stato della chiamata).

## Barge-in (Task #7)

Se l'interlocutore parla mentre il TTS sta riproducendo una risposta:
  - il downlink, durante il TTS, inoltra allo STT solo l'audio sopra una
    soglia di energia (filtra l'eco residua del TTS — euristica, non un AEC
    vero: vedi i limiti noti);
  - se ne risulta un nuovo enunciato, il brain interrompe la risposta in
    corso (svuota la coda audio del TTS e abortisce il segmento corrente) e
    processa subito il nuovo enunciato.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time

import numpy as np

from .audio_hfp import HfpAudioIO, resample
from .config import Config
from .stt_client import STT_RATE, HandySttClient
from .tts_client import HandyTtsClient
from .turn_engine import ReplyProcessor, configure_tts
from .interruption_policy import Esito, classifica
from .util import coalesce_utterances, drain_queue
from .voice_commands import (
    LANG_TABLE, code_for_kokoro_lang, detect_language, parse_command, voice_for,
)

log = logging.getLogger("callbridge.streaming")


def _rms(samples: np.ndarray) -> float:
    """Energia RMS di un blocco audio float32."""
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


class StreamingSession:
    """Loop di streaming bidirezionale audio <-> Handy <-> Hermes.

    `in_idx` / `out_idx` sono indici di device sounddevice (`None` = device
    di default del sistema operativo). `hermes` e' duck-typed:
    `respond(text) -> Iterator[str]`.
    """

    # Comandi vocali (cambio lingua, nuova conversazione) e auto-rilevamento
    # lingua: attivi SOLO nelle sessioni push-to-talk (le sottoclassi PTT lo
    # abilitano). Nelle telefonate resta False (la lingua la decide il prefisso).
    voice_commands_enabled = False
    # Al cambio lingua mantieni la voce corrente (True) invece di scegliere la
    # voce standard della lingua: usato dall'avatar per non perdere il timbro
    # clonato della persona (cambia solo la lingua di sintesi/risposta).
    keep_voice = False
    # Streaming TTS frase-per-frase: sintetizza/riproduce ogni frase mentre
    # l'agente genera il resto. DISATTIVATO di default: la sintesi per-frase
    # aspetta la generazione, quindi tra le frasi la coda audio si svuota e
    # l'OutputStream va in underrun -> glitch "uhap-uhap"/sovrapposizione al
    # confine. L'accumulo (False) sintetizza tutto di fila piu' veloce della
    # riproduzione -> audio liscio. Il lag reale era il warm-up del modello (gia'
    # risolto), non l'output. Riattivare solo con un jitter-buffer (silenzio di
    # riempimento tra le frasi) che tenga fed l'OutputStream.
    stream_tts = False

    def __init__(
        self,
        config: Config,
        hermes,
        in_idx: int | None,
        out_idx: int | None,
        audio_rate: int,
        voice: str,
        lang: str,
        half_duplex: bool = False,
        voice_engine: str = "",
    ) -> None:
        self.cfg = config
        self.hermes = hermes
        self.in_idx = in_idx
        self.out_idx = out_idx
        self.audio_rate = audio_rate
        self.voice = voice
        # Motore con cui pronunciare `voice` ("" = lo deduce il router dal nome).
        # Le voci clonate hanno lo STESSO nome su piu' motori: senza questo, la
        # persona che ha scelto XTTS veniva fatta parlare da un altro.
        self.voice_engine = (voice_engine or "").strip()
        self.lang = lang
        # Codice lingua interno corrente (chiave di LANG_TABLE), per confronto
        # con l'auto-rilevamento. Ricavato dalla lang Kokoro iniziale.
        self._lang_code = code_for_kokoro_lang(lang)
        # half_duplex: mentre il TTS riproduce, NON inoltrare audio allo STT
        # (evita che il microfono catturi l'audio del TTS dalle casse). Usato
        # dal push-to-talk con le casse; le chiamate restano full-duplex.
        self.half_duplex = half_duplex
        self._stop = threading.Event()
        # Il guadagno dell'uscita: la stessa classe del motore del gateway.
        # Quando l'utente comincia a parlare la voce si abbassa invece di
        # fermarsi — e su un PC a casse questo vale doppio, perche' meno
        # volume vuol dire anche meno eco da cancellare.
        from .duplex_engine import Ducking
        self._duck = Ducking()
        self._ultimo_parlato = 0.0
        # _tts_active: il TTS sta riproducendo audio in questo momento.
        self._tts_active = threading.Event()
        # _processing: turno in elaborazione (half-duplex). Set dal momento in
        # cui l'enunciato viene inviato a Hermes fino a fine riproduzione audio.
        # Mentre e' attivo il microfono e' muto (non si ascolta).
        self._processing = threading.Event()
        # _barge_in: richiesta di abortire il segmento TTS in riproduzione.
        self._barge_in = threading.Event()
        # _listening: il microfono e' attivo e gli enunciati vengono accettati.
        # Falso durante reasoning + TTS + post-TTS warmup. Diverso da _processing
        # perche' resta False anche nella fase di "calibrazione" post-TTS dove
        # il mic ascolta ma il brain ignora i primi enunciati.
        self._listening = threading.Event()
        self._listening.set()  # inizialmente attivo
        # Testo dell'ultima risposta pronunciata: serve alla guardia anti-eco
        # (se un enunciato coincide con quanto appena detto = TTS rientrato dal
        # microfono, non e' l'utente).
        self._last_response = ""
        # _cooldown: subito DOPO il TTS il mic resta muto un attimo (coda audio),
        # ma NON e' piu' "elaborazione": l'overlay deve mostrare "ascolto".
        self._cooldown = threading.Event()
        # Cambio lingua chiesto DA FUORI (pannello/GUI): non si applica sul
        # thread del chiamante — il TTS va riconfigurato dal thread `_brain`,
        # che lo possiede. Qui si deposita solo la richiesta.
        self._lang_request: str | None = None
        self._threads: list[threading.Thread] = []

    # --- lingua: leggibile e impostabile dall'esterno ----------------------

    @property
    def language(self) -> str:
        """Codice lingua corrente (chiave di LANG_TABLE, es. "it").

        Cambia anche da solo — comando vocale o auto-rilevamento — quindi chi
        mostra un selettore deve rileggerlo, non fidarsi dell'ultimo valore
        impostato."""
        return self._lang_code

    def request_language(self, code: str) -> bool:
        """Chiede il passaggio a `code`. False se la lingua non è supportata.

        Applicata dal thread `_brain` entro ~0,5s (vedi `_lang_request`)."""
        code = (code or "").strip().lower()
        if code not in LANG_TABLE:
            return False
        self._lang_request = code
        return True

    @property
    def phase(self) -> str:
        """Fase corrente, derivata dagli Event interni. API pubblica cosi' GUI
        e overlay non leggono attributi privati (_tts_active/_processing/...):
        speaking > reasoning > cooldown/listening."""
        if self._tts_active.is_set():
            return "speaking"
        if self._processing.is_set():
            return "reasoning"
        return "listening"

    @staticmethod
    def _words(s: str) -> list[str]:
        return re.findall(r"\w+", s.lower())

    def _looks_like_echo(self, utterance: str) -> bool:
        """True se l'enunciato e' l'eco del TTS appena riprodotto.

        Criterio PRUDENTE (per non scartare frasi vere dell'utente che per caso
        condividono vocabolario): l'enunciato e' eco se e' contenuto quasi
        verbatim nell'ultima risposta (sottostringa di parole), oppure se e'
        lungo e ricalca quasi tutte le parole nell'ordine.

        L'eco che RIENTRA DALLE CASSE arriva pero' storpiata dallo STT ("le mie
        risposte" -> "le mie risponde"): il confronto per parole intere la
        mancava e la risposta dell'avatar tornava dentro come turno dell'utente,
        innescando il loop in cui parlava da solo. Per gli enunciati lunghi si
        usa quindi anche una similarita' fuzzy sui caratteri, che tollera le
        parole sbagliate senza allentare il criterio sulle frasi corte (dove il
        rischio di scartare l'utente vero sarebbe alto).
        """
        uw = self._words(utterance)
        if len(uw) < 3 or not self._last_response:
            return False
        u = " ".join(uw)
        r = " ".join(self._words(self._last_response))
        if u in r:                      # eco verbatim (anche parziale)
            return True
        last = set(self._words(self._last_response))
        overlap = sum(1 for w in uw if w in last) / len(uw)
        if overlap >= 0.85 and len(uw) >= 6:
            return True
        # Enunciati lunghi: confronto tollerante alle storpiature. La soglia
        # scende con la lunghezza perche' piu' l'enunciato e' lungo, meno e'
        # plausibile che somigli per caso a quanto appena detto dall'avatar.
        if len(uw) >= 12:
            from difflib import SequenceMatcher
            ratio = SequenceMatcher(None, u, r).find_longest_match(
                0, len(u), 0, len(r)).size / max(len(u), 1)
            if ratio >= 0.35 or overlap >= 0.6:
                log.debug("Eco riconosciuta per similarita' (ratio=%.2f overlap=%.2f)",
                          ratio, overlap)
                return True
        return False

    def run(self) -> None:
        """Esegue la sessione fino a `stop()` (bloccante)."""
        # Factory: client nativo (in-process) o Handy (WS), secondo il backend
        # scelto in config. Interfaccia identica -> il resto di run() non cambia.
        from .engines import make_stt_client, make_tts_client
        stt = make_stt_client(self.cfg)
        tts = make_tts_client(self.cfg)
        try:
            stt.start()
            tts.start()
            configure_tts(tts, self.cfg, self.voice, self.lang, self.voice_engine)
            # Allinea la lingua di RISPOSTA dell'agente a quella della sessione
            # (il preambolo ACP di default e' cablato sull'italiano): cosi' "la
            # lingua indicata nell'interfaccia" viene davvero usata. Fatto per
            # OGNI sessione (anche telefonate) perche' l'agente ACP e' condiviso:
            # evita che una sessione erediti la lingua di quella precedente.
            self._set_reply_language(self._lang_code)
        except Exception as e:  # noqa: BLE001
            # Qualunque errore in apertura: chiudi ENTRAMBI i WebSocket (uno dei
            # due potrebbe essersi aperto) per non lasciare connessioni/thread orfani.
            log.error("Apertura dei WebSocket di Handy fallita: %s", e)
            stt.close()
            tts.close()
            return

        # Cancellatore d'eco: senza, in vivavoce il microfono risente le casse
        # e lo STT trascrive la voce dell'avatar — misurato, la frase intera a
        # qualunque volume. Se il venv dedicato non c'e' o l'helper non parte,
        # `avvia()` ritorna False e l'audio prosegue come prima.
        aec = None
        if getattr(self.cfg, "aec_pc", True):
            from .aec_client import CancellatoreEco
            aec = CancellatoreEco(
                ritardo_ms=int(getattr(self.cfg, "aec_ritardo_ms", 60)))
            if not aec.avvia():
                aec = None
        with HfpAudioIO(self.in_idx, self.out_idx, self.audio_rate,
                        self.cfg.block_ms, aec=aec) as audio:
            self._threads = [
                threading.Thread(target=self._downlink, args=(audio, stt),
                                 name="downlink", daemon=True),
                threading.Thread(target=self._brain, args=(stt, tts),
                                 name="brain", daemon=True),
                threading.Thread(target=self._uplink, args=(audio, tts),
                                 name="uplink", daemon=True),
            ]
            self._threads.extend(self._extra_threads())
            for t in self._threads:
                t.start()
            log.info("Sessione di streaming avviata (audio %d Hz)", self.audio_rate)

            # Attende la fine: _stop e' impostato da stop() o da un thread extra.
            self._stop.wait()
            log.info("Sessione di streaming in chiusura")

            # Ferma i thread PRIMA di chiudere gli stream audio.
            for t in self._threads:
                t.join(timeout=2.0)

        if aec is not None:
            aec.ferma()
        stt.close()
        tts.close()
        log.info("Sessione di streaming terminata")

    def stop(self) -> None:
        """Richiede la chiusura della sessione."""
        self._stop.set()

    def _extra_threads(self) -> list[threading.Thread]:
        """Thread aggiuntivi specifici della sottoclasse (default: nessuno)."""
        return []

    # --- thread di lavoro -------------------------------------------------

    def _downlink(self, audio: HfpAudioIO, stt: HandySttClient) -> None:
        """Audio in -> (resample 16 kHz) -> Handy STT.

        Durante il TTS inoltra solo l'audio sopra la soglia di energia, per
        non rimandare allo STT l'eco del TTS (euristica anti-eco — Task #7).
        
        Half-duplex: due fasi dopo il TTS:
          1) _processing set → mic completamente muto (niente audio allo STT)
          2) _processing clear, _listening clear → mic attivo per calibrazione
             rumore di fondo, ma il brain scarterà i primi enunciati (eco)
        """
        threshold = self.cfg.barge_in_energy_threshold
        gate = self.cfg.barge_in_enabled
        while not self._stop.is_set():
            try:
                chunk = audio.read()
            except Exception as e:  # noqa: BLE001 - stream chiuso o errore device
                log.warning("downlink: lettura fallita: %s", e)
                break
            if self.half_duplex:
                # Half-duplex: mic muto mentre si elabora (_processing) e nella
                # breve coda post-TTS (_cooldown), per non ricatturare l'audio.
                if self._processing.is_set() or self._cooldown.is_set():
                    continue
            elif gate and self._tts_active.is_set() and _rms(chunk) < threshold:
                # Full-duplex: filtra solo l'eco sotto soglia (barge-in attivo).
                continue
            elif self._tts_active.is_set() and _rms(chunk) >= threshold:
                # Qualcuno parla sopra l'avatar: si abbassa la voce subito,
                # senza aspettare di sapere COSA ha detto. Abbassare non e' una
                # decisione, e' cortesia; decidere viene dopo, con la frase in
                # mano. (Sul PC non c'e' `speech_start`: la soglia di energia e'
                # l'equivalente piu' vicino, e arriva altrettanto presto.)
                self._ultimo_parlato = time.monotonic()
                if not self._duck.abbassato:
                    self._duck.abbassa()
                    log.info("Voce abbassata: l'utente parla")
            if self.audio_rate != STT_RATE:
                chunk = resample(chunk, self.audio_rate, STT_RATE)
            stt.send_audio(chunk)

    def _brain(self, stt: HandySttClient, tts: HandyTtsClient) -> None:
        """Enunciato STT -> Hermes (streaming) -> Handy TTS, con barge-in."""
        while not self._stop.is_set():
            # Cambio lingua chiesto dal pannello/GUI: si applica QUI, sul thread
            # che possiede il TTS, e anche quando non si sta parlando — se
            # aspettasse il prossimo enunciato, la lingua cambierebbe solo dopo
            # aver parlato ancora nella vecchia.
            richiesta, self._lang_request = self._lang_request, None
            if richiesta:
                if self._apply_language(tts, richiesta, None) is None:
                    log.warning("Cambio lingua ignorato: '%s' non supportata",
                                richiesta)
            try:
                utterance = stt.utterances.get(timeout=0.5)
            except queue.Empty:
                continue

            # In half-duplex: se non siamo in fase di ascolto attivo, scarta.
            # Questo previene che eco del TTS o rumore durante il warmup venga
            # processato come enunciato valido.
            if self.half_duplex and not self._listening.is_set():
                log.debug("Brain: enunciato scartato (non in ascolto)")
                continue

            # In half-duplex (turn-based) lo STT spezza una frase lunga su pause
            # brevi in piu' enunciati: li uniamo in un solo turno aspettando una
            # pausa di silenzio credibile, cosi' si fa UNA sola richiesta a Hermes.
            if self.half_duplex:
                utterance = coalesce_utterances(
                    stt.utterances, utterance, self.cfg.ptt_turn_pause_s,
                    stop=self._stop,
                    pause_max_s=getattr(self.cfg, "ptt_turn_pause_max_s", 0.0))
                # Guardia anti-eco: se l'enunciato coincide in gran parte con
                # quanto APPENA detto, e' il TTS rientrato dal microfono -> non
                # e' l'utente: scartalo e RESTA in ascolto (niente turno nuovo).
                if self._looks_like_echo(utterance):
                    log.info("Scartato come eco del TTS: %r", utterance)
                    continue
                # Turno chiuso: da ora si SMETTE di ascoltare (mic muto) fino a
                # fine riproduzione della risposta + warmup post-TTS.
                self._processing.set()
                self._listening.clear()

            log.info("Interlocutore: %s", utterance)

            # Se il TTS sta ancora suonando, questo enunciato e' arrivato
            # sopra la voce dell'avatar: non e' automaticamente
            # un'interruzione. Stesso classificatore del gateway — un "si' si'"
            # non toglie la parola a nessuno, l'eco nemmeno, e una domanda
            # nuova puo' aspettare la fine della frase.
            if self._tts_active.is_set():
                esito, perche = classifica(
                    utterance, detto=self._last_response,
                    consenti_cambia=False)
                log.info("Sopra la voce %r -> %s (%s)",
                         utterance[:48], esito.value, perche)
                if esito is Esito.IGNORA:
                    self._duck.rialza()
                    continue
                if esito is Esito.FERMA:
                    self._interrupt_tts(tts)
                else:
                    # ACCODA: si lascia finire quel che stava dicendo, poi si
                    # risponde. Prima qualunque parola troncava la frase a
                    # meta', compreso un assenso.
                    self._wait_tts_finished(tts)

            try:
                # Comando vocale (cambio lingua / nuova conversazione)
                # riconosciuto come enunciato intero: eseguilo e conferma a voce,
                # SENZA passarlo all'agente. Solo nelle sessioni PTT.
                cmd = parse_command(utterance) if self.voice_commands_enabled else None
                if cmd is not None:
                    log.info("Comando vocale riconosciuto: %s", cmd)
                    confirmation = self._run_voice_command(cmd, tts)
                    if confirmation:
                        self._deliver_response(tts, confirmation, stt)
                else:
                    # Auto-rilevamento lingua: se l'utente parla un'altra lingua,
                    # adegua voce/lingua PRIMA di interrogare l'agente.
                    if self.voice_commands_enabled:
                        self._maybe_auto_language(utterance, tts)
                    self._run_llm_turn(utterance, stt, tts)
            except Exception as e:  # noqa: BLE001 - errore lato Hermes
                log.error("Hermes ha fallito sull'enunciato: %s", e)
            finally:
                if self.half_duplex:
                    # Risposta finita: NON e' piu' "elaborazione". Si entra in una
                    # breve fase di COOLDOWN: il mic resta muto (coda audio del TTS
                    # + eco fisica dalle casse), ma l'overlay mostra "ascolto", non
                    # "elaborazione" (era questo il falso "torna a elaborare" di 1-2s).
                    self._processing.clear()
                    self._cooldown.set()
                    cooldown = self.cfg.ptt_post_tts_pause_s + self.cfg.ptt_listen_warmup_s
                    if cooldown > 0:
                        log.debug("Cooldown post-TTS (mic muto, overlay=ascolto): %.1fs",
                                  cooldown)
                        # Interrompibile: se la sessione chiude durante il
                        # cooldown il thread brain esce subito (niente attesa che
                        # sopravvive al join(2s) di run()).
                        self._stop.wait(cooldown)
                    # Scarta tutto cio' che fosse colato nello STT (eco/coda).
                    drain_queue(stt.utterances)
                    # Ascolto attivo.
                    self._cooldown.clear()
                    self._listening.set()
                    log.debug("Ascolto attivo")

    def _prepara_turno(self, utterance: str) -> None:
        """Aggancio all'INIZIO del turno, prima di interrogare il modello.

        Qui non fa niente. La sessione avatar lo sovrascrive per scaldare la
        GPU: fra questo istante e la generazione dei fotogrammi passano
        l'interrogazione dell'LLM e la sintesi della voce, e in quei secondi la
        scheda scende a 300 MHz — poi il primo batch del labiale paga la
        risalita.
        """

    def _mostra_testo(self, ruolo: str, testo: str, forza: bool = False) -> None:
        """Mostra il testo del turno da qualche parte ("utente" o "avatar").

        Qui non fa niente: una sessione senza video non ha dove scriverlo.
        La sessione avatar lo sovrascrive per stamparlo sotto l'anteprima.
        """

    def _run_llm_turn(self, utterance: str, stt: HandySttClient,
                      tts: HandyTtsClient) -> None:
        """Interroga l'agente sull'enunciato e ne consegna la risposta al TTS."""
        interrupted = False
        # ReplyProcessor condiviso (senza media: il PTT non mostra allegati).
        reply = ReplyProcessor(extract_media=False)
        spoken_parts: list[str] = []
        raw_parts: list[str] = []
        streaming = self.stream_tts
        started = False  # almeno una frase inviata al TTS (streaming)
        _t0 = time.monotonic()
        _ttft = None  # time-to-first-token (diagnostica lag)
        _fuori_llm = 0.0   # tempo speso in sintesi/avatar dentro il ciclo
        # Nuovo turno: si scrive subito cosa si e' capito, e si sgombra la
        # riga della risposta — lasciare quella di prima mentre il modello
        # pensa fa credere che stia ancora rispondendo al turno passato.
        self._prepara_turno(utterance)
        self._mostra_testo("utente", utterance)
        self._mostra_testo("avatar", "")
        # Streaming: ogni frase completa viene sintetizzata/riprodotta SUBITO
        # mentre l'agente genera il resto -> primo audio molto prima. L'attesa di
        # fine avviene DOPO tutta la generazione (non tra le frasi), cosi' le
        # pause di generazione non riaprono il microfono a meta' risposta.
        for text_chunk in self.hermes.respond(utterance):
            if self._stop.is_set():
                interrupted = True
                break
            # Full-duplex (cuffie): un nuovo enunciato in coda significa che
            # l'interlocutore sta gia' riparlando -> barge-in, abbandona la
            # risposta. In half-duplex NON si abbandona (turni gia' coalizzati).
            if not self.half_duplex and not stt.utterances.empty():
                log.info("Barge-in durante la generazione: abbandono la risposta")
                interrupted = True
                break
            # Metadati speciali: voice_change per cambio lingua/voce TTS a runtime
            if isinstance(text_chunk, dict):
                vc = text_chunk.get("voice_change")
                if vc and isinstance(vc, dict):
                    # `keep_voice` (avatar): la voce della persona non si
                    # tocca, cambia solo la lingua di sintesi. Il motore TTS
                    # e' legato alla voce, quindi cambiarla vorrebbe dire
                    # scaricare e ricaricare un motore — decine di secondi, e
                    # per giunta l'avatar parlerebbe con un timbro che nessuno
                    # ha scelto.
                    new_voice = self.voice if self.keep_voice                         else vc.get("voice", self.voice)
                    new_lang = vc.get("lang", self.lang)
                    log.info("🔊 Cambio voce TTS: %s/%s → %s/%s",
                             self.voice, self.lang, new_voice, new_lang)
                    self.voice = new_voice
                    self.lang = new_lang
                    self._lang_code = code_for_kokoro_lang(new_lang)
                    # Il motore segue la voce: se la voce resta, resta anche il
                    # motore. Prima qui c'era `self.voice_engine if new_voice ==
                    # self.voice else ""`, che era codice morto — `self.voice`
                    # era gia' stato riassegnato due righe sopra, quindi il
                    # confronto era sempre vero.
                    configure_tts(tts, self.cfg, new_voice, new_lang,
                                  self.voice_engine)
                continue
            if _ttft is None:
                _ttft = time.monotonic() - _t0
            raw_parts.append(text_chunk)
            _t_fuori0 = time.monotonic()
            # Appena esce dal modello, senza aspettare la frase finita: e' il
            # solo modo di vedere che la LLM ha gia' risposto mentre la
            # sintesi e' ancora indietro.
            self._mostra_testo("avatar", "".join(raw_parts))
            for sent in reply.feed(text_chunk).tts_sentences:
                if not sent.strip():
                    continue
                spoken_parts.append(sent)
                if streaming:
                    self._stream_sentence(tts, sent)
                    started = True
            # `_stream_sentence` sintetizza e manda all'avatar, e BLOCCA: senza
            # scorporarlo, "generazione" sembrava tempo dell'LLM mentre era
            # quasi tutto TTS. Visto dal vivo: 61s attribuiti al modello,
            # quando il modello aveva finito in meno di un secondo e il resto
            # era la sintesi che falliva e la raccolta che aspettava invano.
            _fuori_llm += time.monotonic() - _t_fuori0
        if not interrupted:
            for sent in reply.flush().tts_sentences:
                if not sent.strip():
                    continue
                spoken_parts.append(sent)
                if streaming:
                    self._stream_sentence(tts, sent)
                    started = True
            # `_stream_sentence` sintetizza e manda all'avatar, e BLOCCA: senza
            # scorporarlo, "generazione" sembrava tempo dell'LLM mentre era
            # quasi tutto TTS. Visto dal vivo: 61s attribuiti al modello,
            # quando il modello aveva finito in meno di un secondo e il resto
            # era la sintesi che falliva e la raccolta che aspettava invano.
            _fuori_llm += time.monotonic() - _t_fuori0
        # Ultimo aggiornamento della riga, senza passare dal limitatore:
        # l'ultimo pezzo generato cade quasi sempre dentro la sua finestra, e
        # senza questo la risposta a schermo resterebbe monca della coda.
        self._mostra_testo("avatar", "".join(raw_parts), forza=True)
        full = " ".join(p.strip() for p in spoken_parts if p.strip())
        _tot = time.monotonic() - _t0
        log.info("LLM: TTFT %.2fs, modello %.2fs, sintesi+avatar %.2fs, "
                 "%d frasi%s",
                 _ttft if _ttft is not None else -1.0,
                 _tot - _fuori_llm, _fuori_llm, len(spoken_parts),
                 " (interrotto)" if interrupted else "")
        if not spoken_parts:
            # Nessun parlato: l'agente ha prodotto solo una chiamata-tool (in
            # TESTO, non eseguita dall'ACP) oppure proprio nulla.
            raw = "".join(raw_parts)
            if raw.strip():
                log.warning("Solo tool-call in testo (ACP non l'ha eseguita "
                            "-> nessuna risposta): %r", raw[:400])
            else:
                log.warning("Agente: risposta VUOTA (nessun testo).")
        if streaming:
            if full:
                self._last_response = full  # anti-eco
            # Attendi che TUTTO l'audio streammato sia riprodotto (half-duplex);
            # non attendere se interrotto (barge-in/stop) o in full-duplex.
            if started and not interrupted:
                self._finish_streamed_tts(tts, stt)
        else:
            # Avatar / non-streaming: risposta INTERA in un colpo (le sottoclassi
            # la instradano al worker video). "" se interrotta -> no-op.
            self._deliver_response(tts, full if not interrupted else "", stt)

    def _log_tts_sentence(self, sentence: str) -> None:
        """Traccia COSA va al TTS: senza questo, un audio spezzato o storpiato
        non era diagnosticabile (il log riportava solo il numero di frasi)."""
        log.info("TTS <- %r", sentence[:160])

    def _stream_sentence(self, tts: HandyTtsClient, sentence: str) -> None:
        """Sintetizza e accoda SUBITO una frase (streaming): l'uplink la riproduce
        appena pronta, mentre l'agente genera il resto."""
        self._log_tts_sentence(sentence)
        tts.send_text(sentence)
        tts.flush()

    def _finish_streamed_tts(self, tts: HandyTtsClient, stt=None) -> None:
        """A generazione finita, attende che tutto l'audio streammato sia stato
        riprodotto (solo half-duplex; in full-duplex il mic e' sempre attivo e il
        barge-in lo gestisce `_brain`)."""
        if self.half_duplex:
            self._wait_tts_drained(tts)

    def _wait_tts_drained(self, tts: HandyTtsClient, quiet: float = 1.5) -> None:
        """Attende che la coda audio sia vuota e nessun segmento in riproduzione
        per `quiet` secondi = tutto l'audio streammato e' stato riprodotto.

        A differenza di `_wait_tts_finished` NON attende un "avvio": l'audio dello
        streaming e' gia' partito durante la generazione, quindi se e' gia' tutto
        drenato esce dopo `quiet` (niente attesa a vuoto di 12s)."""
        last_active = time.monotonic()
        while not self._stop.is_set():
            if self._tts_occupato(tts):
                last_active = time.monotonic()
            elif time.monotonic() - last_active >= quiet:
                return
            time.sleep(0.05)

    # --- comandi vocali e cambio lingua ----------------------------------

    def _infer_gender(self) -> str:
        """Genere della voce corrente ('f'/'m'): 2° carattere dell'id Kokoro
        (if_sara='f', im_nicola='m', am_adam='m'). Default femminile."""
        return "m" if len(self.voice) > 1 and self.voice[1] == "m" else "f"

    def _set_reply_language(self, code: str) -> None:
        """Comunica all'agente la lingua in cui rispondere (se lo supporta)."""
        info = LANG_TABLE.get(code)
        fn = getattr(self.hermes, "set_reply_language", None)
        if info and callable(fn):
            try:
                fn(info["reply"])
            except Exception as e:  # noqa: BLE001 - brain senza il metodo/errore
                log.debug("set_reply_language fallito: %s", e)

    def _motore_parla(self, code: str, voce: str) -> bool:
        """Il motore che pronuncerà `voce` sa parlare questa lingua?

        Se non la sa, la lingua NON si cambia: sintetizzare un testo in una
        lingua che il motore non conosce da' pronuncia incomprensibile. In
        dubbio (motore ignoto, router non raggiungibile) si lascia passare."""
        info = LANG_TABLE.get(code)
        if info is None:
            return False
        try:
            from .engines import _get_tts_engine
            router = _get_tts_engine(self.cfg)
            motore = router.motore_di_voce(voce) or router.attivo
            return router.parla_lingua(motore, info["lang"])
        except Exception as e:  # noqa: BLE001 - backend esterno o non pronto
            log.debug("controllo lingua/motore non eseguito: %s", e)
            return True

    def _apply_language(self, tts: HandyTtsClient, code: str,
                        gender: str | None) -> dict | None:
        """Cambia lingua (e voce, se non `keep_voice`) e riconfigura il TTS.
        Ritorna l'entry di LANG_TABLE applicata, o None se la lingua e'
        sconosciuta o il motore non la parla."""
        info = LANG_TABLE.get(code)
        if info is None:
            return None
        # Voce con cui si parlerebbe dopo il cambio: con l'avatar resta quella
        # della persona (`keep_voice`), altrimenti la voce standard della lingua.
        voce_dopo = self.voice if self.keep_voice \
            else voice_for(code, gender or self._infer_gender())
        if not self._motore_parla(code, voce_dopo):
            # Non si cambia: si resta nella lingua di prima (all'inizio della
            # sessione non ce n'e' una "detta", e li' l'inglese e' il ripiego).
            ripiego = self._lang_code or "en"
            log.info("🌐 Lingua %s non supportata dal motore in uso: resto su %s",
                     code, ripiego)
            if ripiego != self._lang_code:
                return self._apply_language(tts, ripiego, gender)
            return None
        # Avatar (keep_voice): conserva la voce clonata della persona, cambia
        # solo la lingua di sintesi. Altrimenti scegli la voce standard della
        # lingua, mantenendo il genere corrente se non specificato.
        self.voice = voce_dopo
        self.lang = info["lang"]
        self._lang_code = code
        configure_tts(tts, self.cfg, self.voice, self.lang, self.voice_engine)
        self._set_reply_language(code)
        log.info("🌐 Lingua -> %s (voce=%s, lang=%s)", code, self.voice, self.lang)
        return info

    def _run_voice_command(self, cmd: dict, tts: HandyTtsClient) -> str:
        """Esegue un comando vocale e ritorna la frase di conferma da pronunciare."""
        if cmd["kind"] == "lang":
            info = self._apply_language(tts, cmd["lang"], cmd.get("gender"))
            if info is None:
                return "Non conosco quella lingua."
            return info["switch"]
        if cmd["kind"] == "new_session":
            fn = getattr(self.hermes, "reset_session", None)
            ok = False
            if callable(fn):
                try:
                    ok = bool(fn())
                except Exception as e:  # noqa: BLE001
                    log.warning("reset_session fallito: %s", e)
            info = LANG_TABLE.get(self._lang_code, LANG_TABLE["it"])
            return info["new_session"] if ok else "Non sono riuscito a ripartire."
        return ""

    def _maybe_auto_language(self, utterance: str, tts: HandyTtsClient) -> None:
        """Se l'enunciato e' in una lingua diversa (e supportata), adegua
        voce/lingua prima di interrogare l'agente."""
        det = detect_language(utterance)
        if det and det != self._lang_code and det in LANG_TABLE:
            log.info("🌐 Auto-rilevata lingua %s (era %s)", det, self._lang_code)
            self._apply_language(tts, det, None)

    def _deliver_response(self, tts: HandyTtsClient, full: str, stt=None) -> None:
        """Consegna la risposta pronunciata `full` al TTS e ne attende la fine
        (in half-duplex). Comportamento di default: audio verso le casse locali
        via `_uplink`. Le sottoclassi (avatar) sovrascrivono per instradare
        l'audio altrove (worker video); `stt` consente loro di rilevare il
        barge-in in full-duplex."""
        if full:
            log.info("TTS <- %r", full)
            tts.send_text(full)
            tts.flush()
            self._last_response = full
        # Half-duplex: attendi che TUTTO l'audio della risposta sia stato
        # riprodotto prima di riprendere l'ascolto. Solo se c'e' davvero audio in
        # arrivo (altrimenti niente attesa a vuoto di 12s).
        if self.half_duplex and full:
            self._wait_tts_finished(tts)

    def _wait_tts_finished(self, tts: HandyTtsClient,
                           start_timeout: float = 12.0, quiet: float = 1.5) -> None:
        """Attende l'avvio e poi il completamento della riproduzione TTS.

        Aspetta fino a `start_timeout` che arrivi il primo audio (Hermes puo'
        metterci a generare); se non arriva nulla, la risposta era vuota -> esce.
        Poi attende che coda audio vuota e nessun segmento in riproduzione per
        `quiet` secondi = riproduzione completata.
        """
        t0 = time.monotonic()
        while time.monotonic() - t0 < start_timeout:
            if self._stop.is_set():
                return
            if self._tts_active.is_set() or not tts.audio.empty():
                break
            time.sleep(0.05)
        else:
            return  # nessun audio prodotto (risposta vuota)
        last_active = time.monotonic()
        while not self._stop.is_set():
            if self._tts_occupato(tts):
                last_active = time.monotonic()
            elif time.monotonic() - last_active >= quiet:
                return
            time.sleep(0.05)

    def _tts_occupato(self, tts: HandyTtsClient) -> bool:
        """Il TTS ha ancora qualcosa da dire: riproduce, ha audio in coda o sta
        ancora sintetizzando (`busy`, per i client che lo dichiarano).

        La sintesi in corso conta: con lo streaming una frase arriva a pezzi e
        fra un pezzo e l'altro la coda puo' restare vuota per un attimo. Senza,
        in half-duplex quel vuoto bastava a dichiarare finita la risposta e a
        riaprire il microfono mentre le casse stavano ancora per parlare."""
        if self._tts_active.is_set() or not tts.audio.empty():
            return True
        occupato = getattr(tts, "busy", None)
        try:
            return bool(occupato()) if callable(occupato) else False
        except Exception:  # noqa: BLE001
            return False

    def _uplink(self, audio: HfpAudioIO, tts: HandyTtsClient) -> None:
        """Handy TTS -> (resample -> rate audio) -> audio out."""
        while not self._stop.is_set():
            try:
                seg = tts.audio.get(timeout=0.5)
            except queue.Empty:
                self._tts_active.clear()
                continue

            # Scarta i segmenti ancora in sintesi per una risposta gia' abortita
            # (barge-in): senza, arriverebbero DOPO il drain e si mischierebbero
            # alla nuova risposta.
            if seg.generation != tts.generation:
                continue
            self._tts_active.set()
            self._barge_in.clear()
            pcm = seg.samples
            if seg.sample_rate != self.audio_rate:
                pcm = resample(pcm, seg.sample_rate, self.audio_rate)
            # Scrive a blocchi controllando stop e barge-in: chiusura reattiva
            # e interruzione immediata della risposta.
            block = audio.block_frames
            for i in range(0, len(pcm), block):
                if self._stop.is_set() or self._barge_in.is_set():
                    break
                # Rilascio del ducking: se nessuno parla piu' da 2,5s era un
                # rumore, e la voce risale.
                if (self._duck.abbassato
                        and time.monotonic() - self._ultimo_parlato > 2.5):
                    self._duck.rialza()
                try:
                    audio.write(self._duck.applica(pcm[i:i + block],
                                                   self.audio_rate))
                except Exception as e:  # noqa: BLE001 - stream chiuso o errore device
                    log.warning("uplink: scrittura fallita: %s", e)
                    return

    def _interrupt_tts(self, tts: HandyTtsClient) -> None:
        """Aborta la riproduzione TTS in corso e svuota la coda dei segmenti."""
        self._barge_in.set()
        # Apre una nuova generazione: i segmenti gia' in sintesi lato Handy per
        # il testo abortito (che arriveranno DOPO questo drain) porteranno la
        # generazione vecchia e verranno scartati da _uplink.
        tts.bump_generation()
        drained = drain_queue(tts.audio)
        if drained:
            log.debug("Barge-in: scartati %d segmenti audio in coda", drained)
