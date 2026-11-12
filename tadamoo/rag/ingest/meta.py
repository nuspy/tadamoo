"""Facts that can be read off a passage or a document by rule, with no model: its language, the patent and publication numbers it names, sequence
identifiers, dates, gene and protein symbols, and quantities with their units. They are kept beside the passage (`facts`), shown with a hit, and used to
narrow a search (`filters` of tadamoo/rag/query.py); they are not part of the text that is searched or embedded.

Everything here is a pattern, or a word list the person can extend (`entities.txt` in the archives' folder: one symbol a line, `#` for a comment); nothing
is made for one subject. A rule that is not reliable is not here: a symbol is taken only when it has the shape of one (letters then a digit: OCT4, Tet1,
TRE3G) or is in the list, never every capitalised word."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from tadamoo.rag.language import guess_language

VERSION = 1
CAPS = {"nums": 8, "seq": 24, "years": 4, "genes": 16, "qty": 12}
ENTITIES_FILE = "entities.txt"

# -- numbers -------------------------------------------------------------------------------------------------------------------

_COUNTRIES = "US|WO|EP|CN|FR|DE|GB|JP|KR|CA|AU|ES|IT|CH|RU|IN|BR|TW|MX|NL|SE|AT|BE|DK|FI|PL|IL|SG|ZA|NZ|EA|AP"
_PATENT = re.compile(rf"\b(?:({_COUNTRIES})[\s-]?((?:19|20)\d{{2}}\s?/?\s?\d{{5,7}}|\d{{7,10}})(?:[\s-]?([ABCUY]\d?)\b)?)", re.UNICODE)
_PCT = re.compile(r"\bPCT\s?/\s?([A-Z]{2})\s?(\d{4})\s?/\s?(\d{4,7})\b")
_APPLICATION = re.compile(rf"\b({_COUNTRIES})\s?(?:Application\s+)?(?:No\.?\s*)?(\d{{2}}\s?\d{{5,6}}\.\d)\b")

# -- sequences ------------------------------------------------------------------------------------------------------------------

_SEQ = re.compile(r"\bSEQ\.?\s*ID\.?\s*(?:NOS?\b\.?|NUMBERS?)\s*[:.]?\s*(\d{1,5}(?:\s*(?:,|and|or|&|to|-|–)\s*\d{1,5}(?!\s?(?!and\b|or\b|to\b)[A-Za-z%µμ])){0,24})", re.IGNORECASE)
_SEQ_ALT = re.compile(r"\b序列(?:表)?\s*(?:编号|号)?\s*[:：]?\s*(\d{1,4})")

# -- dates ------------------------------------------------------------------------------------------------------------------------

_MONTHS = {"jan": 1, "january": 1, "janvier": 1, "gennaio": 1, "feb": 2, "february": 2, "février": 2, "fevrier": 2, "febbraio": 2, "mar": 3, "march": 3, "mars": 3,
           "marzo": 3, "apr": 4, "april": 4, "avril": 4, "aprile": 4, "may": 5, "mai": 5, "maggio": 5, "jun": 6, "june": 6, "juin": 6, "giugno": 6, "jul": 7,
           "july": 7, "juillet": 7, "luglio": 7, "aug": 8, "august": 8, "août": 8, "aout": 8, "agosto": 8, "sep": 9, "sept": 9, "september": 9, "septembre": 9,
           "settembre": 9, "oct": 10, "october": 10, "octobre": 10, "ottobre": 10, "nov": 11, "november": 11, "novembre": 11, "dec": 12, "december": 12,
           "décembre": 12, "decembre": 12, "dicembre": 12}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_ISO = re.compile(r"\b((?:19|20)\d{2})[-./](0?[1-9]|1[0-2])[-./](0?[1-9]|[12]\d|3[01])\b")
_DATE_DMY = re.compile(rf"\b(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th|er)?\s+({_MONTH_NAMES})\.?,?\s+((?:19|20)\d{{2}})\b", re.IGNORECASE)
_DATE_MDY = re.compile(rf"\b({_MONTH_NAMES})\.?\s+(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,?\s+((?:19|20)\d{{2}})\b", re.IGNORECASE)
_DATE_CJK = re.compile(r"((?:19|20)\d{2})\s?年\s?(\d{1,2})\s?月(?:\s?(\d{1,2})\s?日)?")

# -- genes and proteins -------------------------------------------------------------------------------------------------------------

_SYMBOL = re.compile(r"(?<![\w-])[A-Za-z][A-Za-z0-9]{1,9}(?![\w-])")
_SHAPE = re.compile(r"^(?:[A-Za-z]{2,6}\d{1,3}[A-Za-z]{0,2}|[Pp]\d{2,3})$")
NOT_SYMBOLS = {"FIG", "FIGS", "TABLE", "EXAMPLE", "EXAMPLES", "CLAIM", "CLAIMS", "SEQ", "NO", "NOS", "ID", "PMID", "PCT", "USC", "ISO", "COVID", "SARS", "CCL4", "H2O", "CO2",
               "O2", "N2", "H2", "NH3", "PH", "ITR", "ITRS", "ASCII", "UTF8", "GMT", "UTC", "AM", "PM", "MS", "VOL", "NO", "PAGE", "PAGES", "SECTION", "STEP", "STEPS", "WEEK", "DAY"}
DEFAULT_ENTITIES = ("c-Myc", "Myc", "Nanog", "Lin28", "Esrrb", "Sirt1", "mTOR", "Trp53", "Tert", "Cdkn2a", "Gapdh", "Actb", "Egfr", "Kras", "Brca1", "Brca2", "Apoe",
                    "Ace2", "Vegf", "Tnf", "Il6", "Ifng", "Foxo3", "Pten", "Akt1", "Hif1a", "Nfkb", "Stat3", "Rbpms", "Gfp", "Gfap")

# -- quantities ----------------------------------------------------------------------------------------------------------------------

_UNITS = (r"%|mg\s?/\s?kg|mg\s?/\s?m[lL]|[µμu]g\s?/\s?[µμu]?[lL]|vg\s?/\s?kg|vg\s?/\s?eye|vg\s?/\s?mouse|vg|GC|IU|PFU|TU|MOI|kDa|bp|kb|Mb|[µμu]g|mg|kg|ng|pg|g|"
          r"[µμu]L|m[lL]|L|nM|[µμ]M|mM|M|°C|°F|rpm|Hz|kHz|MHz|[×xX]\s?g|days?|weeks?|months?|hours?|hrs?|h|min|mins|minutes?|sec|seconds?|cells|mm|cm|nm|[µμ]m|"
          r"giorni|giorno|settimane|mesi|ore|jours|semaines|mois|heures")
_QTY = re.compile(rf"(?<![\w.])(\d+(?:[.,]\d+)?(?:\s?[×xX]\s?10\s?\^?\s?[-−]?\d+|[eE][+-]?\d+)?)\s?({_UNITS})(?![A-Za-z])")
_NUM_CLEAN = re.compile(r"\s+")


def _tidy(value: str) -> str:
    return _NUM_CLEAN.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def patent_numbers(text: str) -> List[str]:
    """The publication, application and PCT numbers named in `text`, in a plain form with no space or slash (`WO2023196851A1`, `US12274733B2`, `PCT/US2023/071689`)."""
    found: List[str] = []
    for m in _PCT.finditer(text):
        found.append(f"PCT/{m.group(1)}{m.group(2)}/{m.group(3)}")
    text = _PCT.sub(" ", text)                                  # a PCT number is not also a national one
    for m in _PATENT.finditer(text):
        digits = re.sub(r"[\s/]", "", m.group(2))
        found.append(f"{m.group(1)}{digits}{m.group(3) or ''}")
    for m in _APPLICATION.finditer(text):
        found.append(m.group(1) + "".join(m.group(2).split()))
    return list(dict.fromkeys(found))


def sequence_ids(text: str) -> List[int]:
    """The `SEQ ID NO` numbers named in `text`; a list (`SEQ ID NOs: 41, 43 and 45`) gives each, a range (`1-4`, `1 to 4`) its ends and what lies between up to 24."""
    out: List[int] = []
    for m in _SEQ.finditer(text):
        raw = m.group(1)
        parts = re.split(r"\s*(?:,|and|or|&)\s*", raw)
        for part in parts:
            span = re.split(r"\s*(?:to|-|–)\s*", part)
            nums = [int(n) for n in span if n.strip().isdigit()]
            if len(nums) == 2 and nums[0] < nums[1] and nums[1] - nums[0] <= 24:
                out += list(range(nums[0], nums[1] + 1))
            else:
                out += nums
    out += [int(m.group(1)) for m in _SEQ_ALT.finditer(text)]
    return sorted(set(out))


def dates(text: str) -> List[str]:
    """The dates in `text` as `YYYY-MM-DD` (`YYYY-MM` when the day is missing): ISO and dotted, `12 March 2021`, `March 12, 2021` (English, French, Italian month names), `2021年3月12日`."""
    out: List[str] = []
    for m in _DATE_ISO.finditer(text):
        out.append(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
    for m in _DATE_DMY.finditer(text):
        out.append(f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}")
    for m in _DATE_MDY.finditer(text):
        out.append(f"{m.group(3)}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}")
    for m in _DATE_CJK.finditer(text):
        out.append(f"{m.group(1)}-{int(m.group(2)):02d}" + (f"-{int(m.group(3)):02d}" if m.group(3) else ""))
    return list(dict.fromkeys(out))


def load_entities(folder: Optional[Path]) -> Set[str]:
    """The symbols that count as gene or protein names whatever their shape: a short built-in list and the person's `entities.txt` (one a line, `#` comments)."""
    words = set(DEFAULT_ENTITIES)
    if folder is not None:
        try:
            for line in (Path(folder) / ENTITIES_FILE).read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line and len(line) <= 40:
                    words.add(line)
        except OSError:
            pass
    return words


