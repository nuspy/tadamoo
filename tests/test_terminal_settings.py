"""Settings > General > Command line: what is kept in terminal.json, what is refused, the key that is never shown, and the page that is wired."""
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve.terminal_prefs import DEFAULTS, TerminalError, TerminalPrefs
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)


def test_the_defaults_are_the_section_sign_electric_blue_and_this_pc(tmp_path):
    view = TerminalPrefs(tmp_path / "terminal.json").view()
    assert (view["prefix"], view["color"], view["server"], view["mode"], view["has_key"]) == ("§", "#2E7BFF", "http://127.0.0.1:7232", "", False)
    assert view["modes"] == ["plan", "read_only", "default", "auto"] and view["defaults"] == DEFAULTS


def test_values_are_kept_and_the_key_is_written_and_never_given_back(tmp_path):
    prefs = TerminalPrefs(tmp_path / "terminal.json")
    out = prefs.write({"prefix": ">>", "color": "#00ff88", "server": "https://tadamoo.example.org/", "mode": "default", "key": "bsk_secret_value"})
    assert (out["prefix"], out["color"], out["server"], out["mode"]) == (">>", "#00FF88", "https://tadamoo.example.org", "default")
    assert "key" not in prefs.view() and prefs.view()["has_key"] is True and "bsk_secret_value" not in json.dumps(prefs.view())
    assert json.loads((tmp_path / "terminal.json").read_text(encoding="utf-8"))["key"] == "bsk_secret_value"           # the module reads it from the file
    prefs.write({"color": "#112233"})
    assert prefs.read()["prefix"] == ">>" and prefs.view()["has_key"] is True                                           # what is not given is kept
    prefs.write({"key": ""})
    assert prefs.view()["has_key"] is False


@pytest.mark.parametrize("changes,field", [({"prefix": ""}, "prefix"), ({"prefix": "abcd"}, "prefix"), ({"prefix": "a"}, "prefix"), ({"prefix": "1"}, "prefix"), ({"prefix": "> "}, "prefix"),
                                           ({"prefix": 7}, "prefix"), ({"color": "blue"}, "color"), ({"color": "#12345"}, "color"), ({"color": None}, "color"),
                                           ({"server": "ftp://x"}, "server"), ({"server": "http://u:p@host"}, "server"), ({"server": "http://h/api"}, "server"), ({"server": "host"}, "server"),
                                           ({"mode": "root"}, "mode"), ({"mode": 3}, "mode"), ({"key": "a b"}, "key"), ({"key": "x" * 201}, "key"), ({"key": 5}, "key")])
def test_a_wrong_value_is_refused_whole_with_the_field(tmp_path, changes, field):
    prefs = TerminalPrefs(tmp_path / "terminal.json")
    prefs.write({"prefix": "!!"})
    with pytest.raises(TerminalError) as e:
        prefs.write({"color": "#010203", **changes})
    assert e.value.field == field
    assert prefs.read()["prefix"] == "!!" and prefs.read()["color"] == DEFAULTS["color"]                                # nothing of a refused request is kept


def test_a_broken_file_reads_as_the_defaults(tmp_path):
    (tmp_path / "terminal.json").write_text("{not json", encoding="utf-8")
    assert TerminalPrefs(tmp_path / "terminal.json").read() == DEFAULTS
    (tmp_path / "terminal.json").write_text(json.dumps({"prefix": ["x"], "color": 3, "mode": ["auto"]}), encoding="utf-8")
    assert TerminalPrefs(tmp_path / "terminal.json").read() == DEFAULTS


def test_every_string_of_the_page_exists_in_english_and_italian_and_the_page_is_wired():
    web = Path(__file__).resolve().parents[1] / "serve" / "web"
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    source = (web / "terminal.js").read_text(encoding="utf-8")
    used = {k for k in re.findall(r'T\("(tm\.[a-z_.]+)"', source) if not k.endswith(".")} | {"tm.title"} | {"tm.mode." + m for m in ("inherit", "plan", "read_only", "default", "auto")}
    assert len(used) > 15
    for key in used:
        assert i18n.count(f'"{key}":') == 2, key                                                                       # once in English and once in Italian
    html = (web / "index.html").read_text(encoding="utf-8")
    assert 'src="web/terminal.js"' in html and 'id="tm-root"' in html and 'data-sub="general" id="tm-section"' in html
    assert "innerHTML" not in source                                                                                    # what the server says is put in as text


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server([answer("x")])

    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
    return call


def test_the_page_reads_and_saves_through_the_api_and_a_refusal_names_the_field(api, tmp_path):
    code, view = api("GET", "/api/terminal")
    assert code == 200 and view["prefix"] == "§" and view["has_key"] is False
    code, view = api("POST", "/api/terminal", {"prefix": "!!", "key": "bsk_x"})
    assert code == 200 and view["prefix"] == "!!" and view["has_key"] is True and "bsk_x" not in json.dumps(view)
    assert json.loads((tmp_path / "data" / "terminal.json").read_text(encoding="utf-8"))["prefix"] == "!!"             # the file the module reads
    code, body = api("POST", "/api/terminal", {"color": "red"})
    assert code == 400 and body["error"]["field"] == "color"
    assert api("GET", "/api/terminal")[1]["prefix"] == "!!"
