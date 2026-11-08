"""Il copione: cosa l'avatar ha detto, cosa sta dicendo, cosa dirà.

Oggi il gateway spinge l'audio verso il client appena il TTS lo produce, e il
client se lo accoda (`AudioPlayer.nextStart`, in `app/src/lib/audio.ts`). Dopo
qualche secondo di risposta ci sono dieci secondi di voce già fuori dalle nostre
mani: se l'utente interrompe, l'unica cosa che si può fare è chiedere all'app di
buttare via tutto — un taglio netto, in un punto qualsiasi della frase.

Questo modulo sposta quell'orologio dal client al gateway. L'audio resta qui,
in un copione, e se ne consegna solo quanto basta a non lasciare buchi
(`ANTICIPO_S`). Tutto ciò che non è ancora uscito resta modificabile: si può
tagliare a un confine pulito, abbassare di volume, o — più avanti —
sostituirlo con una continuazione che tenga conto di quello che l'utente ha
appena detto.

La granularità è quella che il TTS produce già da sé: `_units_with_pauses` in
`engines/kokoro_tts.py` spezza il testo su frasi E incisi, e ogni pezzo arriva
con la sua pausa in coda. Misurato sul motore vero: cinque segmenti da 1,5 a
5,5 secondi per un testo di tre frasi, ognuno con il proprio testo. Quindi il
confine per tagliare esiste già, ed è **acustico**: non serve cercare i silenzi
nel PCM né stimare a che punto della frase siamo arrivati.

La distinzione che regge tutto è fra **detto** e **previsto**: il primo è ciò
che è davvero uscito dall'altoparlante, il secondo ciò che è stato generato e
non ancora emesso. Oggi il gateway confonde le due cose — `_ultima_risposta`
accumula tutto il testo generato, compreso quello che nessuno ha mai sentito, e
ci confronta l'eco — e da lì nascono sia i falsi riconoscimenti dell'eco sia la
convinzione del modello di aver detto cose mai pronunciate.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

log = logging.getLogger("callbridge.duplex")


class Stato(Enum):
    """Dove si trova una battuta nel suo percorso verso l'altoparlante."""

    ATTESA = "attesa"        # testo deciso, sintesi non ancora chiesta
    SINTESI = "sintesi"      # affidata al TTS, audio non ancora tornato
    PRONTA = "pronta"        # audio completo, non ancora cominciata
    IN_CORSO = "in_corso"    # se ne sta consegnando l'audio adesso
    DETTA = "detta"          # uscita per intero
    SCARTATA = "scartata"    # abbandonata prima o durante: non sarà mai detta


@dataclass
class Battuta:
    """Un pezzo di risposta: il testo e l'audio che lo pronuncia.

    Corrisponde a un `TtsSegment`, cioè a ciò che il motore consegna in un
    colpo: una frase o un inciso, con la sua pausa già in coda.
    """

    testo: str
    gen: int
    pcm: np.ndarray | None = None
    rate: int = 24000
    stato: Stato = Stato.ATTESA
    #: Secondi già consegnati di questa battuta. Non è "riprodotti": è quanto
    #: ne è uscito dal gateway. Fra i due c'è `ANTICIPO_S` di differenza, ed è
    #: una differenza che si conosce e si tiene piccola apposta.
    emesso_s: float = 0.0
    id: int = 0
    #: Frase TTS di appartenenza (`TtsSegment.frase`). Con lo streaming una
    #: frase arriva in piu' battute: il taglio "a fine frase" le tiene insieme.
    frase: int = 0

    @property
    def durata_s(self) -> float:
        if self.pcm is None or not self.rate:
            return 0.0
        return len(self.pcm) / self.rate

    @property
    def residuo_s(self) -> float:
        return max(0.0, self.durata_s - self.emesso_s)

    def preleva(self, secondi: float) -> np.ndarray:
        """Stacca fino a `secondi` di audio dal punto in cui si era arrivati."""
        if self.pcm is None or secondi <= 0:
            return np.zeros(0, dtype=np.float32)
        da = int(self.emesso_s * self.rate)
        a = min(len(self.pcm), da + int(secondi * self.rate))
        if a <= da:
            return np.zeros(0, dtype=np.float32)
        self.emesso_s = a / self.rate
        return self.pcm[da:a]