def _is_symbol(word: str) -> bool:
    base = re.sub(r"\d.*$", "", word).upper()
    return bool(_SHAPE.match(word)) and word.upper() not in NOT_SYMBOLS and base not in NOT_SYMBOLS


def symbols(text: str, entities: Iterable[str] = DEFAULT_ENTITIES) -> List[str]:
    """Gene and protein symbols in `text`: a word of the entity list (in any case), or one shaped like a symbol (letters then a digit, as OCT4, Tet1, TRE3G, p53)."""
    known = {e.lower(): e for e in entities}
    out: List[str] = []
    for m in _SYMBOL.finditer(text):
        word = m.group(0)
        if _is_symbol(word):
            out.append(word)
        elif word.lower() in known:
            out.append(known[word.lower()])
    low = text.lower()
    out += [e for e in known.values() if "-" in e and e.lower() in low]          # names the word pattern cannot see (c-Myc)
    return list(dict.fromkeys(out))


def quantities(text: str) -> List[str]:
    """The quantities with units in `text` (`1.5E12 vg`, `2 mg/mL`, `60 %`, `3 days`), as written, spaces tidied."""
    out = []
    for m in _QTY.finditer(text):
        unit = _tidy(m.group(2))
        out.append(f"{_tidy(m.group(1))} {unit}" if unit != "%" else f"{_tidy(m.group(1))} %")
    return list(dict.fromkeys(out))


