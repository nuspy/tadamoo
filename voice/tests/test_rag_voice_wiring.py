"""The archives in the functions of the voice: the copilot (private window, consent only when a suggestion is said aloud), the meeting bot's spoken replies and its
minutes, phone calls, and the avatar's direct brain. Passages go at the tail of the prompt, never in the shared brain objects, never into the history, and are never
read out. All with fakes: no server, no device, no model."""
import json

import numpy as np
import pytest

from callbridge import analysis, copilot, rag_client
from callbridge.brains import DirectBrain, HarnessBrain, ModeBrain
from callbridge.call_service import CallService
from callbridge.config import Config
from callbridge.meetbot import speak
from callbridge.meetbot.summary import analyze_meeting
from callbridge.private_channel import MicSource, PrivateChannel
from callbridge.rag_client import Knowledge
from fakes import FakeApi
from meet_fakes import Brains, DirectBrain as FakeDirect, FakeSink, FakeStt, FakeTts, Rig, make_plan
from test_copilot_suggestions import Panel, cfg
from test_rag_client import BLOCK, PENDING, RagApi

def session(panel=None, brains=None, voice=None, knowledge=None):
    panel = panel or Panel()
    stt, source = FakeStt(), type("Src", (), {"start": lambda self, cb: None, "stop": lambda self: None})()
    reply = "CONTEXT: they ask for a date.\nREPLIES:\n- Friday.\n- Monday."
    s = copilot.CopilotSession(cfg(), brains or Brains(FakeDirect(reply)), panel, lambda lang: stt, lambda: source, voice=voice, knowledge=knowledge)
    return s, panel, stt, source


SRC = [{"archive_id": "boats-1", "archive": "Boats", "others_ok": "ask_dialog", "preview": "the keel keeps the boat upright"}]


def know(api=None, function="meeting", **kw):
    api = api or RagApi()
    k = Knowledge(Config(), function, ("boats-1",), session="s", api=api, spawn=lambda fn, kind: fn() if kind == "aloud" else k.jobs.append(fn), sleep=lambda s: None, **kw)
    k.jobs = []
    return k, api


# -- the copilot
def test_the_copilots_prompt_has_the_passages_at_the_tail_and_the_window_says_which_archives_were_used():
    k, api = know(function="copilot")
    api.sources = SRC
    s, panel, *_ = session(knowledge=k)
    entry = s.process("When can you deliver the report on the keel of the sloop?")
    prompt = s.brains.direct.prompts[0]
    assert prompt.endswith(BLOCK) and prompt.index("</other_party>") < prompt.index("<untrusted_data")                   # data, after the other party's words
    assert entry.notes == ["[archives: Boats]"] and entry.sources == SRC and api.sent(rag_client.PASSAGES)[0]["how"] == "screen"
    assert api.sent(rag_client.ASK) == []                                                                              # the private window never asks


def test_a_late_search_is_shown_in_the_window_and_the_suggestion_still_comes():
    k, api = know(function="copilot")
    api.replies = [{"block": "", "sources": [], "pending": [], "marker": "words only: the semantic search took more than 1200 ms"}]
    s, panel, *_ = session(knowledge=k)
    entry = s.process("When can you deliver the report on the keel?")
    assert entry.notes == ["[words only: the semantic search took more than 1200 ms]"] and "Friday." in entry.body and "<untrusted_data" not in s.brains.direct.prompts[0]


def test_without_archives_the_copilot_prompt_is_what_it_was():
    s, *_ = session()
    s.process("When can you deliver the report?")
    assert s.brains.direct.prompts[0] == copilot.prompt_for(s.cfg, 3, "When can you deliver the report?") and "<untrusted_data" not in s.brains.direct.prompts[0]


class Voice:
    def __init__(self):
        self.said, self.floor = [], None

    def block_reason(self):
        return ""

    def say(self, text, **kw):
        self.said.append(text)
        return True

    def close(self):
        pass


