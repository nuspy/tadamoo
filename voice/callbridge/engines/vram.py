"""Arbitro dei caricamenti in VRAM.

Problema che risolve: STT, TTS e avatar si caricano ciascuno per conto proprio,
spesso da thread di warm-up diversi. Due caricamenti che si sovrappongono
sommano i loro picchi, e su una GPU gia' occupata (l'LLM ne tiene la gran parte)
il driver non ha altra scelta che riversare una parte in memoria condivisa. Il
modello resta li' anche dopo, quando la VRAM si sarebbe liberata in pochi
millisecondi: da quel momento gira a una frazione della velocita'.

La regola e' una sola: **un caricamento alla volta**. Chi carica prende il
lucchetto, e chi vuole caricare mentre un altro sta caricando aspetta. Non e'
esclusivita' di residenza — due modelli possono restare in memoria insieme se
c'e' spazio — ma nessun picco si somma a un altro.

`scarica_prima()` completa la regola: quello che non serve piu' se ne va PRIMA
che entri il nuovo, non dopo.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("callbridge.vram")

# RLock: un caricamento puo' innescarne un altro annidato (il router scarica un
# motore e ne carica un altro) senza bloccarsi da solo.
_lucchetto = threading.RLock()
_in_corso: str | None = None


@contextmanager
def caricamento(nome: str):
    """Serializza un caricamento in VRAM. Logga l'attesa e la durata."""
    global _in_corso
    atteso = _in_corso
    t_attesa = time.monotonic()
    with _lucchetto:
        if atteso and atteso != nome:
            log.info("VRAM: '%s' ha atteso %.1fs che '%s' finisse di caricare",
                     nome, time.monotonic() - t_attesa, atteso)
        precedente, _in_corso = _in_corso, nome
        t0 = time.monotonic()
        try:
            yield
        finally:
            _in_corso = precedente
            durata = time.monotonic() - t0
            if durata >= 0.5:
                # "ha tenuto la GPU", non "caricato": il lucchetto avvolge tutto
                # il blocco del chiamante, e il router TTS ci mette dentro la
                # SINTESI intera, non solo il caricamento. Il vecchio messaggio
                # ("caricato in 21.0s") faceva sembrare un ricaricamento continuo
                # dei motori quello che era il tempo di sintesi di XTTS.
                log.info("VRAM: '%s' ha tenuto la GPU per %.1fs", nome, durata)


def scarica_prima(nome: str, scarica) -> None:
    """Esegue lo scarico PRIMA di un caricamento, dentro il lucchetto.

    Va chiamato prima di `caricamento()` per il componente entrante: liberare
    dopo aver allocato vanifica lo scopo, perche' il picco e' gia' avvenuto.
    """
    with _lucchetto:
        try:
            scarica()
        except Exception:  # noqa: BLE001 - uno scarico fallito non blocca il turno
            log.warning("VRAM: scarico prima di '%s' fallito", nome, exc_info=True)
