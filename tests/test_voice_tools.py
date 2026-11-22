"""The voice as the harness has it: what the model is told, the tools that reach the voice service, and the live state it gets as a `[state]` note."""
import http.server
import json
import threading
from pathlib import Path

import pytest

from tadamoo import awareness, capstate, voice_tools
from tadamoo.permissions import DEFAULT_ALWAYS_ASK
from tadamoo.session import EXCEPTION_TOOLS
from tadamoo.tools.registry import ToolRegistry


class FakeLink(voice_tools.Link):
    """The voice service: answers from a table of (method, path) -> (status, body) and keeps every call."""

    def __init__(self, answers=None, down=False):
        self.answers, self.down, self.calls, self.heard = dict(answers or {}), down, [], {"text": "ciao a tutti", "language": "it", "language_probability": 0.97}

    def call(self, method, path, body=None, timeout=0.0):
        if self.down:
            raise voice_tools.VoiceError("the voice service has not started: it leaves no info file", "voice_service_down")
        self.calls.append((method, path, body))
        answer = self.answers.get((method, path), (200, {}))
        if callable(answer):
            answer = answer(body)
        return answer

    def transcribe(self, path, lang=""):
        if self.down:
            raise voice_tools.VoiceError("the voice service has not started", "service_down")
        self.calls.append(("TRANSCRIBE", str(path), lang))
        return dict(self.heard) if not lang else {"text": self.heard["text"]}


RUNNING = {("GET", "/status"): (200, {"ok": True}),
           ("GET", "/functions"): (200, {"functions": {"stt": {"state": "loaded", "model": "whisper-medium"}, "tts": {"state": "loaded", "engine": "xtts"}, "avatar": {"state": "unloaded"}}}),
           ("GET", "/session/language"): (200, {"language": "it", "mode": "follow", "follow": True, "engine_fallback": False, "warnings": [], "languages": [{"code": "it"}, {"code": "en"}, {"code": "fr"}]}),
           ("GET", "/session/state"): (200, {"active": True, "mode": "half", "phase": "idle", "avatar": False, "brain": "harness", "language_mode": "follow"}),
           ("GET", "/avatar/state"): (200, {"running": False, "persona": "Shan"}),
           ("GET", "/meetbot/status"): (200, {"state": "idle"})}


class Sandbox:
    def __init__(self, root):
        self.root = Path(root)

    def resolve_safe(self, path, tool):
        return self.root / path


@pytest.fixture
def tools(tmp_path):
    def build(answers=None, down=False):
        link = FakeLink({**RUNNING, **(answers or {})}, down)
        registry = ToolRegistry()
        voice_tools.register_voice_tools(registry, link, Sandbox(tmp_path))
        return registry, link
    return build


def call(registry, name, **args):
    return registry.get(name).handler(**args)


def test_the_eight_tools_are_offered_and_a_meeting_is_asked_every_time():
    registry = ToolRegistry()
    voice_tools.register_voice_tools(registry, FakeLink())
    assert registry.names() == ["meeting_info", "meeting_join", "meeting_leave", "voice_help", "voice_language", "voice_say", "voice_status", "voice_transcribe"]
    assert "meeting_join" in DEFAULT_ALWAYS_ASK and EXCEPTION_TOOLS["meeting_join"] == "people" and registry.get("meeting_join").outward
    assert registry.get("voice_transcribe").untrusted and registry.get("meeting_info").untrusted
    assert registry.get("meeting_info").trusted_if({"what": "status"}) and not registry.get("meeting_info").trusted_if({"what": "minutes"})


