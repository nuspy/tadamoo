"""The person's own search engines and page directories: kept, validated, and searched with the generic link reader."""
import pytest

from tadamoo import websearch_custom as wc


def page(*links):
    body = "<html><body>" + "".join(f'<a href="{h}">{t}</a>' for h, t in links) + "<script>var x='<a href=\"http://z.example/\">no</a>'</script></body></html>"
    return lambda url: (200, "text/html", body.encode())


def test_an_entry_needs_a_name_and_an_address_with_the_query_placeholder(tmp_path):
    store = wc.Store(tmp_path / "custom.json")
    assert store.list("engines") == [] and store.list("directories") == []
    for bad in (("", "https://e.example/?q={query}"), ("x", "https://e.example/?q=fixed"), ("x", "ftp://e.example/?q={query}"), ("x", "https://u:p@e.example/?q={query}")):
        with pytest.raises(wc.CustomSearchError):
            store.add("engines", *bad)
    with pytest.raises(wc.CustomSearchError):
        store.add("other", "x", "https://e.example/?q={query}")


def test_added_entries_are_listed_can_be_switched_off_and_removed(tmp_path):
    store = wc.Store(tmp_path / "custom.json")
    got = store.add("engines", "MyEngine", "https://e.example/search?q={query}")
    assert got == [wc.Source("MyEngine", "https://e.example/search?q={query}", True)]
    got = store.set_enabled("engines", "myengine", False)                 # case does not matter
    assert got[0].enabled is False
    with pytest.raises(wc.CustomSearchError):
        store.set_enabled("engines", "nope", True)
    assert wc.Store(tmp_path / "custom.json").list("engines")[0].enabled is False     # kept on disk
    store.remove("engines", "MYENGINE")
    assert store.list("engines") == []
    with pytest.raises(wc.CustomSearchError):
        store.remove("engines", "MyEngine")
    assert store.list_all() == {"engines": [], "directories": []}


def test_the_two_lists_are_kept_apart_and_adding_the_same_name_again_replaces_it(tmp_path):
    store = wc.Store(tmp_path / "custom.json")
    store.add("engines", "X", "https://e.example/?q={query}")
    store.add("directories", "X", "https://d.example/?q={query}")
    assert len(store.list("engines")) == 1 and len(store.list("directories")) == 1
    store.add("engines", "x", "https://new.example/?q={query}")                       # same name (any case): replaces, not a duplicate
    engines = store.list("engines")
    assert len(engines) == 1 and engines[0].url == "https://new.example/?q={query}"


def test_a_search_keeps_the_links_with_their_words_and_skips_script_text_and_disabled_entries():
    on = wc.Source("On", "https://e.example/s?q={query}", True)
    off = wc.Source("Off", "https://off.example/s?q={query}", False)
    fetch = page(("/r/1", "First  result"), ("/r/1", "again"), ("https://e.example/s?q=wind+power", "itself"))
    got = wc.search_many([on, off], "wind power", fetch)
    assert len(got) == 1 and got[0]["name"] == "On" and "wind+power" in got[0]["url"]
    assert got[0]["hits"] == [{"title": "First result", "url": "https://e.example/r/1"}]        # its own search page, reached again, is not a hit


def test_a_source_that_fails_is_reported_and_does_not_stop_the_others():
    good = wc.Source("G", "https://g.example/s?q={query}", True)
    bad = wc.Source("B", "https://b.example/s?q={query}", True)

    def fetch_for(url):
        if "b.example" in url:
            raise OSError("no route")
        return 200, "text/html", b'<a href="http://x/">x</a>'

    got = wc.search_many([bad, good], "x", fetch_for)
    assert got[0]["error"] and "no route" in got[0]["error"] and got[1]["hits"]


def test_a_bad_status_is_reported_with_no_hits():
    got = wc.search_one(wc.Source("G", "https://g.example/s?q={query}", True), "x", lambda u: (503, "", b""))
    assert got["hits"] == [] and got["error"] == "HTTP 503"
