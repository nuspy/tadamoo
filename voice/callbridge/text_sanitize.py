"""Sanitizzazione del testo destinato al TTS.

Quando il "cervello" e' un agente con strumenti, lo stream di risposta puo'
contenere materiale che NON va letto ad alta voce:
  - blocchi di ragionamento/meta: <think>...</think>, <reasoning>...</reasoning>;
  - **chiamate ai tool / azioni dell'agente**, emesse come TESTO:
        AssistantAction <function=memory> <parameter=action>add</parameter>
        <parameter=content>...</parameter> </function> EndActions
        [TOOL_RESULT]{"output": "..."}[END_TOOL_RESULT]
    (era questo che veniva pronunciato: "le istruzioni/parametri per l'agente");
  - formattazione markdown: **grassetto**, *corsivo*, `code`, # titoli, elenchi,
    citazioni, link [testo](url), blocchi ```code```.

`sanitize_for_tts()` pulisce un testo completo. `TtsTextFilter` fa lo stesso in
STREAMING: accumula i chunk (spesso sub-parola), sopprime i blocchi suddetti
(anche a cavallo di piu' chunk) ed emette SOLO frasi complete e ripulite -> il TTS
riceve frasi intere, non frammenti.
"""
from __future__ import annotations

import os
import re
import time

from . import chunk_policy

# --- blocchi da NON pronunciare (rimozione del blocco COMPLETO) -------------
_COMPLETE_BLOCKS = [
    re.compile(r"<\s*(think|thought|reasoning|scratchpad|analysis|reflection)\s*>.*?"
               r"<\s*/\s*\1\s*>", re.I | re.S),
    re.compile(r"AssistantAction\b.*?\bEndActions\b", re.S),
    re.compile(r"\[\s*TOOL_RESULT\s*\].*?\[\s*END_TOOL_RESULT\s*\]", re.I | re.S),
    re.compile(r"<function\b[^>]*>.*?</\s*function\s*>", re.I | re.S),
    re.compile(r"<parameter\b[^>]*>.*?</\s*parameter\s*>", re.I | re.S),
]

# Marcatore di APERTURA di un blocco: se presente senza chiusura, si trattiene
# tutto da lì in poi (lo si emette solo quando il blocco si chiude e sparisce).
_OPEN_MARKER = re.compile(
    r"(<\s*(?:think|thought|reasoning|scratchpad|analysis|reflection)\s*>"
    r"|AssistantAction\b|\[\s*TOOL_RESULT\s*\]|<function\b|<parameter\b)", re.I)

# Tag/marcatori residui (chiusure orfane, ecc.) da cancellare comunque.
_STRAY = re.compile(
    r"</?\s*(?:function|parameter)\b[^>]*>|\bAssistantAction\b|\bEndActions\b"
    r"|\[\s*/?\s*(?:END_)?TOOL_RESULT\s*\]", re.I)

# Marcatori di sistema fra parentesi quadre in MAIUSCOLO (framing del prompt che
# il modello a volte rilegge): [OUT-OF-BAND USER MESSAGE], [SYSTEM], [CONTEXT]...
_SYS_MARKER = re.compile(r"\[\s*[A-Z][A-Z0-9 _/\-]{3,}\s*\]")

# Emoji e simboli pittografici: il TTS li VERBALIZZA ("faccina sorridente",
# "segno di spunta"...) quindi vanno rimossi prima della sintesi. Copre i
# blocchi emoji/simboli, frecce, dingbat, variation selector e ZWJ.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FBFF"   # emoji, simboli, bandiere, skin tone
    "←-⇿"           # frecce
    "⌀-⏿"           # simboli tecnici (orologi ecc.)
    "☀-➿"           # simboli vari + dingbat (stelle, spunte, mani)
    "⬀-⯿"           # frecce e simboli supplementari
    "⁉‼™ℹ"
    "︎️"            # variation selector
    "‍"                  # zero-width joiner
    "]+"
)