class Copione:
    """La coda delle battute, con il segnaposto di ciò che è già uscito.

    Regola unica di proprietà: **solo chi consegna l'audio fa avanzare il
    segnaposto**. Chiunque altro può aggiungere battute, leggere lo stato o
    tagliare il futuro, ma nessuno può dichiarare che qualcosa è stato detto.
    Senza questa regola servirebbero lock dappertutto, e "detto" smetterebbe di
    voler dire qualcosa di preciso.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._battute: list[Battuta] = []
        #: Le battute abbandonate da un taglio. Non si buttano: se la
        #: sostituzione tarda, la bocca le riprende invece di ammutolire —
        #: un avatar che si ferma a metà discorso sembra un guasto.
        self.riserva: list[Battuta] = []
        self.gen = 0
        self._prossimo_id = 1
        #: (istante, testo) di ciò che è uscito, per rispondere a `detto(entro)`.
        self._storico: list[tuple[float, str]] = []

    # --- scrittura -------------------------------------------------------
    def aggiungi(self, testo: str, gen: int, frase: int = 0) -> Battuta:
        with self._lock:
            b = Battuta(testo=testo, gen=gen, id=self._prossimo_id, frase=frase)
            self._prossimo_id += 1
            self._battute.append(b)
            return b

    def audio_pronto(self, b: Battuta, pcm: np.ndarray, rate: int) -> None:
        with self._lock:
            if b.stato is Stato.SCARTATA:
                return
            b.pcm, b.rate, b.stato = pcm, rate, Stato.PRONTA

    def nuova_generazione(self) -> int:
        with self._lock:
            self.gen += 1
            return self.gen

    # --- lettura ---------------------------------------------------------
    def prossima_da_emettere(self) -> Battuta | None:
        """La battuta in corso, o la prima pronta se non ce n'è una in corso."""
        with self._lock:
            for b in self._battute:
                if b.stato is Stato.IN_CORSO:
                    return b
            for b in self._battute:
                if b.stato is Stato.PRONTA and b.gen == self.gen:
                    return b
            return None

    def anticipo_s(self) -> float:
        """Secondi di parlato già generati e non ancora consegnati.

        È il margine entro cui si può ancora cambiare idea: sotto una certa
        soglia non ha senso provare a riscrivere una risposta, perché la
        riscrittura arriverebbe dopo che è già stata pronunciata.
        """
        with self._lock:
            tot = 0.0
            for b in self._battute:
                if b.gen != self.gen:
                    continue
                if b.stato is Stato.IN_CORSO:
                    tot += b.residuo_s
                elif b.stato is Stato.PRONTA:
                    tot += b.durata_s
            return tot

    def detto(self, entro_s: float | None = None) -> str:
        """Il testo davvero uscito, eventualmente solo quello recente.

        `entro_s` serve al riconoscimento dell'eco: confrontare una
        trascrizione con tutto ciò che si è detto in un turno lungo produce
        falsi positivi su una parola ripetuta trenta secondi prima.
        """
        with self._lock:
            if entro_s is None:
                return " ".join(t for _, t in self._storico).strip()
            limite = time.monotonic() - entro_s
            return " ".join(t for q, t in self._storico if q >= limite).strip()

    def previsto(self) -> str:
        """Il testo generato ma non ancora uscito: quello che si può cambiare."""
        with self._lock:
            pezzi = []
            for b in self._battute:
                if b.gen != self.gen or b.stato in (Stato.DETTA, Stato.SCARTATA):
                    continue
                if b.stato is Stato.IN_CORSO and b.emesso_s > 0:
                    continue          # già cominciata: fa parte del detto
                pezzi.append(b.testo)
            return " ".join(pezzi).strip()

    # --- consegna (solo la bocca) ----------------------------------------
    def consegna(self, secondi: float) -> tuple[np.ndarray, int, Battuta | None]:
        """Stacca fino a `secondi` di audio dalla battuta in testa.

        Non attraversa il confine fra due battute in una sola chiamata: così
        chi consegna può fermarsi, cambiare volume o tagliare esattamente lì,
        che è il punto in cui il TTS ha già messo una pausa.
        """
        with self._lock:
            b = self.prossima_da_emettere()
            if b is None or b.pcm is None:
                return np.zeros(0, dtype=np.float32), 0, None
            if b.stato is Stato.PRONTA:
                b.stato = Stato.IN_CORSO
            fetta = b.preleva(secondi)
            if b.residuo_s <= 1e-6:
                b.stato = Stato.DETTA
                self._storico.append((time.monotonic(), b.testo))
            return fetta, b.rate, b

    # --- taglio ----------------------------------------------------------
    def taglia(self, anche_in_corso: bool = False) -> list[Battuta]:
        """Abbandona il futuro. Ritorna le battute scartate, che vanno in riserva.

        Con `anche_in_corso` si tronca pure la battuta che sta uscendo: è il
        taglio duro, per quando l'utente ha chiesto esplicitamente di fermarsi.
        Senza, si lascia finire la battuta corrente — che costa pochi secondi e
        in cambio evita di spezzare una frase a metà parola.
        """
        with self._lock:
            scartate: list[Battuta] = []
            # Frase in riproduzione: se arriva a pezzi (streaming), i pezzi gia'
            # pronti dopo quello in corso ne sono il seguito, non il futuro.
            # Tagliarli spezzerebbe la frase a meta' parola.
            frase_viva = 0
            if not anche_in_corso:
                frase_viva = next((b.frase for b in self._battute
                                   if b.stato is Stato.IN_CORSO and b.frase), 0)
            for b in self._battute:
                if b.stato in (Stato.DETTA, Stato.SCARTATA):
                    continue
                if b.stato is Stato.IN_CORSO and not anche_in_corso:
                    continue
                if frase_viva and b.frase == frase_viva and b.stato is Stato.PRONTA:
                    b.gen = self.gen          # resta emettibile dopo il taglio
                    continue
                if b.stato is Stato.IN_CORSO and b.emesso_s > 0:
                    # Parzialmente uscita: ciò che è stato sentito resta detto.
                    self._storico.append((time.monotonic(), b.testo))
                b.stato = Stato.SCARTATA
                scartate.append(b)
            self.riserva = [b for b in scartate if b.pcm is not None]
            return scartate

    def ripesca_dalla_riserva(self) -> Battuta | None:
        """Rimette in gioco una battuta abbandonata, se la sostituzione tarda.

        È la rete di sicurezza della riscrittura: si è tagliato il futuro per
        rifarlo, ma se la continuazione non arriva in tempo l'avatar
        ammutolisce a metà discorso — e un avatar che si ferma di colpo non
        sembra pensieroso, sembra rotto. Meglio una frase in più, coerente ma
        non integrata, e riapplicare il taglio una frase più avanti.
        """
        with self._lock:
            while self.riserva:
                b = self.riserva.pop(0)
                if b.pcm is None or b.residuo_s <= 1e-6:
                    continue
                b.stato = Stato.PRONTA
                b.gen = self.gen
                if b not in self._battute:
                    self._battute.append(b)
                return b
            return None

    def stato_breve(self) -> str:
        """Una riga per il log: serve a leggere cosa stava succedendo."""
        with self._lock:
            conteggio: dict[str, int] = {}
            for b in self._battute:
                conteggio[b.stato.value] = conteggio.get(b.stato.value, 0) + 1
            pezzi = ", ".join(f"{k}={v}" for k, v in sorted(conteggio.items()))
            return (f"gen={self.gen} anticipo={self.anticipo_s():.1f}s "
                    f"riserva={len(self.riserva)} [{pezzi}]")


