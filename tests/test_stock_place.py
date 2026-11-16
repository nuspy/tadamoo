"""The place and the subject of the filler footage: a hit must be about the subject, in the place, and not another culture; the registry that keeps a video from coming back for a week; the production that
records what it did. Stand-in search results."""
import json
import shutil
import time

import pytest

from tadamoo.media import stock, stockplace

pytest.importorskip("tadamoo.newscast")      # the production code is a private one, kept out of the repository
from tadamoo.newscast import produce, settings as settings_mod
from test_stock import KEY, Pixabay, hit, pix, run, one_story_script, street_rungs  # noqa: F401  (the stand-in service and the fixtures)
from test_newscast import stage  # noqa: F401

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
HUNGARY = stockplace.own("Hungary")
OTHERS = stockplace.exclusion("Hungary")


def mk(i, tags, title="clip", duration=9, downloads=100):
    return stock.Hit(i, f"https://pixabay.com/videos/{title}-{i}/", [t.strip() for t in tags.split(",")], "x", duration, downloads, {"small": {"url": "u"}})


class Fake:
    """A search that answers with the hits `answer(query)` gives and keeps the queries."""

    def __init__(self, answer):
        self.answer, self.queries = answer, []

    def search(self, query, lang="en", per_page=20):
        self.queries.append(query)
        return self.answer(query)


UNIVERSITY = stock.Rung(("university", "universities", "campus"), (("university campus hungary", "en"),), True)
STUDENTS = stock.Rung(("students", "student"), (("students hungary", "en"),), True)


def test_the_place_is_named_by_its_own_terms_and_nothing_wider():
    assert "budapest" in HUNGARY and "balaton" in HUNGARY and "magyarorszag" in HUNGARY and "poland" not in HUNGARY and "europe" not in HUNGARY and "germany" not in HUNGARY
    assert stockplace.own("Atlantis") == ("atlantis",) and stockplace.exclusion("Atlantis") == ()                       # a place nothing is known about: its own words, no exclusion
    assert not hasattr(stockplace, "ladder")


def test_the_exclusion_names_the_other_regions_and_is_one_documented_list():
    for term in ("china", "chinese", "asian", "japan", "india", "thailand", "vietnam", "korea", "african", "arab", "middle east", "mexican", "latin", "american"):
        assert term in OTHERS
    assert "hungary" not in OTHERS and "europe" not in OTHERS and "budapest" not in OTHERS
    assert stockplace.carries(stockplace.words_of(["Chinese", "market"]), OTHERS) == "chinese" and stockplace.carries(stockplace.words_of(["a middle east bazaar"]), OTHERS) in ("middle east", "bazaar")
    assert stockplace.carries(stockplace.words_of(["Hortobágy", "puszta"]), HUNGARY) == "hortobagy"                         # accents do not matter
    assert "culture" in stockplace.negative("Hungary") and "Set in Hungary" in stockplace.look("Hungary") and stockplace.negative("Atlantis") == ""


def test_a_subject_is_never_the_place_or_a_kind_of_picture():
    assert stockplace.subject_terms(["university", "Budapest", "landscape", "city", "architecture", "Hungary"], "Hungary") == ["university", "universities"]
    assert stockplace.subject_terms(["students", "campus"], "Hungary") == ["students", "student", "campus"]
    assert stockplace.subject_terms(["landscape", "nature", "hu"], "Hungary") == []                                           # nothing that can stand for a subject


def test_a_castle_of_the_place_is_not_a_university_and_the_refusal_says_why():
    castle = mk(1, "castle, visegrad, hungary, architecture, danube, history", title="visegrad-castle", downloads=9999)
    campus = mk(2, "university, campus, budapest, students", downloads=5)
    refused = []
    got, asked = stock.find(Fake(lambda q: [castle, campus]), [UNIVERSITY], 6.4, 6.4, set(), HUNGARY, OTHERS, refused)
    assert got.hit.id == 2 and got.step == "university/universities/campus + place"
    assert any("pixabay 1" in r and "is not about the subject" in r for r in refused)
    alone, _ = stock.find(Fake(lambda q: [castle]), [UNIVERSITY], 6.4, 6.4, set(), HUNGARY, OTHERS)
    assert alone is None                                                                                                   # a video of the place that is not about the subject is not taken


def test_any_video_of_the_capital_does_not_do_for_the_parliament():
    tram = mk(3, "tram, budapest, hungary, city, transport")
    parliament = mk(4, "parliament, budapest, hungary, landmark")
    rung = stock.Rung(("parliament", "parliaments"), (("parliament hungary", "en"),), True)
    got, _ = stock.find(Fake(lambda q: [tram, parliament]), [rung], 6.4, 6.4, set(), HUNGARY, OTHERS)
    assert got.hit.id == 4
    assert stock.find(Fake(lambda q: [tram]), [rung], 6.4, 6.4, set(), HUNGARY, OTHERS)[0] is None


