"""The phone gateway: a connection driven by a fake WebSocket with a fake STT, TTS and brain, and the listening side over loopback.

No model, no microphone, no loudspeaker and no phone: the audio is arrays of numbers and the clients are in-process stand-ins."""
import collections
import json
import time
import types

import numpy as np
import pytest
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidStatus
from websockets.sync.client import connect

from callbridge import voice_gateway as vg
from voice import gateway, service
from voice.tests.fakes import FakeBrain

TOKEN = "gw-token-for-tests"
CLOSED = object()
FRAME = (np.sin(np.arange(1600) / 7.0) * 8000).astype("<i2").tobytes()          # 0.1 s of a tone at 16 kHz


# ---------------------------------------------------------------------------------------- stand-ins
class FakeWs:
    """The server side of a connection. `inbox` holds what the phone sends (a dict becomes JSON, a callable is called when reached, CLOSED closes);
    when it is empty `recv` times out, and after `idle_limit` timeouts the connection closes."""

    def __init__(self, script=(), idle_limit=30):
        self.inbox = collections.deque(script)
        self.sent: list = []
        self.on_send = None
        self.on_idle = None
        self.idle = 0
        self.idle_limit = idle_limit

    def push(self, item):
        self.inbox.append(item)

    def recv(self, timeout=None):
        if not self.inbox and self.on_idle is not None:
            self.on_idle()
        if self.inbox:
            item = self.inbox.popleft()
            if callable(item):
                item = item()
            if item is CLOSED:
                raise ConnectionClosedOK(None, None)
            return json.dumps(item) if isinstance(item, dict) else item
        if not timeout:                              # a look without waiting never closes anything
            if timeout is None:
                raise ConnectionClosedOK(None, None)
            raise TimeoutError
        self.idle += 1
        if self.idle > self.idle_limit:
            raise ConnectionClosedOK(None, None)
        time.sleep(min(timeout, 0.002))
        raise TimeoutError

    def send(self, data):
        self.sent.append(data)
        if self.on_send is not None:
            self.on_send(data)

    @property
    def kinds(self):
        return [json.loads(m)["type"] if isinstance(m, str) else "<pcm>" for m in self.sent]

    def json(self, kind):
        return [json.loads(m) for m in self.sent if isinstance(m, str) and json.loads(m)["type"] == kind]


class FakeStt:
    def __init__(self, text="how are you doing today"):
        self.utterances = __import__("queue").Queue()
        self.speech_events = __import__("queue").Queue()
        self.frames: list = []
        self.said = text
        self.closed = False

    def send_audio(self, pcm):
        self.frames.append(pcm)

    def flush(self):
        self.utterances.put(self.said)

    def close(self):
        self.closed = True


class FakeTts:
    def __init__(self, segments=1, samples=2400):
        self.audio = __import__("queue").Queue()
        self.texts: list = []
        self.configured: list = []
        self.generation = 0
        self.aborted = 0
        self.closed = False
        self._buf: list = []
        self._segments, self._samples = segments, samples

    def configure(self, voice="", lang="", speed=1.0, engine=""):
        self.configured.append((voice, lang, speed, engine))

    def send_text(self, chunk):
        self._buf.append(chunk)

    def flush(self):
        text, self._buf = "".join(self._buf).strip(), []
        if text:
            self.texts.append(text)
            for _ in range(self._segments):
                self.audio.put(types.SimpleNamespace(samples=np.full(self._samples, 0.1, dtype=np.float32), sample_rate=24000, testo=text, generation=0))

    def busy(self):
        return False

    def abort(self, finisci_frase=False):
        self.aborted += 1
        self._buf = []
        while not self.audio.empty():
            self.audio.get_nowait()
        return self.generation

    def close(self):
        self.closed = True


class RecordingBrain(FakeBrain):
    def __init__(self, reply="Sure, I can help with that. "):
        super().__init__(reply)
        self.personas: list = []

    def begin_session(self, persona=None):
        self.personas.append(persona)

    def set_mode(self, mode):
        return mode in ("direct", "harness") and super().set_mode(mode)


