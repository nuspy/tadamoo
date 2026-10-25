"""DuckDuckGo is the search engine when the person set no other: results parsed from its page, no key, the address checked like any page's."""
import http.server
import sys
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.tools import create_standard_tool_registry
from harness.tools import search
from harness.tools.search import ProviderUnavailable, clear_backends, duckduckgo_search

PAGE = """<html><body>
<div class="result results_links results_links_deep web-result result--ad"><h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/y.js?ad_domain=shop.example">An advert</a></h2></div>
<div class="result results_links"><h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=%s&amp;rut=abc">Kyiv <b>news</b> today</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">The latest from the <b>front</b> and the talks.</a></div>
<div class="result results_links"><h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=%s">Second   page</a></h2>
<a class="result__snippet" href="x">Second snippet.</a></div>
</body></html>""" % (quote("https://news.example/a?x=1&y=2", safe=""), quote("https://other.example/b", safe=""))

mode = {"v": "ok"}


class Fake(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Fake.seen.append((self.path, self.headers.get("User-Agent")))
        body = b"<html>Unfortunately, bots use DuckDuckGo too. anomaly-modal</html>" if mode["v"] == "check" else PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def ddg():
    mode["v"] = "ok"
    Fake.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/html/"
    server.shutdown()


def test_the_results_are_the_titles_addresses_and_snippets_without_the_adverts(ddg):
    hits = duckduckgo_search("Ukraine 2026", 5, base=ddg, allow_private=True)
    assert [h["url"] for h in hits] == ["https://news.example/a?x=1&y=2", "https://other.example/b"]
    assert hits[0]["title"] == "Kyiv news today" and hits[0]["snippet"] == "The latest from the front and the talks."
    assert hits[1]["title"] == "Second page"
    path, agent = Fake.seen[0]
    assert path == "/html/?q=Ukraine+2026" and "Basalt" in agent
    assert len(duckduckgo_search("x", 1, base=ddg, allow_private=True)) == 1


def test_a_page_that_asks_for_a_check_is_said_plainly_and_a_closed_door_too(ddg):
    mode["v"] = "check"
    with pytest.raises(ProviderUnavailable, match="asked for a check"):
        duckduckgo_search("x", 5, base=ddg, allow_private=True)
    with pytest.raises(ProviderUnavailable, match="could not be searched"):
        duckduckgo_search("x", 5, base="http://127.0.0.1:9/html/", allow_private=True)


def test_this_pcs_own_address_is_refused_as_for_any_page(ddg):
    with pytest.raises(ProviderUnavailable):
        duckduckgo_search("x", 5, base=ddg, allow_private=False)


def test_search_web_is_offered_with_duckduckgo_as_the_default_and_can_be_turned_off(tmp_path, monkeypatch):
    clear_backends()
    for name in ("TAVILY_API_KEY", "EXA_API_KEY", "BASALT_WEB_SEARCH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(search, "load_providers", lambda: [])
    registry, _ = create_standard_tool_registry(tmp_path)
    tool = registry.get("search_web")
    assert tool is not None and tool.parameters["properties"]["provider"]["enum"] == ["duckduckgo"]
    assert tool.parameters["properties"]["provider"]["default"] == "duckduckgo"
    monkeypatch.setenv("BASALT_WEB_SEARCH", "off")
    registry, _ = create_standard_tool_registry(tmp_path)
    assert registry.get("search_web") is None
