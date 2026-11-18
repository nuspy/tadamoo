"""Refreshing an archive from the web: an address with ETag and robots.txt, a MediaWiki site (revisions, redirects, deleted pages, a category), and the company wiki."""
import pytest

from sources_support import FakeWiki, Site, WikiSite, body, doc_of, html_page, make
from tadamoo.rag import RagError
from tadamoo.rag.sources.net import Http
from tadamoo.rag.sources.model import SourceError


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


@pytest.fixture
def wiki_site():
    s = WikiSite()
    yield s
    s.close()


def states(report):
    return {i["name"]: i["state"] for i in report["items"]}


# -- an address ----------------------------------------------------------------------------------

def test_an_address_is_read_once_and_then_costs_a_304(tmp_path, site):
    site.pages["/page.html"] = html_page("Wind notes")
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/page.html"})
    assert sync.update(aid)["counts"] == {"added": 1}
    doc = doc_of(store, aid, "Wind notes")
    assert doc["origin"]["kind"] == "url" and doc["origin"]["ref"].endswith("/page.html") and doc["updated"] > 0
    site.hits.clear()
    assert sync.check(aid)["counts"] == {"unchanged": 1}
    got = [h for h in site.hits if h[0] == "/page.html"]
    assert len(got) == 1 and got[0][1]                                    # a conditional request, with the ETag it was given
    assert got[0][2].startswith("TaDaMoo-RAG-refresh/") and "test@example.org" in got[0][2]
    site.pages["/page.html"] = html_page("Wind notes", 5, "gamma")
    report = sync.check(aid)
    assert report["counts"] == {"changed": 1}
    done = sync.update(aid)
    assert done["counts"] == {"updated": 1}
    assert any("gamma" in c["text"] for c in store.load(aid).chunks)
    assert doc_of(store, aid, "Wind notes")["revision"] == 2


def test_an_address_without_validators_is_settled_by_the_hash(tmp_path, site):
    site.etags = False
    site.pages["/page.html"] = html_page("Plain")
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/page.html"})
    sync.update(aid)
    assert sync.check(aid)["counts"] == {"unchanged": 1}                  # downloaded again, the same bytes: nothing changed
    site.pages["/page.html"] += b"<p>One more paragraph about the boat and the wind and the delta of the river.</p>"
    assert sync.check(aid)["counts"] == {"changed": 1}


def test_robots_txt_is_respected_and_the_site_is_not_read(tmp_path, site):
    site.pages["/private/secret.html"] = html_page("Secret")
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/private/secret.html"})
    done = sync.update(aid)
    assert done["counts"] == {"error": 1} and "robots" in done["errors"][0]["error"]
    assert not any(h[0] == "/private/secret.html" for h in site.hits)
    sync.settings.write({"respect_robots": False})
    assert sync.update(aid)["counts"] == {"added": 1}


def test_a_redirect_is_followed_and_a_slow_down_waits_then_retries(tmp_path, site):
    site.pages["/page.html"] = html_page("Moved")
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/old"})
    assert sync.update(aid)["counts"] == {"added": 1}
    site.throttle = 1
    naps = []
    sync.ctx.http.sleep = naps.append
    sync.add_source(aid, "url", {"url": site.url + "/slow"})
    assert sync.update(aid)["counts"] == {"added": 1, "unchanged": 1} and 1.0 in naps


def test_a_page_that_is_gone_or_a_server_that_is_down_is_not_a_removal(tmp_path, site):
    site.pages["/page.html"] = html_page("Here")
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/page.html"})
    sync.update(aid)
    del site.pages["/page.html"]
    report = sync.check(aid)
    assert report["counts"] == {"error": 1} and "404" in report["errors"][0]["error"]
    assert store.get(aid)["documents"] == 1
    site.close()
    assert sync.check(aid)["counts"] == {"error": 1}


def test_size_cap_and_format_are_refused_with_the_reason(tmp_path, site):
    site.pages["/big.html"] = b"<html><body>" + b"x" * (6 << 20) + b"</body></html>"
    site.pages["/pic.png"] = b"\x89PNG" + b"0" * 50
    site.types["/pic.png"] = "image/png"
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/big.html"})
    sync.add_source(aid, "url", {"url": site.url + "/pic.png"})
    errors = " | ".join(e["error"] for e in sync.check(aid)["errors"])
    assert "more than" in errors and "not a format" in errors


