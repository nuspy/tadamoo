"""The text of a script made fit to be read aloud by a voice engine: one ordered table of rules, all here, so that it can be improved in one place.

What goes wrong without it (measured with XTTS v2): a full stop at the end of a piece is read as a word ("punto", "punto tale") or followed by a babble in about
one piece in two; an address ("http...") is read letter by letter; an emoji or an emoticon is read as its name or as noise; an abbreviation (pl., kb., stb.) is read
as letters or as another word; an acronym is read as a word or as the wrong letters; a name from another language is read with the wrong sounds.

`RULES` is the table, in the order it is applied: `(id, what it does and why, function(text, lang) -> text)`. `normalise(text, lang, pronunciations)` runs it. The language
tables (abbreviations, letters, words for symbols, acronyms read as words) are the dictionaries below, one entry for each language; a language that has no entry keeps
the rule's general behaviour. `pronunciations` (from the script) maps a written form to the way to write it so that the engine says it right, and goes first.

Numbers: Hungarian numbers, decimals, percentages and dates are spelled out in Hungarian here (the engine reads Hungarian digits badly: "12-én" came out as "12-jé"); for the
other languages the engine says them, and voicecheck.py says when one went wrong."""
from __future__ import annotations

import re
import unicodedata
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# ---- the language tables
ABBREVIATIONS: Dict[str, Dict[str, str]] = {
    "it": {"sig.": "signor", "sig.ra": "signora", "dott.": "dottor", "dott.ssa": "dottoressa", "prof.": "professor", "on.": "onorevole", "ecc.": "eccetera",
           "es.": "per esempio", "pag.": "pagina", "ca.": "circa", "mln": "milioni", "mld": "miliardi", "ing.": "ingegner", "avv.": "avvocato"},
    "en": {"mr.": "mister", "mrs.": "missus", "dr.": "doctor", "prof.": "professor", "etc.": "et cetera", "e.g.": "for example", "i.e.": "that is", "vs.": "versus",
           "approx.": "approximately", "govt.": "government"},
    "hu": {"dr.": "doktor", "pl.": "például", "kb.": "körülbelül", "stb.": "és így tovább", "ill.": "illetve", "ún.": "úgynevezett", "ld.": "lásd", "sz.": "szám",
           "vö.": "vesd össze", "tv.": "törvény", "mrd": "milliárd", "mrd.": "milliárd", "ft": "forint", "eur": "euró", "kft.": "korlátolt felelősségű társaság",
           "zrt.": "zártkörűen működő részvénytársaság", "nyrt.": "nyilvánosan működő részvénytársaság", "ifj.": "ifjabb", "Szt.": "Szent", "hrsz.": "helyrajzi szám"},
}
SYMBOLS: Dict[str, Dict[str, str]] = {          # words for the symbols that stay in a text
    "it": {"&": "e", "%": "per cento", "/": "o", "@": "chiocciola", "+": "più", "=": "uguale", "€": "euro", "$": "dollari", "£": "sterline", "§": "paragrafo", "°": "gradi"},
    "en": {"&": "and", "%": "percent", "/": "or", "@": "at", "+": "plus", "=": "equals", "€": "euros", "$": "dollars", "£": "pounds", "§": "section", "°": "degrees"},
    "hu": {"&": "és", "%": "százalék", "/": "vagy", "@": "kukac", "+": "plusz", "=": "egyenlő", "€": "euró", "$": "dollár", "£": "font", "§": "paragrafus", "°": "fok"},
}
MONTHS: Dict[str, List[str]] = {
    "it": ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    "hu": ["január", "február", "március", "április", "május", "június", "július", "augusztus", "szeptember", "október", "november", "december"],
}
LETTERS: Dict[str, Dict[str, str]] = {          # how a letter is named, for an acronym that is spelled
    "it": dict(zip("abcdefghijklmnopqrstuvwxyz", "a bi ci di e effe gi acca i i-lunga cappa elle emme enne o pi cu erre esse ti u vu doppia-vu ics ipsilon zeta".split())),
    "en": dict(zip("abcdefghijklmnopqrstuvwxyz", "ay bee see dee ee eff gee aitch eye jay kay el em en oh pee cue ar ess tee you vee double-you ex why zed".split())),
    "hu": dict(zip("abcdefghijklmnopqrstuvwxyz", "á bé cé dé é eff gé há í jé ká ell em en ó pé kú er es té ú vé dupla-vé iksz ipszilon zé".split())),
}
AS_WORD: Dict[str, frozenset] = {               # acronyms said as a word, in each language
    "it": frozenset({"NATO", "NASA", "ONU", "ISTAT", "ANSA", "UNESCO", "FIFA", "UEFA", "RAI", "INPS", "CONSOB", "OPEC", "ASL"}),
    "en": frozenset({"NATO", "NASA", "UNESCO", "FIFA", "UEFA", "OPEC", "FBI", "CIA", "NASDAQ"}),
    "hu": frozenset({"NATO", "NASA", "UNESCO", "FIFA", "UEFA", "OPEC", "MOL", "OLAF", "FIDESZ", "KDNP"}),
}
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⌀-⏿⬀-⯿️‍⃣←-⇿]", re.UNICODE)
EMOTICON = re.compile(r"(?<![\w])(?:[:;=8xX][\-^o']?[)(DPpOo/\\|*$@\]\[]+|<3+|</3|\^[_.]?\^|[Oo][_.][Oo]|-_-|[:;][\-']?[-3])(?![\w])")
URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
BARE_DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:hu|it|com|org|net|eu|uk|de|fr|es|io|info|gov|edu|tv|news)\b(?:/[^\s<>\"')\]]*)?", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
MARKUP_CHARS = re.compile(r"[*_`#>|~^\\]+")
QUOTES = "\"“”„‟«»‹›‘’‚‛`´"
OPEN_BRACKETS = "([{"
UPPER = "A-ZÁÉÍÓÖŐÚÜŰÀÈÌÒÙ"


# ---- Hungarian numbers
_HU_ONES = ["nulla", "egy", "kettő", "három", "négy", "öt", "hat", "hét", "nyolc", "kilenc"]
_HU_ONES_COMPOUND = ["", "egy", "két", "három", "négy", "öt", "hat", "hét", "nyolc", "kilenc"]
_HU_TENS = ["", "tizen", "huszon", "harminc", "negyven", "ötven", "hatvan", "hetven", "nyolcvan", "kilencven"]
_HU_TENS_ALONE = ["", "tíz", "húsz", "harminc", "negyven", "ötven", "hatvan", "hetven", "nyolcvan", "kilencven"]
_HU_DAY_ORDINAL = {1: "első", 2: "második", 3: "harmadik", 4: "negyedik", 5: "ötödik", 6: "hatodik", 7: "hetedik", 8: "nyolcadik", 9: "kilencedik", 10: "tizedik",
                   20: "huszadik", 30: "harmincadik"}
_HU_DAY_TENS = {1: "tizen", 2: "huszon", 3: "harminc"}


def hu_number(n: int) -> str:
    """A whole number from 0 to 999 999 999 in Hungarian words, written as the spelling rules have it (hyphen between groups above two thousand)."""
    if n < 0 or n > 999_999_999:
        return str(n)
    if n < 10:
        return _HU_ONES[n]

    def below_thousand(m: int, last: bool) -> str:
        h, rest = divmod(m, 100)
        out = ""
        if h:
            out = ("száz" if h == 1 else _HU_ONES_COMPOUND[h] + "száz")
        t, o = divmod(rest, 10)
        ones = (_HU_ONES[o] if last else _HU_ONES_COMPOUND[o])             # kettő at the end of a number, két before a group word
        if rest == 10:
            out += "tíz"
        elif rest == 20:
            out += "húsz"
        elif t:
            out += _HU_TENS[t] + (ones if o else "")
        elif o:
            out += ones
        return out

    groups: List[str] = []
    millions, rest = divmod(n, 1_000_000)
    thousands, units = divmod(rest, 1000)
    if millions:
        groups.append(("egy" if millions == 1 else below_thousand(millions, False)) + "millió")
    if thousands:
        groups.append(("" if thousands == 1 and not millions and n < 2000 else "") + ("ezer" if thousands == 1 else below_thousand(thousands, False) + "ezer"))
    if units:
        groups.append(below_thousand(units, True))
    word = "-".join(groups) if n > 2000 else "".join(groups)
    return word.replace("egyezer", "ezer") if word.startswith("egyezer") else word


def hu_day(n: int, suffix: str = "") -> str:
    """A day of the month as an ordinal ("tizenkettedik") with the case ending a date takes ("-én": tizenkettedikén)."""
    if not 1 <= n <= 31:
        return hu_number(n)
    if n in _HU_DAY_ORDINAL:
        word = _HU_DAY_ORDINAL[n]
    else:
        t, o = divmod(n, 10)
        unit = {1: "egyedik", 2: "kettedik", 3: "harmadik", 4: "negyedik", 5: "ötödik", 6: "hatodik", 7: "hetedik", 8: "nyolcadik", 9: "kilencedik"}[o]
        word = _HU_DAY_TENS[t] + unit
    if suffix == "én":
        if n == 1:
            return "elsején"
        root = word[:-3]                                                    # without "dik": the last vowel of what is left decides the ending (vowel harmony)
        vowel = next((c for c in reversed(root) if c in "aáeéiíoóöőuúüű"), "e")
        return word + ("án" if vowel in "aáoóuú" else "én")
    return word


# What a number word takes at its end: the ordinal ("-dik"), the adjective ("-es"), and the case endings whose vowel follows the last word of the number.
_HU_LAST = [  # (ending of the number word, ordinal, adjective, back vowels in the ending?)
    ("kettő", "kettedik", "kettes", False), ("egy", "egyedik", "egyes", False), ("három", "harmadik", "hármas", True), ("négy", "negyedik", "négyes", False),
    ("öt", "ötödik", "ötös", False), ("hat", "hatodik", "hatos", True), ("hét", "hetedik", "hetes", False), ("nyolc", "nyolcadik", "nyolcas", True),
    ("kilenc", "kilencedik", "kilences", False), ("tíz", "tizedik", "tízes", False), ("húsz", "huszadik", "húszas", True), ("harminc", "harmincadik", "harmincas", True),
    ("negyven", "negyvenedik", "negyvenes", False), ("ötven", "ötvenedik", "ötvenes", False), ("hatvan", "hatvanadik", "hatvanas", True), ("hetven", "hetvenedik", "hetvenes", False),
    ("nyolcvan", "nyolcvanadik", "nyolcvanas", True), ("kilencven", "kilencvenedik", "kilencvenes", False), ("száz", "századik", "százas", True), ("ezer", "ezredik", "ezres", False),
    ("millió", "milliomodik", "milliós", True), ("nulla", "nulladik", "nullás", True),
]
_HU_CASES = {"ben": "ban", "ig": "ig", "ra": "ra", "ról": "ról", "tól": "tól"}          # back form of each ending; the front form is "ben", "ig", "re", "ről", "től"


def _hu_last(word: str):
    for ending, ordinal, adjective, back in sorted(_HU_LAST, key=lambda e: -len(e[0])):
        if word.endswith(ending):
            return word[: len(word) - len(ending)], ordinal, adjective, back
    return word, word, word, True


def hu_ordinal(n: int) -> str:
    """A whole number as an ordinal ("312." is háromszáztizenkettedik)."""
    if n == 1:
        return "első"
    if n == 2:
        return "második"
    stem, ordinal, _, _ = _hu_last(hu_number(n))
    return stem + ordinal


def hu_suffixed(n: int, ending: str) -> str:
    """A number with the ending it takes in "2027-ben", "1375-ös", "2026-ig"."""
    stem, _, adjective, back = _hu_last(hu_number(n))
    if ending in ("es", "os", "as", "ös"):
        return stem + adjective
    base = hu_number(n)
    if ending in ("ban", "ben"):
        return base + ("ban" if back else "ben")
    if ending in ("ra", "re"):
        return base + ("ra" if back else "re")
    if ending in ("ról", "ről"):
        return base + ("ról" if back else "ről")
    if ending in ("tól", "től"):
        return base + ("tól" if back else "től")
    return base + ending


def _hu_suffixed_numbers(text: str) -> str:
    """Years and numbers with a hyphenated ending: "2028-2034-es" (a period), "2027-es", "2027-ben", "2026-ig"."""
    text = re.sub(r"\b(\d{1,4})\s?[-\u2013]\s?(\d{1,4})-(?:es|os|as|ös)\b", lambda m: f"{hu_number(int(m.group(1)))} és {hu_number(int(m.group(2)))} közötti", text)
    return re.sub(r"\b(\d{1,9})-(es|os|as|ös|ben|ban|ig|ra|re|ról|ről|tól|től)\b", lambda m: hu_suffixed(int(m.group(1)), m.group(2)), text)


def _hu_numbers(text: str, lang: str) -> str:
    """Hungarian dates, percentages, decimals and numbers spelled in words."""
    if lang != "hu":
        return text
    months = "|".join(MONTHS["hu"])
    text = re.sub(rf"\b(\d{{4}})\.?\s+({months})\s+(\d{{1,2}})(?:-([^\W\d_]+)|\.)?", lambda m: f"{hu_number(int(m.group(1)))} {m.group(2)} {hu_day(int(m.group(3)), 'én' if m.group(4) else '')}", text, flags=re.IGNORECASE)
    text = re.sub(rf"\b({months})\s+(\d{{1,2}})(?:-([^\W\d_]+)|\.)", lambda m: f"{m.group(1)} {hu_day(int(m.group(2)), 'én' if m.group(3) else '')}", text, flags=re.IGNORECASE)
    def clock(m: "re.Match[str]") -> str:
        h, minute, suffix = hu_number(int(m.group(1))), int(m.group(2)), m.group(3)
        if suffix:
            return f"{h} {hu_number(minute)} {suffix}" if minute else f"{h} {suffix}"
        return f"{h} óra {hu_number(minute)}" if minute else f"{h} óra"
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b\s*(órakor|óra)?", clock, text)
    text = re.sub(r"%-([^\W\d_]+)", r"százalék\1", text)                                       # 4,5%-kal is said százalékkal
    text = re.sub(r"\b(\d+),(\d+)\s*(%|százalék)", lambda m: f"{hu_number(int(m.group(1)))} egész {hu_number(int(m.group(2)))} {'tized' if len(m.group(2)) == 1 else 'század' if len(m.group(2)) == 2 else 'ezred'} százalék", text)
    text = re.sub(r"\b(\d+)\s*(?:%|százalék)", lambda m: f"{hu_number(int(m.group(1)))} százalék", text)
    text = re.sub(r"\b(\d+),(\d+)\b", lambda m: f"{hu_number(int(m.group(1)))} egész {hu_number(int(m.group(2)))} {'tized' if len(m.group(2)) == 1 else 'század' if len(m.group(2)) == 2 else 'ezred'}", text)
    text = _hu_suffixed_numbers(text)
    text = re.sub(r"\b(\d{1,4})\.(?=\s+[a-záéíóöőúüű])", lambda m: hu_ordinal(int(m.group(1))), text)                  # "4. és 5. klaszter", "312. cikk": ordinals
    return re.sub(r"\b\d{1,9}\b(?!-)", lambda m: hu_number(int(m.group(0))), text)


# ---- the rules, in order
def _markup(text: str, lang: str) -> str:
    text = MARKDOWN_LINK.sub(r"\1", text)
    text = HTML_TAG.sub(" ", text)
    return MARKUP_CHARS.sub(" ", text)


def _source_name(host: str) -> str:
    host = host.lower().removeprefix("www.")
    labels = host.split(".")
    name = labels[-2] if len(labels) >= 2 else labels[0]
    return name.capitalize()


def _addresses(text: str, lang: str) -> str:
    """An address is never read: an e-mail goes, a link or a domain becomes the name of the site (telex.hu -> Telex)."""
    text = EMAIL.sub(" ", text)

    def name(match: "re.Match[str]") -> str:
        raw = match.group(0)
        host = urlsplit(raw if "//" in raw else "//" + raw).hostname or raw
        return _source_name(host)
    text = URL.sub(name, text)
    return BARE_DOMAIN.sub(name, text)


def _emoji(text: str, lang: str) -> str:
    text = EMOJI.sub(" ", text)
    return EMOTICON.sub(" ", text)


def _quotes(text: str, lang: str) -> str:
    """Quotation marks of every kind are typography: the words stay, the marks go. An apostrophe inside a word stays (dell'Istruzione)."""
    text = re.sub(r"(?<=\w)['’](?=\w)", "\u0001", text)
    for q in QUOTES:
        text = text.replace(q, " " if q in "«»„“”\"" else "")
    text = text.replace("'", " ")
    return text.replace("\u0001", "'")


def _brackets(text: str, lang: str) -> str:
    """What is in brackets is an aside: it is read between commas."""
    text = re.sub(r"\s*[(\[{]\s*", ", ", text)
    return re.sub(r"\s*[)\]}]\s*", ", ", text)


def _dates(text: str, lang: str) -> str:
    """2026-11-12 and 12.11.2026 become the day, the month's name and the year, in the order of the language (Hungarian: year, month, day)."""
    months = MONTHS.get(lang, MONTHS["en"])

    def say(y: int, m: int, d: int) -> str:
        if not 1 <= m <= 12:
            return f"{d}.{m}.{y}"
        return f"{y}. {months[m - 1]} {d}" if lang == "hu" else f"{d} {months[m - 1]} {y}"
    text = re.sub(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lambda m: say(int(m.group(1)), int(m.group(2)), int(m.group(3))), text)
    return re.sub(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", lambda m: say(int(m.group(3)), int(m.group(2)), int(m.group(1))), text)


def _numbers(text: str, lang: str) -> str:
    """Thousands written with a dot or a thin space become one number (3.000 -> 3000); the engine says it."""
    text = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", text)
    text = re.sub(r"\b(\d{1,3})(?:\.(\d{3}))+\b", lambda m: m.group(0).replace(".", ""), text)
    return _hu_numbers(text, lang)


def _symbols(text: str, lang: str) -> str:
    table = SYMBOLS.get(lang, SYMBOLS["en"])
    text = re.sub(r"([%€$£])-[^\W\d_]+", r"\1", text)                                              # a Hungarian suffix after a symbol (4,5%-a) is dropped
    text = re.sub(r"([€$£])\s*(\d[\d.,]*)", lambda m: f"{m.group(2)} {table[m.group(1)]}", text)       # a currency in front of the number goes behind it
    text = re.sub(r"(?<=\d)\s*°\s*C\b", f" {table['°']} Celsius", text)
    for symbol, word in table.items():
        if symbol == "/":
            text = re.sub(r"(?<=[^\W\d])\s*/\s*(?=[^\W\d])", f" {word} ", text)                      # a slash between words, not in a date or a fraction
        else:
            text = text.replace(symbol, f" {word} ")
    return text


def _dashes(text: str, lang: str) -> str:
    """An ellipsis, a dash that stands for a comma and a hyphen between spaces are a pause."""
    text = re.sub(r"\s*(?:\.{3,}|…)\s*", ", ", text)
    text = re.sub(r"\s*[-–—−]{1,2}\s+|\s+[-–—−]{1,2}\s*", ", ", text)
    return re.sub(r"[\r\n\t]+", " ", text)


def _abbreviations(text: str, lang: str) -> str:
    for short, full in sorted(ABBREVIATIONS.get(lang, {}).items(), key=lambda kv: -len(kv[0])):
        pattern = r"(?<![\w.])" + re.escape(short) + (r"(?=\s|$|[,;:)])" if short.endswith(".") else r"\b\.?")
        text = re.sub(pattern, full, text, flags=re.IGNORECASE if short.islower() else 0)
    return text


def _initials(text: str, lang: str) -> str:
    """"J. K. Rowling": the dot of an initial goes; the engine reads a capital with a dot as the letter and then stops."""
    return re.sub(rf"\b([{UPPER}])\.(?=\s)", r"\1", text)


def _acronyms(text: str, lang: str) -> str:
    """An acronym in capitals is said as a word when the language's list knows it, otherwise spelled letter by letter with the language's letter names."""
    letters, words = LETTERS.get(lang), AS_WORD.get(lang, frozenset())

    def one(match: "re.Match[str]") -> str:
        acronym = match.group(0)
        if acronym in words or letters is None:
            return acronym.capitalize() if acronym in words else acronym
        return " ".join(letters.get(c.lower(), c) for c in unicodedata.normalize("NFD", acronym).replace("́", "") if c.isalpha())
    return re.sub(rf"\b[{UPPER}]{{2,6}}\b(?![-'])", one, text)


def _punctuation(text: str, lang: str) -> str:
    """Repeated marks become one; a colon or a semicolon (not in a time) is a comma; a mark with a space before it loses the space."""
    text = re.sub(r"([!?])[!?.]+", r"\1", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"(?<!\d):|:(?!\d)", ",", text)
    text = text.replace(";", ",")
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)
    return re.sub(r"[,.!?]\s*(?=,)", lambda m: m.group(0) if m.group(0)[0] in "!?" else "", text)


def _repeats(text: str, lang: str) -> str:
    """A name said twice in a row because a link to it followed it (Telex, Telex) is said once."""
    return re.sub(r"(?i)\b([^\W\d_]{3,})(?:,\s*\1\b)+", r"\1", text)


def _stray(text: str, lang: str) -> str:
    """What is left that is neither a letter, a digit nor a mark of speech."""
    return re.sub(r"[^\w\s,.!?'’:%°-]", " ", text)


def strip_end(text: str) -> str:
    """The piece without what ends it that is not a word: full stops, ellipses, commas and colons go (the silence between pieces is the pause); a question mark and an
    exclamation mark stay. The beginning loses its marks too."""
    text = " ".join(text.split())
    text = re.sub(r"^[\s,.;:!?-]+", "", text)
    return re.sub(r"[\s.,;:…-]+$", "", text)


RULES: List[Tuple[str, str, Callable[[str, str], str]]] = [
    ("markup", "markdown, links and HTML: the text stays, the marks go", _markup),
    ("addresses", "e-mail addresses go; a link or a domain becomes the site's name, never 'http'", _addresses),
    ("emoji", "emoji and emoticons go", _emoji),
    ("dates", "ISO and numeric dates become day, month's name and year in the language's order", _dates),
    ("numbers", "thousands separators go; Hungarian numbers, decimals, percentages and dates are spelled in words (the other languages: the engine says them)", _numbers),
    ("symbols", "% & / @ + = and currencies become words (a currency in front of a number goes behind it)", _symbols),
    ("quotes", "quotation marks of every kind go, apostrophes inside words stay", _quotes),
    ("brackets", "brackets mark an aside: it is read between commas", _brackets),
    ("dashes", "ellipses, dashes and line breaks are a pause", _dashes),
    ("abbreviations", "abbreviations are spelled out in the language of the text", _abbreviations),
    ("initials", "the dot of an initial goes", _initials),
    ("acronyms", "acronyms: a word if the language's list says so, else letter by letter", _acronyms),
    ("punctuation", "repeated marks, colons and semicolons, spaces before marks", _punctuation),
    ("repeats", "a word repeated after a comma (a name and its link) is said once", _repeats),
    ("stray", "any other symbol goes", _stray),
]


def apply_pronunciations(text: str, pronunciations: Optional[Dict[str, str]]) -> str:
    for written, spoken in sorted((pronunciations or {}).items(), key=lambda kv: -len(kv[0])):
        if written.strip():
            text = re.sub(r"(?<!\w)" + re.escape(written) + r"(?!\w)", spoken, text)
    return text


def normalise(text: str, lang: str = "it", pronunciations: Optional[Dict[str, str]] = None) -> str:
    """The text of one piece, ready to be sent to the voice engine."""
    text = text.replace(" ", " ")
    text = apply_pronunciations(text, pronunciations)
    for _, _, rule in RULES:
        text = rule(text, lang)
    return strip_end(" ".join(text.split()))
