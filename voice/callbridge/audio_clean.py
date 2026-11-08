"""Pulizia dell'audio sintetizzato, prima che arrivi all'orecchio.

Per ora una cosa sola: il colpo di coda di XTTS.
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("callbridge.audio_clean")

#: Finestra dell'inviluppo di energia (s). 20 ms e' il compromesso solito fra
#: seguire le sillabe e non inseguire le singole oscillazioni.
_FINESTRA_S = 0.02
#: Sotto questa frazione del picco si considera silenzio.
_SOGLIA = 0.05


def rifila_artefatto_coda(samples: np.ndarray, rate: int,
                          pausa_min: float = 0.35,
                          coda_max: float = 0.35,
                          pausa_lampo: float = 0.10,
                          coda_lampo: float = 0.20,
                          margine: float = 0.10) -> np.ndarray:
    """Toglie il frammento che XTTS aggiunge DOPO la fine della frase.

    Il difetto, sentito e poi misurato sulle clip vere: alla fine di una
    battuta, dopo una pausa netta, compare un frammento brevissimo — una
    vocale o l'attacco di una parola — spesso al massimo volume, che suona
    squillante e non c'entra col testo. Trascrivendo la sola coda di 66 clip
    reali: 0,98 s di pausa poi 0,06 s che non dicono niente; 0,58 s di pausa
    poi 0,18 s che diventano "But" in una risposta italiana.

    Come si distingue da una coda VERA: il parlato non riprende dopo mezzo
    secondo di silenzio per due sillabe e poi finisce. Le code legittime
    misurate ("tesoro.", "Procediamo.") durano 0,5-0,7 s, oppure seguono
    pause brevi. Quindi si taglia solo se la pausa e' lunga E il frammento
    e' corto: sui 66 campioni la regola ne ha ripuliti 7 senza toccare
    nessuna coda vera.

    Nel dubbio non si taglia: perdere una parola vera e' molto peggio che
    lasciare un artefatto.
    """
    if samples is None or len(samples) == 0 or rate <= 0:
        return samples
    a = np.asarray(samples, dtype=np.float32)
    passo = max(1, int(_FINESTRA_S * rate))
    n = len(a) // passo
    if n < 5:
        return samples
    env = np.sqrt((a[:n * passo].reshape(n, passo) ** 2).mean(axis=1))
    picco = float(env.max())
    if picco <= 0:
        return samples
    forti = np.flatnonzero(env > picco * _SOGLIA)
    if len(forti) < 3:
        return samples

    # Ultimo stacco di silenzio dentro il parlato.
    salti = np.flatnonzero(np.diff(forti) > 1)
    if len(salti) == 0:
        return samples
    k = salti[-1]
    pausa = float(forti[k + 1] - forti[k]) * _FINESTRA_S
    durata = float(forti[-1] - forti[k + 1] + 1) * _FINESTRA_S
    # Due forme dello stesso difetto, con soglie diverse:
    #
    #  - coda LUNGHINA dopo una pausa NETTA (>=0,35s): e' il caso trovato per
    #    primo, frammenti di 0,06-0,18s dopo quasi un secondo di silenzio;
    #  - coda BREVISSIMA dopo una pausa qualsiasi (>=0,10s): sentito come
    #    "punto u" a fine frase. Qui la pausa e' corta (0,12-0,16s) ma il
    #    frammento e' di 0,08-0,14s, e a quella durata non c'e' parlato: la
    #    sillaba italiana piu' breve sfiora il decimo di secondo, e comunque
    #    non compare ISOLATA dopo una pausa a fine battuta. Trascrivendone una:
    #    'day.', una parola inglese dentro una clip italiana.
    #
    # Il discriminante e' la durata, non la pausa: le code vere misurate
    # ('tesoro.' 0,54s, 'Procediamo.' 0,74s, 'pigra.' 0,34s) stanno tutte sopra.
    breve = pausa >= pausa_lampo and durata <= coda_lampo
    lunga = pausa >= pausa_min and durata <= coda_max
    if not (breve or lunga):
        return samples

    # Si taglia dopo il corpo, lasciando un margine di respiro: tagliare
    # esattamente sull'ultimo campione forte mozzerebbe la coda naturale
    # della parola.
    fine = int((forti[k] + 1) * passo + margine * rate)
    if fine >= len(a):
        return samples
    log.info("TTS: tolto artefatto di coda (%.2fs dopo %.2fs di pausa)",
             durata, pausa)
    return a[:fine]