def test_the_rungs_go_from_the_subject_to_what_the_text_names_and_a_subject_of_another_place_is_not_taken():
    german = mk(5, "university, campus, germany, berlin")
    students = mk(6, "students, lecture, budapest, hungary")
    got, asked = stock.find(Fake(lambda q: [german, students]), [UNIVERSITY, STUDENTS], 6.4, 6.4, set(), HUNGARY, OTHERS)
    assert got.hit.id == 6 and got.step == "students/student + place"
    assert asked == ["en: university campus hungary [university/universities/campus + place]", "en: students hungary [students/student + place]"]
    chinese = mk(7, "university, campus, china, hungary")
    assert stock.find(Fake(lambda q: [chinese]), [UNIVERSITY], 6.4, 6.4, set(), HUNGARY, OTHERS)[0] is None             # the exclusion applies at every rung


def test_a_video_shown_in_the_last_week_is_kept_apart_from_an_older_one(tmp_path):
    used = stock.Used(tmp_path / "stock_used.json")
    now = time.time()
    used.add(1, "2026-11-15-morning", "p1", now - 2 * 86400)
    used.add(2, "2026-11-01-morning", "p2", now - 9 * 86400)
    assert used.ids() == {"pixabay:1", "pixabay:2"} and used.recent() == {"pixabay:1"} and used.recent(now + 8 * 86400) == set()
    assert stock.Used(tmp_path / "stock_used.json").ids() == {"pixabay:1", "pixabay:2"}                                                          # kept in the file


def test_the_registry_learns_from_the_editions_that_are_there(tmp_path):
    for day, vid in (("2026-11-15-morning", 11), ("2026-11-16-morning", 12)):
        folder = tmp_path / day / "broll"
        folder.mkdir(parents=True)
        (folder / "s1_c1.json").write_text(json.dumps({"source": "pixabay", "id": vid, "page": f"p{vid}"}), encoding="utf-8")
        (folder / "s1_c2.json").write_text(json.dumps({"source": "model"}), encoding="utf-8")
    used = stock.Used(tmp_path / "stock_used.json")
    assert used.seed_from(tmp_path) == 2 and used.ids() == {"pixabay:11", "pixabay:12"} and used.seed_from(tmp_path) == 0


def test_the_place_is_a_setting_with_hungary_as_the_default_and_is_checked():
    assert settings_mod.parse({}).broll_place == "Hungary" and settings_mod.parse({"broll_place": "Poland"}).broll_place == "Poland"
    for bad in ({"broll_place": ""}, {"broll_place": "x" * 90}, {"broll_place": 3}):
        with pytest.raises(settings_mod.SettingsError):
            settings_mod.parse(bad)


def street_in_hungary(monkeypatch):
    monkeypatch.setattr(produce.stockplan, "plan", lambda *a, **k: [stock.Rung(("street", "crowd"), (("crowded street", "en"),), True)])


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_the_production_takes_no_chinese_market_notes_why_and_tells_the_video_model_the_place(stage, pix, monkeypatch):
    street_in_hungary(monkeypatch)
    manifest, work, server = run(stage, lambda q: [hit(41, "street, crowd, chinese, traditional dress", title="chinese-street-market")], pix, stage["tmp"])
    assert not server.downloads and any("refused" in n and "chinese" in n for n in manifest["notes"]) and any("nothing in pixabay is about street in Hungary" in n for n in manifest["notes"])
    asked = [r for k, r, _ in stage["jobs"].asked if k == "video"]
    assert asked and all("Set in Hungary" in r["prompt"] and "another culture or region" in r["negative"] for r in asked)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_the_production_records_the_rung_a_stock_clip_came_from_and_keeps_it_for_a_week(stage, pix, monkeypatch):
    street_in_hungary(monkeypatch)
    manifest, work, server = run(stage, lambda q: [hit(51, "street, crowd, budapest, hungary", title="budapest-street")], pix, stage["tmp"])
    info = json.loads(next((work / "broll").glob("s1_c1.json")).read_text(encoding="utf-8"))
    assert info["id"] == 51 and info["step"] == "street/crowd + place" and info["refused"] == []
    assert stock.Used(work.parent / "stock_used.json").recent() == {"pixabay:51"}


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_video_shown_yesterday_is_left_out_and_one_shown_more_than_a_week_ago_comes_back_only_when_nothing_else_passes(stage, pix, monkeypatch):
    street_in_hungary(monkeypatch)
    stock.Used(stage["tmp"] / "stock_used.json").add(61, "2026-11-15-morning", "p", time.time() - 86400)
    manifest, work, server = run(stage, lambda q: [hit(61, "street, crowd, budapest, hungary"), hit(62, "street, crowd, hungary", downloads=1)], pix, stage["tmp"])
    assert json.loads(next((work / "broll").glob("s1_c1.json")).read_text(encoding="utf-8"))["id"] == 62                  # 61 was shown yesterday
    stock.Used(stage["tmp"] / "stock_used.json").add(63, "2026-11-01-morning", "p", time.time() - 9 * 86400)
    only_old = pix(lambda q: [hit(63, "street, crowd, hungary")])
    again = stage["tmp"] / "again"
    from test_newscast import DEFAULTS, settings
    manifest2 = produce.make(one_story_script(), settings(stage, width=400, height=266, fps=12), again,
                             produce.Options(url=stage["server"].url, media=dict(DEFAULTS), swap="per-job", stock_key=KEY, stock_url=only_old.api))
    assert json.loads(next((again / "broll").glob("s1_c1.json")).read_text(encoding="utf-8"))["id"] == 63
    assert any("shown more than a week ago and nothing else passes" in n for n in manifest2["notes"])
