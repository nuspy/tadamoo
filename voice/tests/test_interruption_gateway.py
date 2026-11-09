"""Speech over the avatar through a connection driven by a fake WebSocket: the messages the phone sends (`user_speaking`, `start` with `barge_in`,
`end`, `resume`) and the ones the PC answers (`duck`, `reply_reset`, `interrupt`, `resume_offer`), in order, for every outcome.

No model, no microphone, no phone: the brain, the STT, the TTS and the technical session are in-process stand-ins and the clock is a list."""
import collections
import json
import time
import types

import pytest
from flask import Flask

from callbridge import interruption as im
from callbridge import voice_gateway as vg
from voice import gateway as gateway_module
from voice.tests.test_gateway import FRAME, TOKEN, FakeHost, FakeStt, FakeTts, FakeWs
from voice.tests.fakes import FakeBrain

ANSWER = "The capital of France is Paris. It sits on the Seine. It has two million people."
ASK = "tell me about Paris"
GREY = "and what about the weather there tomorrow"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Judge:
    def __init__(self, reply=None, error=None, delay=0.0):
        self.reply, self.error, self.delay, self.calls = reply, error, delay, []

    def __call__(self, system, user, timeout_s):
        self.calls.append(user)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def verdict(action, relation="new", urgency="normal"):
    return {"relation": relation, "urgency": urgency, "action": action}


class Brain(FakeBrain):
    """Answers each turn with the next reply of its list (the last one is repeated) and keeps who was asked."""

    def __init__(self, *replies):
        super().__init__(replies[0] if replies else "ok. ")
        self.replies = collections.deque(replies)
        self.mode = "direct"

    def respond_stream(self, text, source="mic", context_note=""):
        self.heard.append((text, source))
        if context_note:
            self.notes.append(context_note)
        yield self.replies.popleft() if len(self.replies) > 1 else self.replies[0]

    def set_mode(self, mode):
        self.mode = mode
        return True


class Host(FakeHost):
    def __init__(self, judge=None, replies=(ANSWER + " ", "It will be sunny. "), stt_text=GREY, **cfg):
        super().__init__(**{})
        self.brains = Brain(*replies)
        self.tts = FakeTts(segments=1, samples=48000)           # two seconds a sentence, six frames
        self.stt = FakeStt(stt_text)
        self.interruption_judge = judge
        self.interruption_clock = Clock()
        self.interruption_log = collections.deque(maxlen=20)
        for k, v in cfg.items():
            setattr(self.cfg, k, v)


BARGE = [{"type": "user_speaking", "state": "start", "level": 0.2, "ms": 0},
         {"type": "start", "mode": "hold", "lang": "en", "sample_rate": 16000, "barge_in": True}, FRAME, FRAME,
         {"type": "user_speaking", "state": "end", "level": 0.3, "ms": 1800}, {"type": "end"}]


class Run:
    """A connection whose phone asks something, then speaks over the answer: while the PCM goes out, or after the answer has been sent."""

    def __init__(self, host, during=True, idle_limit=40, slow=True, lang="en"):
        self.host = host
        self.ws = FakeWs([{"type": "auth", "token": TOKEN}, {"type": "text", "text": ASK, "lang": lang}], idle_limit=idle_limit)
        self.fired = False
        self.pcm = 0

        def fire():
            if not self.fired:
                self.fired = True
                for m in BARGE:
                    self.ws.push({**m, "lang": lang} if isinstance(m, dict) and m["type"] == "start" else m)

        def on_send(data):
            if isinstance(data, bytes):
                self.pcm += 1
                if slow:
                    time.sleep(0.001)
                if during and self.pcm == 2:
                    fire()
            elif not during and json.loads(data)["type"] == "done":
                fire()

        self.ws.on_send = on_send

    def play(self):
        vg.Connection(self.ws, self.host, authed=True).run()
        return self.ws

    def events(self):
        return [json.loads(m) for m in self.ws.sent if isinstance(m, str)]

    def kinds(self):
        return [e["type"] for e in self.events()]


# ---------------------------------------------------------------------------------------- ducking and the rules
@pytest.mark.parametrize("during", [True, False])
def test_the_voice_is_lowered_at_once_and_raised_again_for_a_nod_and_the_answer_goes_on(during):
    host = Host(judge=Judge(verdict("stop_and_answer_now")), stt_text="ok")
    run = Run(host, during=during)
    run.play()
    kinds, events = run.kinds(), run.events()
    ducks = [e["level"] for e in events if e["type"] == "duck"]
    assert ducks == [0.3, 1.0]
    assert "interrupt" not in kinds and host.brains.heard == [(ASK, "remote")]
    assert kinds.count("done") == 2                                        # the answer's and the one of the speech over it
    assert run.pcm >= 18 and host.tts.aborted == 0                         # all the audio went out
    assert host.interruption_judge.calls == []                             # a nod never reaches the model
    assert len(host.stt.frames) == 2                                       # the speech over the avatar went to the STT


