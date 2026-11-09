"""LanguageFollower: the switch rules (threshold, short utterances, fillers, enabled languages, lock) with an injected clock."""
import pytest

from callbridge.language_follow import (DEFAULT_ENABLED, FOLLOW, LOCKED, LanguageFollower, LanguageSettings, iso, merge_detections)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def follower(**kw):
    kw.setdefault("clock", Clock())
    return LanguageFollower("it", ("it", "en", "fr"), **kw)


LONG = "This is a sentence of several words"


def test_iso_normalises_the_tags_of_the_recognizer_and_the_old_tables():
    assert [iso(x) for x in ("en-US", "pt_BR", "cmn", "IT", "fr-fr", "zh", " de ")] == ["en", "pt", "zh", "it", "fr", "zh", "de"]
    assert iso("") == iso(None) == iso("x") == iso("123") == iso("english") == ""


def test_a_long_confident_utterance_in_another_language_switches_at_once():
    f = follower()
    sw = f.observe("en", 0.95, 3.0, LONG)
    assert (sw.language, sw.previous, sw.reason) == ("en", "it", "detected")
    assert f.current == "en" and f.mode == FOLLOW


def test_the_same_language_or_no_language_changes_nothing():
    f = follower()
    assert f.observe("it", 0.99, 3.0, LONG) is None
    assert f.observe("", 0.99, 3.0, LONG) is None
    assert f.observe(None, 0.99, 3.0, LONG) is None
    assert f.current == "it"


def test_low_confidence_never_switches_and_the_threshold_is_configurable():
    f = follower()
    assert f.observe("en", 0.79, 3.0, LONG) is None
    assert f.observe("en", 0.80, 3.0, LONG).language == "en"
    g = follower(threshold=0.95)
    assert g.observe("en", 0.9, 3.0, LONG) is None
    assert g.observe("en", 0.96, 3.0, LONG).language == "en"


def test_code_switching_with_one_foreign_word_does_not_switch():
    f = follower()
    # the recognizer hears Italian with one English word: it says Italian, or English with little confidence
    assert f.observe("it", 0.97, 4.0, "Mi mandi il file dopo il meeting con il team") is None
    assert f.observe("en", 0.55, 4.0, "Mi mandi il file dopo il meeting con il team") is None
    assert f.current == "it"


def test_a_short_utterance_needs_two_in_a_row():
    f = follower()
    assert f.observe("fr", 0.95, 1.0, "bonjour tout le monde") is None
    assert f.current == "it"
    sw = f.observe("fr", 0.95, 1.2, "comment ça va")
    assert sw is not None and sw.language == "fr"


def test_a_utterance_in_between_breaks_the_run():
    f = follower()
    assert f.observe("fr", 0.95, 1.0, "bonjour tout le monde") is None
    assert f.observe("it", 0.95, 1.0, "ciao a tutti quanti") is None       # back to the current language
    assert f.observe("fr", 0.95, 1.0, "comment ça va") is None              # the run starts again
    assert f.current == "it"
    f2 = follower()
    assert f2.observe("fr", 0.95, 1.0, "bonjour tout le monde") is None
    assert f2.observe("en", 0.95, 1.0, "hello everyone here") is None       # another candidate does not complete the first
    assert f2.observe("en", 0.95, 1.0, "how are you") is not None


def test_a_low_confidence_utterance_between_two_short_ones_breaks_the_run():
    f = follower()
    assert f.observe("fr", 0.95, 1.0, "bonjour tout le monde") is None
    assert f.observe("fr", 0.5, 1.0, "comment ça va") is None
    assert f.observe("fr", 0.95, 1.0, "très bien merci") is None
    assert f.current == "it"


def test_the_run_is_forgotten_after_the_time_to_live():
    clock = Clock()
    f = LanguageFollower("it", ("it", "en"), clock=clock, pending_ttl_s=20.0)
    assert f.observe("en", 0.95, 1.0, "hello everyone here") is None
    clock.t += 30.0
    assert f.observe("en", 0.95, 1.0, "how are you") is None
    clock.t += 5.0
    assert f.observe("en", 0.95, 1.0, "fine thanks a lot") is not None


def test_the_short_utterance_duration_is_configurable():
    f = follower(short_s=3.0)
    assert f.observe("en", 0.95, 2.5, LONG) is None                          # short for this setting
    assert f.observe("en", 0.95, 2.5, LONG) is not None