def test_the_prompt_tells_the_model_what_the_voice_is_and_when_it_is_missing():
    block = awareness.self_model(model="", window=1000, mask_at=0.5, compaction_at=0.7, compaction=True, max_turns=10, max_tokens=None, max_seconds=None, absent=[], lessons="", voice=voice_tools.KNOWLEDGE)
    for word in ("voice_status", "voice_transcribe", "voice_say", "avatar", "reply_language=fr", "follows the language the person speaks", "meeting_join", "voice_help"):
        assert word in block
    assert "Voice:" not in awareness.self_model(model="", window=1000, mask_at=0.5, compaction_at=0.7, compaction=True, max_turns=10, max_tokens=None, max_seconds=None, absent=[], lessons="")
    assert "no voice: it cannot listen, speak or show an avatar" in awareness.absent_features({"voice": False})
    assert "no voice" not in " ".join(awareness.absent_features({"voice": True}))
    assert set(voice_tools.TOPICS) == {"overview", "speak", "listen", "language", "avatar", "meetings", "phone", "desktop", "problems"}
    assert all(topic in voice_tools.KNOWLEDGE for topic in voice_tools.TOPICS)


def test_help_gives_one_topic_and_names_the_others_when_it_is_wrong(tools):
    registry, _ = tools()
    assert call(registry, "voice_help", topic="language")["text"].startswith("Two directions.")
    assert call(registry, "voice_help")["topic"] == "overview"
    assert "speak" in call(registry, "voice_help", topic="nope")["note"]


def test_status_says_what_is_loaded_the_language_and_the_session_and_says_when_the_service_is_down(tools):
    registry, _ = tools()
    got = call(registry, "voice_status")
    assert got["service"] == "running" and got["functions"]["stt"] == {"state": "loaded", "model": "whisper-medium"} and got["functions"]["avatar"] == {"state": "unloaded"}
    assert got["language"] == {"language": "it", "mode": "follow", "follow": True, "engine_fallback": False, "warnings": [], "enabled": ["it", "en", "fr"]}
    assert got["session"]["active"] is True and got["avatar"] == {"running": False, "persona": "Shan"} and got["meeting"] == {"state": "idle"}
    down, _ = tools(down=True)
    assert call(down, "voice_status")["service"] == "down" and "voice service" in call(down, "voice_status")["why"]


def test_saying_in_a_language_asks_the_service_for_the_voice_of_that_language_not_for_an_engine(tools):
    registry, link = tools({("POST", "/brain/say"): (200, {"status": "spoken"})})
    out = call(registry, "voice_say", text="Bonjour tout le monde.", lang="fr")
    assert out == {"status": "ok", "spoken": 1, "characters": 22, "by": "voice", "lang": "fr"}
    assert link.calls[-1] == ("POST", "/brain/say", {"text": "Bonjour tout le monde.", "lang": "fr"})
    assert call(registry, "voice_say", text="x", engine="xtts")["status"] == "error"                       # an engine only with a voice the person named
    call(registry, "voice_say", text="Ciao.", voice="if_sara", engine="kokoro", lang="it")
    assert link.calls[-1] == ("POST", "/brain/say", {"text": "Ciao.", "lang": "it", "voice": "if_sara", "engine": "kokoro"})
    assert call(registry, "voice_say", text="x", lang="english")["status"] == "error" and call(registry, "voice_say", text="  ")["status"] == "error"


def test_the_avatar_says_it_through_its_own_route(tools):
    registry, link = tools({("POST", "/avatar/speak"): (200, {"status": "ok"})})
    assert call(registry, "voice_say", text="Hello.", lang="en", avatar=True)["by"] == "avatar"
    assert link.calls[-1] == ("POST", "/avatar/speak", {"text": "Hello.", "lang": "en"})


def test_a_long_text_is_said_in_pieces_and_what_is_left_out_is_told(tools):
    registry, link = tools({("POST", "/brain/say"): (200, {"status": "spoken"})})
    text = " ".join(f"Sentence number {i} says something plain and short enough." for i in range(80))
    out = call(registry, "voice_say", text=text, lang="en")
    assert out["status"] == "ok" and out["characters"] <= voice_tools.SAY_MAX and "left out" in out["not_spoken"]
    assert all(len(c[2]["text"]) <= voice_tools.SAY_PIECE for c in link.calls if c[1] == "/brain/say") and out["spoken"] == len([c for c in link.calls if c[1] == "/brain/say"])


