"""The settings of the look at a request, the questions and the suggestions: kept and checked, handed to the conversation's process, reachable over HTTP."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.session import parse_args as session_args
from serve.intake_prefs import IntakeError, IntakePrefs
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)


def test_defaults_are_rules_chips_on_and_a_bounded_size(tmp_path):
    prefs = IntakePrefs(tmp_path / "i.json")
    assert prefs.read() == {"intake": "rules", "suggestions": True, "side_call_tokens": 6000}
    view = prefs.view()
    assert view["modes"] == ["off", "rules", "full"] and view["defaults"]["intake"] == "rules" and view["limits"]["side_call_tokens"] == [500, 200000]


def test_values_are_saved_checked_and_a_refused_value_changes_nothing(tmp_path):
    prefs = IntakePrefs(tmp_path / "i.json")
    saved = prefs.write({"intake": "full", "suggestions": False, "side_call_tokens": 9000})
    assert saved == {"intake": "full", "suggestions": False, "side_call_tokens": 9000} and IntakePrefs(tmp_path / "i.json").read() == saved
    for bad, field in (({"intake": "always"}, "intake"), ({"suggestions": "yes"}, "suggestions"), ({"side_call_tokens": 10}, "side_call_tokens"),
                       ({"side_call_tokens": 2.5}, "side_call_tokens"), ({"side_call_tokens": True}, "side_call_tokens"), ({"side_call_tokens": 10**9}, "side_call_tokens")):
        with pytest.raises(IntakeError) as raised:
            prefs.write(bad)
        assert raised.value.field == field and str(raised.value)
    assert prefs.read() == saved
    (tmp_path / "i.json").write_text("{broken", encoding="utf-8")
    assert prefs.read()["intake"] == "rules"


def test_the_flags_reach_the_session_process_arguments(tmp_path):
    prefs = IntakePrefs(tmp_path / "i.json")
    args = session_args(prefs.flags())
    assert (args.intake, args.suggestions, args.side_call_tokens) == ("rules", "on", 6000)
    prefs.write({"intake": "off", "suggestions": False, "side_call_tokens": 1234})
    args = session_args(prefs.flags())
    assert (args.intake, args.suggestions, args.side_call_tokens) == ("off", "off", 1234)
    assert session_args([]).suggestions == "off" and session_args([]).intake == "rules"


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
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


def test_the_settings_page_reads_and_saves(api):
    code, view = api("GET", "/api/intake")
    assert code == 200 and view["intake"] == "rules" and view["suggestions"] is True
    code, view = api("POST", "/api/intake", {"intake": "full", "suggestions": False})
    assert code == 200 and view["intake"] == "full" and view["suggestions"] is False
    assert api("GET", "/api/intake")[1]["intake"] == "full"
    code, body = api("POST", "/api/intake", {"side_call_tokens": 3})
    assert code == 400 and body["error"]["field"] == "side_call_tokens"
    assert api("POST", "/api/intake", [1])[0] == 400
