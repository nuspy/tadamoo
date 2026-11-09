"""Da testo italiano a una timeline di visemi, per il lipsync renderizzato sul client.

È il cuore della modalità "visemi" (modo C): invece di generare un video
dell'avatar sul PC — un flusso per ogni utente, che non scala — il gateway
manda al client il TESTO che sta pronunciando e QUANDO ogni forma di bocca
cade nel tempo. Il client anima la foto localmente. Pochi byte al posto di un
video H.264, e il PC non renderizza pixel per nessuno.

**Multilingue, in due strati.** L'avatar non parla solo italiano, e il ponte
fra le lingue è l'IPA: le forme di bocca (i visemi) sono universali, cambia
solo come si arriva ai suoni. Quindi *testo → IPA* dipende dalla lingua (lo fa
espeak-ng, ~100 lingue) e *IPA → viseme* è una mappa sola per tutte. I visemi
sono grossolani per costruzione — /p/, /b/ e /m/ chiudono le labbra allo stesso
modo, l'occhio non distingue — quindi la mappa è corta e stabile.

**Se espeak-ng non c'è**, si ripiega su regole per le lingue quasi-fonetiche
(l'italiano per primo): peggio dell'IPA, ma il lipsync non si spegne per un
binario mancante. La degradazione è trasparente e segnalata nel log.

**Il timing, senza allineatore.** Non c'è un forced-aligner che dica a che
istante esce ogni suono, quindi si distribuisce il testo sulla durata
dell'audio — che il TTS conosce — pesando le vocali più delle consonanti (una
"a" dura in bocca più di una "t"). È un'approssimazione, ma il lipsync a questa
grana è tollerante: il cervello accetta uno scarto di decine di millisecondi
senza accorgersene. Se un giorno servirà più precisione, il posto dove
inserirla è `_pesi`, non l'architettura.

Il set di visemi è compatto e mappabile a immagini di bocca (uno sprite per
viseme, generato dalla foto sul PC): è vicino al set Oculus, che è lo standard
degli avatar realtime.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("callbridge.visemi")

#: I visemi: una forma di bocca ciascuno. `sil` è la bocca a riposo.
VISEMI = ("sil", "PP", "FF", "TH", "DD", "kk", "CH", "SS", "nn", "RR",
          "aa", "E", "I", "O", "U")

#: Lettera italiana -> viseme. Gruppi che la bocca non distingue condividono la
#: forma: p/b/m chiudono le labbra, f/v le mordono, le vocali aprono.
_LETTERA = {
    "a": "aa", "à": "aa",
    "e": "E", "è": "E", "é": "E",
    "i": "I", "ì": "I", "í": "I", "j": "I",
    "o": "O", "ò": "O", "ó": "O",
    "u": "U", "ù": "U", "ú": "U",
    "p": "PP", "b": "PP", "m": "PP",
    "f": "FF", "v": "FF",
    "t": "DD", "d": "DD",
    "n": "nn",
    "l": "DD",
    "r": "RR",
    "s": "SS", "z": "SS",
    "c": "kk", "k": "kk", "g": "kk", "q": "kk", "x": "kk",
    "h": "sil",                 # muta in italiano
    "w": "U", "y": "I",
}

#: Quanto "dura" ogni tipo di viseme, in unità relative: le vocali tengono la
#: bocca aperta più a lungo delle consonanti. Serve a spartire il tempo, non è
#: una durata in secondi.
_PESI = {
    "aa": 1.4, "E": 1.2, "I": 1.1, "O": 1.3, "U": 1.2,
    "PP": 0.7, "FF": 0.8, "DD": 0.7, "nn": 0.7, "RR": 0.7,
    "SS": 0.9, "kk": 0.7, "CH": 0.9, "TH": 0.8, "sil": 0.6,
}

#: Sotto questa durata due visemi uguali di fila si fondono: "mamma" non deve
#: sbattere le labbra quattro volte distinte, sarebbe un tremolio.
_FUSIONE_S = 0.04

#: Simbolo IPA -> viseme. È la mappa UNIVERSALE, uguale per tutte le lingue: le
#: forme di bocca non cambiano col paese. Si guardano i primi caratteri di ogni
#: simbolo (le legature come t͡ʃ hanno più code point), quindi l'ordine di
#: ricerca va dai simboli lunghi ai corti — ci pensa `_ipa_a_visemi`.
_IPA_VISEME = {
    # vocali
    "a": "aa", "ɑ": "aa", "ɐ": "aa", "æ": "aa", "ʌ": "aa",
    "e": "E", "ɛ": "E", "ə": "E", "ɜ": "E", "œ": "E", "ø": "E",
    "i": "I", "ɪ": "I", "ɨ": "I", "y": "I",
    "o": "O", "ɔ": "O", "ɒ": "O",
    "u": "U", "ʊ": "U", "ʉ": "U",
    # consonanti, per luogo d'articolazione (la bocca)
    "p": "PP", "b": "PP", "m": "PP", "ɓ": "PP",
    "f": "FF", "v": "FF", "ʋ": "FF", "ɱ": "FF",
    "θ": "TH", "ð": "TH",
    "t": "DD", "d": "DD", "n": "nn", "l": "DD", "ɗ": "DD", "ɲ": "nn",
    "s": "SS", "z": "SS",
    "ʃ": "CH", "ʒ": "CH", "tʃ": "CH", "dʒ": "CH", "t͡ʃ": "CH", "d͡ʒ": "CH",
    "ʂ": "CH", "ʐ": "CH", "ɕ": "CH", "ʑ": "CH",
    "k": "kk", "g": "kk", "ɡ": "kk", "ŋ": "kk", "x": "kk", "ɣ": "kk",
    "q": "kk", "χ": "kk", "h": "sil", "ɦ": "sil", "ʔ": "sil",
    "r": "RR", "ɾ": "RR", "ʁ": "RR", "ɽ": "RR", "ɹ": "RR", "ʀ": "RR",
    "w": "U", "ʍ": "U", "j": "I", "ɥ": "U",
}
#: I simboli IPA a più code point, provati per primi (le legature affricate).
_IPA_MULTI = sorted((k for k in _IPA_VISEME if len(k) > 1), key=len, reverse=True)

#: Segni diacritici e di prosodia che espeak mette nell'IPA e che non sono
#: suoni: accenti (ˈˌ), lunghezza (ː), legature, spazi di sillaba.
_IPA_SCARTO = str.maketrans("", "", "ˈˌːˑ.͜͡ ")


def _trova_espeak() -> tuple[str, dict] | None:
    """(eseguibile, env) di espeak-ng, o None se non c'è.

    Su Windows la build di scoop segfaulta senza `ESPEAK_DATA_PATH`: la si
    cerca accanto all'eseguibile e la si passa nell'ambiente. Il risultato è
    memoizzato in `_ESPEAK` perché questa ricerca costa qualche syscall e il
    generatore gira una volta per frase.
    """
    # Dalla config del bridge, poi dal PATH, poi dall'ambiente: il servizio
    # gira senza il PATH dell'utente, quindi il percorso in config e' la via
    # affidabile; il PATH resta comodo in sviluppo.
    da_cfg = ""
    dati_cfg = ""
    try:
        from .config import Config
        c = Config.load()
        da_cfg = getattr(c, "espeak_ng_exe", "") or ""
        dati_cfg = getattr(c, "espeak_data_path", "") or ""
    except Exception:  # noqa: BLE001 - la config non deve far cadere i visemi
        pass
    exe = (da_cfg or shutil.which("espeak-ng") or shutil.which("espeak")
           or os.environ.get("ESPEAK_NG_EXE") or "")
    if not exe or not os.path.isfile(exe):
        return None
    from .models_store import child_env
    env = child_env()
    if dati_cfg and os.path.isdir(dati_cfg):
        env["ESPEAK_DATA_PATH"] = dati_cfg
    if "ESPEAK_DATA_PATH" not in env:
        # scoop mette i dati accanto alla vera exe, non allo shim.
        base = os.path.dirname(os.path.realpath(exe))
        for cand in (os.path.join(base, "espeak-ng-data"),
                     os.path.join(base, "..", "espeak-ng-data")):
            if os.path.isdir(cand):
                env["ESPEAK_DATA_PATH"] = os.path.abspath(cand)
                break
    return exe, env


_ESPEAK: "tuple[str, dict] | None | bool" = False   # False = non ancora cercato


def _espeak() -> tuple[str, dict] | None:
    global _ESPEAK
    if _ESPEAK is False:
        _ESPEAK = _trova_espeak()
        if _ESPEAK is None:
            log.info("visemi: espeak-ng non trovato, uso le regole italiane")
    return _ESPEAK  # type: ignore[return-value]


def _ipa_a_visemi(ipa: str) -> list[str]:
    """Una stringa IPA nella sua sequenza di visemi."""
    fuori: list[str] = []
    i = 0
    while i < len(ipa):
        preso = None
        for m in _IPA_MULTI:                 # prima le legature affricate
            if ipa.startswith(m, i):
                preso = (m, _IPA_VISEME[m]); break
        if preso is None:
            v = _IPA_VISEME.get(ipa[i])
            preso = (ipa[i], v) if v else (ipa[i], None)
        sym, vis = preso
        if vis and not (fuori and fuori[-1] == vis):
            fuori.append(vis)
        i += len(sym)
    return fuori


def _sequenza_ipa(testo: str, lang: str) -> list[str] | None:
    """Il testo in visemi passando per l'IPA di espeak. None se non disponibile."""
    trovato = _espeak()
    if trovato is None:
        return None
    exe, env = trovato
    voce = _VOCE_ESPEAK.get((lang or "").split("-")[0].lower(), lang or "en-us")
    fuori: list[str] = []
    for token in re.findall(r"[^\s.,;:!?]+|[.,;:!?]+", testo or ""):
        if token[0] in ".,;:!?":
            fuori.append("sil")
            continue
        try:
            out = subprocess.run(
                [exe, "-v", voce, "--ipa", "-q", token],
                capture_output=True, text=True, encoding="utf-8",
                env=env, timeout=5)
            ipa = (out.stdout or "").strip().translate(_IPA_SCARTO)
        except Exception as e:  # noqa: BLE001 - se espeak inciampa, si ripiega
            log.warning("visemi: espeak ha fallito (%s), ripiego sulle regole", e)
            return None
        vis = _ipa_a_visemi(ipa)
        for v in vis:
            if not (fuori and fuori[-1] == v):
                fuori.append(v)
        fuori.append("sil")
    while fuori and fuori[0] == "sil":
        fuori.pop(0)
    while fuori and fuori[-1] == "sil":
        fuori.pop()
    return fuori


