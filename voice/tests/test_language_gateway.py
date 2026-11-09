"""The phone gateway with the language following the speech: the recognizer is asked to detect, a detection or a command of the person changes the voice,
the brain's language and the phone's `language` message; the phone's own `language` message works; without the setting nothing changes."""
import queue

import pytest

from callbridge import engines
from callbridge import voice_gateway as vg
from callbridge.engines import catalog
from voice.tests.test_gateway import CLOSED, FRAME, TOKEN, FakeHost, FakeStt, FakeWs, hold_turn


class Router:
    attivo = "kokoro"

    def __init__(self):
        self.changes = []

    def attiva(self, *a, **k):
        self.changes.append("attiva")

    def motore_di_voce(self, voice):
        return "kokoro"

    def parla_lingua(self, engine, lang):
        s = catalog.spec(engine)
        return not s.langs or lang in s.langs

    def engine_available(self, engine):
        return True

    def is_piper(self, voice):
        return False

    def piper_voices(self):
        return []


class HeardStt(FakeStt):
    def __init__(self, text="how are you doing today"):
        super().__init__(text)
        self.infos = queue.Queue()
        self.hints = []
        self.heard_next = []                           # what the recognizer reports for the next utterance(s)

    def flush(self):
        if self.heard_next:
            self.infos.put(self.heard_next.pop(0))
        super().flush()

    def take_language_info(self):
        try:
            return self.infos.get_nowait()
        except queue.Empty:
            return None

    def set_language(self, lang):
        self.hints.append(lang)


class Host(FakeHost):
    def __init__(self, follow=True, **kw):
        super().__init__(**kw)
        self.cfg.language_follow = follow
        self.cfg.language_enabled = ["it", "en", "fr"]
        self.cfg.stt_language = ""
        self.stt = HeardStt()
        self.brains.languages = []
        self.brains.set_reply_language = lambda name, code="": self.brains.languages.append((name, code))

    def stt_client(self, lang, follow=False):
        self.stt_langs.append((lang, follow))
        return self.stt


@pytest.fixture
def router(monkeypatch):
    r = Router()
    monkeypatch.setattr(engines, "_get_tts_engine", lambda cfg: r)
    return r


def run(host, script):
    ws = FakeWs([{"type": "auth", "token": TOKEN}, *script])
    vg.Connection(ws, host, authed=True).run()
    return ws


def test_the_recognizer_detects_and_ready_tells_the_language_and_mode(router):
    host = Host()
    ws = run(host, [*hold_turn(), CLOSED])
    ready = ws.json("ready")[0]
    assert (ready["language"], ready["language_mode"]) == ("it", "follow")
    assert host.stt_langs == [("", True)]                                       # automatic detection, not the phone's language


def test_a_detection_changes_voice_brain_and_tells_the_phone_and_the_turn_is_answered_in_the_new_language(router):
    host = Host()
    host.stt.heard_next.append({"language": "fr", "prob": 0.95, "duration_s": 3.0})
    ws = run(host, [*hold_turn(), CLOSED])                                      # the phone said lang "en" in this start
    event = ws.json("language")[0]
    assert (event["language"], event["mode"], event["reason"], event["engine_fallback"]) == ("fr", "follow", "detected", False)
    assert host.tts.configured[-1] == ("ff_siwis", "fr-fr", 1.0, "kokoro")
    assert ("French", "fr") in host.brains.languages
    assert host.brains.heard and host.brains.heard[0][0] == "how are you doing today"
    assert host.stt.hints[-1] is None
    assert router.changes == []


def test_the_phone_lang_of_later_turns_does_not_override_the_followed_language(router):
    host = Host()
    host.stt.heard_next.append({"language": "fr", "prob": 0.95, "duration_s": 3.0})
    ws = run(host, [*hold_turn(), *hold_turn(), CLOSED])                        # both starts say "en"
    configured = [c[:2] for c in host.tts.configured]
    assert configured[-1] == ("ff_siwis", "fr-fr")


def test_a_typed_command_of_the_person_locks_the_language_confirms_it_and_does_not_reach_the_brain(router):
    host = Host()
    ws = run(host, [{"type": "text", "text": "parle français", "lang": "it"}, CLOSED])
    event = ws.json("language")[0]
    assert (event["language"], event["mode"], event["reason"]) == ("fr", "locked", "command")
    assert "D'accord, je parle français." in host.tts.texts
    assert host.brains.heard == [] and ws.kinds[-1] in ("done", "state")
    assert host.stt.hints[-1] == "fr"


def test_a_command_for_a_language_that_is_not_enabled_is_refused_aloud_and_changes_nothing(router):
    host = Host()
    ws = run(host, [{"type": "text", "text": "sprich Deutsch", "lang": "it"}, CLOSED])
    event = ws.json("language")[0]
    assert (event["language"], event["reason"], event["warning"]) == ("it", "refused", "not_enabled")
    assert any("Impostazioni > Voce" in t for t in host.tts.texts)


def test_the_phones_own_language_message_asks_for_a_language_or_automatic(router):
    host = Host()
    ws = run(host, [*hold_turn(), {"type": "language", "language": "fr"}, {"type": "language", "language": "auto"}, CLOSED])
    events = ws.json("language")
    assert [(e["language"], e["mode"]) for e in events[-2:]] == [("fr", "locked"), ("fr", "follow")]
    assert ws.json("language")[0]["reason"] == "command"


def test_the_state_message_carries_the_language_once_it_follows_the_speech(router):
    host = Host()
    ws = run(host, [{"type": "text", "text": "parle français", "lang": "it"}, CLOSED])
    state = ws.json("state")
    assert not state or (state[-1]["language"], state[-1]["language_mode"]) == ("fr", "locked")


def test_without_the_setting_the_phones_lang_rules_and_no_language_message_is_sent(router):
    host = Host(follow=False)
    ws = run(host, [*hold_turn(), {"type": "language", "language": "fr"}, CLOSED])
    assert ws.json("language") == [] and ws.json("error")[0]["code"] == "language_unavailable"
    assert host.stt_langs[0] == ("it", False) and all(not follow for _, follow in host.stt_langs)
    assert ws.json("ready")[0]["language_mode"] == "locked"


def test_a_fixed_recognizer_language_keeps_the_old_behaviour(router):
    host = Host()
    host.cfg.stt_language = "it"
    ws = run(host, [*hold_turn(), CLOSED])
    assert ws.json("language") == [] and ws.json("ready")[0]["language_mode"] == "locked"


def test_the_visemes_follow_the_language_the_voice_speaks(router):
    host = Host()
    host.avatar_info = {"avatar": "ada", "render_modes": ["visemes"]}
    host.stt.heard_next.append({"language": "fr", "prob": 0.95, "duration_s": 3.0})
    ws = FakeWs([{"type": "auth", "token": TOKEN, "render_modes": ["visemes"], "device": "android"}, *hold_turn(), CLOSED])
    vg.Connection(ws, host, authed=True).run()
    assert [t["lang"] for t in ws.json("visemes")] and {t["lang"] for t in ws.json("visemes")} == {"fr"}