class FakeHost:
    token = TOKEN

    def __init__(self, reply="Sure, I can help with that. ", **functions):
        self.cfg = types.SimpleNamespace(default_lang="it", default_voice="if_sara", ptt_turn_pause_s=0.05, tts_speed=1.0, brain_persona="", brain_mode="direct",
                                         avatar_audio_webrtc=True, duplex_motore_nuovo=False)
        self.brains = RecordingBrain(reply)
        self.fn = {"stt": True, "tts": True, "avatar": False, **functions}
        self.stt, self.tts = FakeStt(), FakeTts()
        self.stt_langs: list = []
        self.calls: list = []
        self.answers: dict = {}
        self.avatar_info = {"avatar": "", "render_modes": []}          # what the personality's avatar allows, per persona in `avatar_of`
        self.avatar_of: dict = {}
        self.phone_calls: list = []

    def phone_avatar(self, persona=""):
        self.phone_calls.append(persona)
        return dict(self.avatar_of.get(persona, self.avatar_info))

    def functions(self):
        return dict(self.fn)

    def stt_client(self, lang):
        self.stt_langs.append(lang)
        return self.stt

    def tts_client(self):
        return self.tts

    def control(self, method, path, body=None, timeout=10.0):
        self.calls.append((method, path, body))
        return self.answers.get((method, path), (200, {}))


def run_connection(host, script, authed=True, **kw):
    ws = FakeWs([{"type": "auth", "token": TOKEN}, *script], **kw)
    vg.Connection(ws, host, authed=authed).run()
    return ws


def hold_turn(extra=()):
    return [{"type": "start", "mode": "hold", "lang": "en", "sample_rate": 16000}, FRAME, FRAME, FRAME, {"type": "end"}, *extra]


# ---------------------------------------------------------------------------------------- authentication
def test_a_wrong_token_in_the_first_message_is_refused_and_nothing_else_is_served():
    host = FakeHost()
    ws = FakeWs([{"type": "auth", "token": "wrong"}, *hold_turn()])
    vg.Connection(ws, host, authed=False).run()
    assert ws.kinds == ["error"]
    assert ws.json("error")[0]["code"] == "auth_failed"
    assert host.stt.frames == [] and host.brains.heard == []


@pytest.mark.parametrize("first", [{"type": "start", "mode": "hold"}, {"type": "auth"}, {"type": "auth", "token": ""}, b"\x00\x01", "not json"])
def test_anything_but_a_right_auth_message_is_refused(first):
    ws = FakeWs([first, *hold_turn()])
    vg.Connection(ws, FakeHost(), authed=False).run()
    assert ws.kinds == ["error"]


def test_the_right_token_in_the_first_message_opens_the_connection():
    ws = run_connection(FakeHost(), [CLOSED], authed=False)
    assert ws.kinds[:2] == ["auth_ok", "ready"]


def test_ready_says_what_the_pc_can_do_and_who_answers():
    ws = run_connection(FakeHost(avatar=True), [CLOSED])
    ready = ws.json("ready")[0]
    assert ready["voice"] is True and ready["missing"] == [] and ready["avatar"] is True
    assert ready["brain"] == "direct" and ready["render_modes"] == [] and ready["render_mode"] == "none"          # a device that declares no mode gets the voice only


# ---------------------------------------------------------------------------------------- hold to talk
def test_a_hold_to_talk_turn_goes_through_stt_brain_and_tts_and_reaches_the_phone_as_pcm():
    host = FakeHost()
    ws = run_connection(host, hold_turn())
    kinds = ws.kinds
    assert kinds[:2] == ["auth_ok", "ready"]
    assert kinds.index("transcript") < kinds.index("reply") < kinds.index("audio_start") < kinds.index("<pcm>") < kinds.index("audio_end") < kinds.index("done")
    assert ws.json("transcript")[0]["text"] == "how are you doing today"
    assert len(host.stt.frames) == 3 and host.stt_langs == ["it", "en"]            # opened for the default language, reopened for the turn's
    assert host.brains.heard == [("how are you doing today", "remote")]            # source "remote": an approval is never answered by this voice
    assert host.tts.texts and host.tts.configured[-1][1] == "en"
    pcm = [m for m in ws.sent if isinstance(m, bytes)]
    assert pcm and all(len(m) % 2 == 0 for m in pcm)
    assert ws.json("audio_start")[0]["sample_rate"] == 24000


