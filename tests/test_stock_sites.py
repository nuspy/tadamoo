"""The stock sites read through a browser (Pexels, Envato): what their pages say becomes a result, the choice follows the same rules as for Pixabay, a site whose list gives only part of the description is asked
for the page of the few videos that pass the list, and the sources of a production are asked in order. No browser starts and no site is asked: the texts are those of real pages."""
from tadamoo.media import stock, stockplace
from tadamoo.media.stock_sites import Envato, Pexels

PLACE, OTHERS = stockplace.own("Hungary"), stockplace.exclusion("Hungary")
PEXELS_PAGE = {"text": "Photos\nExplore\nGeorge Pak\n\n453M views\n\nEdit in Canva\n12\nFree download\nGeorge Pak\nFollow\n\nLicense\n\nFree\n\nDimensions\n\n3840x2160\n\nAspect Ratio\n\n16:9\n\n"
                        "Duration\n\n0:10\n\nDate\n\nMay 20, 2021\n\nDescription\n\nA diverse group of students studying together outside using laptops and papers.\nMore like this\n",
               "keywords": "International Students, Students Studying, Study Video", "description": "Students at a campus in Budapest."}
ENVATO_PAGE = {"text": "Sign in\nBrief\nVideos\nuniversity students\nFilters\nSort\nSave\n0:00 / 0:20\nGroup of Students Walking to University Building\nby johntrek120\nDownload 4K\nDownload Preview\n"
                       "Add as video reference\nStudents\nCollege\nCampus\n20 seconds\n3840 x 2160 px\n30 fps\nNo Alpha Channel\nNot Looped\nH.264\nHorizontal\n"
                       "A group of five young adults walking towards a large building in Budapest.\nLifetime Commercial License\nSimilar by johntrek120\n",
               "preview": "https://video-previews.elements.envatousercontent.com/x/watermarked_preview.mp4"}


def pexels_hit(i=5940065, slug="teacher-and-student-in-a-library"):
    return stock.Hit(i, f"https://www.pexels.com/video/{slug}-{i}/", [], "", 0.0, 0, {}, "pexels", "", 0, 0, True)


def test_a_result_has_a_key_made_of_its_site_and_its_id_and_words_from_its_title():
    assert pexels_hit().key == "pexels:5940065"
    assert pexels_hit().words == ["teacher", "and", "student", "in", "a", "library"]                                        # the title is in the address of the page
    envato = stock.Hit("b5d9", "https://app.envato.com/search/stock-video/b5d940a7-bf91-4b80-b51d-06c5ac6f108f?x=1", [], "", 14.0, 0, {}, "envato", "Writing University Student With Marker", 0, 0, True)
    assert envato.key == "envato:b5d9" and envato.words == ["writing", "university", "student", "with", "marker"]              # not the letters of its identifier


def test_the_page_of_a_pexels_video_says_its_length_its_size_its_tags_and_its_author():
    hit = pexels_hit()
    Pexels.read(hit, PEXELS_PAGE)
    assert (hit.duration, hit.width, hit.height, hit.user, hit.partial) == (10, 3840, 2160, "George Pak", False)
    assert "International Students" in hit.tags and "Budapest" in " ".join(hit.tags) and hit.files["download"]["url"].endswith("/download/video/5940065/")


def test_the_page_of_an_envato_video_says_its_tags_its_size_its_description_and_the_address_of_its_preview():
    hit = stock.Hit("df1d", "https://app.envato.com/x", [], "", 20.0, 0, {}, "envato", "Group of Students Walking to University Building", 0, 0, True)
    Envato.read(hit, ENVATO_PAGE)
    assert hit.tags[:3] == ["Students", "College", "Campus"] and "Budapest" in hit.tags[-1]
    assert (hit.duration, hit.width, hit.height, hit.partial) == (20.0, 3840, 2160, False) and hit.files["preview"]["url"].endswith("watermarked_preview.mp4")


class Site:
    """A source like the sites: a list with partial results, a page that completes them, and the order in which they were read."""

    first_fit = True
    max_details = 4

    def __init__(self, hits, pages):
        self.hits, self.pages, self.read = hits, pages, []

    def search(self, query, lang="en", per_page=20):
        return list(self.hits)

    def details(self, hit):
        self.read.append(hit.id)
        Pexels.read(hit, self.pages[hit.id])


def test_only_the_videos_that_pass_the_list_are_read_and_the_first_one_that_fits_is_taken():
    castle = pexels_hit(1, "castle-on-a-hill")
    library = pexels_hit(2, "student-in-a-library")
    campus = pexels_hit(3, "students-on-a-campus")
    page = {2: {**PEXELS_PAGE, "keywords": "Library, Students, Budapest"}, 3: PEXELS_PAGE}
    site = Site([castle, library, campus], page)
    rung = stock.Rung(("student", "students"), (("students budapest", "en"),), True)
    refused = []
    got, _ = stock.find(site, [rung], 6.4, 4.0, set(), PLACE, OTHERS, refused)
    assert got.hit.id == 2 and site.read == [2]                                                  # the castle is not about students: its page is not read; the first that fits ends the search
    assert any("pexels 1" in r and "is not about the subject" in r for r in refused)


def test_a_page_that_says_another_culture_or_a_portrait_or_a_short_video_or_no_place_is_refused_after_it_is_read():
    cases = {2: {**PEXELS_PAGE, "keywords": "Filipino Students, Students"},
             3: {**PEXELS_PAGE, "text": PEXELS_PAGE["text"].replace("3840x2160", "1080x1920")},
             4: {**PEXELS_PAGE, "text": PEXELS_PAGE["text"].replace("0:10", "0:02")},
             5: {**PEXELS_PAGE, "keywords": "Students", "description": "Students on a campus."}}
    site = Site([pexels_hit(i, "students-outside") for i in cases], cases)
    rung = stock.Rung(("student", "students"), (("students budapest", "en"),), True)
    refused = []
    got, _ = stock.find(site, [rung], 6.4, 4.0, set(), PLACE, OTHERS, refused)
    assert got is None and site.read == [2, 3, 4, 5]
    why = " | ".join(refused)
    assert "another region or culture" in why and "is not landscape" in why and "lasts 2 s" in why and "carries no term of the place" in why


def test_a_video_taken_or_shown_before_is_not_read_at_all():
    site = Site([pexels_hit(7, "students-outside")], {7: PEXELS_PAGE})
    rung = stock.Rung(("student", "students"), (("students budapest", "en"),), False)
    assert stock.find(site, [rung], 6.4, 4.0, {"pexels:7"}, PLACE, OTHERS)[0] is None and site.read == []


def test_the_registry_keeps_the_site_with_the_id_and_reads_a_number_written_before_as_a_pixabay_video(tmp_path):
    used = stock.Used(tmp_path / "u.json")
    used.add("pexels:5940065", "ed1", "p")
    used.add(77, "ed1", "p")
    assert used.ids() == {"pexels:5940065", "pixabay:77"} and used.recent() == {"pexels:5940065", "pixabay:77"}
