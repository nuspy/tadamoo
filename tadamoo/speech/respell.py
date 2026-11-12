"""Writing a Hungarian word for its sound, so that a voice engine reads it right. Only the text sent to the voice is respelled: the text on the screen, the captions and the review
page keep the correct spelling, and the read-back check compares what was heard with the correct spelling, never with the respelling.

Three layers, all deterministic (the same input gives the same output):

  1. rules      general respelling rules for the spelling problems of Hungarian for an engine (long digraphs, long vowels, hard letters, word edges). Each has a name and
                is switched on only if the held-out measurement says it helps (`RULES`, `DEFAULT_RULES`, the setting `respell_rules`);
  2. lexicon    a plain JSON data file of word -> respelling, per voice, that the person can edit (`lexicon/hu/<engine>-<voice>.json`, then `lexicon/hu/default.json`, then the
                person's own file); a word is looked up whole, then as a known stem with a suffix;
  3. optimizer  (optimize.py) finds the respelling that reads best for a word, by trying the candidates this module generates, and writes the lexicon.

`candidates(word)` is the systematic list the optimizer tries: syllable hyphenation, long digraphs written out, long vowels, sound-alike spellings of hard letters, the word's
first syllable cut off."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

DATA = Path(__file__).with_name("data")
LEXICON_DIR = DATA / "lexicon" / "hu"
VOWELS = "aáeéiíoóöőuúüű"
DIGRAPHS = ("dzs", "cs", "dz", "gy", "ly", "ny", "sz", "ty", "zs")
LONG = {"ccs": "cs-cs", "ddz": "dz-dz", "ddzs": "dzs-dzs", "ggy": "gy-gy", "lly": "ly-ly", "nny": "ny-ny", "ssz": "sz-sz", "tty": "ty-ty", "zzs": "zs-zs"}
SUFFIXES = sorted(["ban", "ben", "ba", "be", "ból", "ből", "ra", "re", "ról", "ről", "on", "en", "ön", "n", "nál", "nél", "hoz", "hez", "höz", "tól", "től", "val", "vel", "ig", "ért", "ként",
                   "nak", "nek", "t", "at", "et", "ot", "öt", "k", "ak", "ek", "ok", "ök", "i", "ai", "ei", "é", "ja", "je", "a", "e", "ra", "ről", "ban", "ben", "ít", "ában", "ében", "ának", "ének",
                   "ától", "étől", "ával", "ével", "ára", "ére", "ától", "án", "én", "ái", "éi", "ot", "ört", "ból", "ből", "ékben", "okban", "ekben", "ökben", "akat", "eket", "okat", "öket"],
                  key=lambda s: -len(s))


# ---- syllables
def graphemes(word: str) -> List[str]:
    """The letters of a word with the digraphs and trigraphs as one unit each; a long digraph (ssz, ccs ...) is written out as the two units it is said as (sz, sz)."""
    out: List[str] = []
    i, low = 0, word.lower()
    while i < len(word):
        for long in sorted(LONG, key=len, reverse=True):
            if low.startswith(long, i):
                half = LONG[long].split("-")[0]
                first = half.capitalize() if word[i].isupper() else half
                out += [first, half]
                i += len(long)
                break
        else:
            for dg in DIGRAPHS:
                if low.startswith(dg, i):
                    out.append(word[i:i + len(dg)])
                    i += len(dg)
                    break
            else:
                out.append(word[i])
                i += 1
    return out


def syllables(word: str) -> List[str]:
    """The word cut into syllables (Hungarian rules: one consonant goes with the vowel after it, of several the last one does; a long digraph splits in the middle)."""
    units = graphemes(word)
    is_vowel = [u.lower()[0] in VOWELS for u in units]
    cuts: List[int] = []
    i = 0
    while i < len(units):
        if is_vowel[i]:
            j = i + 1
            while j < len(units) and not is_vowel[j]:
                j += 1
            if j < len(units):                                   # the consonants between this vowel and the next
                between = j - i - 1
                cuts.append(j - 1 if between >= 1 else j)
            i = j
        else:
            i += 1
    parts, last = [], 0
    for c in cuts:
        parts.append("".join(units[last:c]))
        last = c
    parts.append("".join(units[last:]))
    return [p for p in parts if p]


# ---- general rules, each a name, a doc and a function on a word
def _expand_long(word: str) -> str:
    return re.sub("|".join(LONG), lambda m: LONG[m.group(0).lower()].replace("-", ""), word, flags=re.IGNORECASE)


def _hyphenate(word: str) -> str:
    return word if "-" in word else "-".join(syllables(word))


def _first_syllable(word: str) -> str:
    parts = syllables(word)
    return parts[0] + "-" + "".join(parts[1:]) if len(parts) > 2 and "-" not in word else word


def _lengthen_first(word: str) -> str:
    parts = syllables(word)
    return parts[0].translate(LENGTHEN) + "".join(parts[1:]) if len(parts) > 1 else word


def _hard_cs(word: str) -> str:
    return re.sub("cs", "ch", word)


def _lower(word: str) -> str:
    return word.lower()


LENGTHEN = str.maketrans({"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú", "ö": "ő", "ü": "ű"})


def _split_suffix(word: str) -> str:
    """A known ending cut off with a hyphen: Brüsszelben > Brüsszel-ben."""
    low = word.lower()
    for suffix in SUFFIXES:
        if len(suffix) >= 2 and "-" not in word and len(low) > len(suffix) + 3 and low.endswith(suffix) and any(c in VOWELS for c in low[:-len(suffix)]):
            return word[:-len(suffix)] + "-" + word[-len(suffix):]
    return word


def _lengthen_last(word: str) -> str:
    parts = syllables(word)
    return "".join(parts[:-1]) + parts[-1].translate(LENGTHEN) if len(parts) > 1 else word


def _double_last(word: str) -> str:
    return word + word[-1] if word and word[-1].isalpha() and word[-1].lower() not in VOWELS else word


def _trail(word: str) -> str:
    return word + "h" if word and word[-1].lower() not in "h" else word


def _spaced(word: str) -> str:
    parts = syllables(word)
    return " ".join([parts[0], "".join(parts[1:])]) if len(parts) > 2 else word


RULES: Dict[str, Tuple[str, Callable[[str], str]]] = {
    "expand_long_digraphs": ("ssz, ccs, zzs, ggy, nny, tty, lly are written as the two digraphs they are said as (Brüsszel > Brüszszel)", _expand_long),
    "hyphenate": ("the word is cut into syllables with hyphens (Brüsszelben > Brüsz-szel-ben)", _hyphenate),
    "first_syllable": ("a hyphen after the first syllable only", _first_syllable),
    "split_suffix": ("a known ending is cut off with a hyphen (Brüsszelben > Brüsszel-ben)", _split_suffix),
    "lengthen_first_vowel": ("the vowel of the first syllable is written long", _lengthen_first),
    "lengthen_last_vowel": ("the vowel of the last syllable is written long", _lengthen_last),
    "soft_cs": ("cs is written ch (Kovács > Kovách)", _hard_cs),
    "double_last": ("the last consonant is written twice, against a cut-off last syllable", _double_last),
    "trail_h": ("an h is added after the end, against a cut-off last syllable", _trail),
    "spaced": ("a space after the first syllable", _spaced),
    "lowercase": ("a capital that is not at the start of a sentence is written small", _lower),
}
DEFAULT_RULES: Tuple[str, ...] = ()                    # which rules apply to every word: none until the held-out measurement says one helps
COMBINATIONS = (("hyphenate", "expand_long_digraphs"), ("hyphenate", "lengthen_first_vowel"), ("split_suffix", "expand_long_digraphs"), ("split_suffix", "lengthen_last_vowel"),
                ("hyphenate", "lowercase"), ("first_syllable", "lengthen_first_vowel"))


def apply_rules(word: str, names: Sequence[str]) -> str:
    for name in names:
        word = RULES[name][1](word)
    return word


def candidates(word: str, limit: int = 40) -> List[str]:
    """The respellings of one word the optimizer tries, the word itself first, without duplicates. A word of several parts (a name, a compound term) is respelled part by part
    in each way and also as a whole."""
    parts = word.split(" ")
    if len(parts) > 1:
        per = [candidates(p, limit=6) for p in parts]
        out = [word]
        for k in range(1, min(len(c) for c in per)):
            out.append(" ".join(c[k] for c in per))
        for i, c in enumerate(per):                       # one part respelled at a time
            for k in range(1, len(c)):
                out.append(" ".join(c[k] if j == i else per[j][0] for j in range(len(parts))))
        return list(dict.fromkeys(out))[:limit]
    cands = [word] + [fn(word) for _, fn in RULES.values()] + [apply_rules(word, combo) for combo in COMBINATIONS]
    return [c for c in dict.fromkeys(cands) if c][:limit]


# ---- the lexicon: plain data, per voice
class Lexicon:
    """word -> respelling. Lookups are exact on the word (any case), then on a known stem followed by a suffix, so one entry for Brüsszel serves Brüsszelben, Brüsszelből ..."""

    def __init__(self, entries: Optional[Dict[str, str]] = None) -> None:
        self.entries: Dict[str, str] = {}
        for word, say in (entries or {}).items():
            self.entries[word.casefold()] = say
        self._stems = sorted(self.entries, key=lambda s: -len(s))

    @classmethod
    def for_voice(cls, engine: str = "", voice: str = "", extra: Iterable[Path] = (), lang: str = "hu") -> "Lexicon":
        """The default lexicon, then the voice's own over it, then the person's files over both."""
        folder = DATA / "lexicon" / lang
        names = ["default.json"] + ([f"{engine}-{voice}".lower().replace(" ", "-") + ".json"] if engine and voice else [])
        merged: Dict[str, str] = {}
        for path in [folder / n for n in names] + [Path(p) for p in extra]:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                merged.update({k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)})
        return cls(merged)

    def lookup(self, word: str) -> Optional[str]:
        key = word.casefold()
        if key in self.entries:
            return self.entries[key]
        for stem in self._stems:
            if key.startswith(stem) and len(key) > len(stem) and key[len(stem):] in SUFFIXES:
                return self.entries[stem] + word[len(stem):]
        return None

    def apply(self, text: str) -> str:
        return re.sub(r"[^\W\d_]+(?:-[^\W\d_]+)*", lambda m: self._word(m.group(0)), text)

    def _word(self, word: str) -> str:
        found = self.lookup(word)
        if found is None:
            return word
        return found[0].upper() + found[1:] if word[:1].isupper() and found[:1].islower() else found


def respell(text: str, lang: str = "hu", lexicon: Optional[Lexicon] = None, rules: Sequence[str] = DEFAULT_RULES) -> str:
    """The speech text respelled: the lexicon first (what was measured), then the general rules named in `rules` for the words the lexicon does not know. Other languages are
    returned as they are."""
    if lang != "hu":
        return text
    lexicon = lexicon if lexicon is not None else Lexicon()

    def word(m: "re.Match[str]") -> str:
        w = m.group(0)
        found = lexicon.lookup(w)
        if found is not None:
            return found[0].upper() + found[1:] if w[:1].isupper() and found[:1].islower() else found
        for name in rules:
            w = RULES[name][1](w)
        return w
    return re.sub(r"[^\W\d_]+", word, text)