#: Secondi di parlato che si tengono consegnati in anticipo rispetto
#: all'orologio. È il compromesso centrale del motore: più è grande, più la
#: riproduzione è al riparo da un singhiozzo di rete; più è piccolo, prima
#: arriva a destinazione un cambio di volume o un taglio. Sarà anche la
#: latenza del ducking, quando ci sarà, perché il guadagno si applica ai
#: campioni che usciranno di lì a poco: per questo si tiene basso.
#: Con la traccia WebRTC si può misurare quanto è già in coda di là
#: (`AudioBuffer.in_coda_s`) e bastano 0,35 s; sul WebSocket non c'è misura di
#: ritorno e il margine dell'app è appena 0,12 s, quindi si è più prudenti.
ANTICIPO_WEBRTC_S = 0.35
ANTICIPO_WS_S = 0.6

#: Quanto si abbassa la voce quando l'utente comincia a parlare: -12 dB.
#: Non zero, mai: a zero sembra che l'avatar si sia fermato o rotto, e non è
#: quello che si vuole dire. Si vuole dire "ti sto ascoltando, e intanto
#: finisco". A un quarto dell'ampiezza l'energia dell'eco cala di sedici volte,
#: che è anche il regalo più grosso che si possa fare al cancellatore.
DUCK_G = 0.25
#: Giù in fretta, su piano. L'asimmetria è voluta: chi parla deve *sentire*
#: subito di essere stato sentito, mentre la risalita, se è brusca, suona come
#: uno scatto in mezzo alla frase.
DUCK_GIU_S = 0.12
DUCK_SU_S = 0.25

