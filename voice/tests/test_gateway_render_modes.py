"""The render modes of the gateway: what a device declares, what the PC serves, the messages of each mode and their order, the interruption,
the video that is for an emergency only, and the chain wav2lip -> visemes -> video -> voice only. Fake WebSocket, fake STT, TTS and brain; the
device of the loopback test is voice/tests/fake_device.py, which speaks what the ESP32 firmware speaks."""
import json
import types

import numpy as np
import pytest

from callbridge import viseme_gen, voice_gateway as vg
from voice import gateway
from voice.tests.fake_device import FakeDevice, ProtocolError
from voice.tests.test_gateway import CLOSED, FRAME, TOKEN, FakeHost, FakeTts, FakeWs, hold_turn, run_connection

BOTH = {"avatar": "ada", "render_modes": ["wav2lip", "visemes"]}


def auth(*modes, device="android"):
    return {"type": "auth", "token": TOKEN, "render_modes": list(modes), "device": device}


def connect(host, first, script, **kw):
    ws = FakeWs([first, *script], **kw)
    vg.Connection(ws, host, authed=True).run()
    return ws


def host_with(info=BOTH, **functions):
    host = FakeHost(**functions)
    host.avatar_info = info
    return host


# ---------------------------------------------------------------------------------------- the declaration
def test_a_device_that_declares_nothing_gets_the_voice_only_and_the_pc_is_not_asked_about_avatars():
    host = host_with()
    ws = connect(host, {"type": "auth", "token": TOKEN}, hold_turn())
    ready = ws.json("ready")[0]
    assert (ready["render_mode"], ready["render_modes"], ready["avatar_name"]) == ("none", [], "")
    assert host.phone_calls == []
    assert "visemes" not in ws.kinds and "audio_end" in ws.kinds                    # the voice still works


def test_ready_answers_which_modes_the_pc_serves_in_the_order_of_the_device_with_the_video_last():
    ws = connect(host_with(avatar=True), auth("video", "visemes", "wav2lip"), [CLOSED])
    ready = ws.json("ready")[0]
    assert ready["render_modes"] == ["visemes", "wav2lip", "video"] and ready["render_mode"] == "visemes" and ready["avatar_name"] == "ada"
    ws = connect(host_with(), auth("visemes", "wav2lip"), [CLOSED])
    assert ws.json("ready")[0]["render_modes"] == ["visemes", "wav2lip"] and ws.json("ready")[0]["render_mode"] == "visemes"


def test_only_what_the_avatar_files_allow_is_offered_and_unknown_or_repeated_modes_are_ignored():
    ws = connect(host_with({"avatar": "ada", "render_modes": ["visemes"]}), auth("wav2lip", "bogus", "visemes", "visemes", 7), [CLOSED])
    assert ws.json("ready")[0]["render_modes"] == ["visemes"]
    ws = connect(host_with({"avatar": "", "render_modes": []}), auth("wav2lip", "visemes"), [CLOSED])
    assert (ws.json("ready")[0]["render_mode"], ws.json("ready")[0]["render_modes"]) == ("none", [])


def test_the_video_is_offered_only_while_the_avatar_function_is_loaded():
    assert connect(host_with(avatar=False), auth("video"), [CLOSED]).json("ready")[0]["render_modes"] == []
    assert connect(host_with({"avatar": "", "render_modes": []}, avatar=True), auth("video"), [CLOSED]).json("ready")[0]["render_modes"] == ["video"]


# ---------------------------------------------------------------------------------------- wav2lip
def test_wav2lip_is_audio_only_no_timeline_no_video_the_phone_draws_the_lips_itself():
    host = host_with(avatar=True)
    ws = connect(host, auth("wav2lip", "visemes"), hold_turn())
    assert ws.json("ready")[0]["render_mode"] == "wav2lip"
    assert "visemes" not in ws.kinds and "video_answer" not in ws.kinds
    assert ws.kinds.index("audio_start") < ws.kinds.index("<pcm>") < ws.kinds.index("audio_end")
    assert ws.json("audio_start")[0]["segment"] == 1 and not [c for c in host.calls if c[1].startswith("/avatar/lipsync")]


