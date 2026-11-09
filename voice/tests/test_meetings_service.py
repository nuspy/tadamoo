"""The meeting routes of the voice service, in-process: a request is checked before anything starts, a join leaves its first audit row, the folder of
past meetings can be read and deleted, and the calendar does nothing without the person's own OAuth values. The network is blocked; no browser, no
audio device, no model."""
import json
import socket
import time
import types
import urllib.request

import pytest

from callbridge import models_store as ms
from callbridge.calendar_oauth import GoogleCalendar, MicrosoftCalendar, Vault, extract_meeting_url
from voice import service
from test_meet_adapters import El, Page
from meet_fakes import FakeBrowser, FakeSink, FakeSource, FakeStt

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}
URL = "https://meet.google.com/abc-defg-hij?pwd=secret"
GOOD = {"url": URL, "rights_confirmed": True, "on_behalf_of": "Mario Rossi"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", ms.ROOT_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(ms, "_service_env", {})


class Env:
    def __init__(self, tmp_path, settings):
        path = tmp_path / "voice.json"
        path.write_text(json.dumps({"models_dir": str(tmp_path / "models"), "meetings": settings}), encoding="utf-8")
        self.app, self.state = service.build_app(path, TOKEN)
        self.client = self.app.test_client()
        self.tmp = tmp_path
        self.svc = self.state.meetings
        self.page = Page([El("Leave call"), El("Send a message"), El('placeholder*="name"'), El("Join now")])
        self.stt = FakeStt()
        self.browser = FakeBrowser()
        self.browser.open = lambda url: self.page
        self.svc._deps_hook = self._hook
        self.state.functions._live = lambda name: "loaded" if name == "stt" else "unloaded"

    def _hook(self, deps):
        deps.browser_factory = lambda media: self.browser
        deps.stt_factory = lambda lang: self.stt
        deps.source_factory = FakeSource
        deps.sink_factory = FakeSink
        deps.mic_names = lambda: []
        deps.sleep = lambda s: time.sleep(0.01)
        return deps

    def post(self, path, body=None):
        return self.client.post(path, json=body, headers=AUTH)

    def get(self, path):
        return self.client.get(path, headers=AUTH)

    def audit(self):
        path = self.tmp / "meetings-audit.jsonl"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def wait(self, predicate, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path, {"on_behalf_of": "Mario Rossi"})


def test_the_routes_need_the_service_token(env):
    for method, path in (("get", "/meetbot/status"), ("post", "/meetbot/join"), ("get", "/meetings/list"), ("get", "/calendar/status"), ("get", "/copilot/state")):
        assert getattr(env.client, method)(path).status_code == 401, path


def test_idle_status_and_empty_folder(env):
    body = env.get("/meetbot/status").get_json()
    assert body["state"] == "idle" and body["copilot"]["active"] is False
    assert env.get("/meetings/list").get_json() == {"meetings": []}
    assert str(env.svc.root()).startswith(str(env.tmp)) and env.svc.root().name == "meetings"


# ---- a request is checked before anything starts
@pytest.mark.parametrize("body,code", [
    ({"url": URL}, "rights_required"),
    ({"url": URL, "rights_confirmed": "yes"}, "rights_required"),
    ({"url": URL, "rights_confirmed": False}, "rights_required"),
    ({"url": "https://acme.webex.com/meet/x", "rights_confirmed": True}, "unsupported_platform"),
    ({"url": "http", "rights_confirmed": True}, "unsupported_platform"),
    ({"url": URL, "rights_confirmed": True, "on_behalf_of": "  "}, "on_behalf_missing"),
    ({**GOOD, "consent_mode": "silent"}, "bad_request"),
    ({**GOOD, "consent_window_s": 19}, "bad_request"),
    ({**GOOD, "consent_window_s": 301}, "bad_request"),
    ({**GOOD, "consent_window_s": "45"}, "bad_request"),
    ({**GOOD, "speak": "yes"}, "bad_request"),
])
def test_a_bad_join_is_refused_and_starts_nothing(env, body, code):
    r = env.post("/meetbot/join", body)
    assert r.status_code == 400 and r.get_json()["code"] == code
    assert env.svc.session is None and env.audit() == []


def test_a_program_cannot_claim_the_calendar_way_in(env):
    r = env.post("/meetbot/join", {"url": URL, "rights": "calendar", "on_behalf_of": "Mario Rossi"})
    assert r.status_code == 400 and r.get_json()["code"] == "rights_required"


def test_without_the_stt_loaded_nothing_starts(env):
    env.state.functions._live = lambda name: "unloaded"
    r = env.post("/meetbot/join", GOOD)
    assert r.status_code == 409 and r.get_json()["code"] == "not_loaded" and r.get_json()["missing"] == ["stt"]
    assert env.audit() == []


def test_a_join_that_cannot_be_audited_does_not_happen(tmp_path):
    env = Env(tmp_path, {"on_behalf_of": "Mario Rossi"})
    (tmp_path / "blocker").write_text("a file where a folder should be", encoding="utf-8")
    env.state.config.meetings_audit_file = str(tmp_path / "blocker" / "meetings-audit.jsonl")
    r = env.post("/meetbot/join", GOOD)
    assert r.status_code == 500 and r.get_json()["code"] == "audit_failed" and env.svc.session is None


def test_a_join_announces_then_leave_before_consent_keeps_nothing(env):
    r = env.post("/meetbot/join", GOOD)
    assert r.status_code == 202 and r.get_json()["state"] in ("joining", "in_call")
    s = env.svc.session
    assert env.wait(lambda: s.announced["chat"] and s.stt is not None)
    status = env.get("/meetbot/status").get_json()
    assert status["consent"]["state"] == "waiting" and status["consent"]["mode"] == "window" and status["consent"]["window_s"] == 45
    assert status["announced"]["chat"] is True and status["transcript_tail"] == [] and status["speak"] == {"on": False, "blocked": "", "proactive": False}
    assert env.post("/meetbot/join", GOOD).status_code == 409                       # one at a time
    assert env.post("/meetbot/leave").get_json()["state"] == "leaving"
    assert env.wait(lambda: s.state == "ended")
    assert list(env.svc.root().iterdir()) == []                                   # nothing was kept
    rows = env.audit()
    assert [x["event"] for x in rows][:2] == ["join", "announced"] and rows[-1]["event"] == "end" and rows[-1]["reason"] == "left_before_consent"
    assert rows[0]["rights"] == "ticked" and "secret" not in json.dumps(rows) and "meet.google.com" not in json.dumps(rows)
    assert env.post("/meetbot/leave").get_json() == {"state": "idle"}


def test_a_page_that_is_not_a_meeting_ends_in_an_error_and_keeps_nothing(env):
    env.page.elements = []
    assert env.post("/meetbot/join", GOOD).status_code == 202
    s = env.svc.session
    assert env.wait(lambda: s.state == "error") and s.end_reason == "join_failed"
    assert list(env.svc.root().iterdir()) == [] and env.audit()[-1]["event"] == "end"


# ---- past meetings
def seed(env, mid="20261109-100000-meet-ab12", transcript=True, analysis=True, deleted=False):
    d = env.svc.root() / mid
    d.mkdir(parents=True)
    meta = {"title": "Weekly", "platform": "meet", "created": "2026-11-09T10:00:00", "ended": "2026-11-09T10:30:00", "participants": ["Ada"], "segments": 2,
            "analysis_state": "done" if analysis else "pending", "deleted": deleted, "end_reason": "left"}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if transcript:
        (d / "transcript.jsonl").write_text('{"speaker":"Ada","text":"hi"}\n{"speaker":"Bruno","text":"ok"}\n', encoding="utf-8")
    if analysis:
        (d / "analysis.json").write_text(json.dumps({"summary": "S", "highlights": [], "tasks": [{"assignee": "Ada", "task": "T"}]}), encoding="utf-8")
        (d / "summary.md").write_text("# Weekly", encoding="utf-8")
    return d


def test_past_meetings_are_listed_read_and_deleted_with_their_transcript_and_minutes(env):
    d = seed(env)
    listed = env.get("/meetings/list").get_json()["meetings"]
    assert [m["id"] for m in listed] == [d.name] and listed[0]["segments"] == 2 and listed[0]["analysis_state"] == "done"
    one = env.get(f"/meetings/{d.name}").get_json()
    assert one["analysis"]["summary"] == "S" and one["participants"] == ["Ada"]
    assert [x["text"] for x in env.get(f"/meetings/{d.name}/transcript").get_json()["segments"]] == ["hi", "ok"]
    assert env.client.delete(f"/meetings/{d.name}", headers=AUTH).status_code == 200
    assert not d.exists() and env.get("/meetings/list").get_json() == {"meetings": []}
    assert env.get(f"/meetings/{d.name}").status_code == 404


def test_a_stub_left_by_an_objection_is_listed_and_can_be_deleted(env):
    d = seed(env, transcript=False, analysis=False, deleted=True)
    listed = env.get("/meetings/list").get_json()["meetings"]
    assert listed[0]["deleted"] is True and listed[0]["end_reason"] == "left"
    assert env.get(f"/meetings/{d.name}/transcript").status_code == 404
    assert env.client.delete(f"/meetings/{d.name}", headers=AUTH).status_code == 200


def test_ids_that_climb_out_of_the_folder_are_refused(env):
    seed(env)
    (env.tmp / "secret.txt").write_text("keep", encoding="utf-8")
    for bad in ("..", "a..b", "x%2F..%2F..", "bad id", "."):
        assert env.client.delete(f"/meetings/{bad}", headers=AUTH).status_code in (400, 404), bad
        assert env.get(f"/meetings/{bad}").status_code in (400, 404, 308), bad
    assert (env.tmp / "secret.txt").exists() and len(list(env.svc.root().iterdir())) == 1


def test_a_meeting_in_progress_or_being_analysed_is_not_deleted(env):
    d = seed(env)
    env.svc._analyzing.add(d.name)
    assert env.client.delete(f"/meetings/{d.name}", headers=AUTH).status_code == 409 and d.exists()
    env.svc._analyzing.clear()
    env.svc.session = types.SimpleNamespace(session_id=d.name, state="in_call")
    assert env.client.delete(f"/meetings/{d.name}", headers=AUTH).status_code == 409 and d.exists()


def test_the_minutes_are_written_by_the_analysis_with_no_wiki_push(env, monkeypatch):
    from callbridge.meetbot import summary
    d = seed(env, analysis=False)
    seen = {}

    def fake_analyze(cfg, segments, **kw):
        seen.update(kw, n=len(segments))
        return {"summary": "Short.", "highlights": [], "agreements": [], "tasks": [{"assignee": "Ada", "task": "send the file"}]}

    monkeypatch.setattr(summary.analysis_mod, "analyze_transcript", fake_analyze)
    meta = summary.analyze_meeting(env.state.config, d, json.loads((d / "meta.json").read_text(encoding="utf-8")))
    assert meta["analysis_state"] == "done" and seen["n"] == 2 and seen["participants"] == ["Ada"]
    assert "send the file" in (d / "summary.md").read_text(encoding="utf-8")
    assert not hasattr(summary, "push_meeting_to_wiki") and "wiki" not in meta


def test_an_empty_transcript_is_an_analysis_error_not_a_crash(env):
    d = seed(env, transcript=False, analysis=False)
    from callbridge.meetbot.summary import analyze_meeting
    meta = analyze_meeting(env.state.config, d, {"title": "x"})
    assert meta["analysis_state"] == "error" and "empty" in meta["analysis_error"]


# ---- the calendar is inert without the person's own values
class MemoryStore:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def read(self, name):
        return self.data.get(name, "")

    def write(self, name, value):
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)


