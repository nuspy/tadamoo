"""The settings of the look at a request, the questions and the suggestions: kept and checked, handed to the conversation's process, reachable over HTTP."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.session import parse_args as session_args
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.intake_prefs import IntakeError, IntakePrefs
from serve.service import Service
from serve.util.text import ByteTokenizer
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)


def test_defaults_are_off_chips_on_and_a_bounded_size(tmp_path):
    prefs = IntakePrefs(tmp_path / "i.json")
    assert prefs.read() == {"intake": "off", "suggestions": True, "optimize_prompt": False, "side_call_tokens": 6000}
    view = prefs.view()
    assert view["modes"] == ["off", "rules", "full"] and view["defaults"]["intake"] == "off" and view["limits"]["side_call_tokens"] == [500, 200000]
    assert view["defaults"]["optimize_prompt"] is False


def test_values_are_saved_checked_and_a_refused_value_changes_nothing(tmp_path):
    prefs = IntakePrefs(tmp_path / "i.json")
    saved = prefs.write({"intake": "full", "suggestions": False, "optimize_prompt": True, "side_call_tokens": 9000})
    assert saved == {"intake": "full", "suggestions": False, "optimize_prompt": True, "side_call_tokens": 9000} and IntakePrefs(tmp_path / "i.json").read() == saved
    for bad, field in (({"intake": "always"}, "intake"), ({"suggestions": "yes"}, "suggestions"), ({"optimize_prompt": "yes"}, "optimize_prompt"), ({"optimize_prompt": 1}, "optimize_prompt"), ({"side_call_tokens": 10}, "side_call_tokens"),
                       ({"side_call_tokens": 2.5}, "side_call_tokens"), ({"side_call_tokens": True}, "side_call_tokens"), ({"side_call_tokens": 10**9}, "side_call_tokens")):
        with pytest.raises(IntakeError) as raised:
            prefs.write(bad)
        assert raised.value.field == field and str(raised.value)
    assert prefs.read() == saved
    (tmp_path / "i.json").write_text("{broken", encoding="utf-8")
    assert prefs.read()["intake"] == "off"


def test_the_choice_is_kept_in_the_file_and_a_new_handler_reads_it_again(tmp_path):
    path = tmp_path / "data" / "intake.json"
    assert IntakePrefs(path).read()["intake"] == "off"                          # no file: off
    IntakePrefs(path).write({"intake": "full"})
    assert IntakePrefs(path).read()["intake"] == "full" and session_args(IntakePrefs(path).flags()).intake == "full"
    IntakePrefs(path).write({"intake": "rules"})
    assert IntakePrefs(path).read()["intake"] == "rules" and IntakePrefs(path).read()["suggestions"] is True
    IntakePrefs(path).write({"intake": IntakePrefs(path).view()["defaults"]["intake"]})        # the defaults button
    assert IntakePrefs(path).read()["intake"] == "off"


def test_the_flags_reach_the_session_process_arguments(tmp_path):
    prefs = IntakePrefs(tmp_path / "i.json")
    args = session_args(prefs.flags())
    assert (args.intake, args.suggestions, args.side_call_tokens) == ("off", "on", 6000)
    prefs.write({"intake": "off", "suggestions": False, "side_call_tokens": 1234})
    args = session_args(prefs.flags())
    assert (args.intake, args.suggestions, args.side_call_tokens) == ("off", "off", 1234)
    assert args.optimize_prompt == "off"
    prefs.write({"optimize_prompt": True})
    assert session_args(prefs.flags()).optimize_prompt == "on"
    assert session_args([]).suggestions == "off" and session_args([]).intake == "off"


def test_optimize_prompt_is_kept_in_the_file_and_a_new_handler_reads_it_again(tmp_path):
    path = tmp_path / "data" / "intake.json"
    assert IntakePrefs(path).read()["optimize_prompt"] is False                 # no file: off
    IntakePrefs(path).write({"optimize_prompt": True})
    assert IntakePrefs(path).read()["optimize_prompt"] is True and json.loads(path.read_text(encoding="utf-8"))["optimize_prompt"] is True
    IntakePrefs(path).write({"intake": "full"})                                 # another setting does not touch it
    assert IntakePrefs(path).read()["optimize_prompt"] is True
    path.write_text(json.dumps({"optimize_prompt": "yes"}), encoding="utf-8")
    assert IntakePrefs(path).read()["optimize_prompt"] is False                 # only a real true counts
    IntakePrefs(path).write({"optimize_prompt": IntakePrefs(path).view()["defaults"]["optimize_prompt"]})      # the defaults button
    assert IntakePrefs(path).read()["optimize_prompt"] is False


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
    assert code == 200 and view["intake"] == "off" and view["suggestions"] is True
    code, view = api("POST", "/api/intake", {"intake": "full", "suggestions": False})
    assert code == 200 and view["intake"] == "full" and view["suggestions"] is False
    assert api("GET", "/api/intake")[1]["intake"] == "full"
    code, body = api("POST", "/api/intake", {"side_call_tokens": 3})
    assert code == 400 and body["error"]["field"] == "side_call_tokens"
    assert api("POST", "/api/intake", [1])[0] == 400
    code, view = api("POST", "/api/intake", {"intake": "off"})                 # nothing running to tell: the saved choice is what a new conversation takes
    assert code == 200 and view["intake"] == "off"


@pytest.fixture
def api_with_side(tmp_path, monkeypatch):
    """The settings route of a server whose engine may or may not have a technical session (set `svc.engine.side_slot`)."""
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, [answer("x")], max_context=65536), tok, ChatTemplate(Path(__file__).resolve().parents[1] / "serve" / "chat_template.jinja"))
    svc.start_telemetry = lambda: None
    httpd = serve(svc, port=0)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
    yield call, svc
    httpd.shutdown()
    httpd.server_close()


def test_optimize_prompt_cannot_be_switched_on_without_a_technical_session_and_the_saved_value_outlives_it(api_with_side):
    call, svc = api_with_side
    assert call("GET", "/api/intake")[1]["optimize_prompt"] is False
    code, body = call("POST", "/api/intake", {"optimize_prompt": True})              # the engine reports no session
    assert code == 400 and body["error"]["field"] == "optimize_prompt" and "technical session" in body["error"]["message"]
    assert call("GET", "/api/intake")[1]["optimize_prompt"] is False
    svc.engine.side_slot = 1                                                        # a session appears (Loader > Technical session)
    assert call("GET", "/v1/status")[1]["concurrency"]["side_slot"]["context"] > 0
    code, view = call("POST", "/api/intake", {"optimize_prompt": True})
    assert code == 200 and view["optimize_prompt"] is True
    svc.engine.side_slot = None                                                     # and goes: the stored value stays, switching it off is always allowed
    assert call("GET", "/api/intake")[1]["optimize_prompt"] is True
    assert call("GET", "/v1/status")[1]["concurrency"]["side_slot"] is None
    code, view = call("POST", "/api/intake", {"optimize_prompt": False})
    assert code == 200 and view["optimize_prompt"] is False
    assert call("POST", "/api/intake", {"optimize_prompt": "yes"})[0] == 400
