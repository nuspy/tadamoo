"""The voice-identification pipeline against real, different synthetic voices - not the fast unit tests' stand-in embeddings
(tests/test_voiceid.py, tests/test_contacts.py), the real WavLM embedder (tadamoo/speech/voiceprint.py) and real Kokoro speech, on
the weights already installed on this machine (nothing here ever downloads anything).

Opt-in on purpose (a project rule: a heavy model test is never run as part of the ordinary suite, only called for by name):
    TADAMOO_RUN_VOICE_MODEL_TESTS=1 .venv/Scripts/python.exe -m pytest tests/test_voiceid_many_voices.py -v -s

At least 10 male and 10 female voices, read from Kokoro's own installed voice bank (54 voices; English only, to keep the
sentences - and so the pace and pitch they carry - comparable across the whole set) - no download, no extra engine."""
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("TADAMOO_RUN_VOICE_MODEL_TESTS"),
                                 reason="opt-in only: TADAMOO_RUN_VOICE_MODEL_TESTS=1 (needs torch, transformers, kokoro-onnx and the real weights)")

KOKORO_DIR = Path(r"C:\Modelli\basalt\voice\tts\kokoro")
KOKORO_ONNX = KOKORO_DIR / "kokoro-v1.0.fp16.onnx"
KOKORO_VOICES = KOKORO_DIR / "voices-v1.0.bin"

# English voices only (American af_/am_, British bf_/bm_), so every voice reads the exact same sentences the same way.
MALE_VOICES = ["am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa",
               "bm_daniel", "bm_fable", "bm_george", "bm_lewis"]
FEMALE_VOICES = ["af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova", "af_river",
                 "af_sarah", "af_sky", "bf_alice", "bf_emma", "bf_isabella", "bf_lily"]
assert len(MALE_VOICES) >= 10 and len(FEMALE_VOICES) >= 10

ENROLL_SENTENCES = [
    "The government announced new measures on Wednesday to cut energy prices.",
    "Teachers marched in the capital and several other cities over the weekend.",
    "The European Commission said the payment of funds depends on certain conditions.",
]
PROBE_SENTENCE = "Good evening, in tonight's bulletin we report the most important news."


def _skip_if_missing():
    if not (KOKORO_ONNX.exists() and KOKORO_VOICES.exists()):
        pytest.skip(f"Kokoro weights not found at {KOKORO_DIR}")
    try:
        from tadamoo.speech import voiceprint
        if not voiceprint.MODEL.exists():
            pytest.skip(f"WavLM weights not found at {voiceprint.MODEL}")
    except Exception as e:
        pytest.skip(f"voiceprint not importable: {e}")


@pytest.fixture(scope="module")
def kokoro():
    _skip_if_missing()
    import os as _os
    _os.environ.setdefault("ONNX_PROVIDER", "CPUExecutionProvider")
    from kokoro_onnx import Kokoro
    return Kokoro(str(KOKORO_ONNX), str(KOKORO_VOICES))


def _wav_bytes(samples, rate: int) -> bytes:
    import io
    import math
    import wave
    import numpy as np
    if rate != 16000:
        from scipy.signal import resample_poly
        g = math.gcd(rate, 16000)
        samples = resample_poly(samples.astype(np.float32), 16000 // g, rate // g)
    pcm = (samples * 32767).clip(-32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _speak(kokoro, voice: str, text: str) -> bytes:
    samples, rate = kokoro.create(text, voice=voice, speed=1.0, lang="en-us")
    return _wav_bytes(samples, rate)


@pytest.fixture(scope="module")
def enrolled(kokoro, tmp_path_factory):
    """One contact per voice, enrolled for real from real Kokoro speech - built once for the whole module, read-only afterwards."""
    from tadamoo.contacts import ContactStore
    store = ContactStore(tmp_path_factory.mktemp("voiceid") / "contacts.json")
    by_voice = {}
    for voice in MALE_VOICES + FEMALE_VOICES:
        contact = store.add_contact(voice)
        wavs = [_speak(kokoro, voice, s) for s in ENROLL_SENTENCES]
        store.enroll_voice(contact["id"], wavs, "mic")
        by_voice[voice] = contact["id"]
    return store, by_voice


def test_at_least_ten_male_and_ten_female_voices_are_used():
    assert len(MALE_VOICES) >= 10 and len(FEMALE_VOICES) >= 10


def test_every_voice_is_recognised_from_a_new_sentence_on_the_microphone_channel(kokoro, enrolled, tmp_path):
    from tadamoo.speech import voiceid
    store, by_voice = enrolled
    confusion = []
    wrong = []
    for voice in MALE_VOICES + FEMALE_VOICES:
        wav = _speak(kokoro, voice, PROBE_SENTENCE)
        reading = voiceid.read(wav)
        found = store.match_voice(reading["embedding"], reading["f0_hz"])
        got = found["contact"]["id"] if found else None
        confusion.append({"voice": voice, "matched": found["contact"]["name"] if found else None,
                          "score": found["score"] if found else None, "confident": found["confident"] if found else None})
        if got != by_voice[voice]:
            wrong.append(voice)
    (tmp_path.parent / "voiceid_confusion_mic.json").write_text(json.dumps(confusion, indent=1), encoding="utf-8")
    print("\n" + json.dumps(confusion, indent=1))
    assert wrong == [], f"misidentified on the mic channel: {wrong}"


def test_every_voice_is_still_found_on_a_simulated_phone_channel(kokoro, enrolled):
    """A mic-only enrollment still has a phone_proxy reading (tadamoo/contacts.py::enroll_voice): a probe synthesised and then
    band-limited the same way should still find its own voice, even if less confidently."""
    from tadamoo.speech import voiceid
    store, by_voice = enrolled
    wrong, unconfident = [], []
    for voice in MALE_VOICES + FEMALE_VOICES:
        wav = voiceid.phone_proxy(_speak(kokoro, voice, PROBE_SENTENCE))
        reading = voiceid.read(wav)
        found = store.match_voice(reading["embedding"], reading["f0_hz"])
        got = found["contact"]["id"] if found else None
        if got != by_voice[voice]:
            wrong.append(voice)
        elif not found["confident"]:
            unconfident.append(voice)
    assert wrong == [], f"misidentified on the simulated phone channel: {wrong}"
    print(f"\nunconfident on the phone channel: {unconfident}")