def test_the_duck_comes_before_anything_else_the_phone_hears_about_the_speech():
    host = Host(judge=Judge(verdict("finish_then_answer")), stt_text="ok")
    run = Run(host)
    run.play()
    kinds = run.kinds()
    assert kinds.index("duck") < kinds.index("done")
    assert kinds.index("audio_start") < kinds.index("duck")


def test_the_duck_level_is_the_persons_setting():
    host = Host(stt_text="ok", interruption_duck_level=0.5)
    run = Run(host)
    run.play()
    assert [e["level"] for e in run.events() if e["type"] == "duck"][0] == 0.5


def test_nothing_is_ducked_when_the_avatar_is_not_speaking():
    host = Host()
    ws = FakeWs([{"type": "auth", "token": TOKEN}, {"type": "user_speaking", "state": "start", "level": 0.2, "ms": 0}], idle_limit=40)
    vg.Connection(ws, host, authed=True).run()
    assert "duck" not in ws.kinds


def test_a_stop_word_stops_the_answer_at_once_without_the_model_and_nothing_is_offered_afterwards():
    host = Host(judge=Judge(verdict("finish_then_answer")), stt_text="basta")
    run = Run(host)
    run.play()
    kinds = run.kinds()
    assert host.interruption_judge.calls == []
    assert kinds.index("interrupt") < kinds.index("transcript")
    assert host.tts.aborted >= 1
    assert "resume_offer" not in kinds
    assert run.host.interruption_log[-1]["source"] == "rules" and run.host.interruption_log[-1]["action"] == "stop_and_answer_now"


# ---------------------------------------------------------------------------------------- the three outcomes of the grey zone
def test_finish_then_answer_lets_the_answer_end_then_answers_the_new_request_without_cutting():
    host = Host(judge=Judge(verdict("finish_then_answer", "new", "low")))
    run = Run(host)
    run.play()
    kinds, events = run.kinds(), run.events()
    assert "interrupt" not in kinds and host.tts.aborted == 0
    assert host.brains.heard == [(ASK, "remote"), (GREY, "remote")] and host.brains.notes == []
    old_done, heard = kinds.index("done"), kinds.index("transcript")
    assert old_done < heard and [e["text"] for e in events if e["type"] == "transcript"] == [GREY]
    assert kinds.count("done") == 2
    assert [e["level"] for e in events if e["type"] == "duck"] == [0.3, 1.0]


def test_stop_and_answer_now_cuts_the_answer_and_answers_at_once_in_the_right_order():
    host = Host(judge=Judge(verdict("stop_and_answer_now", "new", "high")))
    run = Run(host)
    run.play()
    kinds, events = run.kinds(), run.events()
    assert kinds.index("duck") < kinds.index("reply_reset") < kinds.index("interrupt") < kinds.index("transcript")
    assert host.tts.aborted >= 1
    assert host.brains.heard == [(ASK, "remote"), (GREY, "remote")] and host.brains.notes == []
    assert kinds.count("interrupt") == 1 and kinds.count("done") == 2
    assert [e["level"] for e in events if e["type"] == "duck"][-1] == 1.0         # the new answer is not played ducked
    cut = kinds.index("interrupt")
    assert kinds.index("duck", cut) < kinds.index("transcript")                    # the volume is back before the new answer begins


def test_stop_and_integrate_cuts_and_the_new_answer_is_written_with_what_was_said_and_what_was_added():
    host = Host(judge=Judge(verdict("stop_and_integrate", "related", "normal")), stt_text="make it about Lyon", replies=(ANSWER + " ", "Lyon sits on two rivers. "))
    run = Run(host)
    run.play()
    kinds = run.kinds()
    assert kinds.index("interrupt") < kinds.index("transcript")
    assert len(host.brains.notes) == 1
    note = host.brains.notes[0]
    assert note.startswith("You were saying: ") and "The person interrupted with: make it about Lyon." in note
    assert note.endswith("Continue your answer taking it into account.")
    assert host.brains.heard[-1] == ("make it about Lyon", "remote")
    assert "resume_offer" not in kinds                                         # the new answer carries the old one: nothing left to offer