class Ducking:
    """Il guadagno dell'uscita, con la rampa che attraversa i blocchi.

    Sta in una classe sua perché serve in due posti — il motore del gateway e
    l'uscita audio del PC — e perché l'unica parte difficile è proprio quella
    che si perde riscrivendola: la rampa deve riprendere dal valore esatto a
    cui il blocco precedente era arrivata. Applicare un guadagno costante per
    blocco produce una scala di gradini, e ogni gradino è un clic.
    """

    def __init__(self) -> None:
        self.g = 1.0
        self.obiettivo = 1.0
        self.abbassata_da: float | None = None

    def abbassa(self) -> None:
        if self.obiettivo != DUCK_G:
            self.obiettivo = DUCK_G
            self.abbassata_da = time.monotonic()

    def rialza(self) -> None:
        self.obiettivo = 1.0
        self.abbassata_da = None

    @property
    def abbassato(self) -> bool:
        return self.obiettivo != 1.0

    def applica(self, fetta: np.ndarray, rate: int) -> np.ndarray:
        if self.g == self.obiettivo:
            return fetta if self.obiettivo == 1.0 else fetta * self.obiettivo
        n = len(fetta)
        if not n:
            return fetta
        durata = DUCK_GIU_S if self.obiettivo < self.g else DUCK_SU_S
        # La velocità si calcola sull'escursione INTERA (1 -> DUCK_G), non su
        # quanto resta da fare: dividendo il residuo a ogni fetta la rampa
        # diventa esponenziale e ci mette il triplo — misurato, 310 ms invece
        # di 120.
        velocita = (1.0 - DUCK_G) / max(durata * rate, 1.0)
        passo = velocita if self.obiettivo > self.g else -velocita
        g = self.g + passo * np.arange(1, n + 1, dtype=np.float32)
        g = np.clip(g, min(self.g, self.obiettivo), max(self.g, self.obiettivo))
        self.g = float(g[-1])
        return (fetta * g).astype(np.float32)


#: Ogni quanto la bocca si sveglia per vedere se c'è da consegnare.
PASSO_S = 0.02
#: Quanto audio si stacca per volta. Multiplo del passo: fette più corte
#: moltiplicano le chiamate senza rendere il taglio più fine, perché il taglio
#: avviene comunque a confine di battuta.
FETTA_S = PASSO_S * 4


