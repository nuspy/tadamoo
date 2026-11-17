"""Adding from a request in words: the search, the verdicts of the technical session in groups, the links of the pages found, and the import of what was picked."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve.rag_api import RagApi                      # noqa: E402
from tadamoo.rag import RagError, RagStore, discover  # noqa: E402
from sources_support import Site, html_page            # noqa: E402
from tadamoo.rag import project_key                    # noqa: E402

PROJ = project_key("discover-project")


class Session:
    """A technical session that keeps the candidates whose title contains `keep`, and records how it was asked."""

    def __init__(self, keep="wind", window=60000, fail_on=None):
        self.keep, self.window, self.fail_on, self.calls = keep, window, fail_on, []

    def side_context(self):
        return self.window

    def chat_completion(self, messages, **kw):
        body = json.loads(messages[-1]["content"])
        self.calls.append(body)
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("the session is busy")
        rows = [{"id": c["id"], "keep": self.keep in c["title"].lower(), "why": "matches" if self.keep in c["title"].lower() else "other topic"} for c in body["candidates"]]
        return {"choices": [{"message": {"content": json.dumps({"verdicts": rows})}}]}


def results(n, word="wind"):
    return [{"title": f"{word} page {i}", "url": f"https://example.org/{word}/{i}", "snippet": f"about {word} {i}"} for i in range(n)]


def test_results_are_judged_and_numbered_and_what_is_not_a_web_address_or_repeated_is_dropped():
    found = results(3) + [{"title": "dup", "url": "https://example.org/wind/0"}, {"title": "mail", "url": "mailto:a@b.c"}] + [{"title": "shop", "url": "https://shop.example/x", "snippet": ""}]
    items = discover.search(lambda q, n: found, Session(), "wind energy", 10)
    assert [i["id"] for i in items] == [1, 2, 3, 4]
    assert [i["relevant"] for i in items] == [True, True, True, False]
    assert items[0]["reason"] == "matches" and items[3]["reason"] == "other topic"


def test_without_a_session_every_result_is_offered_unjudged_and_a_blank_request_is_refused():
    items = discover.search(lambda q, n: results(2), None, "wind", 5)
    assert [i["relevant"] for i in items] == [None, None]
    with pytest.raises(RagError):
        discover.search(lambda q, n: results(2), None, "   ", 5)


def test_the_session_reads_in_groups_that_fit_the_token_budget_and_a_failed_group_stays_unjudged(monkeypatch):
    big = [{"title": "wind " + "x" * 200, "url": f"https://example.org/{i}", "snippet": "y" * 200} for i in range(6)]
    monkeypatch.setattr(discover.side_uses, "param", lambda use, key: 300 if key == "group_tokens" else 5)
    session = Session(fail_on=2)
    items = discover.search(lambda q, n: big, session, "wind", 10)
    assert len(session.calls) >= 3 and all(len(c["candidates"]) <= 3 for c in session.calls)               # no call carries the whole list
    assert [c["id"] for c in session.calls[0]["candidates"]] == [1, 2, 3][:len(session.calls[0]["candidates"])]
    failed = session.calls[1]["candidates"]
    unjudged = [i for i in items if i["relevant"] is None]
    assert len(unjudged) == len(failed) and all(i["relevant"] is True for i in items if i["relevant"] is not None)


def test_the_answer_is_read_even_when_it_is_partial_or_odd():
    assert discover.parse('{"verdicts": [{"id": 1, "keep": true, "why": "ok"}, {"id": 9, "keep": true}, {"id": 2, "keep": "yes"}]}', 3) == {1: {"keep": True, "why": "ok"}}
    assert discover.parse("not json", 3) == {} and discover.parse("[]", 3) == {}


def test_the_links_of_the_pages_found_are_followed_and_judged_by_their_text_without_repeats():
    pages = {"https://example.org/wind/0": b'<a href="/wind/guide">Wind guide</a> <a href="/about">About us</a> <a href="/wind/0">self</a> <a href="mailto:x@y">mail</a> <a href="/wind/guide#top">again</a>',
             "https://example.org/wind/1": b'<a href="https://other.example/wind/blades">Wind blades</a> <a href="/wind/guide">Wind guide</a>'}
    items = discover.search(lambda q, n: results(2), Session(), "wind", 10)
    new = discover.extend(lambda url: pages.get(url), Session(), "wind", items)
    assert [(i["title"], i["url"]) for i in new] == [("Wind guide", "https://example.org/wind/guide"), ("Wind blades", "https://other.example/wind/blades")]
    assert [i["id"] for i in new] == [3, 4] and new[0]["via"] == "wind page 0" and all(i["relevant"] for i in new)
    assert discover.extend(lambda url: None, Session(), "wind", items) == []                      # pages that cannot be read give nothing


def test_the_extension_starts_from_the_pages_the_session_kept(monkeypatch):
    items = [{"title": "wind a", "url": "https://e.org/a", "snippet": "", "via": "", "relevant": True, "reason": "", "id": 1},
             {"title": "shop", "url": "https://e.org/shop", "snippet": "", "via": "", "relevant": False, "reason": "", "id": 2}]
    asked = []
    discover.extend(lambda url: asked.append(url) or b"", None, "wind", items)
    assert asked == ["https://e.org/a"]


# -- over the API, on a local site -------------------------------------------------------------------

@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


def make_api(tmp_path, session, found):
    api = RagApi(RagStore(tmp_path / "rag", embedder=lambda: None), lambda: [], describer=lambda: session)
    api.discovering.find = lambda: (lambda q, n: found)
    return api


def wait(api, out):
    done = api.jobs.wait(out["job"]["id"])
    assert done["state"] == "done", done
    return done["result"]


def test_a_search_is_a_job_whose_result_is_the_judged_list_and_a_bad_request_is_refused(tmp_path):
    api = make_api(tmp_path, Session(), results(3))
    aid = api.create({"name": "Wind", "project": PROJ})["archive"]["id"]
    found = wait(api, api.route_post(["archives", aid, "discover"], {}, json.dumps({"query": "wind energy", "limit": 5}).encode()))
    assert found["query"] == "wind energy" and [i["relevant"] for i in found["items"]] == [True, True, True]
    for bad in ({}, {"query": "  "}, {"query": "x", "limit": 0}, {"query": "x", "limit": "5"}, {"query": "x", "limit": 99}):
        with pytest.raises(RagError):
            api.route_post(["archives", aid, "discover"], {}, json.dumps(bad).encode())


def test_the_picked_results_become_web_sources_and_the_archive_is_read(tmp_path, site):
    site.pages["/a.html"] = html_page("Wind turbines", word="wind")
    site.pages["/b.html"] = html_page("Solar panels", word="solar")
    api = make_api(tmp_path, Session(), [])
    aid = api.create({"name": "Energy", "project": PROJ})["archive"]["id"]
    out = api.route_post(["archives", aid, "discover", "import"], {}, json.dumps({"urls": [site.url + "/a.html", site.url + "/b.html", site.url + "/a.html"], "depth": 0}).encode())
    assert out["added"] == 2 and out["skipped"] == []
    wait(api, out)
    assert sorted(d["name"] for d in api.store.get(aid)["document_list"]) == ["Solar panels.html", "Wind turbines.html"]
    again = api.route_post(["archives", aid, "discover", "import"], {}, json.dumps({"urls": [site.url + "/a.html"]}).encode())
    assert again["added"] == 0 and "already" in again["skipped"][0]["why"] and "job" not in again                   # nothing new: nothing to read
    for bad in ({}, {"urls": []}, {"urls": [3]}, {"urls": ["x"] * 201}):
        with pytest.raises(RagError):
            api.route_post(["archives", aid, "discover", "import"], {}, json.dumps(bad).encode())


def test_widening_reads_the_pages_found_and_offers_only_what_is_new(tmp_path, site):
    site.pages["/start.html"] = html_page("Start").replace(b"</body>", b'<a href="/wind-guide.html">Wind guide</a> <a href="/login.html">Log in</a></body>')
    api = make_api(tmp_path, Session(), [])
    aid = api.create({"name": "Wind", "project": PROJ})["archive"]["id"]
    first = [{"id": 1, "title": "wind start", "url": site.url + "/start.html", "snippet": "", "via": "", "relevant": True, "reason": ""}]
    got = wait(api, api.route_post(["archives", aid, "discover", "more"], {}, json.dumps({"query": "wind", "items": first}).encode()))
    assert [i["title"] for i in got["items"]] == ["Wind guide"] and got["items"][0]["id"] == 2
    with pytest.raises(RagError):
        api.route_post(["archives", aid, "discover", "more"], {}, json.dumps({"query": "wind", "items": [{"url": "ftp://x"}]}).encode())