def test_the_note_is_for_this_turn_only():
    host = Host(judge=Judge(verdict("stop_and_integrate", "related")), stt_text="make it about Lyon")
    run = Run(host)
    run.play()
    run.ws = FakeWs([{"type": "auth", "token": TOKEN}, {"type": "text", "text": "hello again"}], idle_limit=40)
    vg.Connection(run.ws, host, authed=True).run()
    assert len(host.brains.notes) == 1                                         # the second connection's turn had none


def test_an_agent_brain_does_not_integrate_it_answers_at_once_instead():
    host = Host(judge=Judge(verdict("stop_and_integrate", "related")), stt_text="make it about Lyon")
    host.brains.mode = "harness"
    run = Run(host)
    run.play()
    assert host.brains.notes == []
    assert host.interruption_log[-1]["action"] == "stop_and_answer_now" and "interrupt" in run.kinds()


# ---------------------------------------------------------------------------------------- the fallbacks
@pytest.mark.parametrize("judge", [Judge(verdict("stop_and_answer_now"), delay=1.5), Judge(error=ConnectionError("down")), Judge("not json"),
                                   Judge({"relation": "new", "urgency": "high", "action": "launch_missiles"}), None])
def test_when_the_technical_session_fails_the_avatar_finishes_and_answers_after_and_the_decision_says_degraded(judge):
    host = Host(judge=judge, interruption_judge_timeout_ms=200)
    run = Run(host)
    started = time.monotonic()
    run.play()
    assert time.monotonic() - started < 6
    kinds = run.kinds()
    assert "interrupt" not in kinds and host.brains.heard == [(ASK, "remote"), (GREY, "remote")]
    row = host.interruption_log[-1]
    assert row["action"] == "finish_then_answer" and row["degraded"] is True and row["source"] == "fallback"


def test_the_audio_keeps_going_while_the_judge_is_slow():
    host = Host(judge=Judge(verdict("stop_and_answer_now"), delay=0.5), interruption_judge_timeout_ms=3000)
    run = Run(host)
    run.play()
    assert run.pcm >= 18                                                        # the answer was sent whole while the judge thought
    assert "interrupt" in run.kinds()


def test_with_the_manager_off_a_start_while_the_reply_plays_stops_it_as_before():
    host = Host(judge=Judge(verdict("finish_then_answer")), interruptions_enabled=False)
    run = Run(host)
    run.play()
    kinds = run.kinds()
    assert "duck" not in kinds and "interrupt" in kinds and host.interruption_judge.calls == []


# ---------------------------------------------------------------------------------------- the offer to go on
def stopped(host=None, **kw):
    """A run where the avatar was stopped for a new request; a second of fake time passes at every idle look of the connection."""
    host = host or Host(judge=Judge(verdict("stop_and_answer_now", "new", "high")))
    run = Run(host, idle_limit=40, **kw)

    def on_idle():
        host.interruption_clock.t += 1.0

    run.ws.on_idle = on_idle
    return host, run


def test_after_the_new_answer_and_two_seconds_of_quiet_the_avatar_offers_the_rest_in_the_language_of_the_conversation():
    host, run = stopped()
    run.play()
    events = run.events()
    kinds = run.kinds()
    offers = [e for e in events if e["type"] == "resume_offer"]
    assert len(offers) == 1 and offers[0]["text"] and offers[0]["lang"] == "en"
    assert offers[0]["question"] == "Do you want me to continue with what I was saying?"
    assert offers[0]["text"].split() != ANSWER.split()                          # only what was not spoken
    assert kinds.index("transcript") < kinds.index("resume_offer")              # after the new request was heard
    after = kinds[kinds.index("resume_offer"):]
    assert after.index("audio_start") < after.index("turn_done")               # the question is spoken, then the turn is over
    assert host.tts.texts[-1] == "Do you want me to continue with what I was saying?"


def test_the_offer_is_in_italian_when_the_conversation_is_in_italian():
    host, run = stopped(lang="it")
    run.play()
    assert [e["question"] for e in run.events() if e["type"] == "resume_offer" and e["text"]] == ["Vuoi che continui con quello che stavo dicendo?"]


def test_the_offer_is_not_made_while_the_person_keeps_talking_and_not_when_it_is_switched_off():
    host, run = stopped(Host(judge=Judge(verdict("stop_and_answer_now", "new", "high")), interruption_resume_offer=False))
    run.play()
    assert "resume_offer" not in run.kinds()


def test_accepting_by_tap_says_the_rest_without_asking_the_brain():
    host, run = stopped()
    original = run.ws.on_send

    def on_send(data):
        original(data)
        if isinstance(data, str) and json.loads(data)["type"] == "resume_offer":
            run.ws.push({"type": "resume", "accept": True})

    run.ws.on_send = on_send
    run.play()
    kinds = run.kinds()
    offer = [e for e in run.events() if e["type"] == "resume_offer"][0]
    assert host.tts.texts[-1] == offer["text"]                                  # the rest, as it was written
    assert host.brains.heard == [(ASK, "remote"), (GREY, "remote")]            # the brain was not asked again
    assert kinds.count("resume_offer") == 1 and kinds[-1] in ("turn_done", "done")


