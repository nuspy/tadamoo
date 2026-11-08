"""Settings > Web search: the choice and its file, the keys, each provider's call against a local stand-in, and what the search tool does with the choice."""
import http.server
import json
import threading

import pytest

from tadamoo import websearch as ws
from tadamoo.llm.credentials import FileCredentials
from tadamoo.tools import create_standard_tool_registry
from tadamoo.tools import search


class Stand(http.server.BaseHTTPRequestHandler):
    """A stand-in for the services: remembers the last request and answers with what the test set."""
    seen = []
    answer = {}
    status = 200

    def log_message(self, *a):
        pass

    def _reply(self):
        body = json.dumps(Stand.answer).encode()
        self.send_response(Stand.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Stand.seen.append(("GET", self.path, dict(self.headers), None))
        self._reply()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Stand.seen.append(("POST", self.path, dict(self.headers), json.loads(raw or b"{}")))
        self._reply()


@pytest.fixture
def stand():
    Stand.seen, Stand.answer, Stand.status = [], {}, 200
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stand)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_CREDENTIALS", "file")
    monkeypatch.setattr(ws, "user_dir", lambda: tmp_path)
    return tmp_path


def test_the_choice_has_defaults_is_checked_and_survives_a_restart(tmp_path):
    prefs = ws.Prefs(tmp_path / "w.json")
    assert prefs.read() == {"search": "duckduckgo", "reader": "none", "mode": {}, "urls": {}, "engine": "duckduckgo"}
    prefs.write(search="searxng", reader="playwright", mode={"searxng": "local"}, urls={"searxng": "http://127.0.0.1:9000/"})
    again = ws.Prefs(tmp_path / "w.json").read()
    assert again["search"] == "searxng" and again["reader"] == "playwright" and again["urls"]["searxng"] == "http://127.0.0.1:9000" 
    for bad in ({"search": "bing"}, {"search": "playwright"}, {"reader": "brave"}, {"mode": {"brave": "local"}}, {"mode": {"searxng": "mars"}},
                {"urls": {"searxng": "ftp://x"}}, {"urls": {"searxng": "http://user:pw@host"}}, {"urls": {"nobody": "http://x"}}, {"engine": "yahoo"}):
        with pytest.raises(ValueError):
            prefs.write(**bad)
    (tmp_path / "w.json").write_text("not json", encoding="utf-8")
    assert ws.Prefs(tmp_path / "w.json").read()["search"] == "duckduckgo"           # a damaged file is the default, not a crash


def test_each_provider_says_where_to_make_the_account_and_what_runs_here():
    for pid, p in ws.PROVIDERS.items():
        assert p["kind"] in ("search", "reader", "both")
        assert p["cloud"] or p["local"], pid
        if p["cloud"] and p["cloud"].get("needs_key"):
            assert p["cloud"]["signup_url"].startswith("https://") and p["cloud"]["keys_url"].startswith("https://"), pid
    assert ws.PROVIDERS["searxng"]["local"] and ws.PROVIDERS["openwebsearch"]["cloud"] is None and ws.PROVIDERS["playwright"]["cloud"] is None and ws.PROVIDERS["brave"]["local"] is None
    assert ws.provider_ids("search") == ["duckduckgo", "searxng", "openwebsearch", "brave", "tavily", "exa", "firecrawl"]


def test_a_key_is_kept_in_the_credential_store_never_in_the_choice_file(tmp_path):
    keys = ws.Keys(FileCredentials(tmp_path / "c.json"))
    assert not keys.has("brave")
    keys.set("brave", "  BSA-abc123  ")
    assert keys.get("brave", environ={}) == "BSA-abc123" and keys.has("brave")
    assert "BSA-abc123" not in json.dumps(ws.Prefs(tmp_path / "w.json").write(search="brave"))
    assert keys.get("brave", environ={"BRAVE_API_KEY": "from-env"}) == "from-env"       # the environment wins, as it always did
    keys.set("brave", "")
    assert not keys.has("brave")
    for pid, key in (("duckduckgo", "x"), ("playwright", "x"), ("nobody", "x"), ("brave", "two words"), ("brave", "é" * 5), ("brave", "k" * 401)):
        with pytest.raises(ValueError):
            keys.set(pid, key)