def wait_for(cond, seconds=2.0):
    import time
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_a_suggestion_that_used_an_archive_that_asks_is_said_only_after_the_person_says_yes():
    k, api = know(function="copilot")
    api.sources = SRC
    voice = Voice()
    s, panel, *_ = session(knowledge=k, voice=voice)
    s.process("When can you deliver the report on the keel of the sloop?")
    api.answers["q1"] = "meeting"
    assert s.speak("- Friday.") is True
    assert wait_for(lambda: voice.said) and voice.said == ["Friday."] and api.sent(rag_client.ASK)[0]["how"] == "spoken"
    voice.said.clear()
    s.speak("Monday.")
    assert wait_for(lambda: voice.said) and len(api.sent(rag_client.ASK)) == 1                                           # for this meeting: not asked again


def test_a_no_keeps_the_suggestion_unsaid_and_the_window_says_why():
    k, api = know(function="copilot")
    api.sources = SRC
    voice = Voice()
    s, panel, *_ = session(knowledge=k, voice=voice)
    s.process("When can you deliver the report on the keel of the sloop?")
    api.answers["q1"] = "no"
    s.speak("Friday.")
    assert wait_for(lambda: panel.notes) and voice.said == [] and "Boats" in panel.notes[0]
    k2, api2 = know(function="copilot")                                                                                  # a suggestion with no archive behind it needs nobody's yes
    s2, panel2, *_ = session(knowledge=k2, voice=voice)
    s2.process("When can you deliver the report?")
    s2.speak("Friday.")
    assert wait_for(lambda: voice.said) and api2.sent(rag_client.ASK) == []


def test_the_controller_gives_each_window_its_own_archives():
    from callbridge.desktop import suggestions  # noqa: F401  (imports cleanly without Tk)
    made = []
    ctl = copilot.CopilotController(Config(), Brains(), lambda: type("D", (), {"new_suggestions": lambda self, **kw: Panel()})(), lambda lang: FakeStt(), lambda: type("S", (), {
        "start": lambda self, cb: None, "stop": lambda self: None})(), None, lambda: made.append(know(function="copilot")[0]) or made[-1])
    ctl.toggle()
    assert ctl.is_active and made and ctl._session.knowledge is made[0]
    ctl.toggle()


# -- the meeting bot
def test_a_spoken_reply_carries_the_passages_at_the_tail_and_never_reads_out_a_heading(tmp_path):
    k, api = know()
    api.sources = SRC
    r = Rig(tmp_path, make_plan(consent_window_s=20, speak=True, archives=("boats-1",)))
    r.session.knowledge = k
    r.brains.direct.reply = "The keel keeps it upright. [Boats / sailing.txt, p. 2] Anything else?"
    r.announce()
    r.settle()
    r.run(21)
    r.stt.say("TaDaMoo Bot, how does the keel of the sloop work?")
    r.run(2)
    r.settle()
    prompt = r.brains.direct.prompts[-1]
    assert prompt.endswith(BLOCK) and prompt.index("</addressed_to_you>") < prompt.index("<untrusted_data") and api.sent(rag_client.PASSAGES)[0]["how"] == "speech"
    assert r.tts.said and "Boats" not in r.tts.said[0] and "[" not in r.tts.said[0]


def test_the_meeting_prompt_and_the_cleaning_of_a_reply():
    prompt = speak.reply_prompt("Bot", "Mario", ["Ada: hello"], "what about the keel?", "en", "", BLOCK)
    assert prompt.endswith("\n\n" + BLOCK) and speak.reply_prompt("Bot", "Mario", [], "x", "en").endswith("</addressed_to_you>")
    assert speak.clean_reply("Yes [Boats / a.txt, p. 3, Keel > Shape] it is.") == "Yes it is." and speak.clean_reply("Plain [note] text") == "Plain [note] text"


def test_a_meeting_gets_its_archives_from_the_plan_item_else_from_the_settings_and_records_them(tmp_path):
    r = Rig(tmp_path, make_plan(archives=("a-1", "b-2")))
    assert r.session.plan.archives == ("a-1", "b-2") and r.session.knowledge is None                                       # no factory in this rig: nothing is asked
    made = []
    r.deps.knowledge_factory = lambda plan, sid: made.append((plan.archives, sid)) or "k"
    from callbridge.meetbot.bot import MeetingSession
    s = MeetingSession(make_plan(archives=("a-1",)), r.deps)
    assert s.knowledge == "k" and made == [(("a-1",), s.session_id)]
    assert MeetingSession(make_plan(), r.deps).knowledge is None                                                          # no archives chosen: no Knowledge
    s.dir.mkdir(parents=True)
    s._write_meta()
    assert json.loads((s.dir / "meta.json").read_text(encoding="utf-8"))["archives"] == ["a-1"]