@pytest.mark.parametrize("words", ["yes please", "sì", "continua", "go on"])
def test_accepting_by_voice_in_a_turn_says_the_rest(words):
    host, run = stopped()
    original = run.ws.on_send

    def on_send(data):
        original(data)
        if isinstance(data, str) and json.loads(data)["type"] == "resume_offer":
            host.stt.said = words
            for m in (*[{"type": "start", "mode": "hold", "lang": "en", "sample_rate": 16000}], FRAME, {"type": "end"}):
                run.ws.push(m)

    run.ws.on_send = on_send
    run.play()
    offer = [e for e in run.events() if e["type"] == "resume_offer"][0]
    assert host.tts.texts[-1] == offer["text"]
    assert host.brains.heard == [(ASK, "remote"), (GREY, "remote")]            # "yes" was not sent to the brain as a question


def test_declining_by_tap_drops_the_rest_and_nothing_more_is_said():
    host, run = stopped()
    original = run.ws.on_send

    def on_send(data):
        original(data)
        if isinstance(data, str) and json.loads(data)["type"] == "resume_offer":
            run.ws.push({"type": "resume", "accept": False})

    run.ws.on_send = on_send
    run.play()
    assert host.tts.texts[-1] == "Do you want me to continue with what I was saying?"
    assert run.kinds().count("resume_offer") == 1


def test_an_offer_nobody_answers_is_taken_back_after_a_minute():
    host, run = stopped()
    original = run.ws.on_send

    def on_send(data):
        original(data)
        if isinstance(data, str) and json.loads(data)["type"] == "resume_offer":
            run.ws.on_idle = lambda: setattr(host.interruption_clock, "t", host.interruption_clock.t + 20.0)         # nobody answers

    run.ws.on_send = on_send
    run.play()
    offers = [e for e in run.events() if e["type"] == "resume_offer"]
    assert offers[0]["text"] and offers[-1] == {"type": "resume_offer", "text": ""} and len(offers) == 2


def test_two_connections_share_nothing_each_has_its_own_memory_and_offer():
    a, run_a = stopped()
    b = Host(judge=Judge(verdict("finish_then_answer")))
    run_b = Run(b)
    run_a.play()
    run_b.play()
    assert any(e["type"] == "resume_offer" for e in run_a.events())
    assert not any(e["type"] == "resume_offer" for e in run_b.events())
    assert "interrupt" in run_a.kinds() and "interrupt" not in run_b.kinds()


# ---------------------------------------------------------------------------------------- the record of the decisions
def test_the_decision_is_in_the_log_with_80_characters_of_the_words_and_the_route_serves_it():
    host = Host(judge=Judge(verdict("stop_and_answer_now", "new", "high")), stt_text=GREY + " " + "word " * 30)
    Run(host).play()
    row = host.interruption_log[-1]
    assert row["action"] == "stop_and_answer_now" and row["source"] == "model" and row["urgency"] == "high" and len(row["heard"]) <= 80

    app = Flask("test")
    fake = types.SimpleNamespace(host=host, info=lambda: {})
    gateway_module.register(app, fake)
    body = app.test_client().get("/interruptions").get_json()
    assert body["interruptions"] == list(host.interruption_log) and len(json.dumps(body)) < 4000


def test_the_log_line_names_the_decision_the_action_and_the_reason(caplog):
    host = Host(judge=Judge(verdict("finish_then_answer", "new", "low")))
    with caplog.at_level("INFO", logger="callbridge.interruption"):
        Run(host).play()
    line = [r.getMessage() for r in caplog.records if "interruption" in r.getMessage()][0]
    assert "decision=model" in line and "action=finish_then_answer" in line and "reason=new, low" in line


def test_the_service_gives_each_connection_the_judge_over_the_local_server():
    from callbridge.tadamoo_api import TadamooApi
    cfg = types.SimpleNamespace(tadamoo_base_url="http://127.0.0.1:7242", tadamoo_token="t")
    host = gateway_module.Host(types.SimpleNamespace(config=cfg), "token")
    assert isinstance(host.interruption_judge, im.TechnicalJudge) and isinstance(host.interruption_judge._api, TadamooApi)
    cfg.tadamoo_base_url = "http://example.com:80"
    assert host.interruption_judge is None                                      # never a remote address
