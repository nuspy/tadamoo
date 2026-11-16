"""The place of the filler footage: the ladder from the place to near places, the exclusion of other regions and cultures at every step, and the production that records the step and the refused hits. Stand-in search results."""
import json
import shutil

import pytest

from tadamoo.media import stock, stockplace
import pytest

pytest.importorskip("tadamoo.newscast")      # the production code is a private one, kept out of the repository
from tadamoo.newscast import produce, settings as settings_mod
from test_stock import KEY, Pixabay, hit, pix, run, one_story_script  # noqa: F401  (the stand-in service and the fixture)
from test_newscast import stage  # noqa: F401

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def mk(i, tags, title="clip", duration=9, downloads=100):
    return stock.Hit(i, f"https://pixabay.com/videos/{title}-{i}/", [t.strip() for t in tags.split(",")], "x", duration, downloads, {"small": {"url": "u"}})


class Fake:
    """A search that answers with the hits `answer(query)` gives and keeps the queries."""

    def __init__(self, answer):
        self.answer, self.queries = answer, []

    def search(self, query, lang="en", per_page=20):
        self.queries.append(query)
        return self.answer(query)


def test_the_ladder_goes_from_the_place_to_central_and_eastern_europe_to_europe_and_ends_open():
    steps = stockplace.ladder("Hungary")
    assert [s.label for s in steps] == ["hungary", "central and eastern europe", "europe", "no place term"]
    assert [s.add for s in steps] == ["hungary", "central europe", "europe", ""]
    assert "budapest" in steps[0].require and "balaton" in steps[0].require and "poland" not in steps[0].require
    assert "poland" in steps[1].require and "budapest" in steps[1].require and "germany" not in steps[1].require             # a step widens, it does not forget the narrower one
    assert "germany" in steps[2].require and steps[3].require == ()
    other = stockplace.ladder("Atlantis")
    assert [s.label for s in other] == ["atlantis", "no place term"] and stockplace.exclusion("Atlantis") == ()           # a place nothing is known about: its own words, then open, no exclusion


def test_the_exclusion_names_the_other_regions_and_is_one_documented_list():
    out = stockplace.exclusion("Hungary")
    for term in ("china", "chinese", "asian", "japan", "india", "thailand", "vietnam", "korea", "african", "arab", "middle east", "mexican", "latin", "american"):
        assert term in out
    assert "hungary" not in out and "europe" not in out and "budapest" not in out
    assert stockplace.carries(stockplace.words_of(["Chinese", "market"]), out) == "chinese" and stockplace.carries(stockplace.words_of(["a middle east bazaar"]), out) in ("middle east", "bazaar")
    assert stockplace.carries(stockplace.words_of(["Hortobágy", "puszta"]), stockplace.ladder("Hungary")[0].require) == "hortobagy"          # accents do not matter
    assert "culture" in stockplace.negative("Hungary") and "Set in Hungary" in stockplace.look("Hungary") and stockplace.negative("Atlantis") == ""


def test_a_chinese_market_loses_to_a_hungarian_one_and_the_refusal_is_recorded():
    chinese = mk(1, "market, merchants, elderly women, traditional dress, boxes, road, wooden houses", title="chinese-country-market", downloads=9999)
    hungarian = mk(2, "market, merchants, budapest, hungary", downloads=10)
    refused = []
    got, asked = stock.find(Fake(lambda q: [chinese, hungarian]), [("merchants selling goods at a market", "en")], 6.4, 0.3, set(), stockplace.ladder("Hungary"), stockplace.exclusion("Hungary"), refused)
    assert got is not None and got.hit.id == 2 and got.step == "hungary"
    assert any("video 1" in r and "'chinese'" in r and "'hungary'" in r for r in refused)
    assert asked[0].endswith("[hungary]") and "hungary" in asked[0]                                                          # the place word is in the query