def test_the_minutes_search_each_segment_and_say_which_archives_served(tmp_path, monkeypatch):
    cfg = Config()
    seen = []
    api = RagApi()
    api.sources = SRC

    class Brain:
        def ask_stream(self, prompt, source="remote"):
            seen.append(prompt)
            yield json.dumps({"partial_summary": "s", "summary": "s", "highlights": [], "agreements": [], "tasks": []})

    monkeypatch.setattr(analysis, "_get_brain", lambda cfg: Brain())
    k = Knowledge(cfg, "minutes", ("boats-1",), session="m", api=api, spawn=lambda fn, kind: None)
    out = analysis.analyze_transcript(cfg, [{"text": "we talked about the keel of the sloop", "speaker": "Ada"}], knowledge=k)
    assert seen[0].endswith(BLOCK) and out["archives_used"] == ["Boats"] and out["archives_notes"] == []
    assert api.sent(rag_client.PASSAGES)[0]["function"] == "minutes" and "archives_used" not in analysis.analyze_transcript(cfg, [{"text": "hello there"}])
    folder = tmp_path / "m1"
    folder.mkdir()
    (folder / "transcript.jsonl").write_text(json.dumps({"text": "keel talk", "speaker": "Ada"}) + "\n", encoding="utf-8")
    meta = analyze_meeting(cfg, folder, {"title": "t", "archives": ["boats-1"]})
    assert meta["analysis_state"] in ("done", "error")                                                                   # the real service is not there: it ends cleanly either way


# -- the direct brain, the avatar and calls
def test_the_passages_go_after_the_persons_words_not_in_the_system_message_and_not_in_the_history():
    k, api = know(function="avatar")
    cfg = Config()
    fake = FakeApi()
    b = DirectBrain(cfg, fake)
    said = "".join(b.respond_stream("how does the keel of the sloop work", "mic", knowledge=k))
    messages = [c for c in fake.calls if c[0] == "CHAT"][-1][1]
    assert said and messages[0]["role"] == "system" and "<untrusted_data" not in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "how does the keel of the sloop work\n\n" + BLOCK}
    "".join(b.respond_stream("and the mast", "mic"))
    again = [c for c in fake.calls if c[0] == "CHAT"][-1][1]
    assert {"role": "user", "content": "how does the keel of the sloop work"} in again and not any("<untrusted_data" in m["content"] for m in again[:-1])
    assert b.knowledge is None                                                                                         # a shared brain never holds a session's choice


def test_the_exchange_carries_a_note_about_the_archives_and_a_failed_search_does_not_stop_the_answer():
    k, api = know(function="avatar")
    api.sources = SRC
    log = []
    b = DirectBrain(Config(), FakeApi(), sink=log.append)
    "".join(b.respond_stream("how does the keel of the sloop work", "mic", knowledge=k))
    assert log[-1].note == "archives: Boats"
    from callbridge.tadamoo_api import ApiError
    api.fail = ApiError("down")
    assert "".join(b.respond_stream("how does the mast of the sloop work", "mic", knowledge=k)) and "did not answer" in log[-1].note


def test_each_forked_session_has_its_own_archives_over_the_same_two_brains():
    cfg = Config()
    cfg.brain_archives = ("boats-1",)
    shared = ModeBrain(cfg, DirectBrain(cfg, FakeApi()), HarnessBrain(cfg, FakeApi()))
    a, b = shared.fork(), shared.fork()
    assert a.knowledge is not b.knowledge and a.knowledge.function == "avatar" and a.knowledge.alone and a.direct is b.direct and a.direct.knowledge is None
    assert a.knowledge.archives == ["boats-1"] and shared.knowledge is None
    plain = Config()
    assert ModeBrain(plain, DirectBrain(plain, FakeApi()), HarnessBrain(plain, FakeApi())).fork().knowledge is None