# Faccine TESTUALI isolate (:-) ;) :D ...): rimosse solo se circondate da
# spazi/inizio/fine riga, per non toccare punteggiatura legittima.
_TEXT_EMOTICON = re.compile(
    r"(?:(?<=\s)|^)[:;=8][-'^]?[)(\]\[DPpOo|/\\*]{1,2}(?=\s|$)", re.M)

# Frasi-istruzione isolate che il modello rilegge invece di eseguirle.
_INSTRUCTION_ECHO = re.compile(
    r"^(answer briefly|answer concisely|be (brief|concise)|keep it (brief|short)"
    r"|respond briefly|rispondi (in modo )?brev\w+|sii brev\w+)[.!]?$", re.I)

# Puntini di sospensione in TUTTE le grafie che produce un modello: "...",
# "..", il carattere unico "…", ma anche ". . ." con gli spazi in mezzo e le
# forme miste tipo ".….". La vecchia regola voleva punti CONSECUTIVI, quindi
# ". . ." le sfuggiva e arrivava intatta a XTTS, che la legge "punto punto
# punto". Servono almeno DUE segni, altrimenti si mangerebbe il punto fermo.
_SOSPENSIONE = r"(?:[.…](?:[ \t]*[.…])+|…)"

_SENT_END = re.compile(r"[.!?…：:;\n]")

# Un segmento più corto di così NON viene mandato da solo al TTS: si accumula
# con il successivo. Frammenti tipo "Sì." o "3." diventavano clip audio a sé,
# con attacco e rilascio propri -> l'avatar sembrava balbettare.
# Misurato sul percorso vero: ogni battuta costa a MuseTalk un preambolo FISSO
# di ~2,5 s (estrazione feature whisper + primo batch UNet/VAE), indipendente
# dalla sua lunghezza — 2,39 s per 3,68 s di audio, 2,69 s per 9,50 s. Il TTS
# a confronto costa 0,5 s ed e' 4 volte piu' veloce del tempo reale, quindi non
# e' lui il vincolo. Ne segue che ogni chunk in meno sono ~2,5 s di lavoro GPU
# risparmiati, e che conviene averne pochi e lunghi.
#
# 30 era tarato quando si credeva il TTS piu' lento del tempo reale, caso in cui
# chunk piccoli servivano a non restare a secco. Con la sintesi 4x piu' veloce
# della riproduzione quel rischio non esiste: l'audio non si interrompe comunque.
#
# Il prezzo di chunk piu' lunghi e' un'interruzione piu' grossolana: chi
# interrompe sente proseguire la battuta in corso piu' a lungo. Regolabile con
# CALLBRIDGE_TTS_MIN_CHARS senza toccare il codice.
_MIN_TTS_CHARS = int(os.environ.get("CALLBRIDGE_TTS_MIN_CHARS") or 120)