def test_an_address_with_a_password_is_refused(tmp_path):
    store, sync, aid = make(tmp_path)
    with pytest.raises(RagError, match="user name"):
        sync.add_source(aid, "url", {"url": "http://user:pw@example.org/x"})
    with pytest.raises(RagError, match="http"):
        sync.add_source(aid, "url", {"url": "ftp://example.org/x"})


def test_the_reader_keeps_a_pause_between_requests_to_one_site():
    naps, clock = [], {"t": 0.0}
    http = Http(lambda: {"min_interval_s": 2.0, "max_download_mb": 1, "request_timeout_s": 5, "respect_robots": False}, clock=lambda: clock["t"], sleep=naps.append)
    http._last["example.org"] = 0.0
    http._pause("example.org")
    assert naps == [2.0]
    clock["t"] = 10.0
    http._pause("example.org")
    assert naps == [2.0]
    http._pause("127.0.0.1")                                               # this PC is not made to wait
    http._pause("127.0.0.1")
    assert naps == [2.0]


# -- MediaWiki -----------------------------------------------------------------------------------

def article(text):
    return f"<div><p>{text} " + "The river runs to the sea and the delta is wide. " * 8 + "</p><h2>History</h2><p>" + "Long ago the wind carried boats. " * 8 + "</p></div>"


def test_a_wiki_page_is_followed_by_revision_and_updated_when_it_changes(tmp_path, wiki_site):
    wiki_site.put("Delta", 100, article("The delta"))
    wiki_site.put("River", 200, article("The river"))
    store, sync, aid = make(tmp_path)
    src = sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta", "River"]})
    assert "Delta and 1 more" in src["label"]
    assert sync.update(aid)["counts"] == {"added": 2}
    assert doc_of(store, aid, "Delta")["origin"]["version"] == "revision 100"
    wiki_site.requests.clear()
    assert sync.check(aid)["counts"] == {"unchanged": 2}
    assert all(r["action"] == "query" for r in wiki_site.requests) and len(wiki_site.requests) == 1       # one batched question, no page read
    wiki_site.put("Delta", 101, article("The delta, revised"), "2026-03-01T00:00:00Z")
    report = sync.check(aid)
    assert states(report) == {"Delta.html": "changed", "River.html": "unchanged"}
    assert "revision 100" in report["items"][0]["detail"] and "revision 101" in report["items"][0]["detail"]
    done = sync.update(aid)
    assert done["counts"] == {"updated": 1, "unchanged": 1}
    assert any("revised" in c["text"] for c in store.load(aid).chunks)
    assert doc_of(store, aid, "Delta")["origin"]["version"] == "revision 101"


def test_a_new_revision_with_the_same_text_is_not_an_update(tmp_path, wiki_site):
    wiki_site.put("Delta", 100, article("The delta"))
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta"]})
    sync.update(aid)
    wiki_site.pages["Delta"]["revid"] = 105                                  # a null edit or a revert
    assert sync.check(aid)["counts"] == {"changed": 1}
    done = sync.update(aid)
    assert done["counts"] == {"unchanged": 1} and "same" in done["items"][0]["detail"]
    assert sync.check(aid)["counts"] == {"unchanged": 1}


def test_redirects_deleted_pages_and_the_page_address(tmp_path, wiki_site):
    wiki_site.put("Delta (geography)", 300, article("The delta"))
    wiki_site.pages["Delta"] = {"redirect": "Delta (geography)"}
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "mediawiki", {"url": wiki_site.api.replace("/w/api.php", "/wiki/Delta")})
    done = sync.update(aid)
    assert done["counts"] == {"added": 1} and doc_of(store, aid, "Delta (geography)")
    wiki_site.pages["Delta"] = {"redirect": "Delta (river)"}
    wiki_site.put("Delta (river)", 400, article("The other delta"))
    assert sync.check(aid)["counts"] == {"changed": 1}                       # the redirect's target moved
    sync.update(aid)
    del wiki_site.pages["Delta (river)"]
    gone = sync.check(aid)
    assert gone["counts"] == {"gone": 1} and store.get(aid)["documents"] == 1
    sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Nothing here"]})
    assert "does not exist" in next(i for i in sync.check(aid)["items"] if i["key"] == "Nothing here")["detail"]