def test_the_calendar_is_inert_without_values(env):
    body = env.get("/calendar/status").get_json()
    assert body["autojoin"] is False
    for name in ("google", "microsoft"):
        assert body[name]["configured"] is False and body[name]["connected"] is False and "credential store entry meetings/" in body[name]["hint"]
    assert "error" in env.post("/calendar/google/connect").get_json() and "error" in env.post("/calendar/microsoft/connect").get_json()
    assert env.get("/calendar/events").get_json() == {"events": []}
    assert env.post("/calendar/yahoo/connect").status_code == 400


def test_tokens_and_values_live_in_the_credential_store_only(tmp_path):
    store = MemoryStore({"meetings/google": json.dumps({"client_id": "id", "client_secret": "s"})})
    g = GoogleCalendar(Vault(store))
    assert g.configured() and not g.connected()
    g.vault.set_token("google", "token", '{"refresh_token": "r"}')
    assert json.loads(store.data["meetings/google"])["token"] == '{"refresh_token": "r"}' and json.loads(store.data["meetings/google"])["client_id"] == "id"
    g.disconnect()
    assert "token" not in json.loads(store.data["meetings/google"])
    m = MicrosoftCalendar(Vault(MemoryStore()))
    assert not m.configured() and m.start_connect()["error"].startswith("put client_id")
    assert list(tmp_path.iterdir()) == []                                           # no token file anywhere
    assert Vault(None).get("google") == {}