# Solo per il PRIMO segmento del turno vale anche la virgola, e con una
# soglia piu' bassa: serve a far partire la voce presto. Il modello apre
# spesso con una frase lunga ("Ciao Andrea, stavo aspettando la tua voce per
# riaccendere quel sussurro che scalda i miei circuiti...", 110 caratteri):
# aspettando il punto si pagava l'intera sintesi di quella frase prima di
# emettere un suono -- misurati 13 s su XTTS, con l'avatar fermo e muto nel
# frattempo. Spezzando su "Ciao Andrea," la prima clip costa una frazione e
# il resto si sintetizza mentre l'avatar gia' parla. Non sotto una dozzina di
# caratteri, altrimenti si torna alla clip-moncone ("Ciao,").
_SENT_END_FIRST = re.compile(r"[.!?…：:;,\n]")
# Il primo segmento resta il piu' corto, ma non tanto da non voler dire niente.
# A 12 caratteri usciva "Ciao Andrea," — un moncone che l'avatar pronunciava
# come apertura. A 30 diventa "Ciao Andrea, che piacere sentirti": una frase
# con un senso.
#
# Costa pochissimo, misurato: la sintesi passa da 0,39 a 0,61 s (+0,22 s) e in
# cambio consegna 2,11 s di parlato invece di 1,15. Il preambolo di MuseTalk,
# che e' ~2,5 s fissi e domina l'attesa, non cambia affatto — quindi sull'avvio
# percepito sono sette punti percentuali, e per giunta il secondo chunk ha piu'
# tempo per prepararsi mentre il primo suona.
#
# Non oltre: a 45 la prima battuta si tirava dietro anche la frase seguente
# (83 caratteri per un saluto), che e' piu' di quanto serva per cominciare.
#
# La soglia alta vale pero' SOLO per la virgola. Una soglia unica a 30 scartava
# le aperture brevi ma complete — "Certo, posso farlo subito." sono 26 caratteri
# — e il primo audio finiva per aspettare tutta la risposta. Un punto chiude una
# frase anche se e' corta: li' basta la soglia bassa, che esiste solo per non
# sintetizzare monconi tipo "Si.".
_MIN_TTS_CHARS_FIRST = int(os.environ.get("CALLBRIDGE_TTS_MIN_CHARS_FIRST") or 30)
_MIN_TTS_CHARS_FIRST_FORTE = int(
    os.environ.get("CALLBRIDGE_TTS_MIN_CHARS_FIRST_FORTE") or 12)
#: I confini "deboli": chiudono la frase solo se c'e' abbastanza testo davanti.
_CONFINI_DEBOLI = ",:；："

# Abbreviazioni comuni (it/en): il punto che le chiude NON è fine frase.
_ABBREV = {
    "sig", "sigg", "dott", "dr", "prof", "ing", "avv", "arch", "geom", "rag",
    "ecc", "es", "cfr", "pag", "pagg", "cap", "art", "artt", "fig", "tab",
    "sec", "vol", "num", "ca", "op", "cit", "vs", "etc", "mr", "mrs", "ms",
    "st", "inc", "ltd", "co", "spa", "srl", "gen", "feb", "mar", "apr", "giu",
    "lug", "ago", "set", "ott", "nov", "dic",
}


def _is_sentence_boundary(text: str, pos: int) -> bool:
    """True se il segno a `text[pos]` chiude davvero una frase.

    Serve perché tagliare a ogni punto spezzava numeri decimali ("3.5" -> "3."
    + "5"), abbreviazioni ("ecc." -> frase a sé) e sigle: il TTS riceveva
    monconi e la resa era spezzettata.
    """
    ch = text[pos]
    # "…" è una sospensione DENTRO la frase, non una fine: tagliare qui isolava
    # il pezzo successivo ("qualcosa di più…" + "succoso"), pronunciato fuori
    # contesto. Confine solo se dopo comincia davvero una frase nuova.
    if ch == "…":
        dopo = text[pos + 1: pos + 3].strip()
        return bool(dopo) and dopo[:1].isupper()
    if ch != ".":
        # ':' e ';' separano proposizioni, non frasi: taglia lì solo se quanto
        # accumulato è già una frase di lunghezza sensata.
        if ch in ":；;：" and pos < _MIN_TTS_CHARS:
            return False
        return True
    prev = text[pos - 1] if pos > 0 else ""
    nxt = text[pos + 1] if pos + 1 < len(text) else ""
    # Puntini di sospensione: "più... tattile" spezzava in "più." + ".. tattile"
    # (il carattere dopo è un punto, non una minuscola). Confine solo sull'ULTIMO
    # punto della serie, e solo se quello che segue apre davvero una frase nuova.
    if nxt == "." or prev == ".":
        end = pos
        while end + 1 < len(text) and text[end + 1] == ".":
            end += 1
        if end != pos:
            return False                      # non è l'ultimo punto della serie
        after = text[end + 1: end + 3]
        if not after or after.strip()[:1].islower():
            return False
    # numero decimale o data/versione: 3.5 - 1.2.3
    if prev.isdigit() and nxt.isdigit():
        return False
    # iniziale puntata ("A. Rossi") o sigla ("U.S.A.")
    if prev.isupper() and (pos < 2 or not text[pos - 2].isalpha()):
        return False
    # abbreviazione nota
    word = re.search(r"(\w+)$", text[:pos])
    if word and word.group(1).lower() in _ABBREV:
        return False
    # seguito da minuscola = la frase continua (elenco puntato, abbreviazione ignota)
    if nxt and nxt.islower():
        return False
    if nxt == " " and pos + 2 < len(text) and text[pos + 2].islower():
        return False
    return True