#: Codice lingua (ISO 639-1) -> voce espeak-ng. espeak usa nomi propri per
#: alcune (en-us, pt-br); le altre coincidono col codice.
_VOCE_ESPEAK = {
    "it": "it", "en": "en-us", "fr": "fr", "de": "de", "es": "es",
    "pt": "pt-br", "nl": "nl", "pl": "pl", "ru": "ru", "uk": "uk",
    "ja": "ja", "zh": "cmn", "ko": "ko", "ar": "ar", "hi": "hi",
    "tr": "tr", "sv": "sv", "cs": "cs", "el": "el", "hu": "hu",
}


@dataclass
class Viseme:
    t: float          # istante d'inizio, secondi dall'avvio dell'audio
    v: str            # una voce di VISEMI
    dur: float        # durata, secondi


def _digrammi(parola: str) -> list[str]:
    """Spezza una parola nei suoni che contano per la bocca.

    Non è una sillabazione: raggruppa solo i digrammi che cambiano il viseme —
    "ch"/"gh" suonano /k/ (non /tʃ/), "sc" davanti a e/i è /ʃ/ (viseme CH),
    "gn" è un nasale unico. Tutto il resto va lettera per lettera.
    """
    s = parola
    unita: list[str] = []
    i = 0
    while i < len(s):
        due = s[i:i + 2]
        if due in ("ch", "gh"):
            unita.append("kk"); i += 2; continue
        if due == "gn":
            unita.append("nn"); i += 2; continue
        if due == "sc" and i + 2 < len(s) and s[i + 2] in "eiéèíì":
            unita.append("CH"); i += 2; continue
        if s[i] in "cg" and i + 1 < len(s) and s[i + 1] in "eiéèíì":
            unita.append("CH"); i += 1; continue      # c/g dolci -> /tʃ,dʒ/
        unita.append(_LETTERA.get(s[i], "sil"))
        i += 1
    return unita


