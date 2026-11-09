"""End to end through a push-to-talk session's brain loop: a stand-in STT that reports the language it heard and a stand-in TTS that records the voice,
engine and `lang` it is configured with. The person's own microphone changes the language; anything else does not; the engine is never changed
by a command, and a language the selected engine cannot speak is routed to Piper and back."""
import queue
import threading
import time

from callbridge.config import Config
from callbridge.engines import catalog
from callbridge.language_follow import LOCKED
from callbridge.ptt import PushToTalkSession
from callbridge.streaming import StreamingSession

LONG = "this is a sentence with enough words in it"


class Router:
    attivo = "kokoro"

    def __init__(self, piper=()):
        self._piper = tuple(piper)
        self.changes = []

    def attiva(self, *a, **k):
        self.changes.append("attiva")

    def motore_di_voce(self, voice):
        return "piper" if voice in self._piper else "kokoro"

    def parla_lingua(self, engine, lang):
        s = catalog.spec(engine)
        return not s.langs or lang in s.langs

    def engine_available(self, engine):
        return True

    def is_piper(self, voice):
        return voice in self._piper

    def piper_voices(self):
        return list(self._piper)


class Stt:
    def __init__(self):
        self.utterances = queue.Queue()
        self.infos = queue.Queue()
        self.hints = []

    def take_language_info(self):
        try:
            return self.infos.get_nowait()
        except queue.Empty:
            return None

    def set_language(self, lang):
        self.hints.append(lang)

    def say(self, text, language=None, prob=0.95, seconds=3.0):
        if language:
            self.infos.put({"language": language, "prob": prob, "duration_s": seconds})
        self.utterances.put(text)


class Tts:
    def __init__(self):
        self.configured, self.sent = [], []
        self.audio = queue.Queue()
        self.generation = 0

    def configure(self, voice="", lang="", speed=1.0, engine=""):
        self.configured.append((voice, lang, engine))

    def send_text(self, text):
        self.sent.append(text)

    def flush(self):
        pass

    def bump_generation(self):
        self.generation += 1


class Brain:
    def __init__(self):
        self.asked, self.languages = [], []

    def respond_stream(self, text, source="mic", **kw):
        self.asked.append((text, source))
        yield "Va bene. "

    def set_reply_language(self, name, code=""):
        self.languages.append((name, code))


def make(cls=PushToTalkSession, piper=(), enabled=("it", "en", "fr", "de"), source=None):
    cfg = Config()
    cfg.language_enabled = list(enabled)
    brain, stt, tts, router = Brain(), Stt(), Tts(), Router(piper)
    s = cls(cfg, brain, None, None, 16000, "if_sara", "it", half_duplex=False)
    if source:
        s.utterance_source = source
    if s._lang_session is not None:
        s._lang_session._router_fn = lambda: router
    return s, brain, stt, tts, router


def drive(s, stt, tts, *turns):
    """Runs the brain loop over the turns [(text, language)], then a marker turn, and stops."""
    thread = threading.Thread(target=s._brain, args=(stt, tts), daemon=True)
    thread.start()
    for text, language in turns:
        stt.say(text, language)
    stt.say("marker turn that ends the test", None)
    end = time.time() + 5
    while time.time() < end and not any("marker turn" in a[0] for a in s.brain.asked):
        time.sleep(0.01)
    s._stop.set()
    thread.join(2)
    assert any("marker turn" in a[0] for a in s.brain.asked)


def test_the_person_speaking_another_language_changes_voice_lang_brain_and_stt_hint_stays_automatic():
    s, brain, stt, tts, router = make()
    drive(s, stt, tts, (LONG, "fr"))
    assert tts.configured == [("ff_siwis", "fr-fr", "kokoro")]
    assert brain.languages == [("French", "fr")] and s.language == "fr" and s.language_mode == "follow"
    assert [a[0] for a in brain.asked][0] == LONG                            # the turn is answered after the change
    assert stt.hints and stt.hints[-1] is None                               # follow mode: the recognizer keeps detecting


def test_an_explicit_command_is_confirmed_in_the_new_language_not_asked_to_the_brain_and_locks_with_a_hint():
    s, brain, stt, tts, router = make()
    drive(s, stt, tts, ("parle français", None), (LONG + " again", "it"))
    assert "D'accord, je parle français." in tts.sent
    assert [a[0] for a in brain.asked if "parle" in a[0]] == []
    assert tts.configured == [("ff_siwis", "fr-fr", "kokoro")] and s.language_mode == LOCKED
    assert stt.hints[-1] == "fr"                                              # locked: the recognizer may be hinted
    assert s.language == "fr"                                                 # the Italian detection did not move it


def test_back_to_automatic_in_speech_and_the_panel_request():
    s, brain, stt, tts, router = make()
    drive(s, stt, tts, ("speak French", None), ("back to automatic", None))
    assert s.language_mode == "follow" and stt.hints[-1] is None
    assert s.request_language("fr") is True and s.request_language("xx") is False and s.request_language("hu") is False
    assert s.request_language("auto") is True


def test_what_is_not_the_persons_microphone_changes_nothing():
    s, brain, stt, tts, router = make(source="remote")                        # a session that listens to someone else
    drive(s, stt, tts, ("speak French", None), (LONG, "fr"))
    assert tts.configured == [] and s.language == "it" and s.language_mode == "follow"
    assert ("speak French", "remote") in brain.asked                           # the words go to the brain as any other


def test_a_call_session_has_no_language_state_and_ignores_what_whisper_heard():
    s, brain, stt, tts, router = make(cls=StreamingSession)
    assert s._lang_session is None and s.language_mode == "locked"
    drive(s, stt, tts, ("speak French", None), (LONG, "fr"))
    assert tts.configured == [] and s.language == "it"
    assert s.language_status()["mode"] == "locked"


def test_a_language_the_engine_cannot_speak_is_routed_to_piper_with_the_notice_first_and_comes_back():
    s, brain, stt, tts, router = make(piper=["de_DE-x"])
    drive(s, stt, tts, (LONG, "de"), (LONG + " back", "en"))
    assert tts.configured == [("de_DE-x", "de", "piper"), ("af_heart", "en-us", "kokoro")]
    assert tts.sent[0].endswith("uso Piper.")                                  # said before the voice changed
    assert router.changes == [] and s.voice_engine == "kokoro"


def test_without_any_voice_for_the_language_the_previous_one_stays_and_the_install_button_is_named():
    s, brain, stt, tts, router = make()
    drive(s, stt, tts, (LONG, "de"))
    assert tts.configured == [] and s.language == "it"
    assert any("Installa" in t for t in tts.sent)


def test_no_phrase_selects_an_engine_even_from_the_person():
    s, brain, stt, tts, router = make(piper=["fr_FR-x"])
    drive(s, stt, tts, ("use Piper", None), ("cambia motore", None), ("usa il motore francese", None))
    assert tts.configured == [] and s.voice_engine == "" and router.changes == []
    assert len([a for a in brain.asked if "marker" not in a[0]]) == 3          # all three went to the brain as ordinary speech