def test_a_category_brings_in_its_pages_and_the_new_ones_that_join(tmp_path, wiki_site):
    wiki_site.put("Alpha", 1, article("Alpha"))
    wiki_site.put("Beta", 2, article("Beta"))
    wiki_site.category["Category:Greek"] = ["Alpha", "Beta"]
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "category": "Greek"})
    assert sync.update(aid)["counts"] == {"added": 2}
    wiki_site.put("Gamma", 3, article("Gamma"))
    wiki_site.category["Category:Greek"].append("Gamma")
    report = sync.check(aid)
    assert report["counts"] == {"new": 1, "unchanged": 2}
    assert sync.update(aid, include_new=False)["counts"] == {"unchanged": 2, "new": 1}
    assert sync.update(aid)["counts"] == {"added": 1, "unchanged": 2}


def test_the_wiki_lag_is_waited_for_and_a_wrong_address_is_told(tmp_path, wiki_site):
    wiki_site.put("Delta", 100, article("The delta"))
    wiki_site.lag = 1
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta"]})
    assert sync.update(aid)["counts"] == {"added": 1}
    with pytest.raises(RagError, match="api.php"):
        sync.add_source(aid, "mediawiki", {"api": "http://example.org/nothing", "titles": ["X"]})
    with pytest.raises(RagError, match="at least one page"):
        sync.add_source(aid, "mediawiki", {"api": wiki_site.api})


def test_the_depth_and_limit_of_a_wiki_source_are_kept_and_checked_when_it_is_made(tmp_path, wiki_site):
    wiki_site.put("Delta", 100, article("The delta"))
    store, sync, aid = make(tmp_path)
    with pytest.raises(RagError, match="depth"):
        sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta"], "depth": 11})
    with pytest.raises(RagError, match="limit"):
        sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta"], "depth": 1, "limit": 0})
    src = sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta"], "depth": 2, "limit": 5})
    assert src["config"]["depth"] == 2 and src["config"]["limit"] == 5 and "depth 2" in src["label"]
    without = sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta"]})
    assert "depth" not in without["config"] and "limit" not in without["config"]


def test_a_wiki_source_follows_its_own_links_as_deep_as_asked_within_the_article_namespace(tmp_path, wiki_site):
    wiki_site.put("Delta", 100, article("The delta"))
    wiki_site.put("River", 200, article("The river"))
    wiki_site.put("Sea", 300, article("The sea"))
    wiki_site.put("Boat club", 400, article("A club"))                    # 2 links deep from Delta: not followed at depth 1
    wiki_site.link("Delta", "River", "Talk:Delta")                        # Talk: is not the article namespace: not followed
    wiki_site.link("River", "Sea", "Delta", "Boat club")                  # Delta already seen; Boat club is 2 deep
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta"], "depth": 1})
    done = sync.update(aid)
    assert done["counts"] == {"added": 2}
    assert {d["name"] for d in store.get(aid)["document_list"]} == {"Delta.html", "River.html"}
    assert not any(r.get("prop") == "links" and r.get("titles") == "Boat club" for r in wiki_site.requests)


def test_the_limit_stops_a_wiki_crawl_and_a_page_deeper_in_that_is_gone_does_not_stop_the_others(tmp_path, wiki_site):
    wiki_site.put("Delta", 100, article("The delta"))
    wiki_site.put("River", 200, article("The river"))
    wiki_site.put("Sea", 300, article("The sea"))
    wiki_site.link("Delta", "River", "Sea")
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta"], "depth": 1, "limit": 2})
    assert sync.update(aid)["counts"] == {"added": 2}

    wiki_site.link("Delta", "River", "Ghost")                            # Ghost does not exist: the crawl still brings in River
    sync.add_source(aid, "mediawiki", {"api": wiki_site.api, "titles": ["Delta"], "depth": 1})
    report = sync.check(aid)
    assert {i["key"] for i in report["items"]} == {"Delta", "River", "Ghost"}
    assert next(i for i in report["items"] if i["key"] == "Ghost")["state"] == "error"


# -- the company wiki ----------------------------------------------------------------------------

def company():
    w = FakeWiki()
    w.put("sources/handbook.md", "Handbook", "## Leave\n\n" + "Employees take leave in agreement with the wind. " * 12, "2026-05-01T10:00:00+00:00", content_version=1)
    w.put("sources/vpn.md", "VPN guide", "# VPN\n\n" + "Connect to the gateway and the boat sails. " * 12, "2026-05-02T10:00:00+00:00", content_version=3)
    w.put("entities/diana.md", "Diana", "Diana is the head of the delta office. " * 10, "2026-05-03T10:00:00+00:00")
    return w


