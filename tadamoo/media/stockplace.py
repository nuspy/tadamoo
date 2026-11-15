"""The place of the filler footage: a newscast about a place needs footage of that place, or of a near one, and never of another culture or region.

The matching of stock footage (stock.py) is by keyword overlap and knows nothing about where a video was shot; a market of one country is a market for the keywords of another. This module is the one place where the knowledge is kept,
as data:

  - `PLACES`: for a place (the setting `broll_place`, "Hungary" by default) the **ladder**: the terms that say the place itself (its demonyms, cities, regions, landmarks), then the countries around it (Central and Eastern Europe), then
    European terms, and last a step where no place term is needed. A search goes down the ladder: at each step the place words are added to the query and a hit must carry a term of the step in its tags or in the title of its page.
  - `EXCLUDE`: the terms of the regions and cultures that are not the place's continent (Asia, Africa, the Middle East, Latin America, North America, Oceania), by region. A hit whose tags or title carry one is refused at every step, the last
    one too, and the clips the video model makes get the same terms as a negative.
  - a place that is not in `PLACES` has the place's own words for its first step and then the open step; it has no exclusion (nothing says what a different culture is for it). To add a place: its entry in `PLACES` (`own`, `near`,
    `wider`, `region`) and, for a new continent, its list in `EXCLUDE`.

Terms are compared in lower case without accents, as whole words; a term of several words ("middle east") is looked for in the words of the hit in a row."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

PLACES: Dict[str, Dict[str, Any]] = {
    "hungary": {
        "region": "europe",
        "own": ("hungary", "hungarian", "hungarians", "magyar", "budapest", "debrecen", "szeged", "pecs", "miskolc", "gyor", "eger", "szentendre", "visegrad", "esztergom", "tokaj", "balaton", "lake balaton", "tisza", "danube",
                "hortobagy", "puszta", "pannonia", "szekesfehervar", "kecskemet", "buda", "pest"),
        "near": ("poland", "polish", "warsaw", "krakow", "czech", "czechia", "prague", "slovakia", "slovak", "bratislava", "austria", "austrian", "vienna", "romania", "romanian", "bucharest", "transylvania", "serbia", "belgrade",
                 "croatia", "zagreb", "slovenia", "ljubljana", "ukraine", "kyiv", "bulgaria", "sofia", "lithuania", "latvia", "estonia", "balkan", "balkans", "carpathian", "carpathians", "central europe", "eastern europe"),
        "wider": ("europe", "european", "eu", "germany", "german", "berlin", "france", "french", "paris", "italy", "italian", "rome", "spain", "spanish", "portugal", "netherlands", "dutch", "amsterdam", "belgium", "switzerland", "swiss",
                  "denmark", "sweden", "norway", "finland", "ireland", "england", "british", "london", "greece", "greek", "scandinavia", "alps", "mediterranean"),
    },
}

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


@dataclass(frozen=True)
class Step:
    label: str                                     # what the manifest says: "hungary", "central and eastern europe", "europe", "no place term"
    require: Tuple[str, ...]                       # the hit must carry one of these (empty: nothing is required)
    add: str                                       # added to the keywords of the query ("" for none)


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


def ladder(place: str) -> List[Step]:
    """The steps of the search for a place, from the place itself to the open one."""
    key = plain(place).strip()
    entry = PLACES.get(key)
    name = place.strip() or "place"
    if entry is None:
        own = tuple(dict.fromkeys(w for w in words_of([place]) if len(w) > 2))
        return ([Step(name.lower(), own, name.lower())] if own else []) + [Step("no place term", (), "")]
    return [Step(name.lower(), entry["own"], name.lower()), Step("central and eastern europe", entry["own"] + entry["near"], "central europe"), Step("europe", entry["own"] + entry["near"] + entry["wider"], "europe"),
            Step("no place term", (), "")]


def exclusion(place: str) -> Tuple[str, ...]:
    """The terms of the regions that are not the place's continent; empty for a place that is not known."""
    entry = PLACES.get(plain(place).strip())
    if entry is None:
        return ()
    return tuple(t for region in REGION_OF_OTHERS.get(entry["region"], ()) for t in EXCLUDE[region])


def refused_because(words: Sequence[str], step: Step, excluded: Sequence[str]) -> str:
    """Why a hit with these words is not taken at this step ("" when it may be): it carries a term of another region or culture, or, at a step that needs a place term, none of the step's."""
    term = carries(words, excluded)
    if term:
        return f"carries '{term}', another region or culture"
    if step.require and not carries(words, step.require):
        return f"carries no term of '{step.label}'"
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