def passage(text: str, entities: Iterable[str] = DEFAULT_ENTITIES) -> Dict[str, Any]:
    """The facts of one passage that were found, only the keys with something in them: `lang`, `nums`, `seq`, `years`, `genes`, `qty`."""
    out: Dict[str, Any] = {}
    lang = guess_language(text)
    if lang != "other":
        out["lang"] = lang
    nums = patent_numbers(text)
    if nums:
        out["nums"] = nums[:CAPS["nums"]]
    seq = sequence_ids(text)
    if seq:
        out["seq"] = seq[:CAPS["seq"]]
    years = sorted({int(d[:4]) for d in dates(text)})
    if years:
        out["years"] = years[:CAPS["years"]]
    genes = symbols(text, entities)
    if genes:
        out["genes"] = genes[:CAPS["genes"]]
    qty = quantities(text)
    if qty:
        out["qty"] = qty[:CAPS["qty"]]
    return out


# -- the document --------------------------------------------------------------------------------------------------------------------

_INID = re.compile(r"\((\d{2})\)\s*([^\n(][^\n]{2,300})")
_LABEL = re.compile(r"^(?:Assignee|Applicants?|Inventors?|Title|Attorney[^:]*|Agent|申请人|发明人|专利代理机构|代理人)\s*[:：]?\s*")
_TITLE_CJK = re.compile(r"发明名称\s*[:：]?\s*(\S[^\n]{2,200})")
INID_ROLES = {"54": "title", "71": "applicant", "73": "assignee", "72": "inventors", "75": "inventors", "74": "agent", "43": "published", "45": "published", "22": "filed",
              "21": "application", "11": "number", "10": "number", "12": "kind"}


def _front(chunks: Sequence[Dict[str, Any]], pages: int = 2) -> str:
    return "\n".join(c["text"] for c in chunks if 0 < int(c.get("page_start") or 0) <= pages)[:12000]