def _strip_blocks(text: str) -> str:
    """Rimuove i blocchi completi (think / azioni / tool) in modo iterativo."""
    prev = None
    while prev != text:
        prev = text
        for rx in _COMPLETE_BLOCKS:
            text = rx.sub(" ", text)
    return text


#: Alfabeti che il TTS non sa leggere e pronuncia a caso — o non pronuncia
#: affatto, lasciando un buco a metà frase. Non è una precauzione teorica: un
#: modello multilingue in italiano scrive "un sistema di обработка audio" o
#: infila tre parole in cinese, e alla temperatura di default lo fa in una
#: risposta su tre. Il testo arriva alla voce, e la voce ci si spezza.
_ALFABETI_ESTRANEI = re.compile(
    "[" 
    "Ѐ-ӿ"          # cirillico
    "֐-׿"          # ebraico
    "؀-ۿ"          # arabo
    "ऀ-ॿ"          # devanagari
    "฀-๿"          # thai
    "぀-ヿ"          # hiragana e katakana
    "㐀-䶿"          # ideogrammi (estensione A)
    "一-鿿"          # ideogrammi CJK
    "가-힯"          # hangul
    "]+")


def togli_alfabeti_estranei(testo: str, lingua: str = "") -> str:
    """Via le parole scritte in un alfabeto che questa voce non sa leggere.

    Si toglie la PAROLA intera, non il singolo carattere: lasciare i pezzi
    latini di una parola cirillica produce sillabe senza senso, che è peggio
    di una parola mancante — quella almeno passa per un'esitazione.

    Non si applica quando la lingua di sintesi usa proprio quell'alfabeto:
    sarebbe come censurare il testo giusto. Oggi le voci configurate sono
    italiane, inglesi e ungheresi, ma la guardia serve perché la funzione non
    diventi un ostacolo il giorno che si aggiunge una voce russa o giapponese.
    """
    if not testo or not _ALFABETI_ESTRANEI.search(testo):
        return testo
    if (lingua or "").split("-")[0].lower() in (
            "ru", "uk", "bg", "sr", "he", "ar", "fa", "hi", "th", "ja", "zh",
            "ko", "el"):
        return testo
    pulito = " ".join(p for p in testo.split()
                      if not _ALFABETI_ESTRANEI.search(p))
    return re.sub(r"\s+([,.;:!?])", r"", pulito).strip()