def test_a_turn_with_nothing_heard_ends_at_once(monkeypatch):
    monkeypatch.setattr(vg, "POST_END_TIMEOUT_S", 0.1)
    host = FakeHost()
    host.stt.flush = lambda: None
    ws = run_connection(host, [{"type": "start", "mode": "hold"}, {"type": "end"}], idle_limit=60)
    assert "transcript" in ws.kinds and ws.json("transcript")[0]["text"] == ""
    assert host.brains.heard == []


def test_a_typed_sentence_is_the_same_turn_without_the_stt():
    host = FakeHost()
    ws = run_connection(host, [{"type": "text", "text": "  what time is it  "}])
    assert host.brains.heard == [("what time is it", "remote")]
    assert "transcript" not in ws.kinds and ws.kinds[-1] == "done" and "audio_end" in ws.kinds


def test_dictate_and_correct_returns_only_the_transcript():
    host = FakeHost()
    ws = run_connection(host, [{"type": "start", "mode": "hold", "transcribe_only": True}, FRAME, {"type": "end"}])
    assert ws.json("transcript")[0]["text"] == "how are you doing today"
    assert host.brains.heard == [] and host.tts.texts == []


def test_the_brain_and_the_personality_are_chosen_by_the_phone_and_confirmed():
    host = FakeHost()
    ws = run_connection(host, [{"type": "brain", "brain": "harness"}, {"type": "persona", "persona": "bella"}, {"type": "persona", "persona": "../x"}, CLOSED])
    assert host.brains.mode == "harness"
    assert ws.json("brain") == [{"type": "brain", "brain": "harness"}]
    assert [m["persona"] for m in ws.json("persona")] == ["bella", ""]
    assert host.brains.personas == ["bella", None]                               # a bad name selects nothing


def test_an_unknown_brain_changes_nothing():
    host = FakeHost()
    run_connection(host, [{"type": "brain", "brain": "hermes"}, CLOSED])
    assert host.brains.mode == "direct"


def test_the_functions_not_loaded_on_the_pc_are_reported_and_the_turn_is_not_started():
    host = FakeHost(stt=False)
    ws = run_connection(host, hold_turn())
    assert ws.json("ready")[0]["voice"] is False and ws.json("ready")[0]["missing"] == ["stt"]
    err = ws.json("error")[0]
    assert err["code"] == "not_loaded" and err["missing"] == ["stt"]
    assert ws.kinds[-1] == "done" and host.stt.frames == [] and host.brains.heard == []


def test_a_change_in_the_loaded_functions_is_pushed_to_the_phone_and_the_next_turn_works():
    host = FakeHost(stt=False, tts=False, avatar=False)
    ws = FakeWs([{"type": "auth", "token": TOKEN}], idle_limit=150)
    steps = iter([lambda: host.fn.update(stt=True, tts=True), lambda: host.fn.update(avatar=True)])
    state = {"n": 0}

    def on_idle():
        state["n"] += 1
        if state["n"] in (5, 30):
            next(steps)()
        if state["n"] == 60:
            for m in hold_turn():
                ws.push(m)

    ws.on_idle = on_idle
    vg.Connection(ws, host, authed=True).run()
    states = ws.json("state")
    assert [(s["voice"], s["avatar"]) for s in states] == [(True, False), (True, True)]
    assert "audio_end" in ws.kinds and host.brains.heard


# ---------------------------------------------------------------------------------------- interruption
def test_the_phone_interrupts_the_reply_while_it_plays():
    host = FakeHost()
    host.tts = FakeTts(segments=12)
    ws = FakeWs([{"type": "auth", "token": TOKEN}, *hold_turn()])
    seen = {"pcm": 0}

    def on_send(data):
        if isinstance(data, bytes):
            seen["pcm"] += 1
            if seen["pcm"] == 2:
                ws.push({"type": "interrupt"})

    ws.on_send = on_send
    vg.Connection(ws, host, authed=True).run()
    kinds = ws.kinds
    assert kinds.count("<pcm>") < 12
    assert host.tts.aborted == 1
    assert kinds.index("audio_end") < kinds.index("interrupt") < kinds.index("done")