class MotoreDuplex:
    """Consegna la voce a tempo, invece di spingerla appena è pronta.

    Due mestieri, due thread:

    - `gw-sintesi` raccoglie dal TTS i segmenti man mano che escono e li
      deposita nel copione come battute già pronte. Non decide niente: il
      testo gliel'ha già spezzato il sanitizer, e il motore lo spezza ancora
      su frasi e incisi.
    - `gw-bocca` è l'unico che consegna, e quindi l'unico che può dire che
      qualcosa è stato detto. Tiene un orologio proprio e manda avanti
      l'audio quel tanto che basta a stare `anticipo` secondi davanti.

    Quello che il motore NON fa, in questa prima versione: classificare le
    interruzioni, abbassare il volume, riscrivere la continuazione. Fa una cosa
    sola — tenere in mano l'audio fino all'ultimo momento utile — perché è
    quella che rende possibili le altre.
    """

    def __init__(self, tts, consegna, muovi_labbra=None,
                 anticipo_s: float | None = None, su_dump=None) -> None:
        self.copione = Copione()
        self._tts = tts
        #: `consegna(pcm, rate)`: dove finisce l'audio (WebSocket o traccia).
        self._consegna = consegna
        #: `muovi_labbra(pcm, rate, attendi)`: opzionale, l'avatar può non esserci.
        self._muovi_labbra = muovi_labbra
        self._anticipo = ANTICIPO_WS_S if anticipo_s is None else anticipo_s
        #: `su_dump(pcm, rate)`: riceve l'audio davvero consegnato, per il file
        #: di registrazione. È l'unico modo di giudicare a orecchio ciò che il
        #: motore fa, e serve dalla prima versione.
        self._su_dump = su_dump
        self._stop = threading.Event()
        self._thread: list[threading.Thread] = []
        #: Si alza quando la risposta è finita: da lì la bocca smette di
        #: riempire di silenzio e lascia che il silenzio sia silenzio.
        self._risposta_finita = threading.Event()
        self._risposta_finita.set()
        self.consegnati_s = 0.0
        self.silenzi_s = 0.0
        #: Il guadagno dell'uscita. Stessa classe che usa l'audio del PC: il
        #: ducking e' una cosa sola, in due posti.
        self._duck = Ducking()
        #: Quante battute sono arrivate dal TTS. Serve a distinguere "la
        #: risposta e' finita" da "il testo e' partito ma l'audio non e'
        #: ancora tornato": il copione vuoto da solo non lo dice, e chi
        #: aspetta chiuderebbe il turno mentre la sintesi e' in corso.
        self.battute_ricevute = 0
        self.ultima_battuta_t = time.monotonic()

    # --- ciclo di vita ---------------------------------------------------
    def avvia(self) -> None:
        self.copione.nuova_generazione()
        for nome, funzione in (("gw-sintesi", self._ciclo_sintesi),
                               ("gw-bocca", self._ciclo_bocca)):
            t = threading.Thread(target=funzione, name=nome, daemon=True)
            t.start()
            self._thread.append(t)

    def ferma(self) -> None:
        self._stop.set()
        for t in self._thread:
            t.join(timeout=2.0)

    # --- ingresso del testo ----------------------------------------------
    def dillo(self, testo: str) -> None:
        """Manda del testo in sintesi. Le battute nasceranno da ciò che torna."""
        if testo.strip():
            self._risposta_finita.clear()
            # Da qui il TTS ha del lavoro da fare: l'attesa del suo silenzio
            # riparte da adesso. Senza questo azzeramento il primo turno di
            # una sessione nasce gia' "in silenzio da minuti" e chi aspetta la
            # fine della risposta se ne va prima che l'audio esista.
            self.ultima_battuta_t = time.monotonic()
            self._tts.send_text(testo)

    def fine_risposta(self) -> None:
        """Il cervello ha finito di scrivere. NON vuol dire che l'audio sia finito.

        Fra l'ultima parola generata e l'ultimo campione sintetizzato passa il
        tempo del TTS — misurato su questo sistema, circa due terzi della
        durata del parlato. Chi attende la fine del turno deve guardare
        `silenzio_dal_tts()`, non questo.
        """
        self._tts.flush()
        self._risposta_finita.set()

    # --- volume ----------------------------------------------------------
    @property
    def abbassata_da(self) -> float | None:
        return self._duck.abbassata_da

    def abbassa(self) -> None:
        """L'utente ha aperto bocca: si abbassa la voce e si continua.

        Non è un'interruzione ed è una scelta: fermarsi di netto a ogni rumore
        rende l'avatar esitante, e a quel punto ogni colpo di tosse gli toglie
        la parola. Abbassando si resta reversibili — se era un "sì sì" si
        risale e nessuno se ne accorge; se era un'interruzione vera, chi
        decide ha `ANTICIPO_S` di tempo per tagliare, e intanto l'utente si
        sente già ascoltato.
        """
        self._duck.abbassa()

    def rialza(self) -> None:
        """Falso allarme, o l'utente ha finito: si torna al volume pieno."""
        self._duck.rialza()

    @property
    def abbassato(self) -> bool:
        return self._duck.abbassato

    def _rampa(self, fetta: np.ndarray, rate: int) -> np.ndarray:
        return self._duck.applica(fetta, rate)

    def silenzio_dal_tts(self) -> float:
        """Da quanti secondi il TTS non consegna piu' niente."""
        return time.monotonic() - self.ultima_battuta_t

    def riscrivi(self) -> list[Battuta]:
        """Taglia il futuro lasciando finire la frase in corso: si riscrivera'.

        La differenza con `taglia` e' tutta nella battuta corrente. Qui si
        lascia finire: costa i due-cinque secondi della frase, che sono
        esattamente i secondi che servono al cervello per produrre la
        continuazione. Se si dovesse tacere subito, la strategia giusta non
        era riscrivere ma fermarsi.
        """
        self.copione.nuova_generazione()
        try:
            # La frase in sintesi si completa: con lo streaming e' gia' in
            # riproduzione, e qui si lascia finire la frase in corso.
            self._tts.abort(finisci_frase=True)
        except TypeError:
            self._tts.abort()     # client TTS senza streaming
        except Exception:  # noqa: BLE001 - l'interruzione non deve fallire qui
            pass
        scartate = self.copione.taglia(anche_in_corso=False)
        self._risposta_finita.clear()
        self.ultima_battuta_t = time.monotonic()
        return scartate

    def ripesca(self) -> bool:
        """Se la continuazione tarda, si riprende dalla riserva. True se fatto."""
        b = self.copione.ripesca_dalla_riserva()
        if b is not None:
            log.info("motore: ripescata dalla riserva %r", b.testo[:40])
        return b is not None

    def taglia(self, anche_in_corso: bool = True) -> list[Battuta]:
        """Abbandona la risposta in corso, in tutta la catena.

        L'ordine conta e segue quello di produzione: prima si ferma chi
        produce (il TTS continuerebbe a macinare il testo già ricevuto), poi
        si butta ciò che è già audio. Al contrario si svuoterebbe una coda che
        un istante dopo è di nuovo piena.
        """
        self.copione.nuova_generazione()
        try:
            self._tts.abort()
        except Exception:  # noqa: BLE001 - l'interruzione non deve fallire qui
            pass
        scartate = self.copione.taglia(anche_in_corso=anche_in_corso)
        # Taglio duro: la riserva NON serve. Serve quando si taglia per
        # riscrivere, come rete se la riscrittura tarda; qui l'utente ha
        # chiesto di tacere, e ripescare vorrebbe dire riprendere a parlare.
        self.copione.riserva.clear()
        self._risposta_finita.set()
        return scartate

    # --- i due thread ----------------------------------------------------
    def _ciclo_sintesi(self) -> None:
        """Dal TTS al copione: ogni segmento diventa una battuta già pronta."""
        while not self._stop.is_set():
            try:
                seg = self._tts.audio.get(timeout=0.2)
            except Exception:  # noqa: BLE001 - coda vuota
                continue
            # Un segmento di una generazione superata è audio di una risposta
            # che l'utente ha già interrotto: consegnarlo farebbe riprendere
            # all'avatar una frase abbandonata.
            if getattr(seg, "generation", 0) != getattr(self._tts, "generation", 0):
                log.info("motore: scartato segmento di gen %s (corrente %s)",
                         getattr(seg, "generation", "?"),
                         getattr(self._tts, "generation", "?"))
                continue
            b = self.copione.aggiungi(getattr(seg, "testo", "") or "",
                                      self.copione.gen,
                                      getattr(seg, "frase", 0) or 0)
            self.copione.audio_pronto(b, seg.samples, seg.sample_rate)
            self.battute_ricevute += 1
            self.ultima_battuta_t = time.monotonic()
            # È arrivato materiale nuovo: la riserva ha finito il suo lavoro.
            # Esiste solo per coprire l'attesa fra il taglio e la
            # sostituzione; tenerla oltre vorrebbe dire rischiare di
            # ripescare, più avanti, una frase della risposta che si è
            # deciso di non dare più.
            if self.copione.riserva:
                self.copione.riserva.clear()
            if self.battute_ricevute == 1 or self.battute_ricevute % 10 == 0:
                log.info("motore: battuta %d (%.1fs) %r",
                         self.battute_ricevute, b.durata_s, b.testo[:40])

    def _ciclo_bocca(self) -> None:
        """L'unico che consegna, e quindi l'unico che sa cosa è stato detto."""
        prossimo = time.monotonic()
        while not self._stop.is_set():
            ora = time.monotonic()
            # Si consegna solo quando si è scesi sotto l'anticipo voluto: è
            # questo che tiene l'audio in casa invece di spingerlo tutto fuori.
            if ora < prossimo - self._anticipo:
                time.sleep(PASSO_S)
                continue
            fetta, rate, battuta = self.copione.consegna(FETTA_S)
            if len(fetta):
                if battuta is not None and battuta.emesso_s <= FETTA_S + 1e-6:
                    self._avvia_labiale(battuta)
                fetta = self._rampa(fetta, rate)
                self._consegna(fetta, rate)
                if self._su_dump is not None:
                    # Si registra cio' che esce DOPO il guadagno: e' l'unico
                    # modo di sentire il ducking invece di dedurlo dai log.
                    self._su_dump(fetta, rate)
                durata = len(fetta) / max(rate, 1)
                prossimo += durata
                self.consegnati_s += durata
                continue
            if self._risposta_finita.is_set():
                # Niente da dire e niente in arrivo: il silenzio è silenzio e
                # non va riempito. L'orologio riparte da adesso, così alla
                # ripresa non si trova indietro di tutta l'attesa.
                prossimo = time.monotonic()
                time.sleep(PASSO_S)
                continue
            # Si sta riscrivendo il seguito e la sostituzione non è ancora
            # arrivata: invece di ammutolire si riprende dalla riserva. Costa
            # una frase non integrata; il silenzio in mezzo a un discorso
            # costerebbe la credibilità dell'interlocutore.
            if self.copione.riserva and self.copione.anticipo_s() < 0.8:
                if self.ripesca():
                    continue
            # La risposta continua ma la battuta successiva non è pronta: si
            # manda silenzio per non lasciare a secco il buffer a valle. Un
            # buco vero fa ripartire la schedulazione dell'app con il suo
            # margine, e si sente come uno strappo.
            self._consegna(np.zeros(int(PASSO_S * 24000), dtype=np.float32), 24000)
            prossimo += PASSO_S
            self.silenzi_s += PASSO_S

    def _avvia_labiale(self, battuta: Battuta) -> None:
        """Il labiale parte con la battuta, non con la risposta.

        Prima il gateway mandava il WAV appena il TTS lo consegnava, quindi il
        worker accumulava in coda l'intera risposta e un'interruzione lasciava
        la bocca a recitare da sola. Mandandolo al momento della consegna, nel
        worker c'è al massimo la battuta corrente.
        """
        if self._muovi_labbra is None or battuta.pcm is None:
            return
        try:
            self._muovi_labbra(battuta.pcm, battuta.rate, False)
        except Exception:  # noqa: BLE001 - il labiale non deve fermare la voce
            pass

    def stato(self) -> str:
        return (f"{self.copione.stato_breve()} consegnati={self.consegnati_s:.1f}s "
                f"silenzio={self.silenzi_s:.1f}s")