def test_searxng_is_asked_for_json_and_a_page_without_it_says_what_to_change(stand):
    Stand.answer = {"results": [{"title": "A", "url": "https://a.example/", "content": "about a"}, {"title": "no link", "url": "javascript:x"}, {"title": "B", "url": "http://b.example/"}]}
    got = ws.searxng_search(stand, "dmt effects", 5)
    assert [r["url"] for r in got] == ["https://a.example/", "http://b.example/"] and got[0]["snippet"] == "about a"
    assert "q=dmt+effects" in Stand.seen[-1][1] and "format=json" in Stand.seen[-1][1]
    Stand.status = 403
    with pytest.raises(ws.SearchError, match="search.formats"):
        ws.searxng_search(stand, "x", 3)


def test_brave_sends_its_token_and_reads_the_web_results(stand, monkeypatch):
    monkeypatch.setitem(ws.PROVIDERS["brave"], "base", stand + "/res/v1/web/search")
    Stand.answer = {"web": {"results": [{"title": "T", "url": "https://t.example/", "description": "d"}]}}
    got = ws.brave_search("KEY1", "q", 3)
    assert got == [{"title": "T", "url": "https://t.example/", "snippet": "d"}]
    assert Stand.seen[-1][2]["X-Subscription-Token"] == "KEY1" and "count=3" in Stand.seen[-1][1]
    Stand.status = 429
    with pytest.raises(ws.SearchError, match="limit"):
        ws.brave_search("KEY1", "q", 3)
    Stand.status = 401
    with pytest.raises(ws.SearchError, match="key was refused"):
        ws.brave_search("KEY1", "q", 3)


def test_firecrawl_searches_and_reads_with_a_bearer_key_or_none_on_this_pc(stand):
    Stand.answer = {"data": [{"title": "F", "url": "https://f.example/", "description": "x"}]}
    assert ws.firecrawl_search(stand, "K", "q", 2)[0]["url"] == "https://f.example/"
    assert Stand.seen[-1][2]["Authorization"] == "Bearer K" and Stand.seen[-1][3] == {"query": "q", "limit": 2}
    Stand.answer = {"data": {"markdown": "# Page"}}
    assert ws.firecrawl_read(stand, "", "https://x.example/") == "# Page"
    assert "Authorization" not in Stand.seen[-1][2] and Stand.seen[-1][3]["url"] == "https://x.example/"
    Stand.answer = {"data": {}}
    with pytest.raises(ws.SearchError):
        ws.firecrawl_read(stand, "", "https://x.example/")


def test_the_search_tool_follows_the_choice_at_every_call(home, stand, monkeypatch):
    monkeypatch.setitem(ws.PROVIDERS["brave"], "base", stand + "/res/v1/web/search")
    Stand.answer = {"web": {"results": [{"title": "T", "url": "https://t.example/", "description": "d"}]}}
    registry, _ = create_standard_tool_registry(home)
    handler = registry.get("search_web").handler
    assert "brave" not in registry.get("search_web").description                          # no key and not chosen yet
    ws.Keys().set("brave", "K9")
    ws.Prefs().write(search="brave")
    got = handler("anything")                                                             # no provider named: the person's choice, read now
    assert got[0]["url"] == "https://t.example/" and got[0]["source"] == "brave" and got[0]["untrusted"] is True
    assert Stand.seen[-1][2]["X-Subscription-Token"] == "K9"
    Stand.status = 429
    with pytest.raises(search.ProviderUnavailable, match="brave"):
        handler("anything")                                                               # the provider's own reason reaches the model, no silent swap


def test_a_page_the_site_refuses_is_read_by_the_chosen_reader_and_the_answer_says_so(home, stand):
    Stand.answer = {"data": {"markdown": "# Read by the reader"}}
    ws.Keys().set("firecrawl", "FK")
    ws.Prefs().write(reader="firecrawl", mode={"firecrawl": "local"}, urls={"firecrawl": stand})
    out = ws.read_after_failure("https://example.org/", "the page answered 403 Forbidden")
    assert "# Read by the reader" in out and "Firecrawl" in out and "403" in out
    ws.Prefs().write(reader="none")
    assert ws.read_after_failure("https://example.org/", "x") is None
    assert ws.reader_may_help("the page answered 403 Forbidden") and ws.reader_may_help("the page took too long to arrive")
    assert not ws.reader_may_help("the page answered 404 Not Found. This address does not exist") and not ws.reader_may_help("only web addresses (http or https) can be read")


def test_a_chosen_provider_that_is_not_ready_is_reported_not_replaced_by_another(home):
    registry, _ = create_standard_tool_registry(home)
    handler = registry.get("search_web").handler
    ws.Prefs().write(search="brave")                                                      # chosen, but no key was ever set
    with pytest.raises(search.ProviderUnavailable, match="chosen in Settings"):
        handler("anything")