def document(name: str, chunks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """What a document says about itself, from its name and its front pages: `languages` (passages by language), `kind` (`patent` when it has a patent number or the
    numbered fields of a patent front page, else `document`), `number`, `title`, `assignee`, `inventors`, `published` (a date), `filed`; only what was found."""
    out: Dict[str, Any] = {}
    langs = Counter(c.get("facts", {}).get("lang") for c in chunks if c.get("facts", {}).get("lang"))
    if langs:
        out["languages"] = dict(langs.most_common(4))
        out["language"] = langs.most_common(1)[0][0]
    front = _front(chunks)
    stem = Path(name).stem
    named = patent_numbers(stem.replace("_", ""))
    fields: Dict[str, str] = {}
    for code, value in _INID.findall(front):
        role = INID_ROLES.get(code)
        if role and role not in fields:
            fields[role] = _LABEL.sub("", re.sub(r"\s+", " ", value).strip(" .;:-")).strip()
    cjk = _TITLE_CJK.search(front)
    if cjk and "title" not in fields:
        fields["title"] = cjk.group(1).strip()
    if "title" not in fields:
        plain = re.search(r"(?mi)^\s*Title\s*:\s*(\S.{2,200})$", front)
        if plain:
            fields["title"] = plain.group(1).strip()
    number = named[0] if named else (patent_numbers(front) or [""])[0]
    if number:
        out["number"] = number
    if number or "title" in fields or len(fields) >= 3:
        out["kind"] = "patent"
    else:
        out["kind"] = "document"
    for key in ("title", "assignee", "applicant", "inventors", "agent"):
        if fields.get(key):
            out[key] = fields[key][:200]
    for key in ("published", "filed"):
        found = dates(fields.get(key, ""))
        if found:
            out[key] = found[0]
    return out


# -- filters --------------------------------------------------------------------------------------------------------------------------

FILTER_KEYS = ("language", "number", "gene", "seq", "year", "kind", "document", "unit", "figure")


def clean_filters(raw: Any) -> Dict[str, Any]:
    """The filters of a search checked: `{language: "fr", number: "US12274733", gene: "OCT4", seq: 41, year: 2023 or "2019-2023", kind: "table", document: <doc id>}`; a key that is
    not one of these, or a value of the wrong kind, is an error."""
    from tadamoo.rag.errors import RagError
    if raw in (None, {}, ""):
        return {}
    if not isinstance(raw, dict):
        raise RagError("filters: an object such as {\"language\": \"fr\"}")
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        if key not in FILTER_KEYS:
            raise RagError(f"filters: {key} is not one of {', '.join(FILTER_KEYS)}")
        if isinstance(value, bool) or value in ("", None):
            continue
        if key in ("seq", "year") and isinstance(value, str) and re.fullmatch(r"\d{1,5}(-\d{1,5})?", value.strip()):
            value = value.strip()
        elif key in ("seq", "year") and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = int(value)
        elif isinstance(value, str) and value.strip() and len(value) <= 80:
            value = value.strip()
        else:
            raise RagError(f"filters: {key} takes a short text" + (" or a number" if key in ("seq", "year") else ""))
        out[key] = value
    return out


def _span(value: Any) -> range:
    if isinstance(value, int):
        return range(value, value + 1)
    a, _, b = str(value).partition("-")
    lo, hi = int(a), int(b or a)
    return range(min(lo, hi), max(lo, hi) + 1)


def _norm(text: str) -> str:
    return re.sub(r"[\s/\-.]", "", text).upper()


def matches(meta: Dict[str, Any], doc: Dict[str, Any], kind: str, filters: Dict[str, Any]) -> bool:
    """Whether a passage (its `meta`, its document's `info`, its `kind`) passes every filter."""
    for key, value in filters.items():
        if key == "language":
            if (meta.get("lang") or doc.get("language") or "") != str(value).lower():
                return False
        elif key == "number":
            want = _norm(str(value))
            if not any(_norm(n).startswith(want) for n in list(meta.get("nums", [])) + [doc.get("number", "")] if n):
                return False
        elif key == "gene":
            if str(value).lower() not in {g.lower() for g in meta.get("genes", [])}:
                return False
        elif key == "seq":
            if not set(meta.get("seq", [])) & set(_span(value)):
                return False
        elif key == "year":
            if not set(meta.get("years", [])) & set(_span(value)):
                return False
        elif key == "kind":
            if kind != str(value).lower():
                return False
        elif key == "figure":
            if _norm(str(value)).replace("FIG", "").replace("FIGURE", "") != _norm(str(meta.get("figure", ""))).replace("FIG", "").replace("FIGURE", ""):
                return False
        elif key == "unit":
            if not any(str(value).lower() in q.lower() for q in meta.get("qty", [])):
                return False
    return True
