"""Normalizzazione del testo PRIMA della sintesi vocale (italiano).

I motori TTS leggono quello che ricevono: "15 km" diventa "quindici kappa emme"
e "1250" viene scandito cifra per cifra, perche' nessuno ha detto loro come si
pronunciano. Non e' un difetto del motore ma uno stadio mancante della pipeline:
qui si espande cio' che va letto per esteso.

Copre i casi che rovinano di piu' l'ascolto:
  - numeri (interi, decimali, migliaia con separatore, negativi)
  - unita' di misura e sigle tecniche (km, ms, GB, %, °C, kmh...)
  - ordinali (1°, 2ª), percentuali, valute (€, $)
  - date e orari (12/03/2026, 14:30)
  - abbreviazioni comuni (ecc., pag., sig., art.)

Le parole inglesi NON si traducono: la pronuncia di "throughput" o "server"
dipende dal modello TTS e forzarla foneticamente peggiorerebbe le cose. Si
normalizza solo cio' che ha una lettura italiana univoca.
"""
from __future__ import annotations

import re

# --- numeri -----------------------------------------------------------------

_UNITA = ["zero", "uno", "due", "tre", "quattro", "cinque", "sei", "sette",
          "otto", "nove", "dieci", "undici", "dodici", "tredici", "quattordici",
          "quindici", "sedici", "diciassette", "diciotto", "diciannove"]
_DECINE = {20: "venti", 30: "trenta", 40: "quaranta", 50: "cinquanta",
           60: "sessanta", 70: "settanta", 80: "ottanta", 90: "novanta"}


def _due_cifre(n: int) -> str:
    if n < 20:
        return _UNITA[n]
    d, u = (n // 10) * 10, n % 10
    base = _DECINE[d]
    if u == 0:
        return base
    # elisione: venti+uno -> ventuno, trenta+otto -> trentotto
    if u in (1, 8):
        base = base[:-1]
    return base + _UNITA[u]


def _tre_cifre(n: int) -> str:
    c, r = n // 100, n % 100
    if c == 0:
        return _due_cifre(r)
    testa = "cento" if c == 1 else _UNITA[c] + "cento"
    return testa + (_due_cifre(r) if r else "")


def numero_in_lettere(n: int) -> str:
    """Intero -> parole italiane. Copre fino ai miliardi, che basta e avanza."""
    if n < 0:
        return "meno " + numero_in_lettere(-n)
    if n < 1000:
        return _tre_cifre(n)
    for soglia, sing, plur in ((10**9, "un miliardo", "miliardi"),
                               (10**6, "un milione", "milioni"),
                               (1000, "mille", "mila")):
        if n >= soglia:
            q, r = divmod(n, soglia)
            if soglia == 1000:
                testa = "mille" if q == 1 else _tre_cifre(q) + "mila"
            else:
                testa = sing if q == 1 else f"{_tre_cifre(q)} {plur}"
            coda = numero_in_lettere(r) if r else ""
            sep = " " if coda and soglia >= 10**6 else ""
            return f"{testa}{sep}{coda}"
    return str(n)


# --- unita' di misura e sigle ----------------------------------------------
# (singolare, plurale): il plurale si usa quando il numero che precede != 1.
_UNITA_MISURA = {
    "km": ("chilometro", "chilometri"), "m": ("metro", "metri"),
    "cm": ("centimetro", "centimetri"), "mm": ("millimetro", "millimetri"),
    "kg": ("chilogrammo", "chilogrammi"), "g": ("grammo", "grammi"),
    "mg": ("milligrammo", "milligrammi"), "t": ("tonnellata", "tonnellate"),
    "l": ("litro", "litri"), "ml": ("millilitro", "millilitri"),
    "h": ("ora", "ore"), "min": ("minuto", "minuti"),
    "s": ("secondo", "secondi"), "ms": ("millisecondo", "millisecondi"),
    "kb": ("kilobyte", "kilobyte"), "mb": ("megabyte", "megabyte"),
    "gb": ("gigabyte", "gigabyte"), "tb": ("terabyte", "terabyte"),
    "hz": ("hertz", "hertz"), "khz": ("chilohertz", "chilohertz"),
    "mhz": ("megahertz", "megahertz"), "ghz": ("gigahertz", "gigahertz"),
    "w": ("watt", "watt"), "kw": ("chilowatt", "chilowatt"),
    "v": ("volt", "volt"), "a": ("ampere", "ampere"),
    "€": ("euro", "euro"), "$": ("dollaro", "dollari"),
    "%": ("per cento", "per cento"),
    "km/h": ("chilometro orario", "chilometri orari"),
    "°c": ("grado", "gradi"), "°": ("grado", "gradi"),
}

# Abbreviazioni testuali senza numero davanti.
_ABBREVIAZIONI = {
    "ecc.": "eccetera", "etc.": "eccetera", "es.": "esempio",
    "pag.": "pagina", "pagg.": "pagine", "cfr.": "confronta",
    "sig.": "signor", "sig.ra": "signora", "dott.": "dottor",
    "prof.": "professor", "ing.": "ingegner", "avv.": "avvocato",
    "art.": "articolo", "artt.": "articoli", "fig.": "figura",
    "cap.": "capitolo", "vol.": "volume", "n.": "numero",
    "tel.": "telefono", "ca.": "circa", "p.es.": "per esempio",
}

_RE_ORARIO = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_RE_DATA = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
# `(?![cCfF])`: "21°C" e' una temperatura, non un ordinale — la riconosce la
# regex delle unita', che conosce "°c".
_RE_ORDINALE = re.compile(r"\b(\d+)\s*[°ºª](?![cCfF])")
_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
         "agosto", "settembre", "ottobre", "novembre", "dicembre"]
