"""Contract tests of the wikibridge and callAPIcall clients against local fakes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_services import FakeCallAPICall, FakeWikibridge  # noqa: E402
from harness.integrations import CallAPICallClient, WikiBridgeClient  # noqa: E402
from harness.integrations.call_api_client import build_greeting, format_transcript, has_ai_disclosure  # noqa: E402


@pytest.fixture
def wiki():
    fake = FakeWikibridge(key="k3y")
    yield fake
    fake.close()


@pytest.fixture
def phone():
    fake = FakeCallAPICall()
    yield fake
    fake.close()


def test_wikibridge_reads_the_real_shapes(wiki):
    c = WikiBridgeClient(wiki.url, api_key="k3y")
    assert c.status() == "ok" and c.is_available()
    assert c.list_projects() == ["cervinia", "work"]
    assert c.search("cervinia", "ovini")[0]["path"] == "entities/diana.md"
    assert c.get_page("cervinia", "entities/diana.md") == "Allergica agli ovini."
    assert c.get_page("cervinia", "entities/missing.md") is None
    ctx = c.rag_context("cervinia", "allergie", participants=["diana"])
    assert ctx["blocks"][0]["text"] == "Allergica agli ovini."
    assert wiki.requests[-1][3]["participants"] == ["diana"]


def test_wikibridge_wrong_key_and_offline_are_distinct(wiki):
    assert WikiBridgeClient(wiki.url, api_key="wrong").search("cervinia", "x") == []
    bad = WikiBridgeClient(wiki.url, api_key="wrong")
    bad.search("cervinia", "x")
    assert "unauthorized" in bad.last_error

    off = WikiBridgeClient("http://127.0.0.1:59997", timeout=0.2)
    assert off.status() == "offline"
    assert off.list_projects() == []
    assert "offline" in off.last_error


@pytest.mark.parametrize("bad", ["../secret.md", "/etc/passwd", "a/../../b.md", ""])
def test_wikibridge_rejects_paths_outside_the_wiki(wiki, bad):
    c = WikiBridgeClient(wiki.url, api_key="k3y")
    before = len(wiki.requests)
    assert c.get_page("cervinia", bad) is None
    assert len(wiki.requests) == before


def test_wikibridge_environment_configuration(monkeypatch, wiki):
    monkeypatch.setenv("BASALT_WIKIBRIDGE_URL", wiki.url)
    monkeypatch.setenv("BASALT_WIKIBRIDGE_KEY", "k3y")
    assert WikiBridgeClient.from_environment().is_available()


def test_call_client_follows_the_external_contract(phone):
    c = CallAPICallClient(phone.url, token="t0ken")
    assert c.is_available()
    res = c.make_phone_call("+39 0166 123456", "Verificare l'apertura dell'hotel", persona_prompt="Mario Rossi")
    assert res["call_id"] == "c1" and res["status"] == "calling"
    placed = phone.placed[0]
    assert placed["number"] == "+39 0166 123456" and placed["transport"] == "gsm"
    assert has_ai_disclosure(placed["greeting"]) and "Mario Rossi" in placed["greeting"]

    assert c.say("c1", "Avete camere libere?") and phone.said == ["Avete camere libere?"]
    heard = c.heard("c1", after=-1, timeout_s=1)
    assert heard["text"] == "Pronto?" and heard["index"] == 0
    assert c.get_call_transcript("c1") == "assistant: Buongiorno\ncallee: Pronto?"
    assert c.state("c1")["status"] == "active"
    assert c.hangup("c1") and c.state("c1")["status"] == "ended"


def test_call_client_refuses_to_dial_without_ai_disclosure(phone):
    c = CallAPICallClient(phone.url, token="t0ken")
    with pytest.raises(ValueError, match="AI"):
        c.place_call("+390166123456", "Buongiorno, vorrei prenotare.")
    with pytest.raises(ValueError, match="number"):
        c.place_call("rm -rf /", build_greeting("Mario", "Prenotare"))
    with pytest.raises(ValueError):
        c.make_phone_call("+390166123456", "x", mode="satellite")
    assert phone.placed == []


def test_call_client_without_the_token_is_rejected_and_never_retries(phone):
    c = CallAPICallClient(phone.url, token="wrong")
    res = c.place_call("+390166123456", build_greeting("Mario", "Prenotare un tavolo"))
    assert res["status"] == "failed"
    assert sum(1 for r in phone.requests if r[1] == "/external/call") == 1
    assert phone.placed == []


def test_call_client_rejects_unsafe_call_ids(phone):
    c = CallAPICallClient(phone.url, token="t0ken")
    with pytest.raises(ValueError):
        c.hangup("../admin")
    with pytest.raises(ValueError):
        c.say("c1/../x", "ciao")


def test_call_client_offline_degrades_without_raising():
    c = CallAPICallClient("http://127.0.0.1:59998", timeout=0.1, place_timeout=0.2)
    assert not c.is_available()
    res = c.make_phone_call("+390166123456", "Verifica apertura hotel")
    assert res["status"] == "failed" and "not running" in res["error"]
    assert not c.hangup("call_123")
    assert c.get_call_transcript("call_123") is None


# ---- recordings: audio and video attachments go through the analysis routes of the same service ----------------

@pytest.fixture
def recording(tmp_path):
    f = tmp_path / "riunione.mp3"
    f.write_bytes(b"ID3AUDIOBYTES" * 1000)
    return f


def test_only_the_transcript_can_be_asked_for(phone, recording):
    CallAPICallClient(phone.url, token="t0ken").start_media_analysis(str(recording), analyze=False)
    assert phone.uploads[0]["fields"] == {"speakers": "0", "analyze": "0"}


def test_a_recording_is_sent_in_chunks_and_followed_to_the_end(phone, recording):
    c = CallAPICallClient(phone.url, token="t0ken")
    started = c.start_media_analysis(str(recording), title="Budget", speakers=2)
    assert started == {"status": "queued", "id": "20261024-101500-ab12cd"}
    sent = phone.uploads[0]
    assert sent["filename"] == "riunione.mp3" and sent["has_payload"] and sent["size_seen"] > recording.stat().st_size
    assert sent["fields"] == {"speakers": "2", "title": "Budget"}
    naps = []
    final = c.wait_media_analysis(started["id"], poll_s=0.5, sleep=naps.append)
    assert final["state"] == "done" and final["analysis"]["summary"] == "Riunione sul budget."
    assert naps == [0.5, 0.5]                                       # two polls were still running
    segments = c.media_transcript(started["id"])
    assert format_transcript(segments) == "[00:00] Speaker 1: Buongiorno a tutti.\n[01:05] Speaker 2: Parliamo del budget."
    assert c.delete_media_analysis(started["id"]) and phone.deleted == [started["id"]]


def test_a_recording_the_service_would_refuse_never_leaves(phone, tmp_path):
    c = CallAPICallClient(phone.url, token="t0ken")
    doc = tmp_path / "nota.pdf"
    doc.write_bytes(b"%PDF")
    big = tmp_path / "grande.wav"
    big.write_bytes(b"x" * 100)
    for path, why in ((doc, "audio or video"), (tmp_path / "manca.mp3", "not a file"), (big, "limit")):
        with pytest.raises(ValueError, match=why):
            c.start_media_analysis(str(path), max_bytes=50)
    assert phone.uploads == [] and not any(r[1] == "/analysis/start" for r in phone.requests)


def test_a_busy_or_unready_service_is_said_plainly(phone, recording):
    c = CallAPICallClient(phone.url, token="t0ken")
    phone.busy = True
    assert c.start_media_analysis(str(recording))["status"] == "busy"
    phone.busy, phone.no_ffmpeg = False, True
    res = c.start_media_analysis(str(recording))
    assert res["status"] == "failed" and "ffmpeg" in res["error"]
    assert CallAPICallClient(phone.url, token="wrong").start_media_analysis(str(recording))["status"] == "failed"
    assert phone.uploads == []


def test_waiting_gives_up_on_a_dead_service_and_on_a_job_that_never_ends():
    dead = CallAPICallClient("http://127.0.0.1:59998", timeout=0.1)
    assert dead.wait_media_analysis("20261024-101500-ab12cd", sleep=lambda s: None)["state"] == "unavailable"
    assert dead.media_transcript("20261024-101500-ab12cd") is None and not dead.delete_media_analysis("x")
    with pytest.raises(ValueError):
        dead.media_analysis("../x")


def test_waiting_times_out_on_a_job_that_keeps_running(phone):
    c = CallAPICallClient(phone.url, token="t0ken")
    now = [0.0]
    res = c.wait_media_analysis("20261024-101500-ab12cd", timeout_s=0.3, poll_s=0.4,
                                sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert res["state"] == "timeout" and res["progress"]["phase"] == "transcribing"


def test_the_transcript_text_is_readable_even_when_the_service_is_sparse():
    assert format_transcript([{"text": " ciao "}, {"text": ""}, {"start_s": 3725, "text": "tardi", "speaker": "A"}]) == (
        "[00:00] ciao\n[1:02:05] A: tardi")


# ---- what a failed placement means, and that nothing ever places a call twice ---------------------------------

import json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from harness.integrations.telephony_tools import register_phone_tools  # noqa: E402
from harness.tools.registry import ToolRegistry  # noqa: E402


class ScriptedPhone:
    """A phone service that answers POST /external/call as told and counts how many arrive. No number is ever dialled."""

    def __init__(self, behave):
        self.posts = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.posts += 1
                behave(self)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def reply(status, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    def go(h):
        h.send_response(status)
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        h.wfile.write(raw)
    return go


def hang(h):
    time.sleep(1.5)


def drop(h):
    h.connection.shutdown(socket.SHUT_RDWR)             # the request arrived and the connection dies without an answer
    h.connection.close()


CASES = [
    ("busy", reply(409, {"error": "una chiamata esterna è già in corso"}), "failed", "busy", ["409", "già in corso"]),
    ("refused", reply(400, {"error": "numero non valido per il modem"}), "failed", "refused", ["HTTP 400", "numero non valido per il modem"]),
    ("token", reply(401, {"error": "token errato"}), "failed", "unauthorized", ["token", "HTTP 401"]),
    ("missing", reply(404, {"error": "no such route"}), "failed", "not_found", ["HTTP 404"]),
    ("server_error", reply(500, {"error": "modem seriale bloccato"}), "unknown", "server_error", ["HTTP 500", "modem seriale bloccato", "may have started"]),
    ("no_answer", hang, "unknown", "timeout", ["no answer in time", "may have started"]),
    ("dropped", drop, "unknown", "interrupted", ["connection broke", "may have started"]),
    ("garbage", reply(200, b"<html>not json</html>"), "unknown", "bad_response", ["may have started"]),
    ("no_id", reply(200, {"status": "calling"}), "unknown", "bad_answer", ["without a call id"]),
]


@pytest.mark.parametrize("name,behave,status,reason,words", CASES, ids=[c[0] for c in CASES])
def test_each_way_a_placement_can_fail_is_told_apart_and_the_request_is_never_repeated(name, behave, status, reason, words):
    fake = ScriptedPhone(behave)
    try:
        c = CallAPICallClient(fake.url, token="t0ken", place_timeout=0.4)
        res = c.make_phone_call("+390166123456", "Verificare l'apertura dell'hotel")
        assert (res["status"], res["reason"]) == (status, reason), res
        for w in words:
            assert w in res["error"], (w, res["error"])
        time.sleep(0.3)
        assert fake.posts == 1                                                  # one request, whatever happened: no retry of a call
    finally:
        fake.close()


def test_a_service_that_is_not_running_says_so_and_names_the_address():
    c = CallAPICallClient("http://127.0.0.1:59996", place_timeout=0.3)
    res = c.make_phone_call("+390166123456", "Verifica")
    assert (res["status"], res["reason"]) == ("failed", "offline") and "59996" in res["error"] and "start it" in res["error"]


def test_the_phone_tool_carries_the_real_message_and_never_calls_a_possible_call_not_placed():
    registry = ToolRegistry()
    results = {}
    for name, behave, status, reason, words in CASES:
        fake = ScriptedPhone(behave)
        try:
            register_phone_tools(registry, CallAPICallClient(fake.url, token="t0ken", place_timeout=0.4), on_behalf_of="Mario Rossi")
            with pytest.raises(Exception) as err:
                registry.execute("phone_call", {"number": "+390166123456", "purpose": "Verificare l'apertura"})
            results[name] = str(err.value)
            assert fake.posts == 1
        finally:
            fake.close()
    assert "modem seriale bloccato" in results["server_error"] and "may have started" in results["server_error"] and "Do not place it again" in results["server_error"]
    assert results["no_answer"].startswith("The call may have started") and "ask them whether the phone rang" in results["no_answer"]
    assert results["busy"].startswith("The call was not placed") and "già in corso" in results["busy"]
    assert "numero non valido per il modem" in results["refused"]


def test_a_redirect_never_carries_the_credentials_to_another_host():
    seen = []

    def sink(h):
        seen.append(dict(h.headers))
        reply(200, {"ok": True})(h)

    target = ScriptedPhone(sink)
    bouncer = ScriptedPhone(lambda h: (h.send_response(307), h.send_header("Location", target.url + "/x"), h.send_header("Content-Length", "0"), h.end_headers()))
    try:
        res = CallAPICallClient(bouncer.url, token="t0ken-secret").make_phone_call("+390166123456", "Verifica")
        assert res["status"] in ("failed", "unknown") and seen == []                        # the redirect is not followed: the token stays here
        wiki = WikiBridgeClient(bouncer.url, api_key="wiki-secret")
        assert wiki.search("cervinia", "x") == [] and seen == []
    finally:
        bouncer.close()
        target.close()