def _sequenza(testo: str) -> list[str]:
    """Il testo intero in una sequenza di visemi, con `sil` alle pause."""
    testo = unicodedata.normalize("NFC", (testo or "").lower())
    fuori: list[str] = []
    for token in re.findall(r"[a-zàèéìíòóùú]+|[.,;:!?]+|\s+", testo):
        if not token.strip():
            continue
        if token[0] in ".,;:!?":
            fuori.append("sil")                        # la pausa è bocca chiusa
            continue
        vis = _digrammi(token)
        # doppie: "nn", "tt" sono un solo gesto di bocca
        for v in vis:
            if fuori and fuori[-1] == v:
                continue
            fuori.append(v)
        fuori.append("sil")                            # stacco tra le parole
    while fuori and fuori[0] == "sil":
        fuori.pop(0)
    while fuori and fuori[-1] == "sil":
        fuori.pop()
    return fuori


def genera(testo: str, durata_s: float, lang: str = "it") -> list[Viseme]:
    """La timeline dei visemi che copre esattamente `durata_s` secondi.

    `durata_s` è la lunghezza dell'audio TTS di questo testo: la conosce il
    gateway, che ha appena sintetizzato. I visemi si spartiscono quel tempo in
    proporzione al loro peso, così il parlato e la bocca finiscono insieme.

    `lang` sceglie la voce espeak per il passaggio testo→IPA. Se espeak non c'è
    si ripiega sulle regole, che coprono bene solo l'italiano: per le altre
    lingue senza espeak il lipsync resta plausibile ma approssimato.
    """
    seq = _sequenza_ipa(testo, lang)
    if seq is None:                     # niente espeak: regole (italiano)
        seq = _sequenza(testo)
    if not seq or durata_s <= 0:
        return [Viseme(0.0, "sil", max(durata_s, 0.0))] if durata_s > 0 else []
    peso_tot = sum(_PESI.get(v, 0.8) for v in seq)
    passo = durata_s / peso_tot
    fuori: list[Viseme] = []
    t = 0.0
    for v in seq:
        d = _PESI.get(v, 0.8) * passo
        if fuori and fuori[-1].v == v and d < _FUSIONE_S:
            fuori[-1].dur += d
        else:
            fuori.append(Viseme(t, v, d))
        t += d
    return fuori


def timeline(testo: str, durata_s: float, lang: str = "it") -> dict:
    """The message for the WebSocket: text, duration and visemes, times in whole milliseconds."""
    vs = genera(testo, durata_s, lang)
    return {
        "type": "visemes",
        "text": testo,
        "lang": lang,
        "duration_ms": round(durata_s * 1000),
        "visemes": [{"t": round(v.t * 1000), "v": v.v, "dur": round(v.dur * 1000)} for v in vs],
    }
