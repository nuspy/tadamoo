"""StreamingSession's voice identification: the rolling downlink buffer, the background identification that never blocks a turn, and the
note it leaves for the brain - built on a real PushToTalkSession (no audio device, no model: same as test_language_routes.py)."""
import json
import time
import types

import numpy as np
import pytest

from callbridge import ptt
from callbridge.stt_client import STT_RATE
from tadamoo.contacts import ContactStore
from tadamoo.speech import voiceid
from voice import service

TOKEN = "t0ken-for-tests"


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path))
    settings = tmp_path / "voice.json"
    settings.write_text(json.dumps({"models_dir": str(tmp_path / "models"), "language": {"enabled": ["it", "en"]}}), encoding="utf-8")
    app, state = service.build_app(settings, TOKEN)
    s = ptt.PushToTalkSession(state.config, state.brains.fork(), None, None, 16000, "if_sara", "it", half_duplex=False)
    s._contacts = ContactStore(tmp_path / "contacts.json")           # a real, empty store instead of whatever _open_contacts() found
    return s


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(STT_RATE * seconds), dtype=np.float32)


def test_the_buffer_keeps_only_the_last_twelve_seconds(session):
    for _ in range(20):
        session._remember_for_voice(_silence(1.0))
    assert session._voice_buffer_samples <= STT_RATE * 12


def test_under_a_second_buffered_is_not_enough_to_identify_from(session):
    session._remember_for_voice(_silence(0.5))
    assert session._buffered_voice_wav() is None
    session._remember_for_voice(_silence(0.6))
    assert session._buffered_voice_wav() is not None


def test_no_contacts_store_means_nothing_is_ever_buffered_or_identified(session):
    session._contacts = None
    session._remember_for_voice(_silence(2.0))
    assert session._voice_buffer_samples == 0
    session._identify_speaker_async()                                  # must not raise with nothing to identify
    assert session._voice_note() == ""


def test_identify_speaker_async_never_blocks_and_fills_last_identity(session, monkeypatch):
    mario = session._contacts.add_contact("Mario")
    monkeypatch.setattr(voiceid, "read", lambda wav: {"embedding": [1.0, 0.0], "f0_hz": 110.0})
    monkeypatch.setattr(voiceid, "phone_proxy", lambda wav: wav)
    session._contacts.enroll_voice(mario["id"], [b"anything"], "mic")
    session._remember_for_voice(_silence(2.0))
    started = time.monotonic()
    session._identify_speaker_async()
    assert time.monotonic() - started < 0.2                             # fired in the background, not waited for
    for _ in range(50):
        if session.last_identity is not None:
            break
        time.sleep(0.05)
    assert session.last_identity is not None and session.last_identity["contact"]["name"] == "Mario"
    assert session.last_identity["confident"] is True
    assert "Mario" in session._voice_note()


def test_an_unconfident_match_says_so_in_the_note(session):
    mario = session._contacts.add_contact("Mario")
    session.last_identity = {"contact": mario, "score": 0.81, "channel": "mic", "confident": False}
    note = session._voice_note()
    assert "Mario" in note and "not confident" in note
