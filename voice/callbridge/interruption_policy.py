"""Cosa fare di una frase arrivata mentre l'avatar sta ancora parlando.

Quattro esiti, non due. Il barge-in classico ne ha due — o tronca o non tronca
— e sbaglia in tutti e due i modi: tronca sull'eco della propria voce e su un
"sì sì" di cortesia, e non ha nessun modo di dire "ho capito, ne parliamo fra
un attimo". Le persone, fra loro, fanno altro: distinguono un cenno di ascolto
da un'interruzione, e una domanda nuova da una correzione di ciò che stanno per
dire.

    IGNORA   rumore, eco, cenno di ascolto -> si continua come se niente fosse
    FERMA    l'utente vuole la parola      -> si tronca subito
    ACCODA   cosa nuova, ma può aspettare  -> si finisce, poi si risponde
    CAMBIA   riguarda ciò che si sta per dire -> si riscrive la continuazione

Qui ci sono solo le regole deterministiche, che costano zero e coprono la
maggioranza dei casi. La zona grigia — sostanzialmente la distinzione fra
ACCODA e CAMBIA — vorrebbe una domanda all'LLM, ma la misura di fase 0c dice
che oggi non si può: LM Studio serve una richiesta per volta e la
classificazione arriva per definizione mentre la risposta principale streamma,
quindi si mette in fila e paga 6,1 s contro un tetto di 0,4. Finché non c'è un
secondo slot, la zona grigia va ad ACCODA — il ripiego che non perde mai ciò
che l'utente ha detto e non gli toglie mai la parola di prepotenza.

Le regole sono in ordine e la prima che risponde vince. L'ordine non è
casuale: prima si toglie di mezzo ciò che non è voce dell'utente (rumore ed
eco), poi ciò che è voce ma non è un turno (i cenni), e solo alla fine si
decide fra le cose che un turno lo sono davvero.
"""
from __future__ import annotations

import re
from enum import Enum


class Esito(Enum):
    IGNORA = "ignora"
    FERMA = "ferma"
    ACCODA = "accoda"
    CAMBIA = "cambia"


#: Sotto questa lunghezza un enunciato non è un'interruzione: è un residuo del
#: VAD, un respiro, o una parola persa. Il valore viene dal gateway, dove era
#: nato come `_MIN_BARGEIN_CHARS` dopo averlo visto sbagliare dal vivo.
MIN_CARATTERI = 7

#: I cenni di ascolto: si dicono PER non interrompere. Trattarli come un
#: barge-in è il difetto più fastidioso di un vivavoce, perché punisce
#: esattamente chi sta collaborando. Lessico chiuso e corto di proposito: una
#: lista lunga comincia a mangiarsi le interruzioni vere.
BACKCHANNEL = {
    "sì", "si", "sisi", "sì sì", "certo", "ok", "okay", "va bene", "esatto",
    "giusto", "capito", "ho capito", "chiaro", "perfetto", "mmh", "mhm", "mm",
    "ah", "aha", "eh", "uhm", "bene", "vero", "d'accordo", "assolutamente",
    "yes", "yeah", "right", "sure",
}

#: Chi dice una di queste cose vuole la parola, adesso. È l'unico caso in cui
#: il ritardo si sente davvero — e, non per caso, è anche quello in cui un
#: modello non serve a niente.
STOP_ESPLICITO = re.compile(
    r"\b(basta|fermo|fermati|ferma|aspetta|aspetti|stop|zitto|taci|silenzio|"
    r"un attimo|un momento|no no|ho capito basta)\b",
    re.IGNORECASE)

#: "senti", "scusa", "ascolta" chiedono la parola solo in APERTURA. In mezzo a
#: una frase sono tutt'altro: "mi senti bene?" è una verifica del collegamento,
#: "scusa il ritardo" è una scusa. Costava un falso positivo su ogni "mi
#: senti", visto dal vivo al primo turno di prova.
ATTENZIONALE = re.compile(r"^(senti|senta|scusa|scusi|ascolta|ascolti)\b",
                          re.IGNORECASE)

#: Una domanda breve è un turno, non un cenno: "e domani?" sono nove caratteri
#: e vale come interruzione, mentre "va bene" ne ha sette e non vale.
DOMANDA = re.compile(r"[?]|^(che|chi|come|cosa|quando|quale|quanto|dove|perch)",
                     re.IGNORECASE)


def _parole(s: str) -> set:
    return set(re.findall(r"\w+", (s or "").lower()))