def test_with_only_a_chinese_market_to_be_had_there_is_no_stock_and_the_video_model_makes_it():
    chinese = mk(1, "market, merchants, old women, traditional dress, boxes, road", title="chinese-country-market")
    tagged = mk(2, "market, merchants, goods, china, asia", title="market-stall")
    refused = []
    got, asked = stock.find(Fake(lambda q: [chinese, tagged]), [("merchants selling goods at a market", "en")], 6.4, 0.3, set(), stockplace.ladder("Hungary"), stockplace.exclusion("Hungary"), refused)
    assert got is None                                                                                                       # not even the open last step takes them: the exclusion applies at every step
    assert any("[no place term]" in a for a in asked) and any("china" in r or "chinese" in r for r in refused) and any("'asia'" in r or "'china'" in r for r in refused)


def test_the_steps_are_tried_in_order_and_a_hit_is_taken_at_the_first_step_that_has_a_term_for_it():
    german = mk(3, "market, merchants, germany, berlin")
    polish = mk(4, "market, merchants, poland, warsaw")
    plain_hit = mk(5, "market, merchants, goods")
    fake = Fake(lambda q: [german, polish, plain_hit])
    got, asked = stock.find(fake, [("merchants at a market", "en")], 6.4, 0.3, set(), stockplace.ladder("Hungary"), stockplace.exclusion("Hungary"))
    assert got.hit.id == 4 and got.step == "central and eastern europe"                                                      # Poland is near, Germany is wider, a hit with no place is last
    steps_in_asked = [a.rsplit("[", 1)[1] for a in asked]
    assert steps_in_asked[0].startswith("hungary") and all(s.startswith("hungary") for s in steps_in_asked[:steps_in_asked.index(next(s for s in steps_in_asked if not s.startswith("hungary")))])
    only_open = stock.find(Fake(lambda q: [plain_hit]), [("merchants at a market", "en")], 6.4, 0.3, set(), stockplace.ladder("Hungary"), stockplace.exclusion("Hungary"))[0]
    assert only_open.hit.id == 5 and only_open.step == "no place term"                                                       # nothing wrong with it, nothing that places it: only at the open step
    europe = stock.find(Fake(lambda q: [german]), [("merchants at a market", "en")], 6.4, 0.3, set(), stockplace.ladder("Hungary"), stockplace.exclusion("Hungary"))[0]
    assert europe.step == "europe"


def test_without_a_ladder_the_search_is_as_it_was():
    got, asked = stock.find(Fake(lambda q: [mk(6, "market, merchants, goods, china")]), [("merchants at a market", "en")], 6.4, 0.3, set())
    assert got.hit.id == 6 and got.step == "" and all("[" not in a for a in asked)


def test_the_place_is_a_setting_with_hungary_as_the_default_and_is_checked():
    assert settings_mod.parse({}).broll_place == "Hungary" and settings_mod.parse({"broll_place": "Poland"}).broll_place == "Poland"
    for bad in ({"broll_place": ""}, {"broll_place": "x" * 90}, {"broll_place": 3}):
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.parse(bad)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_the_production_takes_no_chinese_market_notes_why_and_tells_the_video_model_the_place(stage, pix):
    manifest, work, server = run(stage, lambda q: [hit(41, "street, crowd, chinese, traditional dress", title="chinese-street-market")], pix, stage["tmp"])
    assert not server.downloads and any("refused" in n and "chinese" in n for n in manifest["notes"]) and any("nothing on Pixabay fits the place Hungary" in n for n in manifest["notes"])
    asked = [r for k, r, _ in stage["jobs"].asked if k == "video"]
    assert asked and all("Set in Hungary" in r["prompt"] and "another culture or region" in r["negative"] for r in asked)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_the_production_records_the_step_a_stock_clip_came_from(stage, pix):
    manifest, work, server = run(stage, lambda q: [hit(51, "street, crowd, budapest, hungary", title="budapest-street")], pix, stage["tmp"])
    info = json.loads(next((work / "broll").glob("s1_c1.json")).read_text(encoding="utf-8"))
    assert info["id"] == 51 and info["step"] == "hungary" and info["refused"] == []