_ORDINALI = ["", "primo", "secondo", "terzo", "quarto", "quinto", "sesto",
             "settimo", "ottavo", "nono", "decimo"]

# numero (con separatori/decimali) seguito eventualmente da un'unita'
_RE_NUM_UNITA = re.compile(
    r"(-?\d{1,3}(?:[.\s]\d{3})+|-?\d+)([,.]\d+)?\s*"
    r"(km/h|°c|°|km|cm|mm|kg|mg|ml|ms|kb|mb|gb|tb|khz|mhz|ghz|kw|min|hz|"
    r"[mgltshwva€$%])?(?![\w/])",
    re.IGNORECASE)


def _numero_testo(intero: str, decimale: str | None) -> tuple[str, int]:
    """(testo pronunciabile, valore intero) da un numero con separatori."""
    pulito = re.sub(r"[.\s]", "", intero)
    try:
        val = int(pulito)
    except ValueError:
        return intero, 0
    testo = numero_in_lettere(val)
    if decimale:
        cifre = decimale[1:]
        # "3,5" -> "tre virgola cinque"; le cifre dopo la virgola si leggono
        # a gruppi solo se sono poche, altrimenti una per una (come si fa).
        if len(cifre) <= 2:
            dec = numero_in_lettere(int(cifre))
        else:
            dec = " ".join(_UNITA[int(c)] for c in cifre)
        testo += " virgola " + dec
    return testo, val


def normalizza_per_tts(testo: str) -> str:
    """Espande numeri, unita', sigle, date e orari in forma pronunciabile."""
    if not testo:
        return testo
    t = testo

    # date: 12/03/2026 -> dodici marzo duemilaventisei
    def _data(m):
        g, mm, aa = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not 1 <= mm <= 12:
            return m.group(0)
        giorno = "primo" if g == 1 else numero_in_lettere(g)
        return f"{giorno} {_MESI[mm - 1]} {numero_in_lettere(aa)}"
    t = _RE_DATA.sub(_data, t)

    # orari: 14:30 -> quattordici e trenta ; 14:00 -> quattordici
    def _ora(m):
        h, mi = int(m.group(1)), int(m.group(2))
        if mi == 0:
            return numero_in_lettere(h)
        return f"{numero_in_lettere(h)} e {numero_in_lettere(mi)}"
    t = _RE_ORARIO.sub(_ora, t)

    # ordinali: 1° -> primo, 15° -> quindicesimo
    def _ord(m):
        n = int(m.group(1))
        if n < len(_ORDINALI) and _ORDINALI[n]:
            return _ORDINALI[n]
        base = numero_in_lettere(n)
        return base[:-1] + "esimo" if base[-1] in "aeiou" else base + "esimo"
    t = _RE_ORDINALE.sub(_ord, t)

    # numeri con eventuale unita' di misura
    def _num(m):
        intero, decimale, unita = m.group(1), m.group(2), m.group(3)
        testo, val = _numero_testo(intero, decimale)
        if not unita:
            return testo
        sing, plur = _UNITA_MISURA[unita.lower()]
        plurale = not (val == 1 and not decimale)
        if not plurale and testo.endswith("uno"):
            # "uno chilometro" non si dice: davanti a un sostantivo vuole "un".
            testo = testo[:-3] + "un"
        return f"{testo} {plur if plurale else sing}"
    t = _RE_NUM_UNITA.sub(_num, t)

    # abbreviazioni testuali (dopo i numeri: "art. 5" e' gia' gestito sopra)
    for abbr, esteso in _ABBREVIAZIONI.items():
        t = re.sub(r"(?<!\w)" + re.escape(abbr), esteso, t, flags=re.IGNORECASE)

    # simboli isolati rimasti (senza numero davanti)
    t = re.sub(r"(?<!\w)%(?!\w)", " per cento", t)
    t = re.sub(r"(?<!\w)€(?!\w)", " euro", t)
    # Elisione dell'articolo-numerale: "uno secondo" non si dice, si dice "un
    # secondo". Vale davanti a qualunque sostantivo tranne s+consonante, z, gn,
    # ps, pn, x, y, dove l'italiano tiene "uno" (uno zaino, uno studio).
    t = re.sub(r"\buno (?!(?:s[bcdfglmnpqrtv]|z|gn|ps|pn|x|y)\w)(?=\w)", "un ", t,
               flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", t).strip()