def test_pressing_the_button_while_the_reply_plays_interrupts_it_and_starts_the_next_turn():
    host = FakeHost()
    host.tts = FakeTts(segments=12)
    ws = FakeWs([{"type": "auth", "token": TOKEN}, *hold_turn()])
    seen = {"pcm": 0}

    def on_send(data):
        if isinstance(data, bytes):
            seen["pcm"] += 1
            if seen["pcm"] == 2:
                for m in hold_turn():
                    ws.push(m)

    ws.on_send = on_send
    vg.Connection(ws, host, authed=True).run()
    assert "interrupt" in ws.kinds
    assert len(host.brains.heard) == 2                                           # the start that interrupted was not lost
    assert ws.kinds.count("done") == 2


def test_the_phone_interrupts_while_the_brain_is_still_writing():
    host = FakeHost()
    ws = FakeWs([{"type": "auth", "token": TOKEN}, *hold_turn()])

    def respond_stream(text, source="mic"):
        yield "First part of the answer. "
        ws.push({"type": "interrupt"})
        yield "Second part that nobody will hear. "
        yield "Third part. "

    host.brains.respond_stream = respond_stream
    vg.Connection(ws, host, authed=True).run()
    assert "interrupt" in ws.kinds and ws.kinds[-1] == "done"
    assert "audio_start" not in ws.kinds and host.tts.aborted == 1


# ---------------------------------------------------------------------------------------- full duplex
def test_a_full_duplex_session_answers_each_utterance_and_stops_on_stop():
    host = FakeHost()
    ws = FakeWs([{"type": "auth", "token": TOKEN}, {"type": "start", "duplex": True, "lang": "en", "sample_rate": 16000}, FRAME, FRAME], idle_limit=200)
    done = {"said": False}

    def on_idle():
        if not done["said"] and len(host.stt.frames) >= 2:
            done["said"] = True
            host.stt.utterances.put("please tell me the weather")
        if "turn_done" in ws.kinds and not done.get("stopped"):
            done["stopped"] = True
            ws.push({"type": "stop"})

    ws.on_idle = on_idle
    vg.Connection(ws, host, authed=True).run()
    kinds = ws.kinds
    assert kinds.index("session_started") < kinds.index("transcript") < kinds.index("audio_start") < kinds.index("turn_done")
    assert host.brains.heard == [("please tell me the weather", "remote")]
    assert kinds[-1] == "done"


def test_a_real_phrase_during_a_full_duplex_answer_interrupts_it_but_the_echo_does_not():
    host = FakeHost(reply="The forecast says rain tomorrow morning. ")
    host.tts = FakeTts(segments=30)
    ws = FakeWs([{"type": "auth", "token": TOKEN}, {"type": "start", "duplex": True, "lang": "en", "sample_rate": 16000}, FRAME], idle_limit=200)
    state = {"said": False, "pcm": 0}

    def on_idle():
        if not state["said"] and host.stt.frames:
            state["said"] = True
            host.stt.utterances.put("what is the forecast")
        if ("interrupt" in ws.kinds or "turn_done" in ws.kinds) and not state.get("stopped"):
            state["stopped"] = True
            ws.push({"type": "stop"})

    def on_send(data):
        if isinstance(data, bytes):
            state["pcm"] += 1
            if state["pcm"] == 2:
                host.stt.utterances.put("the forecast says rain tomorrow")             # its own voice coming back: not an interruption
            if state["pcm"] == 4:
                host.stt.utterances.put("stop right there please")                   # a real interruption

    ws.on_idle, ws.on_send = on_idle, on_send
    vg.Connection(ws, host, authed=True).run()
    assert "interrupt" in ws.kinds
    assert host.tts.aborted >= 1