def test_the_meeting_link_is_found_in_an_event():
    assert extract_meeting_url("see https://meet.google.com/abc-defg-hij now") == "https://meet.google.com/abc-defg-hij"
    assert extract_meeting_url(None, "https://us02web.zoom.us/j/123?pwd=x") == "https://us02web.zoom.us/j/123?pwd=x"
    assert extract_meeting_url("nothing here") is None


# ---- the copilot routes
def test_the_copilot_routes_carry_the_controller(env):
    calls = []
    stub = types.SimpleNamespace(is_active=False, note="", toggle=lambda: calls.append("toggle"), state=lambda: {"enabled": True, "active": False})
    env.app.config["CB_COPILOT"] = stub
    assert env.get("/copilot/state").get_json() == {"enabled": True, "active": False}
    assert env.post("/copilot/toggle").get_json() == {"active": False, "note": ""} and calls == ["toggle"]
    assert not any(r.rule.startswith("/copilot/") and "project" in r.rule or "save-meeting" in r.rule for r in env.app.url_map.iter_rules())


def test_the_hotkey_manager_is_given_the_same_copilot(env, monkeypatch):
    import sys
    made = {}

    class FakeManager:
        def __init__(self, config, brains, **kw):
            made.update(kw)

    monkeypatch.setitem(sys.modules, "callbridge.ptt", types.SimpleNamespace(PushToTalkManager=FakeManager))
    env.state.session._default_make()
    assert made["copilot"] is env.state.meetings.copilot