@pytest.mark.parametrize("text", ["ok", "yes", "oui merci", "ah", "Okay, thanks"])
def test_fillers_of_one_or_two_words_never_switch(text):
    f = follower()
    assert f.observe("en", 0.99, 3.0, text) is None
    assert f.observe("en", 0.99, 3.0, text) is None
    assert f.current == "it"


def test_a_filler_neither_counts_nor_breaks_a_run():
    f = follower()
    assert f.observe("fr", 0.95, 1.0, "bonjour tout le monde") is None
    assert f.observe("en", 0.99, 0.4, "yes") is None
    assert f.observe("fr", 0.95, 1.0, "comment ça va") is not None


def test_oscillating_filler_between_italian_and_english_is_ignored():
    f = follower()
    for i in range(10):
        assert f.observe("en" if i % 2 else "it", 0.9, 0.5, "ok" if i % 2 else "va bene") is None
    assert f.current == "it"
    # short but real sentences that alternate never reach two in a row
    for i in range(10):
        assert f.observe("en" if i % 2 else "it", 0.9, 1.0, "this is fine now" if i % 2 else "va bene così ora") is None
    assert f.current == "it"


def test_a_language_the_person_did_not_enable_is_ignored():
    f = follower()
    assert f.observe("de", 0.99, 4.0, LONG) is None
    assert f.observe("de", 0.99, 4.0, LONG) is None
    f.set_enabled(["it", "de"])
    assert f.observe("de", 0.99, 4.0, LONG).language == "de"
    assert f.observe("en", 0.99, 4.0, LONG) is None                          # en is no longer enabled


def test_a_locked_language_ignores_the_detection_until_back_to_automatic():
    f = follower()
    sw = f.lock("fr")
    assert (sw.language, sw.reason) == ("fr", "command") and f.mode == LOCKED
    for _ in range(3):
        assert f.observe("it", 0.99, 5.0, LONG) is None
    assert f.current == "fr"
    assert f.unlock() is True and f.mode == FOLLOW
    assert f.unlock() is False
    assert f.observe("it", 0.99, 5.0, LONG).language == "it"


def test_locking_the_current_language_still_locks():
    f = follower()
    assert f.lock("it") is None and f.mode == LOCKED


def test_with_follow_off_nothing_is_detected_and_the_mode_is_locked():
    f = follower(follow=False)
    assert f.mode == LOCKED
    assert f.observe("en", 0.99, 5.0, LONG) is None
    f.unlock()
    assert f.mode == LOCKED


def test_set_current_puts_the_language_back_without_changing_the_mode():
    f = follower()
    f.observe("en", 0.95, 4.0, LONG)
    f.set_current("it")
    assert f.current == "it" and f.mode == FOLLOW


def test_merge_detections_takes_the_language_with_most_speech():
    assert merge_detections([("it", 0.9, 3.0), ("en", 0.99, 1.0)]) == ("it", pytest.approx(0.9), 4.0)
    lang, p, secs = merge_detections([("en", 0.8, 1.0), ("en", 1.0, 3.0), ("it", 0.9, 0.5)])
    assert lang == "en" and p == pytest.approx(0.95) and secs == pytest.approx(4.5)
    assert merge_detections([]) == ("", 0.0, 0.0) and merge_detections([("??", 1, 1)]) == ("", 0.0, 0.0)


def test_settings_parse_defaults_and_bad_values():
    d = LanguageSettings.parse(None)
    assert (d.follow, d.enabled, d.threshold, d.short_s, d.voices) == (True, DEFAULT_ENABLED, 0.8, 1.5, {})
    s = LanguageSettings.parse({"follow": False, "enabled": ["IT", "fr-FR", "xx1", 5, "fr"], "threshold": 0.9, "short_s": 2,
                                "voices": {"fr": " ff_siwis ", "de": "", "??": "x", "es": {"voice": "x"}}})
    assert s.follow is False and s.enabled == ("it", "fr") and s.threshold == 0.9 and s.short_s == 2.0
    assert s.voices == {"fr": "ff_siwis"}
    bad = LanguageSettings.parse({"enabled": [], "threshold": 5, "short_s": "x", "follow": "no"})
    assert (bad.enabled, bad.threshold, bad.short_s, bad.follow) == (DEFAULT_ENABLED, 0.8, 1.5, True)
