"""The place and the subject of the filler footage: a clip shows what the words over it are about, in the place of the newscast, and never another culture or region.

The matching of stock footage (stock.py) knows nothing about where a video was shot or what it is about beyond its tags and title. This module is the one place where that knowledge is kept as data:

  - `PLACES`: for a place (the setting `broll_place`, "Hungary" by default) the terms that say the place itself: its demonyms, cities, regions and landmarks, in English and in the newscast's language. A search that
    asks for the place wants a hit that carries one of them in its tags or in the title of its page; there is no wider step: a video of Europe, of a country nearby or of any city is not a video of the subject.
  - `GENERIC`: words that name a kind of place or picture and no subject (city, landscape, architecture, street...). A search is never about them alone.
  - `EXCLUDE`: the terms of the regions and cultures that are not the place's continent (Asia, Africa, the Middle East, Latin America, North America, Oceania), by region. A hit whose tags or title carry one is refused,
    and the clips the video model makes get the same terms as a negative.
  - a place that is not in `PLACES` has its own words as its terms; it has no exclusion (nothing says what a different culture is for it). To add a place: its entry in `PLACES` and, for a new continent, its list in `EXCLUDE`.

Terms are compared in lower case without accents, as whole words; a term of several words ("middle east") is looked for in the words of the hit in a row."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Sequence, Tuple

PLACES: Dict[str, Dict[str, Any]] = {
    "hungary": {
        "region": "europe",
        "own": ("hungary", "hungarian", "hungarians", "magyar", "magyarorszag", "magyarorszagi", "budapest", "budapesti", "debrecen", "szeged", "pecs", "miskolc", "gyor", "eger", "szentendre", "visegrad",
                "esztergom", "tokaj", "balaton", "lake balaton", "tisza", "danube", "hortobagy", "puszta", "pannonia", "szekesfehervar", "kecskemet", "buda", "pest"),
    },
}

GENERIC = frozenset("""city cities town village landscape landscapes nature architecture building buildings street streets skyline aerial drone travel tourism tourist tourists view views
country countryside rural urban sky clouds sunset sunrise europe european world day night people person man woman group traffic road roads water river lake""".split())

EXCLUDE: Dict[str, Tuple[str, ...]] = {
    "asia": ("asia", "asian", "china", "chinese", "beijing", "shanghai", "hong kong", "tibet", "tibetan", "japan", "japanese", "tokyo", "korea", "korean", "seoul", "india", "indian", "delhi", "mumbai", "nepal", "nepali", "thailand", "thai",
             "bangkok", "vietnam", "vietnamese", "hanoi", "indonesia", "indonesian", "bali", "malaysia", "malaysian", "philippines", "filipino", "singapore", "cambodia", "laos", "myanmar", "burma", "mongolia", "pakistan", "bangladesh"),
    "africa": ("africa", "african", "egypt", "egyptian", "morocco", "moroccan", "marrakech", "kenya", "nigeria", "ethiopia", "tanzania", "safari", "sahara", "zulu", "maasai", "cairo"),
    "middle east": ("middle east", "arab", "arabic", "dubai", "saudi", "iran", "iranian", "iraq", "syria", "lebanon", "jordan", "bedouin", "souk", "bazaar"),
    "latin america": ("latin", "latino", "mexican", "mexico", "brazil", "brazilian", "peru", "peruvian", "argentina", "colombia", "cuba", "cuban", "caribbean", "inca", "andes", "amazon"),
    "north america": ("usa", "america", "american", "texas", "new york", "california", "canada", "canadian", "alaska", "hollywood"),
    "oceania": ("australia", "australian", "aboriginal", "maori", "polynesian", "hawaii", "hawaiian", "fiji", "new zealand"),
}
REGION_OF_OTHERS = {"europe": ("asia", "africa", "middle east", "latin america", "north america", "oceania")}      # for a place of this continent, the regions of EXCLUDE that are not it


def plain(text: str) -> str:
    """Lower case, no accents."""
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")


def words_of(texts: Sequence[str]) -> List[str]:
    return re.findall(r"[a-z]+", plain(" ".join(texts)))


def carries(words: Sequence[str], terms: Sequence[str]) -> str:
    """The first term (one word, or several in a row) found in the words of a hit; "" when none."""
    joined = " " + " ".join(words) + " "
    for term in terms:
        if f" {plain(term)} " in joined:
            return term
    return ""


def own(place: str) -> Tuple[str, ...]:
    """The terms that say the place itself: the ones kept for it, or, for a place nothing is known about, the words of its name."""
    entry = PLACES.get(plain(place).strip())
    if entry is not None:
        return tuple(entry["own"])
    return tuple(dict.fromkeys(w for w in words_of([place]) if len(w) > 2))


def exclusion(place: str) -> Tuple[str, ...]:
    """The terms of the regions that are not the place's continent; empty for a place that is not known."""
    entry = PLACES.get(plain(place).strip())
    if entry is None:
        return ()
    return tuple(t for region in REGION_OF_OTHERS.get(entry["region"], ()) for t in EXCLUDE[region])


def _other_number(w: str) -> str:
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("y"):
        return w[:-1] + "ies"
    if w.endswith(("us", "ss", "is")):
        return w
    if w.endswith("s") and len(w) > 4:
        return w[:-1]
    return w + "s"


def subject_terms(words: Sequence[str], place: str) -> List[str]:
    """The words of a subject that can stand for it: no generic word, no term of the place (a search for the place is not a search for a subject), at least three letters."""
    banned = set(GENERIC) | {plain(t) for t in own(place)}
    out: List[str] = []
    for w in (plain(x) for x in words):
        if re.fullmatch(r"[a-z]{3,}", w) and w not in banned:
            out += [w, _other_number(w)]                                              # a tag says the thing in the singular or in the plural
    return list(dict.fromkeys(out))


def other_culture(words: Sequence[str], excluded: Sequence[str]) -> str:
    """Why a hit with these words is refused for showing another region or culture ("" when it does not)."""
    term = carries(words, excluded)
    return f"carries '{term}', another region or culture" if term else ""


def refused_because(words: Sequence[str], subject: Sequence[str], place_terms: Sequence[str], excluded: Sequence[str]) -> str:
    """Why a hit with these words is not taken ("" when it may be): it carries a term of another region or culture, none of the subject's words, or, when the place is wanted, none of the place's terms."""
    why = other_culture(words, excluded)
    if why:
        return why
    if not carries(words, subject):
        return "is not about the subject (" + ", ".join(list(subject)[:4]) + ")"
    if place_terms and not carries(words, place_terms):
        return "carries no term of the place"
    return ""


def negative(place: str) -> str:
    """What the video model is told not to show for a place: scenes, people, buildings and clothes of the other regions."""
    entry = PLACES.get(plain(place).strip())
    if entry is None:
        return ""
    names = ("Asian", "East Asian", "South Asian", "African", "Middle Eastern", "Arab", "Latin American", "North American", "Oceanian")
    return "scenes, people, buildings, markets, streets or clothes of another culture or region (" + ", ".join(names) + ")"


def look(place: str) -> str:
    """The sentence that puts the footage in the place."""
    name = place.strip()
    return f"Set in {name}: its people, streets, buildings, landscape and clothes, nothing of a different culture or region." if name else ""