# ---------------------------------------------------------------------------------------- visemes
def test_visemes_mode_sends_a_timeline_before_the_audio_of_every_segment_with_the_segment_number():
    host = host_with()
    host.tts = FakeTts(segments=3, samples=4800)                                   # 0.2 s at 24 kHz, three segments
    ws = connect(host, auth("visemes"), hold_turn())
    kinds = ws.kinds
    assert kinds.index("audio_start") < kinds.index("visemes") < kinds.index("<pcm>")
    assert ws.json("audio_start")[0]["segment"] == 1
    timelines = ws.json("visemes")
    assert [t["segment"] for t in timelines] == [1, 2, 3]
    for t in timelines:
        assert t["type"] == "visemes" and t["text"] and t["duration_ms"] == 200 and t["pcm_bytes"] == 4800 * 2
        assert t["visemes"][0]["t"] == 0 and all(set(v) == {"t", "v", "dur"} and v["v"] in viseme_gen.VISEMI for v in t["visemes"])
        assert abs(sum(v["dur"] for v in t["visemes"]) - t["duration_ms"]) <= 12
    for i, k in enumerate(kinds):
        if k == "<pcm>":
            assert "visemes" in kinds[:i]                                          # no PCM before the first timeline
        if k == "visemes":
            assert kinds[i + 1] == "<pcm>"                                         # every timeline is followed by its own audio
    assert kinds[-1] == "done"


def test_a_segment_that_is_cut_by_an_interrupt_sends_no_timeline_without_its_audio():
    host = host_with()
    host.tts = FakeTts(segments=12)
    ws = FakeWs([auth("visemes"), *hold_turn()])
    seen = {"pcm": 0}

    def on_send(data):
        if isinstance(data, bytes):
            seen["pcm"] += 1
            if seen["pcm"] == 2:
                ws.push({"type": "interrupt"})

    ws.on_send = on_send
    vg.Connection(ws, host, authed=True).run()
    kinds = ws.kinds
    assert 0 < len(ws.json("visemes")) < 12 and host.tts.aborted == 1
    for i, k in enumerate(kinds):
        if k == "visemes":
            assert kinds[i + 1] == "<pcm>"                                         # a timeline is always followed by its audio, never left alone
    assert kinds.index("audio_end") < kinds.index("interrupt") < kinds.index("done")


def test_an_interrupt_before_the_first_sound_sends_neither_audio_nor_timeline():
    host = host_with()
    ws = FakeWs([auth("visemes"), *hold_turn()])

    def respond_stream(text, source="mic"):
        yield "First part of the answer. "
        ws.push({"type": "interrupt"})
        yield "Second part nobody hears. "

    host.brains.respond_stream = respond_stream
    vg.Connection(ws, host, authed=True).run()
    assert "visemes" not in ws.kinds and "audio_start" not in ws.kinds and "interrupt" in ws.kinds