def test_a_busy_voice_is_waited_for_a_language_no_voice_speaks_is_told_and_nothing_is_spoken_twice(tools, monkeypatch):
    monkeypatch.setattr(voice_tools.time, "sleep", lambda s: None)
    answers = iter([(409, {"code": "busy", "error": "a sentence is being spoken"}), (200, {"status": "spoken"})])
    registry, link = tools({("POST", "/brain/say"): lambda body: next(answers)})
    assert call(registry, "voice_say", text="Ciao.")["spoken"] == 1 and len([c for c in link.calls if c[1] == "/brain/say"]) == 2             # asked again once, said once
    nope, _ = tools({("POST", "/brain/say"): (409, {"code": "language_unavailable", "error": "no voice speaks 'ja'"})})
    out = call(nope, "voice_say", text="こんにちは。", lang="ja")
    assert out["status"] == "error" and out["code"] == "language_unavailable" and "'ja'" in out["note"] and out["said"] == ""
    down, _ = tools(down=True)
    assert call(down, "voice_say", text="Ciao.")["code"] == "voice_service_down"


def test_listening_reads_a_file_of_the_working_folder_and_says_the_language_it_heard(tools, tmp_path):
    registry, link = tools()
    (tmp_path / "note.ogg").write_bytes(b"OggS")
    out = call(registry, "voice_transcribe", path="note.ogg")
    assert out == {"status": "ok", "text": "ciao a tutti", "language": "it", "language_probability": 0.97}
    assert call(registry, "voice_transcribe", path="note.ogg", lang="it") == {"status": "ok", "text": "ciao a tutti"}
    assert link.calls[0][0] == "TRANSCRIBE" and link.calls[0][2] == ""
    assert call(registry, "voice_transcribe", path="missing.ogg")["status"] == "error" and call(registry, "voice_transcribe")["status"] == "error"
    assert call(registry, "voice_transcribe", path="note.ogg", lang="italian")["status"] == "error"
    down, _ = tools(down=True)
    assert call(down, "voice_transcribe", path="note.ogg")["code"] == "service_down"


def test_the_language_is_read_and_changed_for_the_running_session_and_a_missing_session_is_told(tools):
    registry, link = tools({("POST", "/session/language"): (200, {"status": "ok", "language": "fr"})})
    got = call(registry, "voice_language")
    assert got["language"] == "it" and got["mode"] == "follow" and got["enabled"] == ["it", "en", "fr"] and "changed_to" not in got
    call(registry, "voice_language", language="fr")
    assert ("POST", "/session/language", {"language": "fr"}) in link.calls
    assert call(registry, "voice_language", language="auto")["changed_to"] == "auto"
    assert call(registry, "voice_language", language="French")["status"] == "error"
    none, _ = tools({("POST", "/session/language"): (409, {"code": "no_session", "error": "nessuna sessione voce attiva"})})
    assert call(none, "voice_language", language="fr")["status"] == "no_session"
    notenabled, _ = tools({("POST", "/session/language"): (400, {"code": "not_enabled", "error": "language 'xx' is not enabled (Settings > Voice)"})})
    assert call(notenabled, "voice_language", language="xx")["code"] == "not_enabled"


def test_meetings_are_read_by_id_and_joined_only_with_the_right_confirmed(tools):
    registry, link = tools({("GET", "/meetings/list"): (200, {"meetings": [{"id": "m1"}]}), ("GET", "/meetings/m1"): (200, {"summary": "ok"}), ("GET", "/meetings/m1/transcript"): (200, {"lines": []}),
                            ("POST", "/meetbot/join"): (200, {"state": "joining"}), ("POST", "/meetbot/leave"): (200, {"state": "left"})})
    assert call(registry, "meeting_info", what="list")["data"] == {"meetings": [{"id": "m1"}]}
    assert call(registry, "meeting_info", what="minutes", id="m1")["data"] == {"summary": "ok"}
    assert call(registry, "meeting_info", what="transcript", id="m1")["data"] == {"lines": []}
    assert call(registry, "meeting_info", what="minutes", id="../x")["status"] == "error" and call(registry, "meeting_info", what="nothing")["status"] == "error"
    refused = call(registry, "meeting_join", url="https://meet.google.com/abc-defg-hij")
    assert refused["status"] == "error" and "right" in refused["note"] and not any(c[1] == "/meetbot/join" for c in link.calls)
    ok = call(registry, "meeting_join", url="https://meet.google.com/abc-defg-hij", rights_confirmed=True, title="Weekly")
    assert ok["status"] == "ok" and link.calls[-1] == ("POST", "/meetbot/join", {"url": "https://meet.google.com/abc-defg-hij", "rights_confirmed": True, "consent_mode": "window",
                                                                                    "consent_window_s": 45, "speak": False, "title": "Weekly"})
    assert call(registry, "meeting_leave")["status"] == "ok"