def sovrapposizione(testo: str, detto: str) -> float:
    """Quanta parte di `testo` è fatta di parole appena pronunciate dall'avatar.

    Il confronto è sulle parole e non sui caratteri perché l'eco si trascrive
    male: le vocali si perdono, la punteggiatura si inventa, ma le parole
    piene restano.
    """
    p = _parole(testo)
    if not p or not detto:
        return 0.0
    return len(p & _parole(detto)) / len(p)


def _normalizza(testo: str) -> str:
    return re.sub(r"[^\w\s']", "", (testo or "").strip().lower()).strip()


def classifica(testo: str, detto: str = "", durata_s: float = 0.0,
               anticipo_s: float = 0.0, integra_da_s: float = 2.5,
               consenti_cambia: bool = False) -> tuple[Esito, str]:
    """Decide cosa fare, e dice perché: il perché serve nei log più dell'esito.

    `detto` è ciò che l'avatar ha DAVVERO pronunciato negli ultimi secondi —
    non ciò che ha generato. La differenza è tutta la partita: confrontare
    l'eco col testo generato produce falsi positivi su materiale che nessuno
    ha mai sentito, ed è il difetto che il Copione esiste per risolvere.
    """
    t = (testo or "").strip()
    n = _normalizza(t)
    if not n:
        return Esito.IGNORA, "vuoto"

    # R1 — non è voce dell'utente: troppo corto, o una frase-fantasma di
    # quelle che Whisper produce sui silenzi ("Sottotitoli e revisione...").
    if len(n) < 3:
        return Esito.IGNORA, "troppo corto"
    try:
        from .engines.whisper_stt import _e_allucinazione
        if _e_allucinazione(t):
            return Esito.IGNORA, "frase-fantasma dello STT"
    except Exception:  # noqa: BLE001 - motore non importabile: si prosegue
        pass

    # R2 — l'eco della propria voce. Prima di tutto il resto: un'eco può
    # somigliare a qualunque cosa, comprese le parole di stop.
    ov = sovrapposizione(t, detto)
    if ov >= 0.6:
        return Esito.IGNORA, f"eco ({ov:.0%} di ciò che ho appena detto)"

    # R3 — cenno di ascolto. Va riconosciuto PRIMA dello stop esplicito:
    # "ok" e "va bene" sono cortesia, non un ordine di fermarsi.
    parole = n.split()
    if n in BACKCHANNEL or (len(parole) <= 3 and durata_s and durata_s < 1.2
                            and all(p in BACKCHANNEL for p in parole)):
        return Esito.IGNORA, "cenno di ascolto"

    # R4 — stop esplicito, senza passare da nessun modello: è il caso in cui
    # mezzo secondo di ritardo si sente come maleducazione.
    # Gli attenzionali hanno la soglia più stretta degli stop veri: da soli
    # chiedono la parola ("senti un attimo"), ma con un complemento dietro
    # sono già il turno ("scusa il ritardo di stamattina"), e quello può
    # aspettare la fine della frase.
    if len(parole) <= 5 and STOP_ESPLICITO.search(n):
        return Esito.FERMA, "stop esplicito"
    if len(parole) <= 3 and ATTENZIONALE.match(n):
        return Esito.FERMA, "chiede la parola"

    # R5 — voce vera ma troppo poca per essere un turno. Le domande brevi
    # sono l'eccezione: "e domani?" è corto ma è una richiesta.
    if len(n) < MIN_CARATTERI and not DOMANDA.search(t):
        return Esito.IGNORA, "troppo poco per essere un turno"

    # R6 — è un turno. Qui vivrebbe la domanda all'LLM; per ora si sceglie
    # con quel che si sa. CAMBIA solo se c'è abbastanza parlato non ancora
    # uscito da valere la pena di riscriverlo: sotto la soglia la
    # continuazione arriverebbe dopo essere già stata pronunciata.
    if consenti_cambia and anticipo_s >= integra_da_s:
        return Esito.CAMBIA, f"c'è margine ({anticipo_s:.1f}s non ancora detti)"
    # Nessun ripescaggio dello stop qui: una richiesta di tacere è corta, e R4
    # l'ha già vista. Cercarla anche nelle frasi lunghe vuol dire troncare chi
    # sta semplicemente parlando — dal vivo, "mi senti bene adesso?" fermava
    # la risposta a metà perché conteneva "senti".
    return Esito.ACCODA, "turno nuovo, può aspettare"
