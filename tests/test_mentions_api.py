"""The conversations a message can mention ("@" in the message box): the other conversations, ten at a time, newest use first, filtered by the letters typed over the name and the card,
and the `mentions` field of a send, which rides along with the message and is shown back in the user event. Their delivery to the mentioned conversations is in tests/test_session_messaging.py."""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.agent_sessions import SessionManager
from test_e2e_mock_server import start_server  # noqa: F401  (fixture)

STUB = Path(__file__).resolve().parent.parent / "serve" / "tests" / "session_stub.py"


class Cards:
    def __init__(self, texts):
        self.texts = texts

    def card_of(self, sid):
        return {"text": self.texts[sid]} if sid in self.texts else None


class Api:
    def __init__(self, url):
        self.url = url

    def __call__(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")


@pytest.fixture
def world(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server(["unused"])
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(tmp_path / "j", url, tick_s=0.2, default_workspace=ws,
                         command=lambda sid, rec, history: [sys.executable, str(STUB), "--session-id", sid, "--history-dir", str(history)])
    monkeypatch.setattr(handler, "_SESSIONS", mgr)
    ids = []
    for i in range(14):
        sid = mgr.create(str(ws), title=f"Conversation number {i:02d}")["id"]
        ids.append(sid)
        with mgr._lock:
            mgr._record(sid)["updated"] = 1000 + i                      # the later the number, the later it was used
    yield Api(url), mgr, ids
    mgr.shutdown()


def names(page):
    return [s["name"] for s in page["sessions"]]


def test_the_list_is_ten_at_a_time_newest_use_first_and_never_holds_the_conversation_asking(world):
    api, mgr, ids = world
    code, page = api("GET", f"/api/agent/sessions/mentions?exclude={ids[13]}")
    assert code == 200 and len(page["sessions"]) == 10 and page["total"] == 13 and page["next"] == 10
    assert names(page)[0] == "Conversation number 12" and names(page)[-1] == "Conversation number 03" and ids[13] not in [s["id"] for s in page["sessions"]]
    code, rest = api("GET", f"/api/agent/sessions/mentions?exclude={ids[13]}&offset=10")
    assert names(rest) == ["Conversation number 02", "Conversation number 01", "Conversation number 00"] and rest["next"] is None
    first = page["sessions"][0]
    assert set(first) == {"id", "name", "summary", "updated", "folder", "state"} and first["folder"] == ""                  # the Chat's own folder is no project name
    assert api("GET", "/api/agent/sessions/mentions?offset=x")[0] == 404                      # a bad page is refused like every other agent-route problem


def test_a_use_moves_a_conversation_to_the_top_and_a_closed_one_is_not_offered(world):
    api, mgr, ids = world
    with mgr._lock:
        mgr._record(ids[0])["updated"] = 5000
    assert names(api("GET", "/api/agent/sessions/mentions")[1])[0] == "Conversation number 00"
    mgr.close(ids[0])
    assert "Conversation number 00" not in names(api("GET", "/api/agent/sessions/mentions?limit=50")[1])


def test_the_letters_typed_after_the_at_sign_filter_by_name_and_by_card_ignoring_capitals_and_accents(world):
    api, mgr, ids = world
    mgr.past = Cards({ids[3]: "Il tour della città: abbiamo deciso gli orari dei treni", ids[5]: "Budget of the trip"})
    with mgr._lock:
        mgr._record(ids[7])["alias"] = "Viaggio"
    assert names(api("GET", "/api/agent/sessions/mentions?q=number%2006")[1]) == ["Conversation number 06"]
    assert names(api("GET", "/api/agent/sessions/mentions?q=CITTA")[1]) == ["Conversation number 03"]                      # in the card, not in the name
    assert names(api("GET", "/api/agent/sessions/mentions?q=viag")[1]) == ["Viaggio"]
    page = api("GET", "/api/agent/sessions/mentions?q=trip")[1]
    assert names(page) == ["Conversation number 05"] and page["sessions"][0]["summary"] == "Budget of the trip"
    assert api("GET", "/api/agent/sessions/mentions?q=zzzzzz")[1]["sessions"] == []


def test_a_message_carries_the_ids_it_mentions_and_the_user_event_shows_them_back(world):
    api, mgr, ids = world
    code, out = api("POST", f"/api/agent/sessions/{ids[0]}/send", {"text": "ask @Conversation number 05 about the dates", "mentions": [ids[5], ids[5], "nonexistent", ids[0], ids[9]]})
    assert code == 200
    end = time.monotonic() + 10
    shown = []
    while time.monotonic() < end and not shown:
        events = api("GET", f"/api/agent/sessions/{ids[0]}/events?after=0")[1]["events"]
        shown = [e for e in events if e["event"] == "user"]
        time.sleep(0.05)
    assert shown[0]["mentions"] == [{"id": ids[5], "name": "Conversation number 05"}, {"id": ids[9], "name": "Conversation number 09"}]       # each once, not itself, none that is gone
    assert shown[0]["text"] == "ask @Conversation number 05 about the dates"                                                                 # the text is kept as it was typed
    assert api("POST", f"/api/agent/sessions/{ids[1]}/send", {"text": "x", "mentions": "no"})[0] == 400
    plain = api("POST", f"/api/agent/sessions/{ids[2]}/send", {"text": "no mention"})
    assert plain[0] == 200