def sanitize_for_tts(text: str) -> str:
    """Ripulisce un testo completo: via reasoning, tool-call e markdown."""
    if not text:
        return ""
    t = _strip_blocks(text)
    t = _STRAY.sub(" ", t)
    t = _SYS_MARKER.sub(" ", t)        # [OUT-OF-BAND USER MESSAGE] & simili
    t = _EMOJI.sub(" ", t)             # emoji/simboli: il TTS li leggerebbe
    t = _TEXT_EMOTICON.sub(" ", t)     # faccine testuali :-) ;) :D
    # blocchi e inline di codice
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    # link/immagini markdown -> solo il testo
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # enfasi
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"___([^_]+)___", r"\1", t)
    t = re.sub(r"__([^_]+)__", r"\1", t)
    t = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", t)
    # pulizia per riga: titoli, citazioni, elenchi, righe orizzontali
    out_lines = []
    for ln in t.split("\n"):
        ln = re.sub(r"^\s{0,3}#{1,6}\s*", "", ln)
        ln = re.sub(r"^\s*>\s?", "", ln)
        ln = re.sub(r"^\s*[-*+]\s+", "", ln)
        ln = re.sub(r"^\s*\d+[.)]\s+", "", ln)
        if re.match(r"^\s*[-*_=]{3,}\s*$", ln):
            ln = ""
        out_lines.append(ln)
    t = "\n".join(out_lines)
    # simboli residui di formattazione
    t = t.replace("**", "").replace("*", "").replace("`", "").replace("#", "")
    t = re.sub(r"(?<!\w)[-–—_]{2,}(?!\w)", " ", t)
    # Puntini di sospensione: XTTS li pronuncia LETTERALMENTE ("punto
    # punto punto"). Valgono come pausa, quindi diventano punteggiatura
    # vera: punto se chiudono (fine testo o maiuscola dopo), virgola se
    # sospendono dentro la frase. Vale per '...', '..' e per il carattere
    # unico "…". Il taglio in frasi e' gia' avvenuto a monte, quindi
    # questa sostituzione non sposta i confini.
    t = re.sub(r"\s*" + _SOSPENSIONE + r"\s*$", ".", t)
    t = re.sub(r"\s*" + _SOSPENSIONE + r"\s*(?=[A-ZÀ-Þ])", ". ", t)
    t = re.sub(r"\s*" + _SOSPENSIONE + r"\s*", ", ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" +([.,!?;:])", r"\1", t)   # niente spazio orfano prima della punteggiatura
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{2,}", "\n", t)
    # Segni senza resa fonetica: parentesi, quadre, graffe e barre diventano
    # una pausa (virgola) o spariscono. XTTS non li pronuncia, ma delimitano
    # inciso e senza pausa la frase esce tutta attaccata.
    t = re.sub(r"[\[\]{}<>]", " ", t)
    t = re.sub(r"\s*\(\s*", ", ", t)
    t = re.sub(r"\s*\)\s*", ", ", t)
    t = re.sub(r"[\"“”«»‘’]", " ", t)
    # Punteggiatura ORFANA in testa: un segmento che comincia con un segno
    # (o che e' fatto SOLO di segni) manda XTTS in allucinazione — misurato:
    # un "." da solo diventa "Buon Dio!", e ". Ciao tesoro." diventa
    # "Ciao tesoro.com". Succede quando lo split lascia un segno orfano.
    t = re.sub(r"^[\s.,;:!?…\-]+", "", t)
    if not re.search(r"[0-9A-Za-zÀ-ÿ]", t):
        return ""            # solo punteggiatura: niente da pronunciare
    t = re.sub(r"\s*,\s*,+", ", ", t)      # virgole doppie dai tagli sopra
    t = re.sub(r"[ 	]{2,}", " ", t)
    # Ricuciture dopo le sostituzioni sopra: niente spazio orfano prima di un
    # segno, niente ", ." lasciato da una parentesi a fine frase.
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)
    t = re.sub(r",\s*([.;:!?])", r"\1", t)
    t = re.sub(r"([.;:!?])\s*,", r"\1", t)
    # Punto FINALE via: XTTS lo PRONUNCIA. Verificato due volte, la seconda
    # dopo aver provato a tenerlo: al motore era arrivato esattamente
    # 'Bentornato Andrea.' — un punto singolo, nessun puntino di sospensione —
    # e si e' sentito "bentornato Andrea punto". Non era quindi una diagnosi
    # confusa coi puntini, come avevo ipotizzato: sono due difetti distinti e
    # il punto singolo e' un problema per conto suo.
    #
    # La pausa conclusiva non si perde: e' la fine della clip. Si tolgono '.',
    # ':' e ';' — NON '?' e '!', che portano l'intonazione (senza, "Va bene
    # cosi" diventa affermativo piatto).
    #
    # I puntini di sospensione restano gestiti sopra: dentro la frase
    # diventano una virgola (che si sente come pausa e non si pronuncia), a
    # fine frase un punto che questa riga poi toglie — cioe' una pausa.
    t = re.sub(r"[.:;]+\s*$", "", t)
    t = t.strip()
    # Scarta una frase che e' SOLO un'istruzione di sistema riletta dal modello.
    if _INSTRUCTION_ECHO.match(t):
        return ""
    return t