def test_the_echo_of_the_avatars_own_voice_does_not_interrupt_a_full_duplex_answer():
    host = FakeHost(reply="The forecast says rain tomorrow morning. ")
    host.tts = FakeTts(segments=10)
    ws = FakeWs([{"type": "auth", "token": TOKEN}, {"type": "start", "duplex": True, "lang": "en", "sample_rate": 16000}, FRAME], idle_limit=200)
    state = {"said": False, "pcm": 0}

    def on_idle():
        if not state["said"] and host.stt.frames:
            state["said"] = True
            host.stt.utterances.put("what is the forecast")
        if "turn_done" in ws.kinds and not state.get("stopped"):
            state["stopped"] = True
            ws.push({"type": "stop"})

    def on_send(data):
        if isinstance(data, bytes):
            state["pcm"] += 1
            if state["pcm"] == 2:
                host.stt.utterances.put("the forecast says rain tomorrow")

    ws.on_idle, ws.on_send = on_idle, on_send
    vg.Connection(ws, host, authed=True).run()
    assert "turn_done" in ws.kinds and "interrupt" not in ws.kinds and host.tts.aborted == 0


# ---------------------------------------------------------------------------------------- video
def test_video_start_without_an_avatar_is_answered_no_avatar_and_the_connection_stays_in_voice():
    host = FakeHost(avatar=False)
    ws = FakeWs([{"type": "auth", "token": TOKEN}, {"type": "video_start", "sdp": "v=0", "persona": "Shan", "reason": "emergency"}])
    deadline = time.time() + 5

    def on_idle():
        if ws.json("video_unavailable") and not ws.inbox and not getattr(ws, "_turn", False):
            ws._turn = True
            for m in hold_turn():
                ws.push(m)
        if time.time() > deadline:
            ws.push(CLOSED)

    ws.on_idle = on_idle
    vg.Connection(ws, host, authed=True).run()
    assert ws.json("video_unavailable") == [{"type": "video_unavailable", "reason": "no_avatar"}]
    assert "video_answer" not in ws.kinds and host.calls == []
    assert "audio_end" in ws.kinds                                             # the voice still works


def test_video_start_with_an_avatar_negotiates_then_reports_live_and_video_stop_gives_the_frames_back(monkeypatch):
    from callbridge import avatar_webrtc

    class FakeFrames:
        port = 4242
        closed = False

        def close(self):
            FakeFrames.closed = True

    class FakeSession:
        def __init__(self, frames, audio=True):
            self.audio_attivo = audio
            self.closed = False

        def apri(self, sdp, kind):
            return "answer:" + sdp

        def scrivi_audio(self, samples, rate):
            return False

        def svuota_audio(self):
            pass

        def chiudi(self):
            self.closed = True

    monkeypatch.setattr(avatar_webrtc, "FrameServer", FakeFrames)
    monkeypatch.setattr(avatar_webrtc, "SessioneVideo", FakeSession)
    host = FakeHost(avatar=True)
    host.answers = {("GET", "/avatar/state"): (200, {"running": True, "persona": "Shan"}),
                    ("GET", "/personas"): (200, {"personas": [{"name": "Shan", "voice": {"engine": "xtts", "voice": "Shan Voice"}}]})}
    ws = FakeWs([{"type": "auth", "token": TOKEN}, {"type": "video_start", "sdp": "offer-sdp", "persona": "Shan", "brain": "harness", "reason": "emergency"}], idle_limit=300)

    def on_idle():
        if ws.json("video_state") and not getattr(ws, "_stopped", False):
            ws._stopped = True
            ws.push({"type": "video_stop"})

    ws.on_idle = on_idle
    vg.Connection(ws, host, authed=True).run()
    assert ws.json("video_answer")[0]["sdp"] == "answer:offer-sdp"
    assert ws.json("video_state")[-1]["state"] == "live"
    assert ("POST", "/avatar/start", {"persona": "Shan"}) in host.calls
    assert ("POST", "/avatar/stream", {"on": True, "port": 4242}) in host.calls
    assert ("POST", "/avatar/stream", {"on": False}) in host.calls
    assert "video_stopped" in ws.kinds and host.brains.mode == "harness" and FakeFrames.closed


# ---------------------------------------------------------------------------------------- the listening side
@pytest.fixture
def listening():
    host = FakeHost()
    server = gateway.Gateway(host)
    port = server.start()
    yield types.SimpleNamespace(host=host, server=server, port=port, url=f"ws://127.0.0.1:{port}{gateway.PATH}")
    server.stop()