def test_the_state_line_is_one_line_cached_for_a_few_seconds_and_says_off_when_the_service_is_not_there(tools):
    registry, link = tools()
    cache, now = voice_tools._Cache(), [100.0]
    assert voice_tools.state_line(link, cache, lambda: now[0]) == "on (stt loaded, tts loaded, avatar unloaded); speaking it (follow)"
    asked = len(link.calls)
    now[0] += 2
    voice_tools.state_line(link, cache, lambda: now[0])
    assert len(link.calls) == asked                                                             # inside the few seconds: nothing is asked again
    now[0] += 10
    voice_tools.state_line(link, cache, lambda: now[0])
    assert len(link.calls) > asked
    assert voice_tools.state_line(FakeLink(down=True)) == "off (the voice service is not running)"
    idle = FakeLink({**RUNNING, ("GET", "/session/language"): (200, {"language": "", "mode": "", "languages": []})})
    assert voice_tools.state_line(idle).endswith("; no voice session")


def test_the_state_note_carries_the_voice_when_it_changes_and_not_again_when_it_does_not(tools):
    class Agent:
        pass
    agent, link = Agent(), FakeLink(RUNNING)
    cache = voice_tools._Cache()
    agent.voice_state = lambda: voice_tools.state_line(link, cache)
    teller = capstate.Teller()
    first = teller.note({"voice": capstate.read(agent, window=1000, unattended=False)["voice"]})
    assert first == "[state] voice=on (stt loaded, tts loaded, avatar unloaded); speaking it (follow)"
    assert teller.note({"voice": capstate.read(agent, window=1000, unattended=False)["voice"]}) == ""
    link.answers[("GET", "/session/language")] = (200, {"language": "fr", "mode": "locked", "languages": []})
    cache.at = 0
    assert teller.note({"voice": capstate.read(agent, window=1000, unattended=False)["voice"]}).endswith("speaking fr (locked)")
    agent.voice_state = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    assert capstate.read(agent, window=1000, unattended=False)["voice"].startswith("not known now")
    assert "voice" in capstate.FIELDS


class Service(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        Service.seen.append((self.path, self.headers.get("Authorization"), body))
        status, reply = (200, {"text": "hola", "language": "es", "language_probability": 0.9}) if b"good.ogg" in body else (409, {"code": "model_not_installed", "message": "whisper-medium is not installed"})
        raw = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def test_the_real_link_posts_the_file_with_the_token_asks_for_the_language_and_reports_a_missing_model(tmp_path, monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from serve import voice_service
        monkeypatch.setattr(voice_service, "endpoint", lambda: ("127.0.0.1", server.server_address[1], "tok"))
        link = voice_tools.ServiceLink()
        good = tmp_path / "good.ogg"
        good.write_bytes(b"OggS")
        assert link.transcribe(good, "") == {"text": "hola", "language": "es", "language_probability": 0.9}
        link.transcribe(good, "es")
        assert Service.seen[0][0] == "/transcribe?segments=1" and Service.seen[1][0] == "/transcribe?segments=1&lang=es" and Service.seen[0][1] == "Bearer tok"
        bad = tmp_path / "bad.ogg"
        bad.write_bytes(b"OggS")
        with pytest.raises(voice_tools.VoiceError) as caught:
            link.transcribe(bad, "")
        assert caught.value.code == "model_not_installed" and "not installed" in str(caught.value) and "tok" not in str(caught.value)
    finally:
        server.shutdown()
