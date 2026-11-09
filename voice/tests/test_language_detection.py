"""The language the recognizer heard, from whisper to the utterance: pick_language, the engine's detect option (a stand-in model), the client's
take_language_info, the shared engine's protocol (a stand-in helper). No model, no GPU."""
import os
import sys

import numpy as np
import pytest

from callbridge import engines
from callbridge.engines import stt_shared
from callbridge.engines.stt_shared import SharedSttEngine
from callbridge.engines.whisper_stt import NativeSttClient, WhisperSttEngine, pick_language

FINTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_stt_helper.py")
VOICE = (np.sin(np.linspace(0, 400, 16000)) * 0.3).astype(np.float32)        # one second with energy


def test_pick_language_reports_what_was_heard_and_decodes_in_an_allowed_language():
    probs = {"it": 0.1, "en": 0.2, "de": 0.7}
    assert pick_language(probs) == ("de", 0.7, "de")
    assert pick_language(probs, ["it", "en"]) == ("de", 0.7, "en")           # heard German, nobody enabled it: decode as the best allowed
    assert pick_language(probs, ["de", "it"]) == ("de", 0.7, "de")
    assert pick_language(probs, ["xx"]) == ("de", 0.7, "de")                 # none of them is known to the model
    assert pick_language({}) == ("", 0.0, "")


class FakeModel:
    is_multilingual = True

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, language=None, fp16=False, word_timestamps=False):
        self.calls.append(language)
        return {"text": " hello there", "segments": [{"start": 0, "end": 1, "text": " hello there"}], "language": language or "en"}


def engine(monkeypatch, heard=("en", 0.91, "en")):
    e = WhisperSttEngine(model="small", device="cpu")
    e._model, e._loaded_on, e._loaded_half = FakeModel(), "cpu", False
    seen = []
    monkeypatch.setattr(e, "_detect", lambda audio, allowed=None: (seen.append(allowed), heard)[1])
    return e, seen


def test_the_engine_detects_only_when_asked_and_only_without_a_hint(monkeypatch):
    e, seen = engine(monkeypatch, ("fr", 0.88, "fr"))
    plain = e.transcribe_detailed(VOICE)
    assert set(plain) == {"text", "segments"} and not seen and e._model.calls == [None]
    out = e.transcribe_detailed(VOICE, None, False, detect=True, allowed=["it", "fr"])
    assert (out["language"], out["language_prob"]) == ("fr", 0.88) and seen == [["it", "fr"]] and e._model.calls[-1] == "fr"
    hinted = e.transcribe_detailed(VOICE, "it", False, detect=True)
    assert (hinted["language"], hinted["language_prob"]) == ("it", 1.0) and len(seen) == 1 and e._model.calls[-1] == "it"


def test_an_unallowed_language_is_reported_but_decoded_in_an_allowed_one(monkeypatch):
    e, _ = engine(monkeypatch, ("de", 0.9, "en"))
    out = e.transcribe_detailed(VOICE, None, False, detect=True, allowed=["it", "en"])
    assert out["language"] == "de" and e._model.calls[-1] == "en"


class FakeEngine:
    """What NativeSttClient needs: transcribe_detailed with detection, and the old transcribe."""

    def __init__(self, language="en", prob=0.9, text="this is a real sentence"):
        self.language, self.prob, self.text, self.calls = language, prob, text, []

    def load(self):
        pass

    def transcribe_detailed(self, audio, lang=None, words=False, detect=False, allowed=None):
        self.calls.append((lang, detect, allowed))
        return {"text": self.text, "segments": [], "language": lang or self.language, "language_prob": 1.0 if lang else self.prob}

    def transcribe(self, audio, lang=None):
        self.calls.append((lang, "plain"))
        return self.text


def test_the_client_reports_the_language_of_each_utterance_and_merges_what_was_not_taken():
    fake = FakeEngine("fr", 0.9)
    c = NativeSttClient(fake, lang=None, report_language=True, allowed_languages=("it", "fr"))
    c._finalize([VOICE])
    assert c.utterances.get_nowait() == "this is a real sentence"
    info = c.take_language_info()
    assert info["language"] == "fr" and info["prob"] == pytest.approx(0.9) and info["duration_s"] == pytest.approx(1.0)
    assert fake.calls == [(None, True, ("it", "fr"))]
    assert c.take_language_info() is None                                    # taken
    fake.language = "it"
    c._finalize([VOICE])
    fake.language, fake.prob = "fr", 0.7
    c._finalize([VOICE, VOICE])
    info = c.take_language_info()                                            # two utterances merged (coalesced turn): the longer speech wins
    assert info["language"] == "fr" and info["duration_s"] == pytest.approx(3.0)


def test_the_chunks_of_a_turn_count_with_its_tail():
    fake = FakeEngine("en", 0.95)
    c = NativeSttClient(fake, lang=None, report_language=True)
    c._parz_infos.append(("it", 0.9, 4.0))
    c._parziali.append("la prima parte della frase")
    c._finalize([VOICE])
    assert c.utterances.get_nowait().startswith("la prima parte")
    info = c.take_language_info()
    assert info["language"] == "it" and info["duration_s"] == pytest.approx(5.0)


def test_without_the_option_nothing_changes_and_the_old_engine_interface_is_enough():
    fake = FakeEngine()
    c = NativeSttClient(fake, lang="it")
    c._finalize([VOICE])
    assert c.utterances.get_nowait() == "this is a real sentence" and fake.calls == [("it", "plain")]
    assert c.take_language_info() is None


def test_the_hint_can_change_without_touching_the_model():
    fake = FakeEngine()
    c = NativeSttClient(fake, lang=None, report_language=True)
    c.set_language("fr")
    c._finalize([VOICE])
    assert fake.calls[-1][0] == "fr"
    c.set_language("")
    c._finalize([VOICE])
    assert fake.calls[-1][0] is None


@pytest.fixture
def finto(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(stt_shared, "_comando_helper", lambda: [sys.executable, "-u", FINTO])
    cfg = SimpleNamespace(stt_model="small", stt_device="cpu", stt_gpu_memory="shared", stt_compute_type="auto", stt_language="")
    e = SharedSttEngine(cfg)
    yield e
    e.chiudi()


def test_the_shared_engine_carries_detect_and_the_language_back(finto, monkeypatch):
    monkeypatch.setenv("FAKE_STT_LANG", "de")
    finto.load()
    audio = np.zeros(1600, dtype=np.float32)
    out = finto.transcribe_detailed(audio, None, False, detect=True, allowed=["it", "de"])
    assert out["language"] == "de" and out["language_prob"] == pytest.approx(0.93)
    assert set(finto.transcribe_detailed(audio, "it")) == {"text", "segments"}


def test_make_stt_client_follow_detects_instead_of_hinting(monkeypatch):
    from types import SimpleNamespace
    cfg = SimpleNamespace(stt_backend="native", stt_language="it", stt_vad_threshold=0.01, stt_endpoint_silence_s=0.8, stt_min_utterance_s=0.3,
                          stt_vad_model="", stt_vad_speech_prob=0.5, language_enabled=["it", "fr"])
    monkeypatch.setattr(engines, "_get_stt_engine", lambda c: FakeEngine())
    plain = engines.make_stt_client(cfg)
    follow = engines.make_stt_client(cfg, follow=True)
    assert plain._lang == "it" and not plain._report
    assert follow._lang is None and follow._report and follow._allowed == ("it", "fr")