def _open(env, token=TOKEN, path=gateway.PATH):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return connect(f"ws://127.0.0.1:{env.port}{path}", additional_headers=headers, open_timeout=5)


def test_the_gateway_listens_on_loopback_only():
    with pytest.raises(ValueError):
        gateway.Gateway(FakeHost(), bind="0.0.0.0")
    with pytest.raises(ValueError):
        gateway.Gateway(FakeHost(), bind="100.64.0.1")


def test_a_handshake_with_a_wrong_token_or_a_wrong_path_never_becomes_a_connection(listening):
    with pytest.raises(InvalidStatus) as e:
        _open(listening, token="wrong")
    assert e.value.response.status_code == 401
    with pytest.raises(InvalidStatus) as e:
        _open(listening, token="")
    assert e.value.response.status_code == 401
    with pytest.raises(InvalidStatus) as e:
        _open(listening, path="/other")
    assert e.value.response.status_code == 404
    assert listening.server.info()["clients"] == 0


def test_without_a_token_in_the_handshake_the_first_message_must_carry_it(listening):
    with _open(listening, token=None) as ws:
        ws.send(json.dumps({"type": "auth", "token": "wrong"}))
        assert json.loads(ws.recv(timeout=5))["code"] == "auth_failed"
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=5)
    with _open(listening, token=None) as ws:
        ws.send(json.dumps({"type": "auth", "token": TOKEN}))
        assert json.loads(ws.recv(timeout=5))["type"] == "auth_ok"


def _talk(ws):
    ws.send(json.dumps({"type": "auth"}))
    assert json.loads(ws.recv(timeout=5))["type"] == "auth_ok"
    assert json.loads(ws.recv(timeout=5))["type"] == "ready"
    ws.send(json.dumps({"type": "text", "text": "hello there"}))
    got = []
    while True:
        m = ws.recv(timeout=5)
        got.append("<pcm>" if isinstance(m, bytes) else json.loads(m)["type"])
        if got[-1] == "done":
            return got


def test_a_whole_turn_over_a_real_loopback_connection(listening):
    with _open(listening) as ws:
        got = _talk(ws)
    assert got[0] == "reply" and "audio_start" in got and "<pcm>" in got and got[-1] == "done"
    assert listening.host.brains.heard == [("hello there", "remote")]


def test_a_phone_that_drops_can_connect_again_and_talk(listening):
    with _open(listening) as ws:
        _talk(ws)
        ws.close()
    deadline = time.time() + 5
    while listening.server.info()["clients"] and time.time() < deadline:
        time.sleep(0.02)
    assert listening.server.info()["clients"] == 0
    with _open(listening) as ws:
        assert _talk(ws)[-1] == "done"
    assert len(listening.host.brains.heard) == 2


def test_a_phone_that_drops_in_the_middle_of_a_reply_leaves_nothing_behind(listening):
    listening.host.tts = FakeTts(segments=200)
    with _open(listening) as ws:
        ws.send(json.dumps({"type": "auth"}))
        ws.recv(timeout=5)
        ws.recv(timeout=5)
        ws.send(json.dumps({"type": "text", "text": "tell me a long story"}))
        while not isinstance(ws.recv(timeout=5), bytes):
            pass
    deadline = time.time() + 5
    while listening.server.info()["clients"] and time.time() < deadline:
        time.sleep(0.02)
    assert listening.server.info()["clients"] == 0
    assert listening.host.tts.closed and listening.host.stt.closed


# ---------------------------------------------------------------------------------------- in the service
@pytest.fixture
def svc(tmp_path):
    settings = tmp_path / "voice.json"
    settings.write_text(json.dumps({"models_dir": str(tmp_path / "models")}), encoding="utf-8")
    app, state = service.build_app(settings, TOKEN)
    return types.SimpleNamespace(app=app, state=state, client=app.test_client())