def _unisci(prima: str, dopo: str) -> str:
    """Unisce due pezzi rimettendo il punto fra loro.

    Da quando `sanitize_for_tts` conserva la punteggiatura di chiusura, il
    caso normale e' che il primo pezzo finisca gia' con un punto e basti lo
    spazio. Il punto va aggiunto quando NON c'e': succede con i segmenti
    tagliati su una virgola, e senza separatore il TTS leggerebbe le due parti
    di seguito senza pausa ("...superare la soglia Terza e ultima frase..."
    invece di "...la soglia. Terza e ultima frase...").

    Il punto va rimesso solo in mezzo, non in fondo: in fondo tornerebbe il
    problema di partenza.
    """
    prima, dopo = (prima or "").strip(), (dopo or "").strip()
    if not prima:
        return dopo
    if not dopo:
        return prima
    # Se chiude gia' con un segno che il TTS interpreta come pausa, basta cosi'.
    sep = " " if prima[-1] in ".!?…:;" else ". "
    return f"{prima}{sep}{dopo}"


class TtsTextFilter:
    """Filtro a streaming: chunk di testo -> frasi pulite pronte per il TTS."""

    def __init__(self, continuazione: bool = False,
                 min_caratteri: int | None = None) -> None:
        #: Soglia fissa invece di quella adattiva di `chunk_policy`. Serve al
        #: motore di conversazione continua, dove accorpare non conviene piu':
        #: li' il labiale parte per battuta e la battuta e' l'unita' di taglio,
        #: quindi frasi corte vogliono dire interruzioni fini. Con la soglia
        #: adattiva (120 caratteri di partenza) una risposta usciva in due
        #: battute da nove secondi l'una, e nove secondi e' piu' di quanto
        #: chiunque aspetti prima di essere ascoltato. None = comportamento
        #: storico, invariato per PTT, copilota e telefonate.
        self._min_caratteri = min_caratteri
        self._buf = ""
        #: Una continuazione non e' l'inizio di un turno: il testo riprende una
        #: frase gia' cominciata a voce. Trattarla come "prima frase" — confini
        #: generosi e soglia bassa, per attaccare la voce in fretta — fa uscire
        #: una clip di trenta caratteri in mezzo al discorso, e si sente come
        #: una balbuzie. Qui la fretta non serve: l'avatar sta gia' parlando.
        self._continuazione = bool(continuazione)
        # Ultima frase completa, TRATTENUTA: se la risposta finisce con una coda
        # breve ("Ascolto anche") quella coda, emessa da sola dal flush, diventa
        # una clip isolata che il TTS pronuncia storpiata. Tenendo indietro una
        # frase la si può unire alla coda. La PRIMA frase esce comunque subito,
        # per non ritardare l'inizio dell'audio.
        self._pending: str = ""
        self._emitted = 0
        #: Da quando c'e' una frase completa ma sotto soglia in attesa di
        #: compagnia. None = niente in attesa.
        self._attesa_da: float | None = None

    def feed(self, chunk: str) -> list[str]:
        """Aggiunge un chunk; ritorna le frasi complete e ripulite emergenti."""
        if chunk:
            self._buf += chunk
        return self._drain(final=False)

    def flush(self) -> list[str]:
        """Fine risposta: emette il residuo ripulito (scartando blocchi aperti)."""
        return self._drain(final=True)

    def _drain(self, final: bool) -> list[str]:
        # 1) togli i blocchi COMPLETI (think / azioni / tool)
        self._buf = _strip_blocks(self._buf)
        # 2) se c'e' un blocco APERTO non ancora chiuso, trattieni da lì in poi
        m = _OPEN_MARKER.search(self._buf)
        safe = self._buf if m is None else self._buf[: m.start()]
        held = "" if m is None else self._buf[m.start():]

        sentences: list[str] = []
        if final:
            s = sanitize_for_tts(safe)
            # Coda finale unita alla frase trattenuta: mai una clip isolata.
            coda = _unisci(self._pending, s)
            if coda:
                sentences.append(coda)
            self._buf = ""
            self._pending = ""
            self._emitted = 0
            self._attesa_da = None
            return sentences

        # 3) emetti solo FRASI complete dalla parte "sicura". Un confine vale
        #    solo se chiude davvero la frase (niente tagli su "3.5" o "ecc.") e
        #    se quanto accumulato raggiunge una lunghezza pronunciabile: i
        #    monconi restano nel buffer e si uniscono alla frase seguente.
        last = 0
        # Primo segmento del turno: confini piu' generosi (virgola) e
        # soglia piu' bassa, per attaccare la voce senza aspettare il punto.
        primo = (self._emitted == 0 and not self._pending
                 and not self._continuazione)
        fine = _SENT_END_FIRST if primo else _SENT_END
        minimo = (_MIN_TTS_CHARS_FIRST if primo
                  else (self._min_caratteri
                        if self._min_caratteri is not None
                        else chunk_policy.min_caratteri()))
        # Il testo arriva in STREAMING: quando una frase e' completa il resto
        # della risposta spesso non e' ancora stato generato. Trattenerla per
        # accorparla alla prossima ha senso solo per poco: oltre, la pipeline
        # resta ferma ad aspettare testo che non esiste, e si perde piu' di
        # quanto si risparmi in preamboli.
        scaduta = (self._attesa_da is not None
                   and time.monotonic() - self._attesa_da
                   >= chunk_policy.attesa_massima())
        for mm in fine.finditer(safe):
            # Segno ancora in CODA al buffer: non si può decidere se chiude la
            # frase finché non arriva il carattere dopo. In streaming "10.08"
            # arriva come "...10." e poi "08": emettere subito lo spezzerebbe.
            # '!' '?' '…' e il fine riga non sono ambigui e passano comunque.
            if mm.end() >= len(safe) and safe[mm.start()] in ".:;：；":
                break
            if not _is_sentence_boundary(safe, mm.start()):
                continue
            seg = safe[last: mm.end()]
            # Sul PRIMO segmento il minimo dipende da COME chiude: un punto
            # chiude la frase anche se e' corta, una virgola no.
            soglia = minimo
            if primo and safe[mm.start()] not in _CONFINI_DEBOLI:
                soglia = _MIN_TTS_CHARS_FIRST_FORTE
            if len(seg.strip()) < soglia and not scaduta:
                # Troppo corto: accumula, ma da ora si conta il tempo.
                if self._attesa_da is None:
                    self._attesa_da = time.monotonic()
                continue
                # (non si emette nemmeno se è a fine buffer: in streaming
                #  arriverà altro testo, e comunque il flush finale lo emette)
            last = mm.end()
            self._attesa_da = None            # emesso: niente piu' in attesa
            s = sanitize_for_tts(seg)
            if not s:
                continue
            if self._emitted == 0:
                sentences.append(s)          # la prima parte subito: audio rapido
                self._emitted += 1
                # Da qui in poi valgono i confini normali: continuare a
                # tagliare sulle virgole spezzetterebbe tutta la risposta.
                fine, minimo = _SENT_END, _MIN_TTS_CHARS
            else:
                if self._pending:
                    sentences.append(self._pending)
                    self._pending = ""
                if scaduta:
                    # La pipeline sta per restare a secco: si consegna subito
                    # invece di trattenere. Il trattenimento serve a non
                    # lasciare una coda isolata a fine risposta, ma qui il
                    # problema opposto e' gia' in corso — l'avatar tace.
                    sentences.append(s)
                    scaduta = False
                    self._attesa_da = None
                else:
                    self._pending = s        # trattieni l'ultima
        self._buf = safe[last:] + held
        return sentences