def test_the_company_wiki_is_checked_by_its_tree_and_only_changed_pages_are_read(tmp_path):
    w = company()
    store, sync, aid = make(tmp_path, wiki=w)
    sync.add_source(aid, "wikibridge", {"project": "acme"})
    assert sync.update(aid)["counts"] == {"added": 3}
    assert doc_of(store, aid, "VPN")["origin"]["version"] == "version 3"
    w.calls.clear()
    assert sync.check(aid)["counts"] == {"unchanged": 3}
    assert w.calls == ["tree"]                                              # one call; no page was read
    w.put("sources/handbook.md", "Handbook", "## Leave\n\nLeave is now agreed with the manager, not with the wind. " * 12, "2026-06-01T10:00:00+00:00", content_version=2)
    report = sync.check(aid)
    assert states(report)["Handbook.md"] == "changed" and "2026-05-01" in report["items"][0]["detail"] or "version" in report["items"][0]["detail"]
    w.calls.clear()
    done = sync.update(aid)
    assert done["counts"] == {"updated": 1, "unchanged": 2} and w.calls == ["tree", "page sources/handbook.md"]
    assert any("manager" in c["text"] for c in store.load(aid).chunks)
    assert doc_of(store, aid, "Handbook")["origin"]["version"] == "version 2"


def test_a_page_that_disappears_from_the_tree_is_gone_not_deleted_and_a_new_one_is_added(tmp_path):
    w = company()
    store, sync, aid = make(tmp_path, wiki=w)
    sync.add_source(aid, "wikibridge", {"project": "acme", "sections": ["sources"]})
    assert sync.update(aid)["counts"] == {"added": 2}                       # only the chosen section
    w.hidden.add("sources/vpn.md")                                          # deleted, renamed or no longer visible: the wiki answers the same
    w.put("sources/new.md", "New page", "A new page about the boat. " * 20, "2026-06-02T10:00:00+00:00")
    report = sync.check(aid)
    assert report["counts"] == {"gone": 1, "new": 1, "unchanged": 1}
    assert "not visible" in next(i for i in report["items"] if i["state"] == "gone")["detail"]
    done = sync.update(aid)
    assert done["counts"] == {"added": 1, "gone": 1, "unchanged": 1} and store.get(aid)["documents"] == 3


def test_a_wiki_that_does_not_answer_or_lists_nothing_is_not_everything_gone(tmp_path):
    w = company()
    store, sync, aid = make(tmp_path, wiki=w)
    sync.add_source(aid, "wikibridge", {"project": "acme"})
    sync.update(aid)
    w.down = True
    report = sync.check(aid)
    assert report["counts"] == {"error": 3} and "offline" in report["errors"][0]["error"]
    w.down = False
    w.hidden = set(w.pages)                                                 # the key sees nothing now
    report = sync.check(aid)
    assert report["counts"] == {"error": 3} and "not taken as a change" in report["errors"][0]["error"]
    assert store.get(aid)["documents"] == 3


def test_a_page_without_a_time_is_read_to_know(tmp_path):
    w = company()
    w.pages["sources/vpn.md"]["updated"] = ""
    store, sync, aid = make(tmp_path, wiki=w)
    sync.add_source(aid, "wikibridge", {"project": "acme"})
    sync.update(aid)
    w.calls.clear()
    assert sync.check(aid)["counts"] == {"changed": 1, "unchanged": 2}      # unknown cannot be called unchanged
    assert sync.update(aid)["counts"] == {"unchanged": 3}                   # but the text is the same, so nothing is replaced


def test_the_wiki_project_name_is_checked(tmp_path):
    store, sync, aid = make(tmp_path, wiki=company())
    with pytest.raises(RagError, match="project"):
        sync.add_source(aid, "wikibridge", {"project": "../x"})
    with pytest.raises(RagError, match="sections"):
        sync.add_source(aid, "wikibridge", {"project": "acme", "sections": ["secrets"]})
    with pytest.raises(RagError, match="address"):
        sync.add_source(aid, "wikibridge", {"project": "acme", "address": "http://u:p@h/"})