def test_the_service_has_a_gateway_that_is_not_listening_until_it_is_started_and_reports_itself(svc):
    auth = {"Authorization": "Bearer " + TOKEN}
    assert svc.client.get("/gateway").status_code == 401
    info = svc.client.get("/gateway", headers=auth).get_json()
    assert info["port"] == 0 and info["host"] == "127.0.0.1" and info["voice"] is False and info["avatar"] is False
    assert info["render_modes"] == ["wav2lip", "visemes", "video"]
    assert svc.client.get("/status", headers=auth).get_json()["gateway"]["host"] == "127.0.0.1"
    assert svc.state.config.stt_backend == "native" and svc.state.config.tts_backend == "native"


def test_a_service_without_the_gateway_says_so(tmp_path):
    settings = tmp_path / "voice.json"
    settings.write_text("{}", encoding="utf-8")
    app, state = service.build_app(settings, TOKEN, with_gateway=False)
    assert state.gateway is None
    assert app.test_client().get("/status", headers={"Authorization": "Bearer " + TOKEN}).get_json()["gateway"] is None


def test_the_service_gateway_in_standby_accepts_the_phone_and_says_what_is_not_loaded(svc):
    server = svc.state.gateway
    port = server.start()
    try:
        with connect(f"ws://127.0.0.1:{port}{gateway.PATH}", additional_headers={"Authorization": f"Bearer {TOKEN}"}, open_timeout=5) as ws:
            ws.send(json.dumps({"type": "auth"}))
            assert json.loads(ws.recv(timeout=5))["type"] == "auth_ok"
            ready = json.loads(ws.recv(timeout=5))
            assert ready["voice"] is False and ready["missing"] == ["stt", "tts"] and ready["avatar"] is False
            ws.send(json.dumps({"type": "start", "mode": "hold"}))
            err = json.loads(ws.recv(timeout=5))
            assert err["code"] == "not_loaded"
            assert json.loads(ws.recv(timeout=5))["type"] == "done"
            ws.send(json.dumps({"type": "video_start", "sdp": "v=0", "reason": "emergency"}))
            assert json.loads(ws.recv(timeout=5)) == {"type": "video_unavailable", "reason": "no_avatar"}
        assert svc.client.get("/gateway", headers={"Authorization": "Bearer " + TOKEN}).get_json()["port"] == port
    finally:
        server.stop()


def test_the_host_makes_native_clients_over_the_engines_of_the_service_and_starts_them(svc, monkeypatch):
    from callbridge import engines
    made = {}

    class Stt:
        def __init__(self, engine, **kw):
            made["stt"] = (engine, kw)

        def start(self):
            made["stt_started"] = True

    class Tts:
        def __init__(self, engine, cfg):
            made["tts"] = (engine, cfg)

        def start(self):
            made["tts_started"] = True

    monkeypatch.setattr(engines, "NativeSttClient", Stt)
    monkeypatch.setattr(engines, "NativeTtsClient", Tts)
    monkeypatch.setattr(engines, "_get_stt_engine", lambda cfg: "the-stt-engine")
    monkeypatch.setattr(engines, "_get_tts_engine", lambda cfg: "the-tts-router")
    host = gateway.Host(svc.state, TOKEN)
    host.stt_client("en")
    host.tts_client()
    assert made["stt"][0] == "the-stt-engine" and made["stt"][1]["lang"] == "en" and made["stt_started"]
    assert made["tts"] == ("the-tts-router", svc.state.config) and made["tts_started"]
    assert host.functions() == {"stt": False, "tts": False, "avatar": False}


def test_the_hosts_control_calls_carry_the_control_token_and_never_raise(svc, monkeypatch):
    import requests
    seen = {}

    class Answer:
        status_code = 200

        def json(self):
            return {"running": True}

    def fake(method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return Answer()

    monkeypatch.setattr(requests, "request", fake)
    svc.state.config.control_port = 4321
    host = gateway.Host(svc.state, TOKEN)
    assert host.control("GET", "/avatar/state") == (200, {"running": True})
    assert seen["url"] == "http://127.0.0.1:4321/avatar/state" and seen["headers"] == {"Authorization": "Bearer " + TOKEN} and seen["json"] is None

    def down(method, url, **kw):
        raise requests.ConnectionError("no")

    monkeypatch.setattr(requests, "request", down)
    assert host.control("POST", "/avatar/stream", {"on": False}) == (0, {})