def test_the_avatar_talking_to_the_person_alone_asks_for_the_private_audience():
    api = RagApi()
    k = Knowledge(Config(), "avatar", ("boats-1",), api=api, alone=True)
    k.fetch("how does the keel of the sloop work")
    assert api.sent(rag_client.PASSAGES)[0]["audience"] == "me"


def test_the_harness_conversation_is_made_with_the_archives_and_remade_when_they_change():
    cfg = Config()
    cfg.brain_archives = ("boats-1",)
    api = FakeApi()
    h = HarnessBrain(cfg, api)
    h._ensure(api)
    assert api.sent("/api/agent/sessions")[0][2]["knowledge"] == ["boats-1"]
    cfg.brain_archives = ("boats-1", "rice-2")
    h._ensure(api)
    assert api.sessions == 2 and api.sent("/api/agent/sessions")[1][2]["knowledge"] == ["boats-1", "rice-2"]
    cfg.brain_archives = ()
    h._ensure(api)
    assert "knowledge" not in api.sent("/api/agent/sessions")[2][2]


def test_a_call_has_a_brain_of_its_own_with_the_calls_archives():
    cfg = Config()
    cfg.calls_archives = ("boats-1",)
    svc = CallService(cfg, None, None)
    first, second = svc._direct_brain(), svc._direct_brain()
    assert first.knowledge.function == "call" and first.knowledge is not second.knowledge and not first.knowledge.alone
    assert CallService(Config(), None, None)._direct_brain().knowledge is None


# -- the private channel for questions asked by voice
class Mic:
    def __init__(self):
        self.started = self.stopped = False

    def start(self, cb):
        self.started = True

    def stop(self):
        self.stopped = True


def test_a_question_aloud_uses_the_named_private_output_only_and_listens_to_the_persons_microphone():
    FakeSink.plays = []
    stt = FakeStt()
    mic = Mic()
    chan = PrivateChannel("headphones", FakeTts, FakeSink, lambda lang: stt, lambda: mic, names=lambda: ["CABLE Input (VB-Audio)", "Headphones (Realtek)"], lang="en")
    assert chan.available and chan.device == "Headphones (Realtek)" and chan.asker() is not None
    assert chan.say("May I say this?") is True and FakeSink.plays and FakeSink.plays[-1][0] == "Headphones (Realtek)"
    stt.utterances.put("for this meeting")
    assert chan.listen(1.0) == "for this meeting" and mic.started and mic.stopped and stt.closed
    assert chan.listen(0.05) is None
    none = PrivateChannel("", FakeTts, FakeSink, lambda lang: stt, lambda: mic, names=lambda: ["CABLE Input"])
    assert not none.available and none.asker() is None and none.say("x") is False                                       # nothing named: the dialog alone
    absent = PrivateChannel("airpods", FakeTts, FakeSink, lambda lang: stt, lambda: mic, names=lambda: ["CABLE Input"])
    assert absent.device is None and not absent.available                                                              # the virtual microphone is never a private channel


def test_the_microphone_source_hands_mono_audio_to_the_recognizer():
    got = []

    class Stream:
        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    holder = {}
    src = MicSource(open_stream=lambda cb: holder.setdefault("cb", cb) and Stream())
    src.start(got.append)
    holder["cb"](np.ones((4, 1), dtype=np.float32), 4, None, None)
    assert got[0].shape == (4,) and got[0].dtype == np.float32
    src.stop()


def test_every_request_of_the_voice_service_to_the_model_names_the_voice_function():
    import http.server
    import threading

    from callbridge.tadamoo_api import FUNCTION_HEADER, TadamooApi
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append(self.headers.get(FUNCTION_HEADER))
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\ndata: [DONE]\n\n'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        api = TadamooApi(f"http://127.0.0.1:{server.server_address[1]}", "token")
        assert "".join(api.chat_stream([{"role": "user", "content": "x"}])) == "hi"
    finally:
        server.shutdown()
    assert FUNCTION_HEADER == "X-Tadamoo-Function" and seen == ["voice"]