# -- a site followed link by link -----------------------------------------------------------------

def linked(title, *links):
    """A page that links to `links` (addresses or paths)."""
    anchors = "".join(f'<a href="{l}">{l}</a> ' for l in links)
    return html_page(title).replace(b"</body>", f"<p>{anchors}</p></body>".encode())


def test_the_depth_and_the_limit_are_kept_on_the_source_and_checked_when_it_is_made(tmp_path, site):
    store, sync, aid = make(tmp_path)
    src = sync.add_source(aid, "url", {"url": site.url + "/a.html", "depth": 2, "limit": 5})
    assert src["config"] == {"url": site.url + "/a.html", "depth": 2, "same_site": True, "limit": 5} and "(depth 2)" in src["label"]
    flat = sync.add_source(aid, "url", {"url": site.url + "/b.html"})
    assert flat["config"] == {"url": site.url + "/b.html"} and "depth" not in flat["label"]                   # as before: the address only
    for bad in ({"depth": 11}, {"depth": -1}, {"depth": "2"}, {"depth": True}, {"depth": 1, "limit": 0}, {"depth": 1, "limit": "ten"}):
        with pytest.raises(RagError):
            sync.add_source(aid, "url", {"url": site.url + "/c.html", **bad})


def test_a_site_is_followed_as_deep_as_asked_on_its_own_host_and_each_page_is_a_document(tmp_path, site):
    site.pages["/root.html"] = linked("Root", "/one.html", "/two.html", "http://elsewhere.example/other.html", "mailto:x@y.z", "/one.html#part")
    site.pages["/one.html"] = linked("One", "/deep.html", "/root.html")
    site.pages["/two.html"] = html_page("Two")
    site.pages["/deep.html"] = linked("Deep", "/deeper.html")
    site.pages["/deeper.html"] = html_page("Deeper")
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/root.html", "depth": 1})
    sync.update(aid)
    assert sorted(d["name"] for d in store.get(aid)["document_list"]) == ["One.html", "Root.html", "Two.html"]              # the page, and the pages it links to; not the other host, not deeper
    store2, sync2, aid2 = make(tmp_path / "two")
    sync2.add_source(aid2, "url", {"url": site.url + "/root.html", "depth": 3})
    sync2.update(aid2)
    assert sorted(d["name"] for d in store2.get(aid2)["document_list"]) == ["Deep.html", "Deeper.html", "One.html", "Root.html", "Two.html"]
    assert not any("elsewhere" in str(h) for h in site.hits)


def test_the_limit_stops_the_reading_and_an_unchanged_page_is_still_followed_from_its_kept_links(tmp_path, site):
    site.pages["/root.html"] = linked("Root", "/one.html", "/two.html")
    site.pages["/one.html"] = linked("One", "/deep.html")
    site.pages["/two.html"] = html_page("Two")
    site.pages["/deep.html"] = html_page("Deep")
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/root.html", "depth": 2, "limit": 2})
    sync.update(aid)
    assert store.get(aid)["documents"] == 2
    store2, sync2, aid2 = make(tmp_path / "two")
    sync2.add_source(aid2, "url", {"url": site.url + "/root.html", "depth": 2})
    sync2.update(aid2)
    assert store2.get(aid2)["documents"] == 4
    site.hits.clear()
    site.pages["/deep.html"] = html_page("Deep changed", word="river")
    report = sync2.check(aid2)
    assert [v for k, v in states(report).items() if k.startswith("Deep")] == ["changed"] and report["counts"].get("unchanged") == 3                                  # the 304 pages were still followed down to the one that changed
    assert any(h[0] == "/deep.html" for h in site.hits) and all(h[1] for h in site.hits if h[0] in ("/root.html", "/one.html", "/two.html"))


def test_a_page_deeper_in_that_goes_away_is_gone_and_one_that_never_answered_does_not_stop_the_others(tmp_path, site):
    site.pages["/root.html"] = linked("Root", "/one.html", "/missing.html")
    site.pages["/one.html"] = html_page("One")
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/root.html", "depth": 1})
    sync.update(aid)
    assert sorted(d["name"] for d in store.get(aid)["document_list"]) == ["One.html", "Root.html"]                              # /missing.html gave 404: no document, no error for the rest
    del site.pages["/one.html"]
    assert states(sync.check(aid))["One.html"] == "gone"