def test_the_timeline_follows_the_language_of_the_turn_and_a_failing_generator_sends_a_closed_mouth(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("no espeak")
    monkeypatch.setattr(viseme_gen, "timeline", broken)
    host = host_with()
    ws = connect(host, auth("visemes"), hold_turn())
    (t,) = ws.json("visemes")
    assert t["visemes"] == [{"t": 0, "v": "sil", "dur": t["duration_ms"]}] and t["segment"] == 1 and t["pcm_bytes"] > 0


# ---------------------------------------------------------------------------------------- the video: an emergency only
class FakeSession:
    def __init__(self, frames, audio=True):
        self.audio_attivo = audio

    def apri(self, sdp, kind):
        return "answer:" + sdp

    def scrivi_audio(self, samples, rate):
        return False

    def svuota_audio(self):
        pass

    def chiudi(self):
        pass


@pytest.fixture
def webrtc(monkeypatch):
    from callbridge import avatar_webrtc

    class Frames:
        port = 4242

        def close(self):
            pass

    monkeypatch.setattr(avatar_webrtc, "FrameServer", Frames)
    monkeypatch.setattr(avatar_webrtc, "SessioneVideo", FakeSession)


def _after_unavailable(ws, extra):
    state = {"sent": False}

    def on_idle():
        if (ws.json("video_unavailable") or ws.json("video_answer")) and not state["sent"]:
            state["sent"] = True
            for m in extra:
                ws.push(m)

    ws.on_idle = on_idle


def test_a_video_start_without_the_emergency_reason_is_refused_even_with_the_avatar_loaded(webrtc):
    host = host_with(avatar=True)
    for message in ({"type": "video_start", "sdp": "v=0"}, {"type": "video_start", "sdp": "v=0", "reason": "curiosity"}):
        ws = FakeWs([auth("video"), message], idle_limit=40)
        vg.Connection(ws, host, authed=True).run()
        assert ws.json("video_unavailable") == [{"type": "video_unavailable", "reason": "emergency_only"}] and "video_answer" not in ws.kinds
    assert host.calls == []


def test_an_emergency_video_needs_the_avatar_function_and_then_negotiates(webrtc):
    ws = FakeWs([auth("video"), {"type": "video_start", "sdp": "v=0", "reason": "emergency"}], idle_limit=40)
    vg.Connection(ws, host_with(avatar=False), authed=True).run()
    assert ws.json("video_unavailable") == [{"type": "video_unavailable", "reason": "no_avatar"}]
    host = host_with(avatar=True)
    host.answers = {("GET", "/avatar/state"): (200, {"running": True, "persona": "Shan"})}
    ws = FakeWs([auth("video"), {"type": "video_start", "sdp": "offer", "reason": "emergency"}], idle_limit=120)
    vg.Connection(ws, host, authed=True).run()
    assert ws.json("video_answer")[0]["sdp"] == "answer:offer"


def test_while_the_emergency_video_runs_no_timeline_is_sent(webrtc):
    host = host_with(avatar=True)
    host.answers = {("GET", "/avatar/state"): (200, {"running": True, "persona": ""})}
    ws = FakeWs([auth("visemes", "video"), {"type": "video_start", "sdp": "offer", "reason": "emergency"}], idle_limit=400)
    state = {"turn": False}

    def on_idle():
        if ws.json("video_state") and not state["turn"]:
            state["turn"] = True
            for m in hold_turn():
                ws.push(m)

    ws.on_idle = on_idle
    vg.Connection(ws, host, authed=True).run()
    assert "audio_end" in ws.kinds and "visemes" not in ws.kinds


# ---------------------------------------------------------------------------------------- the degradation chain
def test_a_failed_mode_is_answered_with_the_next_one_and_remembered_for_the_connection():
    host = host_with(avatar=True)
    ws = connect(host, auth("wav2lip", "visemes", "video"), [
        {"type": "render_failed", "mode": "wav2lip", "why": "ONNX did not load"},
        {"type": "render_failed", "mode": "visemes", "why": "no sprites"},
        {"type": "render_mode", "mode": "wav2lip"},                                   # asking for a mode that failed is refused
        {"type": "render_failed", "mode": "video", "why": "no WebRTC"},
        CLOSED])
    answers = ws.json("render_mode")
    assert [(a["previous"], a["mode"]) for a in answers] == [("wav2lip", "visemes"), ("visemes", "video"), ("video", "none")]
    assert answers[0]["why"] == "ONNX did not load" and answers[0]["render_modes"] == ["visemes", "video"] and answers[2]["render_modes"] == []
    assert [e["code"] for e in ws.json("error")] == ["render_mode_unavailable"]


def test_the_chain_ends_in_the_voice_only_when_the_video_function_is_not_loaded_and_the_next_turn_follows_the_new_mode():
    host = host_with(avatar=False)
    ws = connect(host, auth("wav2lip", "visemes", "video"), [{"type": "render_failed", "mode": "wav2lip", "why": "slow"}, *hold_turn(), CLOSED])
    assert ws.json("ready")[0]["render_modes"] == ["wav2lip", "visemes"]
    (answer,) = ws.json("render_mode")
    assert (answer["previous"], answer["mode"], answer["render_modes"]) == ("wav2lip", "visemes", ["visemes"])
    assert len(ws.json("visemes")) >= 1                                               # the turn after the failure is in the new mode
    ws = connect(host, auth("wav2lip", "visemes"), [{"type": "render_failed", "mode": "wav2lip", "why": "x"}, {"type": "render_failed", "mode": "visemes", "why": "y"}, *hold_turn(), CLOSED])
    assert [a["mode"] for a in ws.json("render_mode")] == ["visemes", "none"] and "visemes" not in ws.kinds and "audio_end" in ws.kinds


def test_the_device_may_choose_among_the_offered_modes_and_a_wrong_choice_is_an_error():
    ws = connect(host_with(), auth("wav2lip", "visemes"), [{"type": "render_mode", "mode": "visemes"}, {"type": "render_mode", "mode": "nonsense"},
                                                          {"type": "render_mode", "mode": "video"}, *hold_turn(), CLOSED])
    assert [(m["mode"], m["render_modes"]) for m in ws.json("render_mode")] == [("visemes", ["wav2lip", "visemes"])]
    assert [e["code"] for e in ws.json("error")] == ["render_mode_unavailable", "render_mode_unavailable"]
    assert len(ws.json("visemes")) >= 1


def test_choosing_a_personality_looks_at_its_avatar_again_and_tells_the_device_when_the_mode_changes():
    host = host_with()
    host.avatar_of = {"bella": {"avatar": "bella", "render_modes": ["visemes"]}, "nobody": {"avatar": "", "render_modes": []}}
    ws = connect(host, auth("wav2lip", "visemes"), [{"type": "persona", "persona": "bella"}, {"type": "persona", "persona": "nobody"}, CLOSED])
    assert ws.json("ready")[0]["render_mode"] == "wav2lip"
    assert [(m["mode"], m["avatar_name"]) for m in ws.json("render_mode")] == [("visemes", "bella"), ("none", "")]
    assert host.phone_calls == ["", "bella", "nobody"]


# ---------------------------------------------------------------------------------------- the ESP32 on a real loopback socket
@pytest.fixture
def served():
    host = host_with()
    host.tts = FakeTts(segments=3, samples=4800)
    server = gateway.Gateway(host)
    port = server.start()
    yield types.SimpleNamespace(host=host, url=f"ws://127.0.0.1:{port}{gateway.PATH}")
    server.stop()


def test_the_fake_esp32_follows_a_whole_reply_in_visemes_mode_and_gets_a_schedule_for_its_screen(served):
    with FakeDevice(served.url, TOKEN, render_modes=["visemes"]) as dev:
        ready = dev.open()
        assert (ready["render_mode"], ready["avatar_name"]) == ("visemes", "ada")
        dev.say("hello there")
        play = dev.listen("done")
    assert play.sample_rate == 24000 and play.audio_ended and not play.interrupted
    assert [s.segment for s in play.segments] == [1, 2, 3] and all(s.received == s.pcm_bytes for s in play.segments)
    when = [t for t, _v in play.schedule()]
    assert when == sorted(when) and when[0] == 0 and when[-1] < 600                  # three segments of 200 ms, one after the other
    assert all(v in viseme_gen.VISEMI for _t, v in play.schedule())


def test_the_fake_esp32_sends_its_microphone_as_pcm16_and_interrupts_in_the_middle_of_the_reply(served):
    served.host.tts = FakeTts(segments=40, samples=4800)
    pcm = bytes(FRAME) * 3
    with FakeDevice(served.url, TOKEN, render_modes=["visemes"]) as dev:
        dev.open()
        dev.speak(pcm, rate=16000)
        first = dev.listen("audio_start")

        def cut(play, _n):
            if sum(s.received for s in play.segments) > 9600 and not getattr(cut, "done", False):
                cut.done = True
                dev.interrupt()

        play = dev.listen("done", on_pcm=cut, play=first)
    assert play.interrupted and len(play.segments) < 40
    assert served.host.stt.frames and served.host.brains.heard == [("how are you doing today", "remote")]
    assert first.sample_rate == 24000


def test_the_fake_device_catches_a_gateway_that_breaks_the_protocol():
    ws = types.SimpleNamespace()
    dev = FakeDevice("ws://unused", TOKEN)
    frames = iter([json.dumps({"type": "audio_start", "sample_rate": 24000, "segment": 1}), b"\x00\x01\x02\x03"])
    ws.recv = lambda timeout=None: next(frames)
    dev.ws = ws
    with pytest.raises(ProtocolError, match="no viseme timeline"):
        dev.listen("done")
    frames = iter([b"\x00\x01"])
    dev.ws = types.SimpleNamespace(recv=lambda timeout=None: next(frames))
    with pytest.raises(ProtocolError, match="before audio_start"):
        dev.listen("done")
