"""Quanto devono essere lunghi i chunk di parlato, deciso sui tempi veri.

Il problema. Ogni battuta costa un PREAMBOLO fisso — il TTS carica la frase,
MuseTalk estrae le feature whisper e calcola il primo blocco di fotogrammi —
e poi procede a una certa velocita'. Chunk corti pagano il preambolo tante
volte; chunk lunghi lo pagano di rado ma rendono piu' grossolana
l'interruzione, perche' chi interrompe sente proseguire la battuta in corso.

La formula. Perche' la bocca non accumuli ritardo, il chunk successivo deve
essere pronto prima che finisca quello in riproduzione:

    C + d/r <= d_precedente

con C preambolo fisso e r velocita' rispetto al tempo reale. A chunk di durata
uguale diventa d*(1 - 1/r) >= C, cioe':

    d_min = C / (1 - 1/r)

Due letture utili. Se r <= 1 il denominatore e' nullo o negativo: nessuna
lunghezza basta, il ritardo cresce comunque e l'unica via e' ridurre il numero
di battute. Se r e' grande, d_min tende a C: basta che il chunk duri quanto il
preambolo.

Perche' misurarlo invece di fissarlo. C dipende da cosa e' acceso: con l'avatar
in funzione il preambolo di MuseTalk (~2,5 s) domina, senza avatar resta solo
quello del TTS (~0,5 s) e i chunk possono essere molto piu' corti — il che
migliora l'interruzione proprio quando non costa niente. Dipende anche dalla
macchina, dal BATCH_SIZE e da quanto e' carica la GPU. Un numero fisso sarebbe
giusto in una configurazione sola.

Si impara anche quanti caratteri corrispondono a un secondo di parlato, che
lega la formula (in secondi) alla soglia del sanitizer (in caratteri). Si usano
i TOTALI e non le coppie frase-per-frase: il TTS a volte unisce due frasi in un
segmento solo, e un rapporto cumulativo non se ne accorge nemmeno.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("callbridge.chunk_policy")

#: Velocita' di MuseTalk rispetto al tempo reale. Misurata sul worker a
#: BATCH_SIZE=4 (55 fps generati contro i 25 riprodotti). Non e' osservabile
#: dal bridge — il worker vive in un altro processo — quindi e' un parametro,
#: da rivedere se cambia BATCH_SIZE o la GPU.
_R_LABIALE = float(os.environ.get("CALLBRIDGE_R_LABIALE") or 2.2)
#: Velocita' del TTS rispetto al tempo reale. MISURATA con
#: `tools/misura-tts.py` su XTTS caldo, con la voce vera e frasi italiane di
#: 40/80/160/320 caratteri: 1,43 - 1,50x, mediana 1,46. Il rapporto e'
#: notevolmente stabile al variare della lunghezza, il che da' fiducia nel
#: numero.
#:
#: Qui c'era 4.0, e in ptt.py si legge il contrario ("XTTS va a 0,86x
#: realtime, cioe' piu' piano di quanto l'avatar parli"): erano SBAGLIATE
#: entrambe, e nessuna delle due veniva da una misura strumentata. La
#: differenza conta perche' e' il margine che consente di preparare la frase
#: successiva mentre la corrente suona — a 1,46x per ogni secondo di parlato
#: se ne sintetizzano 1,46, quindi il vantaggio si accumula, ma piano.
#:
#: Correggerlo NON cambia il comportamento di oggi, verificato: con l'avatar
#: acceso domina comunque il preambolo del labiale, con l'avatar spento si
#: finisce sul minimo di 40 caratteri in entrambi i casi.
_R_TTS = float(os.environ.get("CALLBRIDGE_R_TTS") or 1.46)

#: Estremi della soglia in caratteri. Il minimo evita clip-moncone quando le
#: misure sono ancora poche; il massimo impedisce che una macchina lenta
#: trasformi ogni risposta in un unico blocco ininterrompibile.
_MIN_CARATTERI = 40
_MAX_CARATTERI = 400
#: Finche' non si e' visto abbastanza parlato, si usa il valore di ripiego.
_MIN_CAMPIONI_S = 4.0
_RIPIEGO_CARATTERI = 120
#: Caratteri al secondo di ripiego (italiano, XTTS a velocita' 1.0). Misurato
#: sulle battute vere: 66 caratteri in 3,7s e 79 in 4,7s fanno 17,3-17,8. Il
#: 12,5 di prima veniva da una stima a occhio e sbagliava per difetto, cioe'
#: faceva ATTENDERE audio che non sarebbe mai arrivato.
_CPS_RIPIEGO = 17.5

_lock = threading.Lock()
_stato = {
    "caratteri": 0.0,      # testo mandato al TTS
    "durata": 0.0,         # parlato tornato indietro
    "preambolo": 0.0,      # media mobile dell'avvio labiale (s)
    "campioni_pre": 0,
}
#: Peso della misura nuova nella media mobile: il preambolo varia parecchio
#: (GPU carica, worker appena svegliato), quindi si insegue piano.
_ALFA = 0.3


def osserva_testo(n_caratteri: int) -> None:
    """Testo consegnato al TTS."""
    if n_caratteri > 0:
        with _lock:
            _stato["caratteri"] += n_caratteri


def osserva_audio(durata_s: float) -> None:
    """Parlato prodotto dal TTS."""
    if durata_s > 0:
        with _lock:
            _stato["durata"] += durata_s


def osserva_labiale(attesa_s: float) -> None:
    """Quanto e' durato l'avvio del labiale per la prima battuta del turno.

    Zero e' un'informazione valida, non un campione da scartare: significa
    avatar spento, e in quel caso i chunk devono tornare corti.
    """
    if attesa_s < 0:
        return
    with _lock:
        if _stato["campioni_pre"] == 0:
            _stato["preambolo"] = attesa_s
        else:
            _stato["preambolo"] = (1 - _ALFA) * _stato["preambolo"] + _ALFA * attesa_s
        _stato["campioni_pre"] += 1


def caratteri_al_secondo() -> float:
    with _lock:
        car, dur = _stato["caratteri"], _stato["durata"]
    if dur < _MIN_CAMPIONI_S or car <= 0:
        return _CPS_RIPIEGO
    return car / dur


def _durata_minima(preambolo: float, r: float) -> float:
    """d_min = C / (1 - 1/r), con r <= 1 trattato come "nessun margine"."""
    if r <= 1.0:
        return float(_MAX_CARATTERI)      # oltre il tetto: si usera' il tetto
    return preambolo / (1.0 - 1.0 / r)


def min_caratteri() -> int:
    """Soglia consigliata per i chunk DOPO il primo."""
    forzato = os.environ.get("CALLBRIDGE_TTS_MIN_CHARS")
    if forzato:
        return int(forzato)
    with _lock:
        pre_lab, n = _stato["preambolo"], _stato["campioni_pre"]
    if n == 0:
        return _RIPIEGO_CARATTERI
    # Il preambolo del TTS (~0,5 s) c'e' sempre; quello del labiale solo se
    # l'avatar sta animando. Le due fasi stanno in processi diversi e si
    # sovrappongono, quindi vincola la piu' lenta, non la somma.
    d_tts = _durata_minima(0.5, _R_TTS)
    d_lab = _durata_minima(pre_lab, _R_LABIALE) if pre_lab > 0.05 else 0.0
    secondi = max(d_tts, d_lab)
    car = int(secondi * caratteri_al_secondo())
    return max(_MIN_CARATTERI, min(_MAX_CARATTERI, car))


#: Quanto si puo' tenere in caldo una frase gia' completa, sperando che ne
#: arrivi un'altra da accorpare. Il testo arriva in streaming: quando la prima
#: frase e' finita il resto della risposta spesso NON e' ancora stato generato,
#: e aspettarlo per risparmiare un preambolo e' un pessimo affare se intanto la
#: pipeline resta ferma. Mezzo secondo e' circa il preambolo del TTS: oltre, si
#: sta perdendo piu' di quanto si risparmi.
_ATTESA_MAX_S = float(os.environ.get("CALLBRIDGE_TTS_ATTESA_MAX") or 0.6)


def attesa_massima() -> float:
    """Per quanto vale la pena trattenere una frase completa sotto soglia."""
    return _ATTESA_MAX_S


#: Quanto si puo' attendere OLTRE il silenzio normale quando l'audio raccolto
#: sembra meno del previsto. E' un tetto stretto di proposito: la previsione si
#: basa su una media di caratteri al secondo, e una media sbagliata per difetto
#: fa aspettare audio che non arrivera' mai. Visto dal vivo: sei secondi persi
#: a ogni battuta perche' la stima diceva 5,3s di parlato dove ne uscivano 3,7.
#: Meglio chiudere una battuta un filo presto che allungare ogni turno.
_ATTESA_EXTRA_MAX_S = 2.0


def attesa_extra_massima() -> float:
    return _ATTESA_EXTRA_MAX_S


def stato() -> dict:
    """Riepilogo leggibile, per i log e la diagnostica."""
    with _lock:
        pre, n = _stato["preambolo"], _stato["campioni_pre"]
    return {
        "preambolo_labiale_s": round(pre, 2),
        "campioni": n,
        "caratteri_al_secondo": round(caratteri_al_secondo(), 1),
        "campioni_cps": _stato["durata"] >= _MIN_CAMPIONI_S and _stato["caratteri"] > 0,
        "min_caratteri": min_caratteri(),
        "attesa_max_s": attesa_massima(),
    }
